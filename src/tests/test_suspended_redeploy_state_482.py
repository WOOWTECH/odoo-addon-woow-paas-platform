"""停權中的服務被 redeploy／reconfigure 後必須留在停權（#482）。

## 缺陷

``_do_suspend`` 是**外科式**的（operator ``kubectl scale`` / ``patch vm``，不動
helm release），但三條 upgrade 路徑都以 ``reset_values=True`` 重新渲染 chart。
於是一次無關的 upgrade 會同時造成兩個獨立傷害：

1. **state 汙染** — upgrade 尾端把 state 從 ``suspended`` 改寫成
   ``deploying``／``upgrading``，而 :meth:`_do_resume` 的 guard 只認
   ``suspended`` → 租戶付款後**永遠復不了權**（#482 主症狀）。
2. **workload 被喚醒** — 平台組給 helm 的 values **完全不帶 replicas**
   （只有 ``workload_type == 'vm'`` 會被注入 ``runStrategy``），所以 chart 預設
   的 ``replicas: 1`` 直接讓停權中的容器復活。

第 2 點是容器型專屬、且比 VM 的卡死更嚴重：**欠費服務照跑＝計費繞過**。
2026-07-31 於 docker-desktop 實測（真 operator + 真 helm 3.13.3 + 真 chart）：

    階段              n8n(Deployment)  postgresql(StatefulSet)  state
    _do_suspend()     replicas 0       replicas 0               suspended
    action_redeploy() replicas 1 ←🔴   replicas 1 ←🔴           deploying
    _do_resume()      （no-op）        （no-op）                deploying

且 ``fix482-pg-postgresql-0`` 實測為 Running/ready=true——喚醒是實質的，不只是
欄位。``subscription_lifecycle_cron`` 每天只跑一次，最壞情況白燒約一天；若租戶
在窗口內付款，cron 分支 2 要求 ``state == 'suspended'`` 才 resume → 既不 resume
也不再 suspend → **永久卡在 deploying 且容器永久在跑**。

## 為什麼容器型不能照抄 VM 的解法

VM 型能在 values 注入 ``vm.runStrategy``，是因為 haos 是自家 chart、鍵固定。
容器型的 replicas 鍵名**因 chart 而異**（bitnami 是 ``primary.replicaCount``、
n8n 又是另一套），平台對任意第三方 chart 無法猜鍵名。故收斂走與 ``_do_suspend``
相同的外科式 ``scale_release(replicas=0)``——與 chart 無關，且對 VM 型冪等
（operator 把 ``replicas=0`` 映射成 ``runStrategy: Halted``）。

⚠️ ``_is_halted()`` 的聯集守衛是對的，**這裡不動它**——它保證 upgrade 期間 VM
不被喚醒（stg 實測過）。本檔補的是「upgrade 把 state 改掉之後沒人負責復原」。
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '482 Workspace',
        })
        self.vm_template = Template.create({
            'name': 'HAOS 482',
            'slug': 'haos',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'oci://example.com/haos',
            'helm_chart_version': '0.1.1',
            'workload_type': 'vm',
            'post_deploy_init_type': 'none',
            'default_port': 8123,
        })
        self.container_template = Template.create({
            'name': 'Container 482',
            'slug': 'container-482',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'oci://example.com/container',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
        })

    def _service(self, template, state='running'):
        return self.Service.create({
            'name': 'svc-482',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-482',
        })

    def _suspend(self, svc):
        """走真正的 _do_suspend，讓 state / pre_suspend_state 由產品碼決定。"""
        patcher, client = _patch_operator_client()
        with patcher:
            svc._do_suspend()
        self.assertEqual(svc.state, 'suspended')
        self.assertEqual(svc.pre_suspend_state, 'running')
        return client


def _patch_operator_client(client=None):
    """patch model 層取得 operator client 的入口，回 (patcher, mock_client)。"""
    client = client or MagicMock()
    client.upgrade_release.return_value = {'revision': 2}
    return patch(
        'odoo.addons.woow_paas_platform.services.paas_operator'
        '.get_paas_operator_client',
        return_value=client,
    ), client


def _scaled_replicas(client):
    """取 scale_release 被呼叫時的 replicas（未呼叫回 None）。

    ``_do_suspend`` 用 kwargs 傳 replicas、位置參數傳 release/ns，這裡對兩種
    傳法都容忍，避免測試鎖死呼叫風格。
    """
    if not client.scale_release.call_args_list:
        return None
    call = client.scale_release.call_args_list[-1]
    if 'replicas' in call.kwargs:
        return call.kwargs['replicas']
    return call.args[2] if len(call.args) > 2 else None


class TestRedeployKeepsSuspendedServiceSuspended(_Base):
    """路徑 1／3：``action_redeploy`` → ``client.upgrade_release``。"""

    def test_container_suspended_redeploy_keeps_state_suspended(self):
        """🔴 主症狀：容器型停權服務 redeploy 後 state 不得離開 suspended。

        少了這條，state 變 deploying → _do_resume 的 guard 擋掉復權 →
        租戶付了錢也回不來。
        """
        svc = self._service(self.container_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            svc.action_redeploy()

        self.assertEqual(
            svc.state, 'suspended',
            "停權中的服務被 redeploy 後必須留在 suspended——寫成 deploying 會讓 "
            "_do_resume 的 guard 永久擋掉復權",
        )
        self.assertEqual(
            svc.pre_suspend_state, 'running',
            "pre_suspend_state 是復權時要還原的目標狀態，不可被 redeploy 清掉",
        )

    def test_container_suspended_redeploy_rescales_workload_to_zero(self):
        """🔴 計費繞過：upgrade 用 chart 預設把 replicas 渲染回 1，必須收回 0。

        平台送出的 values 不帶 replicas（容器型的鍵名因 chart 而異，無法注入），
        所以只能在 upgrade 後外科式地把 workload 重新縮回去。
        """
        svc = self._service(self.container_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            svc.action_redeploy()

        self.assertEqual(
            _scaled_replicas(client), 0,
            "upgrade 會把停權中的容器喚醒（實測 replicas 0→1、pod 真的 Running），"
            "必須在 upgrade 後 scale 回 0，否則欠費服務照跑＝計費繞過",
        )

    def test_vm_suspended_redeploy_keeps_state_suspended(self):
        """VM 型的 state 汙染與容器型同構（#482 原始實測情境）。"""
        svc = self._service(self.vm_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            svc.action_redeploy()

        self.assertEqual(svc.state, 'suspended')
        self.assertEqual(svc.pre_suspend_state, 'running')

    def test_vm_suspended_redeploy_also_rescales_to_zero(self):
        """VM 型走同一條收斂——冪等的縱深。

        VM 的 values 已被 W5a.4 守衛寫成 runStrategy: Halted，所以這一刀對 VM
        是重複的；但 operator 把 replicas=0 映射成 Halted，重複無害，而少了它
        就得在收斂邏輯裡分流 workload type（多一個會漂移的判斷）。
        """
        svc = self._service(self.vm_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            svc.action_redeploy()

        self.assertEqual(_scaled_replicas(client), 0)

    def test_running_service_redeploy_still_goes_deploying(self):
        """不回歸：正常服務的 redeploy 行為完全不變。"""
        svc = self._service(self.container_template, state='running')

        patcher, client = _patch_operator_client()
        with patcher:
            svc.action_redeploy()

        self.assertEqual(svc.state, 'deploying')
        self.assertFalse(svc.pre_suspend_state)

    def test_running_service_redeploy_does_not_rescale(self):
        """不回歸：正常服務不得被這條收斂誤縮到 0。"""
        svc = self._service(self.container_template, state='running')

        patcher, client = _patch_operator_client()
        with patcher:
            svc.action_redeploy()

        self.assertIsNone(
            _scaled_replicas(client),
            "沒有停權的服務被 redeploy 時不該碰 replicas——誤縮＝把正常服務關掉",
        )


class TestReconfigureKeepsSuspendedServiceSuspended(_Base):
    """路徑 2／3：controller ``_update_service`` → 背景執行緒 ``upgrade_release``。

    這條比 redeploy 更危險：它寫的是 ``upgrading``，而 poll
    （``_cron_check_deploying_services`` 掃 deploying/upgrading/initializing）
    會把容器型一路收斂成 **running**——停權服務在帳面上直接復權。

    ``_update_service`` 的入口守衛只擋 pending/deleting/error
    （``controllers/paas.py``），**suspended 是放行的**，所以這條路真的可達。
    """

    def _run_update(self, svc, client):
        """跑 _update_service，並把背景 upgrade 執行緒同步化。

        背景執行緒自建 cursor 與自建 client，都不吃主執行緒的 patch：

        * ``threading.Thread`` 換成同步執行，讓 target 在測試的呼叫堆疊裡跑完。
        * ``PaaSOperatorClient`` 必須 patch **在 services.paas_operator 上**——
          ``_bg_upgrade`` 是在函式內 import 的，patch controller module 的同名
          屬性攔不到（會讓背景執行緒拿真 client 去打 MagicMock 當 URL）。
        * 背景那半用自己的 cursor，讀不到測試 transaction 尚未 commit 的 row
          （``svc.exists()`` 為 False），所以**背景對 DB 的寫入在測試中不可觀察**。
          state 斷言因此只對主執行緒有效，scale 斷言則靠 client mock 成立。
        """
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        module = 'odoo.addons.woow_paas_platform.controllers.paas'
        client_path = (
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.PaaSOperatorClient'
        )
        fake_request = MagicMock()
        fake_request.env = self.env

        class _SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        with patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch(f'{module}.request', new=fake_request), \
                patch(client_path, return_value=client), \
                patch(f'{module}.threading.Thread', _SyncThread):
            return PaasController()._update_service(svc, values={}, version=None)

    def test_suspended_reconfigure_does_not_write_upgrading(self):
        """🔴 停權服務 reconfigure 後不得停在 upgrading。

        upgrading 會被 poll 收斂成 running＝帳面復權；而 _do_resume 的 guard
        又只認 suspended，於是租戶的服務「看起來活著」卻沒有任何訂閱在付費。
        """
        svc = self._service(self.container_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            self._run_update(svc, client)

        self.assertEqual(
            svc.state, 'suspended',
            "reconfigure 停權服務後 state 必須留在 suspended，否則 poll 會把它 "
            "一路收斂成 running（帳面復權）",
        )
        self.assertEqual(svc.pre_suspend_state, 'running')

    def test_suspended_reconfigure_rescales_workload_to_zero(self):
        """reconfigure 同樣以 reset_values 重渲染 → 同樣要把 workload 收回 0。"""
        svc = self._service(self.container_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            self._run_update(svc, client)

        self.assertEqual(_scaled_replicas(client), 0)

    def test_running_service_reconfigure_still_writes_upgrading(self):
        """不回歸：正常服務的 reconfigure 仍走 upgrading。"""
        svc = self._service(self.container_template, state='running')

        patcher, client = _patch_operator_client()
        with patcher:
            self._run_update(svc, client)

        self.assertEqual(svc.state, 'upgrading')
        self.assertIsNone(_scaled_replicas(client))


class TestResetAdminPasswordHaltedDefenseInDepth(_Base):
    """路徑 3／3：``_reset_admin_password`` → ``client.reset_release_config``。

    ⚠️ 這條路對 **suspended 服務不可達**——``_reset_admin_password`` 開頭就有
    ``if self.state != 'running': raise UserError``。所以這裡加的是**縱深**，
    針對的是二階情境：服務已被 reconfigure 汙染（suspended → upgrading → poll
    → running）而 ``pre_suspend_state`` 仍在，此時 reset 又是一次 reset_values
    重渲染，會再把它推上線。
    """

    def test_reset_still_refuses_a_suspended_service(self):
        """守住既有前置守衛：本 PR 不得把 reset 對停權服務放行。"""
        svc = self._service(self.container_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher, self.assertRaises(UserError):
            svc._reset_admin_password()

        client.reset_release_config.assert_not_called()

    def test_reset_on_halted_service_keeps_it_halted(self):
        """二階情境：state 已被汙染成 running，但 pre_suspend_state 仍在。

        ``_is_halted()`` 仍為真＝這台服務其實還在停權中，reset 完必須把它收回
        停機，而不是留下一台跑著的欠費服務。
        """
        svc = self._service(self.container_template, state='running')
        svc.write({'pre_suspend_state': 'running'})
        self.assertTrue(svc._is_halted())

        patcher, client = _patch_operator_client()
        with patcher:
            svc._reset_admin_password()

        self.assertEqual(_scaled_replicas(client), 0)
        self.assertEqual(
            svc.state, 'suspended',
            "reset 完的服務若仍處於停權（pre_suspend_state 有值），state 必須"
            "收斂回 suspended，而不是 upgrading",
        )

    def test_reset_on_normal_service_still_writes_upgrading(self):
        """不回歸：正常服務 reset 後仍是 upgrading、不被縮到 0。"""
        svc = self._service(self.container_template, state='running')

        patcher, client = _patch_operator_client()
        with patcher:
            svc._reset_admin_password()

        self.assertEqual(svc.state, 'upgrading')
        self.assertIsNone(_scaled_replicas(client))


class TestTimeoutInteraction489(_Base):
    """與 #489／#491 age timeout 的語意交互（rebase 到 f0bcd5ec 後補）。

    #491 讓久滯 ``deploying``／``upgrading`` 的服務逾時轉 ``error``，且它的
    ``_timeout_diagnosis`` **明確認得 #482**——`_SUSPENDED_HINT` 的註解寫著
    「停權中的服務被 redeploy 後會卡在 deploying，VM 被正確地 Halted 著所以
    永遠不會 ready」。換句話說 #491 是在**假設 #482 的缺陷還在**的前提下，把
    那批必然逾時的服務的錯誤訊息講清楚。

    本 PR 修掉缺陷之後，兩者的正確關係是**不相交**：停權服務根本不再進入
    ``_TIMEOUT_GOVERNED_STATES``，所以 age timeout 掃不到它，也就不會把一台
    「本來就該停著」的服務打成 ``error``（那會是 #482 換皮重演——error 同樣
    不是 lifecycle cron 分支 2 認得的 ``suspended``）。

    這裡把「不相交」釘成契約：任何讓停權服務重新落回轉移狀態的改動都會紅。
    """

    def test_suspended_redeploy_leaves_service_outside_timeout_governance(self):
        """redeploy 後的停權服務不得落在 age timeout 的管轄狀態內。"""
        svc = self._service(self.container_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            svc.action_redeploy()

        self.assertFalse(
            svc._is_timeout_governed(),
            "停權服務被 redeploy 後若落在 deploying/upgrading，#489 的 age "
            "timeout 會把它打成 error —— 而 error 同樣不是 lifecycle cron 認得"
            "的復權起點，等於 #482 換皮重演",
        )

    def test_suspended_reconfigure_leaves_service_outside_timeout_governance(self):
        """reconfigure 路徑同上（它寫的是 upgrading，同樣受 timeout 管轄）。"""
        svc = self._service(self.container_template)
        self._suspend(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            TestReconfigureKeepsSuspendedServiceSuspended._run_update(
                self, svc, client)

        self.assertFalse(svc._is_timeout_governed())

    def test_timeout_never_fires_on_a_suspended_service(self):
        """直接對 `_check_transition_timeout` 斷言：停權狀態一律不裁決逾時。

        錨點刻意設成遠古（門檻預設 30 分），確認擋下來的是 state 判斷而不是
        「還沒到期」這種會隨預設值漂移的理由。
        """
        svc = self._service(self.container_template)
        self._suspend(svc)
        svc.write({
            'deployed_at': '2020-01-01 00:00:00',
            'last_upgraded_at': '2020-01-01 00:00:00',
        })

        fired = svc._check_transition_timeout('no pods found')

        self.assertFalse(fired, "停權服務不該被 age timeout 裁決")
        self.assertEqual(svc.state, 'suspended')

    def test_halted_hint_still_reachable_for_legacy_stuck_rows(self):
        """#491 的 `_SUSPENDED_HINT` 不因本 PR 而失效。

        本 PR 只讓**新**的 redeploy 不再汙染 state；合併前就已經卡在
        ``deploying`` 的既有資料仍存在，那批仍要拿得到「這台是停權中」的診斷，
        否則維運會被 pod/quota 方向的 hint 帶偏。
        """
        svc = self._service(self.container_template)
        self._suspend(svc)
        # 模擬合併前留下的卡死列：state 已被舊碼汙染，停權訊號仍在
        svc.write({'state': 'deploying'})

        hint = svc._timeout_diagnosis('no pods found')

        self.assertIn('suspended', hint)


class TestResumeGuardDefenseInDepth(_Base):
    """(b) 縱深：``_do_resume`` 的 guard 補認 ``pre_suspend_state``。

    主修讓 state 不再被汙染；這條保證**即使**哪天又有新的路徑把 state 改掉
    （或處理既有已卡死的資料），復權仍然做得到。不是主修的替代品。
    """

    def test_resume_recovers_service_whose_state_was_polluted(self):
        """🔴 #482 的脫困路徑要能自己走通，不必人工先改 state。

        對照 issue 的人工 runbook：先 write({'state':'suspended'}) 再
        _do_resume()。補上 guard 之後這一步不該再需要人來做。
        """
        svc = self._service(self.container_template)
        self._suspend(svc)
        # 模擬被 upgrade 汙染後的狀態（issue 實測時序的第 3 步）
        svc.write({'state': 'deploying'})

        patcher, client = _patch_operator_client()
        with patcher:
            svc._do_resume()

        self.assertEqual(_scaled_replicas(client), 1)
        self.assertEqual(
            svc.state, 'running',
            "pre_suspend_state 有值＝這台服務其實停權中，復權必須走得通",
        )
        self.assertFalse(svc.pre_suspend_state)

    def test_resume_is_still_a_noop_for_a_never_suspended_service(self):
        """不回歸：沒停權過的服務不該被 resume 動到。"""
        svc = self._service(self.container_template, state='running')

        patcher, client = _patch_operator_client()
        with patcher:
            svc._do_resume()

        self.assertIsNone(_scaled_replicas(client))
        self.assertEqual(svc.state, 'running')

    def test_resume_restores_pre_suspend_state_verbatim(self):
        """復權要還原成停權前的狀態，而不是一律 running。"""
        svc = self._service(self.container_template, state='error')
        patcher, client = _patch_operator_client()
        with patcher:
            svc._do_suspend()
        self.assertEqual(svc.pre_suspend_state, 'error')

        svc.write({'state': 'deploying'})
        with patcher:
            svc._do_resume()

        self.assertEqual(svc.state, 'error')

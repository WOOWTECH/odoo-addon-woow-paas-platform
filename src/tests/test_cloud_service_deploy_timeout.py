"""#489 F-423-1 Step 2：部署／升級 age timeout——轉移狀態的時間上界。

**問題**：``deploying`` / ``upgrading`` 沒有任何時間上界。永遠不會就緒的服務會
無限期停在轉移狀態，而「卡住的」與「正常部署中的」在 Odoo 側外觀完全相同——
cron 每分鐘、前台每 5 秒都只是「再問一次 operator」，沒有一方會說「這條不會好了」。

**本案**：poll 判定未就緒時順帶算 age，超過門檻就翻 ``error`` 並帶上可診斷的訊息。
只變狀態與訊息，**不刪任何叢集資源、不觸發 rollback**。

## 為什麼「寧鬆勿緊」是硬約束而不是偏好

翻 ``error`` **不可自癒**：兩層 poll 都只在 ``deploying`` / ``upgrading`` /
``initializing`` 才呼叫（``cloud_service.py`` 的 cron domain、``paas.py:2312``
與 ``:2562``），所以一旦寫進 ``error``，就再也沒有任何路徑會把它 poll 回
``running``——即使叢集裡的服務其實在門檻之後就緒了。誤殺的代價是「本來會好的服務
被打成 error，租戶得重新下單」，晚報的代價只是「維運晚 N 分鐘知道」。兩者不對稱，
故門檻一律取寬。這也是下面 #4（錨取較晚者）與 #6（VM 獨立門檻）存在的理由：它們
各自堵住一種會**提早**觸發的錯算。

## 模擬時鐘的做法

不 patch ``fields.Datetime.now``，改**把錨往過去推**——被測的量是「now − 錨」的
差，推錨與推時鐘等價，且沒有「patch 失效後靜默變死碼」的假綠風險
（``reference_odoo_shell_run_testcase_simulated_clock`` 的教訓）。每條測試的錨都
刻意壓在門檻的**兩側 1 分鐘**內，所以門檻若被算錯一個量級（分鐘當秒之類）必紅。
"""
import logging
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision
from .operator_contract import status_response  # #1052：get_status 形狀的唯一定義處


# 容器型的預設門檻（分鐘）。實作端以模組常數提供，測試這裡獨立寫一份——
# 兩邊漂移時會紅，那正是我們要的（門檻是對外契約，不該被靜默改動）。
_CONTAINER_DEFAULT = 30
_VM_DEFAULT = 120

_PARAM_CONTAINER = 'woow_paas_platform.deploy_timeout_minutes'
_PARAM_VM = 'woow_paas_platform.vm_deploy_timeout_minutes'

# 一顆就緒的 pod：用來組「已就緒」的 status（就緒優先於逾時的錨測試要用）。
_READY_POD = [{'name': 'p', 'phase': 'Running', 'ready': '1/1'}]
# KubeVirt 的 VM 跑在 virt-launcher pod 裡且帶 operator 的 selector（F-423-1
# Step 1 實測），VM 就緒後仍要過容器閘，故 VM 的「已就緒」fixture 要帶這顆。
_VIRT_LAUNCHER = [{'name': 'virt-launcher-svc-test', 'phase': 'Running',
                   'ready': '2/2'}]


def _status(release_status='deployed', pods=None, **vm_fields):
    """組一份 operator ``get_status`` 形狀的回應（與 W5a 測試同型）。

    #1052：形狀的唯一定義處是 ``operator_contract.status_response``；本檔原本手抄的
    7 key 版本少了 ``pod_retrieval_error``，消費端一律 ``.get()``，多一個 ``None`` key
    與缺 key 等價。
    """
    return status_response(release_status, pods, **vm_fields)


def _vm_stuck_status(**overrides):
    """VM 型「永久卡住」的 status：RBAC 缺權 → ``vmi_retrieval_error`` 有值。

    這正是 #489 點名的 W5a 遺留洞——``_is_vm_ready`` 對它 fail-closed，而它
    不會自己好，所以在本案之前 VM 服務會**永久**停在 ``deploying``。
    """
    body = _status(
        vm_included=True,
        vmis=None,
        vmi_retrieval_error='virtualmachineinstances.kubevirt.io is forbidden',
    )
    body.update(overrides)
    return body


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '489 Timeout Workspace',
        })
        # 每條測試都從「參數未設」出發，避免別條測試留下的值互相污染。
        self._clear_params()

    def _clear_params(self):
        self.env['ir.config_parameter'].sudo().search([
            ('key', 'in', [_PARAM_CONTAINER, _PARAM_VM]),
        ]).unlink()

    def _set_param(self, key, value):
        self.env['ir.config_parameter'].sudo().set_param(key, value)

    def _make_service(self, workload_type='container', state='deploying',
                      anchor_minutes_ago=None, anchor_field='deployed_at'):
        """建一個處於轉移狀態的服務。

        Args:
            anchor_minutes_ago: 把錨設在幾分鐘前（``None`` ＝不設錨）。
            anchor_field: 錨落在 ``deployed_at`` 還是 ``last_upgraded_at``。
        """
        template = self.Template.create({
            'name': f'T-{workload_type}',
            'slug': f'slug-489-{workload_type}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0',
            'workload_type': workload_type,
            'post_deploy_init_type': 'none',
        })
        vals = {
            'name': f'svc-{workload_type}',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-test',
        }
        if anchor_minutes_ago is not None:
            vals[anchor_field] = (
                fields.Datetime.now() - timedelta(minutes=anchor_minutes_ago)
            )
        return _internal_provision(self.Service).create(vals)

    def _poll(self, svc, status_body):
        """跑 model 層 poll（mock 掉 operator client）。"""
        client = MagicMock()
        client.get_status.return_value = status_body
        svc._poll_and_update_status(client)
        return client

    def _poll_controller(self, svc, status_body):
        """跑 controller 層 poll（與 W5a 測試同一套 request 替身手法）。"""
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        ctrl = PaasController()
        client = MagicMock()
        client.get_status.return_value = status_body

        module = 'odoo.addons.woow_paas_platform.controllers.paas'
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch(f'{module}.request', new=fake_request):
            ctrl._update_service_status(svc)
        return client


class TestTransitionAnchor(_Base):
    """錨的選法——**本案最容易誤殺的一環**。

    ``deployed_at`` 只在首裝的 helm install 回來後寫一次；``last_upgraded_at``
    在合併前只有 reconfigure 那一條路徑會寫。若照「依 state 擇一」取錨，
    ``action_redeploy``（寫 ``deploying``、走 upgrade）就會取到**首裝**時間——
    一台部署了三個月的服務按下重新部署，第一輪 poll 就被判逾時。
    """

    def test_anchor_is_none_without_timestamps(self):
        """兩個錨都空 → 無法判定 age，回 None（呼叫端據此不逾時）。"""
        svc = self._make_service()

        self.assertIsNone(svc._transition_anchor())

    def test_anchor_uses_deployed_at_on_first_deploy(self):
        svc = self._make_service(anchor_minutes_ago=5)

        self.assertEqual(svc._transition_anchor(), svc.deployed_at)

    def test_anchor_takes_the_later_of_the_two(self):
        """🔴 防誤殺核心：redeploy 後 ``last_upgraded_at`` 較新 → 取它。

        取較晚者而不是依 state 擇一：``action_redeploy`` 寫的是 ``deploying``
        卻是 upgrade 語意，依 state 擇一會選到陳舊的 ``deployed_at``。
        """
        svc = self._make_service(anchor_minutes_ago=60 * 24 * 90)  # 首裝 90 天前
        svc.last_upgraded_at = fields.Datetime.now() - timedelta(minutes=2)

        self.assertEqual(svc._transition_anchor(), svc.last_upgraded_at)

    def test_stale_deployed_at_plus_fresh_upgrade_does_not_time_out(self):
        """🔴 上一條的端到端版：90 天前首裝 ＋ 剛剛 redeploy → **不可**翻 error。"""
        svc = self._make_service(anchor_minutes_ago=60 * 24 * 90)
        svc.last_upgraded_at = fields.Datetime.now() - timedelta(minutes=2)

        self._poll(svc, _status(pods=[]))

        self.assertEqual(
            svc.state, 'deploying',
            '剛按下重新部署的服務被首裝時間判逾時＝不可自癒的誤殺',
        )

    def test_missing_anchor_never_times_out(self):
        """錨為空 → 不逾時（沒有依據就不裁決）。

        這個窗口只存在於 ``_execute_deploy`` 的 helm install 執行期間（同一個
        transaction 內，install 回來就寫 ``deployed_at``），而硬翻 error 的代價
        是不可自癒的誤殺，故此處刻意 fail-open——與「參數缺值走內建預設」的
        fail-safe 方向鎖不衝突：那條鎖的是**設定漏設**，這裡是**事實未知**。
        """
        svc = self._make_service()

        self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'deploying')


class TestTimeoutThreshold(_Base):
    """門檻的取值與可配置性。"""

    def test_container_default_when_param_absent(self):
        svc = self._make_service('container')

        self.assertEqual(svc._transition_timeout_minutes(), _CONTAINER_DEFAULT)

    def test_vm_default_when_param_absent(self):
        svc = self._make_service('vm')

        self.assertEqual(svc._transition_timeout_minutes(), _VM_DEFAULT)

    def test_param_overrides_default(self):
        self._set_param(_PARAM_CONTAINER, '5')
        svc = self._make_service('container')

        self.assertEqual(svc._transition_timeout_minutes(), 5)

    def test_vm_param_overrides_vm_default_only(self):
        """VM 參數不得影響容器型，反之亦然（兩檔各自獨立）。"""
        self._set_param(_PARAM_VM, '7')

        self.assertEqual(self._make_service('vm')._transition_timeout_minutes(), 7)
        self.assertEqual(
            self._make_service('container')._transition_timeout_minutes(),
            _CONTAINER_DEFAULT,
        )

    def test_garbage_param_falls_back_to_default(self):
        """垃圾值 → 內建預設，**不是**停用逾時（fail-safe 方向鎖，同 #404 教訓）。"""
        for garbage in ('', '   ', 'abc', '0', '-5', '3.5.1'):
            with self.subTest(value=garbage):
                self._set_param(_PARAM_CONTAINER, garbage)
                svc = self._make_service('container')

                self.assertEqual(
                    svc._transition_timeout_minutes(), _CONTAINER_DEFAULT,
                    f'參數值 {garbage!r} 應退回內建預設而非停用逾時',
                )

    def test_missing_param_still_times_out(self):
        """🔴 fail-safe 方向鎖的行為版：漏設參數不得讓這道保護靜默失效。"""
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'error')

    def test_param_shortening_takes_effect(self):
        """把門檻調短後，原本不會逾時的 age 要開始逾時（參數真的接上）。"""
        self._set_param(_PARAM_CONTAINER, '5')
        svc = self._make_service('container', anchor_minutes_ago=6)

        self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'error')


class TestModelPollTimeout(_Base):
    """model 層 poll 的端到端行為（cron 走這條）。"""

    def test_container_times_out_after_threshold(self):
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'error')
        self.assertIn('timed out', svc.error_message)
        self.assertIn(str(_CONTAINER_DEFAULT), svc.error_message)

    def test_container_below_threshold_stays_deploying(self):
        """🔴 反向錨：門檻內的正常部署不得被動到。"""
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT - 1)

        self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'deploying')
        self.assertFalse(svc.error_message)

    def test_upgrading_times_out_too(self):
        """``upgrading`` 與 ``deploying`` 同樣受管（兩者都是無上界的轉移狀態）。"""
        svc = self._make_service(
            'container', state='upgrading',
            anchor_minutes_ago=_CONTAINER_DEFAULT + 1,
            anchor_field='last_upgraded_at',
        )

        self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'error')

    def test_vm_stuck_on_rbac_times_out(self):
        """🔴 #489 的主要收穫：W5a 留下的「VMI 查詢失敗＝永久卡 deploying」。"""
        svc = self._make_service('vm', anchor_minutes_ago=_VM_DEFAULT + 1)

        self._poll(svc, _vm_stuck_status())

        self.assertEqual(svc.state, 'error')
        self.assertIn('timed out', svc.error_message)

    def test_vm_missing_include_vm_times_out(self):
        """另一種永久卡住：呼叫端漏帶 ``include_vm`` / operator 太舊。"""
        svc = self._make_service('vm', anchor_minutes_ago=_VM_DEFAULT + 1)

        self._poll(svc, _status())  # vm_included=False

        self.assertEqual(svc.state, 'error')

    def test_pending_rollback_times_out(self):
        """helm release 卡在 ``pending-*`` 同樣封頂。

        兩層 poll 原本對 ``pending-install`` / ``pending-upgrade`` /
        ``pending-rollback`` 一律靜置等下一輪。但被 SIGKILL 腰斬的 rollback 會
        把 release 永久留在 ``pending-rollback``，沒有任何東西會再推它
        （``docs/reference/gotchas/helm-atomic-failure-double-timeout.md`` 的實錄）——就緒判定
        根本不會被呼叫，所以這條路必須自己有話可說。
        """
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        self._poll(svc, _status(release_status='pending-rollback'))

        self.assertEqual(svc.state, 'error')
        self.assertIn('pending-rollback', svc.error_message)

    def test_pending_install_below_threshold_stays_deploying(self):
        """門檻內的 ``pending-install`` 是正常首裝，不得動它。"""
        svc = self._make_service('container', anchor_minutes_ago=1)

        self._poll(svc, _status(release_status='pending-install'))

        self.assertEqual(svc.state, 'deploying')

    def test_normal_pending_install_is_not_logged_as_hard(self):
        """正常首裝就會撞到 ``pending-install``（helm 還在跑、cron 已在 poll）。

        把它記成 ``HARD:`` 會讓每一次正常部署都刷一行 warning，正好稀釋掉
        ``HARD`` 的語意——那個前綴專指「不會自己好」，是維運在正式環境
        （INFO 級）唯一能撈到硬失效的訊號（W5a MINOR-1）。卡住與否由時間
        分辨，真的逾時時 `_check_transition_timeout` 會印 error 級。
        """
        svc = self._make_service('container', anchor_minutes_ago=1)

        with self.assertLogs(
            'odoo.addons.woow_paas_platform.models.cloud_service', level='DEBUG',
        ) as logs:
            self._poll(svc, _status(release_status='pending-install'))

        self.assertFalse(
            [r for r in logs.records if r.levelno >= logging.WARNING],
            '正常的 pending-install 不該升 warning：'
            f'{[r.getMessage() for r in logs.records]}',
        )

    def test_vm_uses_its_own_longer_threshold(self):
        """🔴 防誤殺核心：CDI 磁碟 import 動輒數十分鐘，VM 不得套容器門檻。

        錨壓在「容器門檻已過、VM 門檻未過」的區間——實作若對兩種 workload
        用同一個門檻，這條必紅。
        """
        svc = self._make_service('vm', anchor_minutes_ago=_CONTAINER_DEFAULT + 10)

        self._poll(svc, _status(vm_included=True, vmis=[]))

        self.assertEqual(
            svc.state, 'deploying',
            '還在 import 的 VM 被容器型門檻誤殺',
        )


class TestTimeoutDoesNotOverreach(_Base):
    """反過度修復：本案只該碰「卡在轉移狀態」的服務。"""

    def test_ready_service_becomes_running_even_past_threshold(self):
        """🔴 就緒優先於逾時：門檻外才就緒的服務要 ``running``，不是 ``error``。

        判定順序若寫反（先判 age 再判就緒），一台只是部署得慢、但最後真的起來的
        服務會被打成 error——而 error 不可自癒。
        """
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 60)

        self._poll(svc, _status(pods=_READY_POD))

        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.error_message)

    def test_vm_ready_past_threshold_becomes_running(self):
        svc = self._make_service('vm', anchor_minutes_ago=_VM_DEFAULT + 60)

        self._poll(svc, _status(
            vm_included=True,
            vmis=[{'name': 'svc-test', 'phase': 'Running', 'ready': True}],
            pods=_VIRT_LAUNCHER,
        ))

        self.assertEqual(svc.state, 'running')

    def test_initializing_is_not_touched(self):
        """``initializing`` 不歸 age timeout 管，不該被兩套機制夾擊。

        （#489 明列：age timeout 只管 ``deploying`` / ``upgrading``。#1257 起
        ``initializing`` 有自己的上界：等待時限錨在 ``init_wait_since``，次數看
        ``init_retries``／``init_unknown_count``——都與這裡的部署錨無關。）
        """
        svc = self._make_service('container', state='initializing',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 999)

        self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'initializing')
        self.assertFalse(svc.error_message)

    def test_running_service_is_never_polled_by_cron(self):
        """已 ``running`` 的服務永遠不會被 age timeout 掃到——cron domain 不含它。

        直接對 cron 的行為斷言而不是對 domain 字串斷言：換寫法不該讓這條紅，
        真的把 running 撈進來才該紅。

        ⚠ 斷言的是「**這台**沒被 poll」而不是「完全沒有任何 poll」：cron 撈的是
        全 DB 的轉移中服務，所以後者會在**任何有既有資料的 DB** 上誤紅（stg 沙盒
        跑 `-u --test-enable`、或本地測完 migration 的 DB 都是）。那種紅與被測
        行為無關，只是把「這個 DB 剛好乾淨」寫進了斷言。
        """
        svc = self._make_service('container', state='running',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 999)
        client = MagicMock()
        client.get_status.return_value = _status(pods=[])

        module = 'odoo.addons.woow_paas_platform.services.paas_operator'
        # #1159 Q36：cron 每台服務各 commit 一次；本測試只看它 poll 了哪些服務，
        # TransactionCase 禁 commit ⇒ 換成 no-op。
        with patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch.object(self.env.cr, 'commit', lambda: None):
            self.Service._cron_check_deploying_services()

        polled = [
            call.kwargs.get('release_name')
            for call in client.get_status.call_args_list
        ]
        self.assertEqual(svc.state, 'running')
        self.assertNotIn(svc.helm_release_name, polled,
                         'running 的服務不該被 poll，遑論被判逾時')

    def test_timeout_does_not_uninstall_or_rollback(self):
        """只變狀態與訊息：不得呼叫任何會動到叢集資源的 operator 方法。"""
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        client = self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'error')
        self.assertFalse(client.uninstall_release.called)
        self.assertFalse(client.rollback_release.called)
        self.assertFalse(client.delete_release.called)

    def test_optimistic_lock_blocks_timeout_write(self):
        """狀態在 poll 途中被別的 process 改掉時，逾時**不得**覆寫回去。

        既有 poll 用 re-fetch + 比對 ``original_state`` 做 optimistic locking；
        逾時寫 error 必須走同一套，否則會蓋掉別人剛寫進去的狀態。
        """
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        client = MagicMock()

        def _steal_state(*args, **kwargs):
            # 模擬「另一個 process 在 operator 回應期間把服務停權了」
            svc.write({'state': 'suspended'})
            return _status(pods=[])

        client.get_status.side_effect = _steal_state
        svc._poll_and_update_status(client)

        self.assertEqual(svc.state, 'suspended')


class TestTimeoutDiagnosis(_Base):
    """訊息分型——維運不用進叢集就知道往哪查。"""

    def _timeout_message(self, svc, status_body):
        self._poll(svc, status_body)
        self.assertEqual(svc.state, 'error', '前置：這條應該要逾時')
        return svc.error_message

    def test_message_has_grepable_prefix(self):
        """固定前綴讓維運能把「逾時」與「operator 明確回報失敗」分開撈。"""
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        msg = self._timeout_message(svc, _status(pods=[]))

        self.assertTrue(
            msg.startswith('Deployment timed out after'),
            f'訊息前綴不穩定，維運無法 grep：{msg!r}',
        )

    def test_operator_reported_failure_is_not_labelled_timeout(self):
        """對照組：operator 回 failed 的 error 不得帶逾時前綴（兩種來源要分得開）。"""
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        self._poll(svc, _status(release_status='failed'))

        self.assertEqual(svc.state, 'error')
        self.assertNotIn('timed out', svc.error_message or '')

    def test_no_pods_points_at_quota_or_labels(self):
        """無 pod：Pod 物件從沒被建出來 → 方向是 quota 或 chart 標籤。"""
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        msg = self._timeout_message(svc, _status(pods=[]))

        self.assertIn('quota', msg.lower())

    def test_pods_not_ready_points_at_the_pods(self):
        """有 pod 但沒 ready → 方向是 pod 本身（events / logs）。"""
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        msg = self._timeout_message(
            svc, _status(pods=[{'name': 'p', 'phase': 'Running', 'ready': '0/1'}]),
        )

        self.assertIn('pod', msg.lower())
        self.assertNotIn('quota', msg.lower())

    def test_pod_lookup_failure_points_at_rbac(self):
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        msg = self._timeout_message(
            svc, _status(pod_retrieval_error='pods is forbidden'),
        )

        self.assertIn('RBAC', msg)

    def test_vmi_lookup_failure_points_at_rbac(self):
        """W5a 明載的成因：operator 缺 ``virtualmachineinstances`` 權限。"""
        svc = self._make_service('vm', anchor_minutes_ago=_VM_DEFAULT + 1)

        msg = self._timeout_message(svc, _vm_stuck_status())

        self.assertIn('RBAC', msg)

    def test_vm_status_missing_points_at_operator_version(self):
        svc = self._make_service('vm', anchor_minutes_ago=_VM_DEFAULT + 1)

        msg = self._timeout_message(svc, _status())

        self.assertIn('operator', msg.lower())

    def test_vmi_pending_points_at_the_disk_import(self):
        """VMI 還沒建出來 → 方向是 DataVolume / CDI importer。"""
        svc = self._make_service('vm', anchor_minutes_ago=_VM_DEFAULT + 1)

        msg = self._timeout_message(svc, _status(vm_included=True, vmis=[]))

        self.assertIn('DataVolume', msg)

    def test_suspended_service_is_labelled_as_such(self):
        """#482 交互面：停權中的服務被 redeploy 後卡 ``deploying``。

        本案不修 #482（那是 redeploy 管線該收斂 state 的事），但逾時訊息**不得**
        把維運指向叢集——那台 VM 是**被正確地** Halted 著的。訊息要點名停權，
        否則維運會照著「查 pod / 查 quota」的指引白跑一趟。
        """
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)
        svc.pre_suspend_state = 'running'   # _do_suspend 留下的停權標記

        msg = self._timeout_message(svc, _status(pods=[]))

        self.assertIn('suspend', msg.lower())
        self.assertIn('482', msg)


class TestMigrationReAnchor489(_Base):
    """`18.0.1.0.84/post-migrate.py` 的重新錨定（上線窗口消除）。

    合併前有三條轉移入口不記錨，所以升上來那一刻，經它們進入轉移狀態的服務錨是
    **首裝**時間 → 第一次 cron 就誤殺正在正常部署的服務。migration 給那批一個
    新鮮起點。測 model 方法而不是跑 `migrate()`——邏輯全在 model（repo 的
    delegate-to-model 慣例），而真正跑一次 `-u` 的實跑證據附在 PR。
    """

    def _anchors(self, svc):
        svc.invalidate_recordset()
        return svc.deployed_at, svc.last_upgraded_at

    def test_transitional_services_get_a_fresh_anchor(self):
        deploying = self._make_service('container', state='deploying',
                                       anchor_minutes_ago=60 * 24 * 90)
        upgrading = self._make_service('vm', state='upgrading',
                                       anchor_minutes_ago=60 * 24 * 90)

        self.Service._woow_migrate_refresh_transition_anchor_489()

        for svc in (deploying, upgrading):
            with self.subTest(state=svc.state):
                _, last_upgraded = self._anchors(svc)
                age = fields.Datetime.now() - last_upgraded
                self.assertLess(
                    age, timedelta(minutes=1),
                    '轉移中的服務沒拿到新鮮的錨 → -u 後第一次 cron 就誤殺',
                )

    def test_re_anchored_service_no_longer_times_out(self):
        """🔴 端到端：migration 跑完之後，那台服務不會在下一輪 poll 被判逾時。"""
        svc = self._make_service('container', state='deploying',
                                 anchor_minutes_ago=60 * 24 * 90)

        self.Service._woow_migrate_refresh_transition_anchor_489()
        self._poll(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'deploying')

    def test_settled_states_are_not_touched(self):
        """``running`` / ``suspended`` / ``error`` 的 last_upgraded_at 是真實的
        升級歷史，migration 改它就是竄改紀錄。"""
        for state in ('running', 'suspended', 'error'):
            with self.subTest(state=state):
                svc = self._make_service('container', state=state,
                                         anchor_minutes_ago=60 * 24 * 90)
                svc.last_upgraded_at = fields.Datetime.now() - timedelta(days=7)
                before = svc.last_upgraded_at

                self.Service._woow_migrate_refresh_transition_anchor_489()

                _, after = self._anchors(svc)
                self.assertEqual(after, before, f'{state} 的錨被動到了')

    def test_deployed_at_is_never_rewritten(self):
        """``deployed_at`` 是「首次部署時間」，還掛在前台 Overview 上——不能改。"""
        svc = self._make_service('container', state='deploying',
                                 anchor_minutes_ago=60 * 24 * 90)
        before = svc.deployed_at

        self.Service._woow_migrate_refresh_transition_anchor_489()

        deployed_at, _ = self._anchors(svc)
        self.assertEqual(deployed_at, before)

    def test_is_idempotent(self):
        """重跑不累加、不依賴前次結果——效果只取決於執行當下的 state 集合。"""
        svc = self._make_service('container', state='deploying',
                                 anchor_minutes_ago=60 * 24 * 90)

        self.Service._woow_migrate_refresh_transition_anchor_489()
        first = self._anchors(svc)[1]
        self.Service._woow_migrate_refresh_transition_anchor_489()
        second = self._anchors(svc)[1]

        self.assertGreaterEqual(second, first)
        self.assertLess(fields.Datetime.now() - second, timedelta(minutes=1))

    def test_empty_fleet_does_not_raise(self):
        """沒有任何轉移中的服務時要安靜返回，不能炸掉整個 -u。"""
        self.Service.sudo().search([
            ('state', 'in', ['deploying', 'upgrading']),
        ]).write({'state': 'running'})

        self.assertFalse(self.Service._woow_migrate_refresh_transition_anchor_489())

    def test_migration_dir_is_newer_than_manifest_version(self):
        """🔴 F4 鐵律：migration 目錄版號 ≤ manifest 版號 ＝ **靜默 no-op**。

        Odoo 只跑 ``latest_version < 目錄版號`` 的 migration。若有人 bump 了
        manifest 卻沒同步搬目錄（或反過來建了一個 ≤ 現版號的目錄），整支 migration
        不會執行、也不會有任何錯誤訊息——那正是 #489 上線窗口重新打開的形狀。

        Odoo 的實際判準是 ``latest_version < 目錄版號 <= 新版號``，所以真正的
        不變式是**目錄版號不得高於 manifest 版號**——低於是正常且必然的：後續
        每個 PR 都會 bump manifest，而這個目錄要留在原地（它的語意是「升到這一
        版時要跑的一次性動作」）。目錄 ``.84`` ＋ manifest ``.85``，一台還在
        ``.83`` 的實例升上來時 ``.83 < .84 <= .85`` 仍然成立、照跑。

        ⚠️ #482 修正（原斷言是 ``manifest == .84``）：那把「#491 合併當下 manifest
        恰好也是 .84」這個巧合鎖成了契約，於是**任何**後續 bump 版號的 PR 都會
        踩紅——包括不碰 migration 的 PR。守的東西沒變（目錄 > manifest ＝ 永遠
        不執行），只是不再連帶鎖死一個與本 gate 無關的變數。

        ⚠️ #497 再修（原斷言還有 ``max(dirs) == .84``）：同一種病的另一個變體——
        它把「.84 是當下最新的 migration 目錄」這個一次性事實鎖成契約，於是**任何
        新增 migration 目錄**的 PR 都會踩紅。Odoo 會依序跑所有落在
        ``latest_version < 目錄 <= 新版號`` 區間內的目錄，所以新目錄與 .84 並不
        互斥（一台還在 .83 的實例升到 .87 時，``.83 < .84 <= .87`` 仍成立、.84
        照跑）。真正的不變式只有下面那條「目錄不得高於 manifest」。
        """
        from odoo.modules.module import get_module_path

        module_path = get_module_path('woow_paas_platform')
        manifest_version = self.env['ir.module.module'].sudo().search(
            [('name', '=', 'woow_paas_platform')], limit=1,
        ).latest_version

        def _key(v):
            return tuple(int(p) for p in v.split('.') if p.isdigit())

        migrations = os.path.join(module_path, 'migrations')
        dirs = [d for d in os.listdir(migrations)
                if os.path.isdir(os.path.join(migrations, d))
                and d.startswith('18.0.')]

        self.assertIn('18.0.1.0.84', dirs, '#489 的 migration 目錄不見了')
        self.assertLessEqual(
            _key('18.0.1.0.84'), _key(manifest_version),
            f'migration 目錄 18.0.1.0.84 高於 manifest 版號 {manifest_version}'
            ' ⇒ 這個目錄永遠不會被執行（F4 陷阱）',
        )


class TestControllerLayerTimeout(_Base):
    """controller 層（前台每 5 秒那條）必須有同樣的上界。

    就緒判定已經是單一事實源，但**寫 error 的動作在兩層各自的 poll 裡**——
    只接 model 層的話，前台一 poll 就把服務從 error 蓋回 deploying。
    """

    def test_controller_times_out_container(self):
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT + 1)

        self._poll_controller(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'error')
        self.assertIn('timed out', svc.error_message)

    def test_controller_below_threshold_stays_deploying(self):
        svc = self._make_service('container',
                                 anchor_minutes_ago=_CONTAINER_DEFAULT - 1)

        self._poll_controller(svc, _status(pods=[]))

        self.assertEqual(svc.state, 'deploying')

    def test_controller_times_out_vm(self):
        svc = self._make_service('vm', anchor_minutes_ago=_VM_DEFAULT + 1)

        self._poll_controller(svc, _vm_stuck_status())

        self.assertEqual(svc.state, 'error')

    def test_controller_vm_uses_vm_threshold(self):
        svc = self._make_service('vm', anchor_minutes_ago=_CONTAINER_DEFAULT + 10)

        self._poll_controller(svc, _status(vm_included=True, vmis=[]))

        self.assertEqual(svc.state, 'deploying')

    def test_both_layers_agree(self):
        """兩層對同一份 (status, age) 必須給出同一個答案。"""
        cases = [
            (_CONTAINER_DEFAULT + 1, _status(pods=[]), 'error'),
            (_CONTAINER_DEFAULT - 1, _status(pods=[]), 'deploying'),
            (_CONTAINER_DEFAULT + 1, _status(pods=_READY_POD), 'running'),
        ]
        for age, body, expected in cases:
            with self.subTest(age=age):
                svc_model = self._make_service('container', anchor_minutes_ago=age)
                svc_ctrl = self._make_service('container', anchor_minutes_ago=age)

                self._poll(svc_model, body)
                self._poll_controller(svc_ctrl, body)

                self.assertEqual(svc_model.state, expected)
                self.assertEqual(svc_ctrl.state, expected)

"""#482 合併前就已卡死的既有資料：收斂回停權，並讓「訂閱已恢復」者能自癒（#497）。

## 缺陷

#482（PR #493）從源頭修掉了「停權服務被 redeploy／reconfigure 汙染 state」——但那
是對**新**操作而言。合併之前就已經被汙染的既有 row 不在該 PR 範圍內，而 #489／#491
的 age timeout 一上線就會把其中 ``deploying`` / ``upgrading`` 那批逐一打成
``error``。那批的 ``pre_suspend_state`` 仍在＝:meth:`_is_halted` 仍為真＝**它其實還
在停權中**，於是撞上 ``subscription_lifecycle_cron`` 的分支落差：

    訂閱狀態              分支         對 state == 'error'          結果
    in_progress（已付款） :78 → :79    error ≠ suspended → 不 resume 🔴 卡死，無自癒
    expire / close        :88 → :96    error 符合 → _do_suspend()   ✅ 拉回 suspended
    cancel                :105 → :108  同上                          ✅ 同上

也就是說：租戶只要在「被打成 error」之後、「下一輪欠費分支跑到」之前完成付款，服務就
**永久**卡在 error——付了錢服務不會回來，且沒有任何排程會修好它。lifecycle cron 是
interval 1 day，這個窗口是天級的。

## 實查補強（issue 的清單不完整）

1. **病理集合比 ``deploying`` / ``upgrading`` / ``error`` 大**。容器型被 upgrade
   喚醒（#482 的 A/B 實證）之後，poll cron（1 分鐘）會判 pod ready 而把 state 一路
   推進 ``initializing`` → ``running``——:meth:`_poll_and_update_status` 不看
   :meth:`_is_halted`。所以存量裡還有 ``state == 'running'`` 而停權旗標仍在的 row。
2. **那批 running row 是顆地雷**：#493 的 (a′-1) 上線後，它們下一次被 reconfigure /
   redeploy 就會被判為 halted → 寫回 ``suspended`` ＋ scale 0 ＝**已付款租戶正在跑的
   服務被關掉**。拆雷的方式是清掉那個 stale 旗標，而 :meth:`_do_resume` 正好會清。
3. 🔴 **收斂 state 必須同時把 workload 收回 0**，不是可選：:meth:`_do_suspend` 開頭
   ``if self.state == 'suspended': return``，所以一旦把 state 寫成 ``suspended``，
   cron 的欠費分支就永遠早退——唯一還會 scale 0 的路徑被自己關掉了。
4. **二次汙染**：state 已是 ``error`` 的病理 row 若讓欠費分支先跑到，
   :meth:`_do_suspend` 會把 ``pre_suspend_state`` 從 ``'running'`` 覆寫成
   ``'error'``（``self.state if self.state in ('running', 'error')``）→ 之後復權會
   還原成 error 態。

## 修法（三件事，各有分工）

* **(A) 一次性 migration**（``18.0.1.0.87``）：把 ``deploying`` / ``upgrading`` /
  ``initializing`` / ``error`` ＋ 停權旗標的那批 workload 收回 0（重用 #493 的
  :meth:`_rescale_halted_after_upgrade`）並把 state 收斂回 ``suspended``。**不碰
  ``running``**——誤傷代價是把在跑的服務關掉，而 cron 24h 內會正確分流。
* **(B) cron 縱深**：分支 2 的 guard 從 ``state == 'suspended'`` 改讀
  :meth:`_is_halted`（mixin 補預設實作，SmartHome / SecurityAccess 語意零變化）。
  這是 running 那批唯一的去雷路徑，也讓未來任何同構殘留都能自癒。
* **(D) 不覆寫**：:meth:`_do_suspend` 對已 halted 者保留既有 ``pre_suspend_state``，
  堵住上面第 4 點。

判別式為什麼不會誤傷「真部署失敗」的 error 服務：``pre_suspend_state`` 全 repo 只在
:meth:`_do_suspend` 寫、:meth:`_do_resume` 清（``migrations/18.0.1.0.7`` 只建欄位、
無 seed、無其他寫入點），所以沒停權過的服務該欄位恆為 ``False``——判別式天然排除它，
不需要再疊訂閱狀態這種會漏掉欠費那批的條件。
"""
import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from ..models.cloud_service import _internal_provision


# 殘留記號的固定前綴。scale 失敗但 state 已收斂時寫進 ``error_message``，讓 prod
# rollout 的 checklist 能用一條 grep／search 盤出「收斂了但 workload 未確認停機」的
# 那批。測試這裡獨立寫一份，前綴被改動時會紅——它是對維運的契約，不該靜默漂移。
_MARKER_497 = '#497 已收斂但無法確認 workload 已停機'


def _patch_operator_client(client=None):
    """patch model 層取得 operator client 的入口，回 (patcher, mock_client)。"""
    client = client or MagicMock()
    return patch(
        'odoo.addons.woow_paas_platform.services.paas_operator'
        '.get_paas_operator_client',
        return_value=client,
    ), client


def _scaled_replicas(client):
    """取最後一次 scale_release 的 replicas（未呼叫回 None）。"""
    if not client.scale_release.call_args_list:
        return None
    call = client.scale_release.call_args_list[-1]
    if 'replicas' in call.kwargs:
        return call.kwargs['replicas']
    return call.args[2] if len(call.args) > 2 else None


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '497 Workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Container 497',
            'slug': 'container-497',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'oci://example.com/container',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
        })

    def _stuck(self, state='deploying', pre_suspend_state='running', **kwargs):
        """組一筆「#493 合併前留下的」卡死 row。

        直接 create 而不走 ``_do_suspend`` ＋ 舊碼 —— 舊碼已經不存在了，能重現的只有
        它留下的**資料形狀**：state 被汙染、停權旗標仍在。
        """
        vals = {
            'name': f'svc-497-{state}',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': state,
            'pre_suspend_state': pre_suspend_state,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': f'svc-497-{state}',
        }
        vals.update(kwargs)
        return _internal_provision(self.Service).create(vals)

    def _converge(self, client=None):
        """跑 migration 的 model 方法（operator client 被 patch 掉）。"""
        patcher, client = _patch_operator_client(client)
        with patcher:
            converged = self.Service._woow_migrate_converge_stuck_suspended_497()
        return converged, client


class TestConvergeStuckSuspended497(_Base):
    """(A) 一次性 migration：把卡死的停權 row 收斂回 ``suspended``。"""

    def test_deploying_row_converges_to_suspended(self):
        """🔴 #482 的原始形狀：redeploy 把 state 汙染成 ``deploying``。"""
        svc = self._stuck(state='deploying')

        self._converge()

        self.assertEqual(svc.state, 'suspended')

    def test_upgrading_row_converges_to_suspended(self):
        """reconfigure 路徑留下的是 ``upgrading``。"""
        svc = self._stuck(state='upgrading')

        self._converge()

        self.assertEqual(svc.state, 'suspended')

    def test_error_row_converges_to_suspended(self):
        """🔴 #497 主症狀：age timeout 已經把它打成 ``error``。

        這批就是「訂閱已恢復卻無自癒路徑」的那條分支——cron 分支 2 要求
        ``state == 'suspended'`` 才 resume，而它是 ``error``。
        """
        svc = self._stuck(state='error',
                          error_message='Deployment timed out after 30 minutes')

        self._converge()

        self.assertEqual(svc.state, 'suspended')

    def test_initializing_row_converges_to_suspended(self):
        """容器型被喚醒後 poll 會把它推進 ``initializing``（issue 清單漏列）。"""
        svc = self._stuck(state='initializing')

        self._converge()

        self.assertEqual(svc.state, 'suspended')

    def test_workload_is_scaled_back_to_zero(self):
        """🔴 收斂 state 而不收 workload ＝ 把唯一會 scale 0 的路徑關掉。

        ``_do_suspend`` 開頭就 ``if self.state == 'suspended': return``，所以寫完
        ``suspended`` 之後 cron 的欠費分支再也不會去收 workload。容器型被 upgrade
        喚醒過（#482 A/B 實證 pod 真的 Running），不收＝欠費服務照燒＝計費繞過。
        """
        self._stuck(state='deploying')

        _, client = self._converge()

        self.assertEqual(_scaled_replicas(client), 0)

    def test_pre_suspend_state_is_preserved_so_resume_restores_running(self):
        """收斂只動 state——``pre_suspend_state`` 是復權要還原的目標，不能碰。"""
        svc = self._stuck(state='error', pre_suspend_state='running')

        self._converge()

        self.assertEqual(svc.pre_suspend_state, 'running')

    def test_misleading_timeout_message_is_cleared(self):
        """收斂成功後，age timeout 留下的訊息會誤導維運（它不是部署逾時，是停權）。"""
        svc = self._stuck(
            state='error',
            error_message='Deployment timed out after 30 minutes: no pods found',
        )

        self._converge()

        self.assertFalse(svc.error_message)

    # ---------------------------------------------------------------- #
    #  不該碰的（誤傷防線）                                             #
    # ---------------------------------------------------------------- #

    def test_a_genuine_deploy_failure_is_not_touched(self):
        """🔴 沒停權過的 ``error`` 服務＝真部署失敗，收斂它就是誤傷。

        判別式靠 ``pre_suspend_state``：它全 repo 只在 ``_do_suspend`` 寫、
        ``_do_resume`` 清，所以沒停權過的服務恆為 ``False``。
        """
        svc = self._stuck(state='error', pre_suspend_state=False,
                          error_message='ImagePullBackOff')

        _, client = self._converge()

        self.assertEqual(svc.state, 'error')
        self.assertEqual(svc.error_message, 'ImagePullBackOff')
        self.assertIsNone(_scaled_replicas(client), '真失敗的服務被 scale 了')

    def test_a_genuine_deploying_service_is_not_touched(self):
        """正在正常部署中的服務（無停權旗標）不得被收斂成 suspended。"""
        svc = self._stuck(state='deploying', pre_suspend_state=False)

        self._converge()

        self.assertEqual(svc.state, 'deploying')

    def test_running_row_is_left_for_the_cron(self):
        """🔴 ``running`` ＋ 停權旗標：**不碰**。

        migration 若把它 scale 0，錯的時候就是把正在跑的服務關掉；而 cron 24h 內會
        正確分流（訂閱有效 → resume 並清掉 stale 旗標；欠費 → suspend）。誤傷代價
        不對稱，故留給 cron。
        """
        svc = self._stuck(state='running')

        _, client = self._converge()

        self.assertEqual(svc.state, 'running')
        self.assertEqual(svc.pre_suspend_state, 'running')
        self.assertIsNone(_scaled_replicas(client))

    def test_suspended_row_is_not_touched(self):
        """已經正確停權的服務不在收斂範圍（scale 一次也是多餘的叢集呼叫）。"""
        svc = self._stuck(state='suspended')

        _, client = self._converge()

        self.assertEqual(svc.state, 'suspended')
        self.assertIsNone(_scaled_replicas(client))

    def test_deleting_row_is_not_touched(self):
        """``deleting`` ＝ cleanup 已跑；把它拉回 ``suspended`` 是反向的狀態抖動。"""
        svc = self._stuck(state='deleting')

        self._converge()

        self.assertEqual(svc.state, 'deleting')

    # ---------------------------------------------------------------- #
    #  operator 失敗容忍（migration context）                           #
    # ---------------------------------------------------------------- #

    def test_operator_failure_does_not_raise(self):
        """🔴 migration 拋例外＝整支 ``-u`` 失敗＝平台起不來。一律吞。"""
        client = MagicMock()
        client.scale_release.side_effect = Exception('operator unreachable')
        self._stuck(state='deploying')

        self._converge(client)   # 不得拋出

    def test_state_converges_even_when_the_scale_fails(self):
        """state 收斂是本地事實修正，不依賴叢集。

        scale 失敗就不收斂的話，這筆 row 會留在 ``error``——自癒路徑仍然是斷的，
        等於白跑一趟 migration。
        """
        client = MagicMock()
        client.scale_release.side_effect = Exception('operator unreachable')
        svc = self._stuck(state='error')

        self._converge(client)

        self.assertEqual(svc.state, 'suspended')

    def test_scale_failure_leaves_a_greppable_marker(self):
        """🔴 殘留記號要能機械盤點：prod rollout 的 checklist 用它掃上線後殘留。

        停權服務照跑就是計費繞過，靜默吞掉等於把帳務問題藏起來。前綴固定，細節
        （operator 的實際錯誤）接在後面。
        """
        client = MagicMock()
        client.scale_release.side_effect = Exception('operator unreachable')
        svc = self._stuck(state='deploying')

        self._converge(client)

        self.assertIn(_MARKER_497, svc.error_message)
        self.assertIn('operator unreachable', svc.error_message)

    def test_missing_operator_client_leaves_the_same_marker(self):
        """operator 未設定時同樣要留記號——「沒去做」和「做了失敗」都是未確認。"""
        svc = self._stuck(state='deploying')

        patcher = patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=None,
        )
        with patcher:
            self.Service._woow_migrate_converge_stuck_suspended_497()

        self.assertEqual(svc.state, 'suspended')
        self.assertIn(_MARKER_497, svc.error_message)

    def test_row_without_release_coordinates_leaves_the_marker(self):
        """沒有 release 名／namespace 的 row 無從 scale——一樣算未確認。"""
        svc = self._stuck(state='deploying', helm_release_name=False,
                          helm_namespace=False)

        self._converge()

        self.assertEqual(svc.state, 'suspended')
        self.assertIn(_MARKER_497, svc.error_message)

    # ---------------------------------------------------------------- #
    #  形制                                                             #
    # ---------------------------------------------------------------- #

    def test_is_idempotent(self):
        """重跑不改變結果：第二輪那批已是 ``suspended``，不再進判別式。"""
        svc = self._stuck(state='deploying')

        self._converge()
        _, client = self._converge()

        self.assertEqual(svc.state, 'suspended')
        self.assertIsNone(_scaled_replicas(client), '第二輪又打了一次 operator')

    def test_empty_fleet_does_not_raise(self):
        """沒有卡死資料時要安靜返回，不能炸掉整個 ``-u``。"""
        converged, _ = self._converge()

        self.assertFalse(converged)

    def test_converged_row_is_outside_timeout_governance(self):
        """🔴 端到端：收斂後 age timeout 不再裁決它。

        ``_TIMEOUT_GOVERNED_STATES`` 只有 ``deploying`` / ``upgrading``，所以收斂
        本身就是把它移出 #491 的管轄——否則 ``-u`` 之後第一輪 cron 又把它打回
        ``error``，migration 等於白做。
        """
        svc = self._stuck(state='deploying')
        svc.write({'deployed_at': '2020-01-01 00:00:00'})

        self._converge()

        self.assertFalse(svc._is_timeout_governed())
        self.assertFalse(svc._check_transition_timeout('no pods found'))
        self.assertEqual(svc.state, 'suspended')

    def test_migration_dir_is_not_higher_than_manifest_version(self):
        """🔴 F4 鐵律：migration 目錄版號 > manifest ＝ **靜默 no-op**。

        Odoo 只跑 ``latest_version < 目錄版號 <= 新版號`` 的 migration。目錄留在
        原地、manifest 隨後續 PR 往上長是正常的；反過來（目錄比 manifest 高）則是
        整支不會執行、也不會有任何錯誤訊息。
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

        self.assertIn('18.0.1.0.87', dirs, '#497 的 migration 目錄不見了')
        self.assertLessEqual(
            _key('18.0.1.0.87'), _key(manifest_version),
            f'migration 目錄 18.0.1.0.87 高於 manifest 版號 {manifest_version}'
            ' ⇒ 這個目錄永遠不會被執行（F4 陷阱）',
        )


class TestSuspendKeepsOriginalPreSuspendState497(_Base):
    """(D) 縱深：``_do_suspend`` 不覆寫既有的 ``pre_suspend_state``。

    卡死 row 的 state 已被汙染成 ``error``。若欠費分支先跑到，``_do_suspend`` 會把
    停權前狀態記成 ``'error'``（它讀的是**當下**的 state），於是租戶付款復權後拿回
    一台顯示 error 的服務——癒後帶傷。
    """

    def test_suspending_an_already_halted_row_keeps_the_original_state(self):
        """🔴 已 halted（旗標在）者再 suspend，停權前狀態必須維持 ``running``。"""
        svc = self._stuck(state='error', pre_suspend_state='running')

        patcher, _ = _patch_operator_client()
        with patcher:
            svc._do_suspend()

        self.assertEqual(svc.state, 'suspended')
        self.assertEqual(
            svc.pre_suspend_state, 'running',
            '停權前狀態被當下的 error 蓋掉 → 復權會還原成 error',
        )

    def test_first_suspend_still_records_the_current_state(self):
        """不回歸：首次停權（旗標為空）仍要如實記下當下狀態。"""
        svc = self._stuck(state='error', pre_suspend_state=False)

        patcher, _ = _patch_operator_client()
        with patcher:
            svc._do_suspend()

        self.assertEqual(svc.pre_suspend_state, 'error')

    def test_resume_after_converge_restores_running(self):
        """端到端：收斂 → 復權 → 回到停權前的 ``running``（不是 error）。"""
        svc = self._stuck(state='error', pre_suspend_state='running')

        self._converge()
        patcher, client = _patch_operator_client()
        with patcher:
            svc._do_resume()

        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.pre_suspend_state)
        self.assertEqual(_scaled_replicas(client), 1)


class TestLifecycleCronResumesHaltedRows497(_Base):
    """(B) 縱深：cron 分支 2 的 guard 改讀 ``_is_halted()``。

    這是 ``running`` ＋ stale 旗標那批唯一的去雷路徑，也讓未來任何「讓停權服務落到
    非 suspended 狀態」的路徑都能自癒。
    """

    def setUp(self):
        super().setUp()
        self.Cron = self.env['woow_paas_platform.subscription_lifecycle_cron']

    def _subscribe(self, entity, state='in_progress'):
        """掛一張最小可用的訂閱（沿用 test_subscription_lifecycle 的形制）。"""
        Plan = self.env['sh.subscription.plan']
        plan = Plan.search([], limit=1) or Plan.create({
            'name': '497 Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
        })
        ProductTemplate = self.env['product.template']
        product_tmpl = ProductTemplate.search([
            ('sh_product_subscribe', '=', True),
            ('sh_subscription_plan_id', '=', plan.id),
        ], limit=1) or ProductTemplate.create({
            'name': '497 Subscription Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id,
            'list_price': 100,
        })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': plan.id,
            'state': state,
            'sh_start_date': date.today() - timedelta(days=30),
            'sh_end_date': date.today() + timedelta(days=30),
        })
        sub.write({'state': state})
        entity.write({'subscription_id': sub.id})
        return sub

    def test_cron_resumes_a_row_whose_state_was_polluted(self):
        """🔴 #497 主症狀的自癒：付了款的 ``error`` row 要被 cron 拉回來。"""
        svc = self._stuck(state='error')
        self._subscribe(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            self.Cron._cron_enforce_subscription_lifecycle()

        svc.invalidate_recordset()
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.pre_suspend_state)
        self.assertEqual(_scaled_replicas(client), 1)

    def test_cron_defuses_a_running_row_with_a_stale_flag(self):
        """🔴 去雷：``running`` ＋ stale 旗標的 row 必須被清掉旗標。

        不清的話，#493 的 (a′-1) 上線後它下一次被 reconfigure／redeploy 就會被判為
        halted → 寫回 ``suspended`` ＋ scale 0 ＝ 已付款租戶正在跑的服務被關掉。
        ``_do_resume`` 的 scale 1 對一台本來就在跑的服務是冪等的，所以零停機。
        """
        svc = self._stuck(state='running')
        self._subscribe(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            self.Cron._cron_enforce_subscription_lifecycle()

        svc.invalidate_recordset()
        self.assertFalse(
            svc.pre_suspend_state,
            'stale 旗標還在 → 下次 upgrade 會把在跑的付費服務關掉',
        )
        self.assertEqual(svc.state, 'running')
        self.assertEqual(_scaled_replicas(client), 1)

    def test_cron_still_ignores_a_never_suspended_service(self):
        """不回歸：沒停權過的正常服務不該被 cron 動到。"""
        svc = self._stuck(state='running', pre_suspend_state=False)
        self._subscribe(svc)

        patcher, client = _patch_operator_client()
        with patcher:
            self.Cron._cron_enforce_subscription_lifecycle()

        svc.invalidate_recordset()
        self.assertEqual(svc.state, 'running')
        self.assertIsNone(_scaled_replicas(client))

    def test_smart_home_semantics_are_unchanged(self):
        """🔴 零 delta：SmartHome 的判準仍只認 ``suspended``。

        mixin 的預設實作是 ``state == 'suspended'``——與 SmartHome ``_do_resume``
        自己的 guard 一致。放寬只發生在 CloudService（它的 state 會被 upgrade 管線
        改寫，需要一個不受呼叫順序影響的事實源）。
        """
        home = self.SmartHome.create({
            'name': 'home-497',
            'workspace_id': self.workspace.id,
            'state': 'error',
            'pre_suspend_state': 'active',
        })
        self._subscribe(home)

        with patch('odoo.addons.woow_paas_platform.services.paas_operator'
                   '.get_paas_operator_client', return_value=MagicMock()):
            self.Cron._cron_enforce_subscription_lifecycle()

        home.invalidate_recordset()
        self.assertEqual(home.state, 'error')
        self.assertEqual(home.pre_suspend_state, 'active')

    def test_cron_still_suspends_an_unpaid_stuck_row(self):
        """不回歸：欠費那條分支照舊把卡死 row 拉回 ``suspended`` 並收回 workload。

        順帶驗 (D)：這條路徑讀的是**當下**的 state（``error``），沒有 (D) 的話停權前
        狀態會被記成 ``'error'``，租戶付款後拿回一台顯示 error 的服務。
        """
        svc = self._stuck(state='error')
        sub = self._subscribe(svc, state='expire')
        sub.write({'sh_end_date': date.today() - timedelta(days=30)})

        patcher, client = _patch_operator_client()
        with patcher:
            self.Cron._cron_enforce_subscription_lifecycle()

        svc.invalidate_recordset()
        self.assertEqual(svc.state, 'suspended')
        self.assertEqual(_scaled_replicas(client), 0)
        self.assertEqual(svc.pre_suspend_state, 'running')

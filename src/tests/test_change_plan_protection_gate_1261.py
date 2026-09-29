"""#1261：降級到 0 席方案時，仍受 Access 保護的 route 會卡死（prod 2026-09-29 實測）。

重現（prod，SH）：'+1access 月繳' 開保護 → 只留一條 Block（country）policy →
Change Plan 到 'Connect Monthly 不加購'（``ac_max_access_seats == 0``）**成功**；
route 仍 ``is_protected``、Cloudflare Access app 原樣留著 ⇒ 對外 403，而 0 席方案
的 Access 頁籤（與 ``/access`` 內容）整個不顯示 ⇒ 租戶關不掉保護。

本檔釘兩層修正的後端那層：

1. **換方案 gate**（``_seat_gate_denial`` → ``_protection_gate_denial``）：CS／SH 換到
   0 席方案時，只要還有 route 受保護就擋，``code == 'access_protection_active'``；
   擋在任何副作用（退點、關舊訂閱）之前；關掉保護後同一個換方案放行。
2. **已卡住記錄的復原通道**：0 席方案下，租戶仍能透過 access-config 端點把保護
   關掉（後端關閉方向本來就不看席位——這裡把它釘成可證偽的行為，前端復原模式
   全靠它）。

## 判別式

- gate 的 marker ``Turn protection off`` 與 #773 的兩個 marker
  （``Delete access rules or policies`` / ``Delete every email allow-list rule``）
  互不為子字串 ⇒「被擋了」分得出是哪一種拒絕；另以 ``code`` 雙重確認。
- 「擋住」一律連同**副作用沒發生**一起斷言（舊訂閱狀態、方案、退點交易、
  route 仍受保護）——「擋住」與「跑一半才被擋」只看回傳值完全同形。
- 「放行」斷言到具體結果（success、新方案、退點筆數），不是「沒丟例外」。
- SA 刻意**不適用**（SA 的 Access 分頁不看席位、永遠關得掉）：有一條陽性對照釘住
  「0 席 SA ＋ 受保護 route ⇒ gate 放行」，免得有人「順手對齊」把 SA 的合法降級擋掉。

## ⛔ 本檔沒有涵蓋

- 前端復原模式（SmartHomeDetailPage／ServiceDetailPage 的 ``accessRecoveryMode``
  與 Access 頁籤的 ``seatless``）——無 Odoo 測試，於 PR 以 owl＋jsdom 另行渲染驗證。
- CS 換方案端點的端到端行為：CS 走同一支 gate（model 層已測），端點接線由
  ``test_change_plan_seat_gate_773.TestSeatGateIsWiredBeforeFirstWrite`` 的 AST
  檢查涵蓋。
"""
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.smart_home import (
    SmartHomeController,
)

from ..models.cloud_service import _internal_provision
from ..models.security_access import _skip_route_sync
from ..models.smart_home_route import _sm_skip_access_sync
from .smart_home_fixture import sh_model
from .test_change_plan_seat_gate_773 import GATE_MARKER, GATE_MARKER_ZERO

#: #1261 gate 的訊息 marker（⛔ 不得是 #773 兩個 marker 的子字串／超字串）
PROTECTION_MARKER = 'Turn protection off'
PROTECTION_CODE = 'access_protection_active'
SEAT_CODE = 'access_seats_exceeded'

_POLICY = 'woow_paas_platform.security_access_access_policy'
_ROUTE = 'woow_paas_platform.smart_home_route'


def _patch_request(env):
    return patch(
        'odoo.addons.woow_paas_platform.controllers.smart_home.request',
        new=MagicMock(env=env),
    )


class _ProtectionGateFixture:
    """一台 SH（無 tunnel ⇒ Access sync 一律略過，零 mock）＋ primary route
    ＋ 兩個上架中的 SH 方案：1 席（'+1access'）與 0 席（base）。

    刻意**不**繼承 ``TransactionCase``，且本 class 內**不得**有 ``test_*`` 方法
    （mixin 的 test 不會被收集，理由同 ``_SeatFixture``）。

    SH 的訂閱以 workspace owner（只有 ``base.group_user``）的 partner 建立，並補一筆
    首購真扣款（#324：退點前提＝本訂閱有成功付款）——這樣換方案當天的退點是
    **非零**，「gate 擋在退點之前」才有鑑別力（退點本來就是 0 的話，擋在前或後
    輸出相同）。
    """

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        self.Plan = self.env['sh.subscription.plan']
        self.Policy = self.env[_POLICY]
        self.Route = self.env[_ROUTE]
        self.WalletTx = self.env['wallet.transaction']
        self.controller = SmartHomeController()

        self.plan_ac1 = self._plan('Gate1261 +1access', seats=1, price=300.0)
        self.plan_base = self._plan('Gate1261 base', seats=0, price=100.0)
        self.env['woow_paas_platform.smart_home_template'].create({
            'name': 'Gate1261 Tmpl',
            'subscription_plan_ids': [
                (6, 0, [self.plan_ac1.id, self.plan_base.id])],
        })

        self.owner = self.env['res.users'].create({
            'name': 'Gate1261 Owner',
            'login': 'gate1261_owner_%s' % uuid.uuid4().hex[:8],
            'email': 'gate1261_owner@example.test',
        })
        # 與 test_smart_home_subscription_flow 同一條 fixture 紀律：沙盒 DB 的
        # default_user_template 會自動給管理群組，不重設的話 fixture 形同 admin。
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Gate1261 WS', 'owner_id': self.owner.id,
        })
        # `with_env(self.env)` 洗掉 sh_model 的開閘 sentinel（理由見
        # test_sh_access_endpoints_768::_smart_home）。
        self.smart_home = sh_model(self.env).sudo().create({
            'name': 'Gate1261 Home', 'workspace_id': self.workspace.id,
        }).with_env(self.env)
        self.smart_home.sudo().write(
            {'subdomain': 'paas-sm-1261-%s' % uuid.uuid4().hex[:10]})
        self.route = _sm_skip_access_sync(self.Route.sudo()).create({
            'smart_home_id': self.smart_home.id, 'is_primary': True,
        }).with_context(sm_skip_access_sync=False)

        self.partner = self.owner.partner_id
        self.smart_home._create_and_confirm_subscription(
            self.partner, self.plan_ac1)
        self.old_sub = self.smart_home.sudo().subscription_id
        self.WalletTx.create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -self.old_sub.sh_plan_price,
            'subscription_id': self.old_sub.id, 'state': 'done',
        })
        self.assertEqual(self.old_sub.state, 'in_progress', 'fixture 前提')
        self.assertTrue(self.route.hostname, 'fixture 前提：route 要有 hostname')

    # ---------------- helpers ---------------- #

    def _plan(self, name, seats, price):
        plan = self.Plan.create({
            'name': name, 'sh_plan_price': price,
            'sh_duration': 1, 'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1, 'sh_trial': False,
            'sa_max_access_policies': 10, 'sa_max_emails_per_policy': 0,
            'ac_max_access_seats': seats,
        })
        # `_create_and_confirm_subscription` 以 product.template 反查 plan
        self.env['product.template'].sudo().create({
            'name': name + ' P', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id, 'list_price': price,
        })
        return plan

    def _protect(self, on=True):
        # 只搬 DB 狀態：本 SH 沒有 tunnel，sync 本來就會略過；開閘只是讓
        # 「這裡不打 operator」不依賴那個間接前提。
        _sm_skip_access_sync(self.route.sudo()).write({'is_protected': on})

    def _block_country_policy(self):
        """prod 重現用的那一條：Block（country），不佔任何席位。"""
        return self.Policy.create({
            'smart_home_route_id': self.route.id, 'name': 'block-country',
            'decision': 'deny',
            'rule_ids': [(0, 0, {'group': 'include', 'rule_type': 'country',
                                 'rule_value': 'CN'})],
        })

    def _refund_count(self):
        return self.WalletTx.search_count([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund'),
        ])

    def _change_plan(self, plan):
        self.env.invalidate_all()
        with _patch_request(self.env(user=self.owner)):
            return self.controller.api_smarthome_change_plan(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                plan_id=plan.id,
            )

    def _set_protected_as_tenant(self, on):
        """租戶自己打 access-config 端點（前端復原模式的「Turn protection off」）。"""
        self.env.invalidate_all()
        with _patch_request(self.env(user=self.owner)):
            return self.controller.api_smarthome_route_access_config(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                route_id=self.route.id,
                is_protected=on,
            )


class TestProtectionGateRuler(_ProtectionGateFixture, TransactionCase):
    """gate 本身的判準（model 層）。"""

    allow_inherited_tests_method = True

    def test_protected_route_blocks_zero_seat_target(self):
        """主場景：受保護 route ＋ 目標 0 席 ⇒ 擋，且說得出是哪一條 hostname。"""
        self._protect()
        self._block_country_policy()
        denial = self.Policy._seat_gate_denial(self.smart_home, self.plan_base)
        self.assertIsNotNone(denial, '0 席目標＋仍受保護必須擋（#1261 的洞）')
        self.assertEqual(denial['code'], PROTECTION_CODE, denial)
        self.assertIn(PROTECTION_MARKER, denial['error'], denial)
        self.assertIn(self.route.hostname, denial['error'], denial)
        self.assertEqual(denial['protected_hostnames'], [self.route.hostname])
        self.assertEqual(denial['target_seats'], 0)
        # Block（country）不佔席位 ⇒ 席位尺本身是合規的，擋它的只能是 #1261 這條
        self.assertEqual(denial['current_seats'], 0)

    def test_unprotected_route_passes(self):
        """陽性對照：同一台 SH、保護關著 ⇒ 放行（policy 留著也一樣）。"""
        self._block_country_policy()
        self.assertFalse(self.route.is_protected)
        self.assertIsNone(
            self.Policy._seat_gate_denial(self.smart_home, self.plan_base))

    def test_seated_target_passes_even_when_protected(self):
        """目標方案有席位 ⇒ 頁籤照樣在、保護管得到 ⇒ 不擋（升級天然通過）。"""
        self._protect()
        self.assertIsNone(
            self.Policy._seat_gate_denial(self.smart_home, self.plan_ac1))

    def test_protection_lost_route_still_counts(self):
        """``access_protection_lost`` 的 route 照算：is_protected 仍是意圖，
        #803 cron 會把 Access app 推回去，換到 0 席後一樣卡住。"""
        self._protect()
        self.route.sudo().write({'access_protection_lost': True})
        denial = self.Policy._seat_gate_denial(self.smart_home, self.plan_base)
        self.assertIsNotNone(denial)
        self.assertEqual(denial['code'], PROTECTION_CODE)

    def test_seat_violation_takes_precedence(self):
        """兩者都不合規 ⇒ 先回席位那一種（email 白名單關掉保護也收斂不了）。"""
        self._protect()
        self.Policy.create({
            'smart_home_route_id': self.route.id, 'name': 'allow-mail',
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include', 'rule_type': 'email_list',
                                 'rule_value': 'a@x.com'})],
        })
        denial = self.Policy._seat_gate_denial(self.smart_home, self.plan_base)
        self.assertEqual(denial['code'], SEAT_CODE, denial)
        self.assertIn(GATE_MARKER_ZERO, denial['error'])
        self.assertNotIn(PROTECTION_MARKER, denial['error'])

    def test_markers_do_not_overlap(self):
        """marker 互不包含 ⇒ 用任一方的斷言測另一方必紅（鑑別力的前提）。"""
        for other in (GATE_MARKER, GATE_MARKER_ZERO):
            self.assertNotIn(PROTECTION_MARKER, other)
            self.assertNotIn(other, PROTECTION_MARKER)
        self._protect()
        msg = self.Policy._seat_gate_denial(
            self.smart_home, self.plan_base)['error']
        self.assertNotIn(GATE_MARKER, msg)
        self.assertNotIn(GATE_MARKER_ZERO, msg)

    def test_gate_changes_nothing(self):
        """gate 只回報：⛔ 不關保護、不刪 policy（與 #773 同一條裁決）。"""
        self._protect()
        pol = self._block_country_policy()
        self.assertIsNotNone(
            self.Policy._seat_gate_denial(self.smart_home, self.plan_base))
        self.route.invalidate_recordset(['is_protected'])
        self.assertTrue(self.route.is_protected, 'gate 不得自動關掉保護')
        self.assertTrue(pol.exists(), 'gate 不得刪 policy')

    def test_cloud_service_is_gated_too(self):
        """CS 的 Access 頁籤同樣只給有席位的方案（#746 拍板 6）⇒ 同一個洞、同一條 gate。

        CS 不設 subdomain ⇒ Access sync 在第一道 gate 就略過，零 mock。
        """
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Gate1261 CS Tmpl', 'slug': 'gate-1261-cs',
            'helm_repo_url': 'oci://example.invalid',
            'helm_chart_name': 'gate-1261-cs', 'helm_chart_version': '0.0.0',
            'is_active': True,
        })
        svc = _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create({
                'name': 'Gate1261 CS', 'workspace_id': self.workspace.id,
                'template_id': template.id,
                'reference_id': 'test-1261-cs-%s' % uuid.uuid4().hex[:8],
                'state': 'running',
                'provision_partner_id': self.env.user.partner_id.id,
            })
        self.assertIsNone(self.Policy._seat_gate_denial(svc, self.plan_base),
                          '陽性對照：未保護的 CS 不該被擋')
        svc.sudo().write({'is_protected': True})
        denial = self.Policy._seat_gate_denial(svc, self.plan_base)
        self.assertIsNotNone(denial, '受保護的 CS 換到 0 席必須擋')
        self.assertEqual(denial['code'], PROTECTION_CODE)
        self.assertIsNone(self.Policy._seat_gate_denial(svc, self.plan_ac1),
                          '有席位的目標方案不擋')

    def test_security_access_is_not_gated(self):
        """🔴 SA 刻意不適用：SA 的 Access 分頁不看席位、永遠關得掉保護，0 席 SA
        掛 Block／Bypass policy 是合法狀態。擋 SA 會擋掉合法降級。"""
        sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'Gate1261 SA', 'workspace_id': self.workspace.id,
        })
        route = self.env['woow_paas_platform.security_access_route'].create({
            'security_access_id': sa.id, 'subdomain_prefix': 'gate-1261-sa'})
        _skip_route_sync(route).write({'is_protected': True})
        self.assertTrue(route.is_protected, '前提：SA route 受保護')
        self.assertIsNone(
            self.Policy._seat_gate_denial(sa, self.plan_base),
            'SA 不在 _SEAT_GATED_PROTECTION_ANCHORS 內，0 席降級不得被 #1261 擋')

    def test_unregistered_anchor_fails_loud(self):
        """hostname 讀法沒登記的錨點 ⇒ raise，而不是靜默回 []（那等於 gate 失效）。"""
        sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'Gate1261 SA2', 'workspace_id': self.workspace.id,
        })
        with self.assertRaises(ValidationError):
            self.Policy._protected_hostnames_for_anchor(sa)


class TestSmartHomeChangePlanProtectionGate(_ProtectionGateFixture,
                                            TransactionCase):
    """SH 換方案端點（租戶身分）：擋在副作用之前、關保護後放行、卡死記錄可自救。"""

    allow_inherited_tests_method = True

    def test_downgrade_refused_while_protected_without_side_effects(self):
        """prod 重現路徑：+1access 開保護＋Block policy → 換到 0 席 ⇒ 被擋、什麼都沒變。"""
        self._protect()
        self._block_country_policy()
        refunds_before = self._refund_count()

        res = self._change_plan(self.plan_base)

        self.assertFalse(res.get('success'), res)
        self.assertIn(PROTECTION_MARKER, res.get('error') or '', res)
        self.assertEqual(res['data']['code'], PROTECTION_CODE, res)
        self.assertEqual(res['data']['protected_hostnames'],
                         [self.route.hostname], res)
        # 副作用一個都不能發生
        self.old_sub.invalidate_recordset(['state'])
        self.assertEqual(self.old_sub.state, 'in_progress',
                         'gate 應擋在關閉舊訂閱之前')
        self.smart_home.invalidate_recordset(['subscription_id'])
        self.assertEqual(self.smart_home.sudo().subscription_id, self.old_sub)
        self.assertEqual(
            self.smart_home.sudo().subscription_id.sh_subscription_plan_id,
            self.plan_ac1, '方案不得被換掉')
        self.assertEqual(self._refund_count(), refunds_before,
                         'gate 應擋在退點之前')
        self.route.invalidate_recordset(['is_protected'])
        self.assertTrue(self.route.is_protected, 'gate 不得自動關保護')

    def test_downgrade_allowed_after_tenant_turns_protection_off(self):
        """租戶先關保護（同一個端點、同一個身分）⇒ 同一個降級放行，且真的換成功。"""
        self._protect()
        self._block_country_policy()
        self.assertFalse(self._change_plan(self.plan_base).get('success'),
                         '前提：保護開著時被擋')

        off = self._set_protected_as_tenant(False)
        self.assertTrue(off.get('success'), off)
        self.assertFalse(off['data']['is_protected'], off)

        refunds_before = self._refund_count()
        res = self._change_plan(self.plan_base)
        self.assertTrue(res.get('success'), res.get('error'))
        new_sub = self.smart_home.sudo().subscription_id
        self.assertNotEqual(new_sub, self.old_sub)
        self.assertEqual(new_sub.sh_subscription_plan_id, self.plan_base)
        self.assertEqual(self._refund_count(), refunds_before + 1,
                         '放行後退點照常發生（證明前一條的「沒退點」是 gate 擋的）')

    def test_stuck_record_can_turn_protection_off_on_zero_seat_plan(self):
        """已經卡住的記錄（修正前換過去的）：0 席方案＋受保護 ⇒ 租戶仍關得掉。

        前端復原模式的「Turn protection off」全靠這一條：access-config 端點的關閉
        方向不看席位。若日後有人在端點加上「0 席不准碰 Access 設定」的 gate，
        卡死會從前端搬到後端，這裡會紅。
        """
        self._protect()
        self._block_country_policy()
        # 直接把訂閱掛到 0 席方案＝重演修正前那次「成功」的換方案
        self.old_sub.sudo().sh_subscription_plan_id = self.plan_base.id
        self.assertEqual(
            self.smart_home.sudo().subscription_id.sh_subscription_plan_id
            .ac_max_access_seats, 0, '前提：0 席方案')

        res = self._set_protected_as_tenant(False)
        self.assertTrue(res.get('success'), res)
        self.route.invalidate_recordset(['is_protected'])
        self.assertFalse(self.route.is_protected)

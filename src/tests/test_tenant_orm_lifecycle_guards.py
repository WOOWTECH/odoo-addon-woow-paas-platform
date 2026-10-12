# -*- coding: utf-8 -*-
"""租戶經 `/web/dataset/call_kw` 直接操作 SA／SH／工作區 model 的 ORM 層守衛。

## 背景

租戶帳號是 `base.group_user`，可以直接打 `/web/dataset/call_kw`，只受 ACL 與
ir.rule 約束，**不經過** /woow controller 的角色閘、訂閱閘
（`_check_operation_allowed`）與輸入驗證。本檔守的是審查中確認「可利用、且有
實際後果」的五格：

| 系列 | 缺口 | 守線 |
| --- | --- | --- |
| A | owner／admin 裸 `create` 一筆無訂閱的 SA，再打 /woow provision ＝ 免費 tunnel | `SecurityAccess.create` 建立閘（同 #524） |
| B | owner／admin 清空或改指 SA／SH 的 `subscription_id`（逃出停權 cron、`_is_subscription_active()` 對無訂閱放行） | mixin `_guard_tenant_lifecycle_write` |
| C | 把 `suspended` 的 SA／SH 改回對外服務的 state，或對停權中的 SA 打 `action_provision` 重推真 service_url | mixin 停權逃逸守衛 ＋ `SecurityAccess.action_provision` 前置拒絕 |
| D | owner 對 pending SH 直打 `action_provision`：`create_tunnel` 先成功、`tunnel_token` 欄位遮罩後炸、交易回捲 ⇒ 孤兒 tunnel | `SmartHome.action_provision` 把遮罩檢查提前到外部副作用之前 |
| E | workspace **admin** 經 ORM 封存／還原 owner 的工作區（產品面封存是 owner-only、無還原端點） | `Workspace._check_state_change_by_owner` |

## 紀律（照抄 #524／#658／#805）

- **一律 HttpCase ＋ 真租戶 session ＋ 真 HTTP**：上面五道守衛都豁免 su／
  `base.group_system`，`TransactionCase`（su env）在這裡結構上沒有鑑別力。
- fixture user 只掛 `base.group_user`（setUp 有前提斷言）。
- 每個否定斷言都**歸因**到它那一道守衛（例外型別名／訊息片段），並配一格
  「同一條正常產品路徑仍然通」的陽性對照——否則「擋住了」與「探針壞了」在輸出
  上長得一樣。
- 每次 RPC 後 `invalidate_all()`；狀態一律用 raw SQL 讀。
- operator 一律 mock（受 spec 約束的 `operator_client()`），不對外送。
"""
import json
import uuid
from unittest.mock import patch
from .publication_receipt_fixture import (
    install_publication_receipts,
    routes_receipt,
)

from odoo.tests import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _sa_internal_identity,
)
from odoo.addons.woow_paas_platform.models.smart_home import (
    _internal_identity as _sh_internal_identity,
)
from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_USER,
)
from .operator_contract import operator_client

_WS = 'woow_paas_platform.workspace'
_SA = 'woow_paas_platform.security_access'
_SH = 'woow_paas_platform.smart_home'

# SA 的 provision／route sync 用 models/security_access.py 頂部 import 的那份
# copy，controller 的可用性檢查走 services 那份——兩個都要蓋（見
# docs/decisions/patterns/unittest-mock-dual-namespace-patch.md）。
_PATCH_TARGETS = (
    'odoo.addons.woow_paas_platform.models.security_access.get_paas_operator_client',
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client',
    'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client',
)


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    login = '%s_%s' % (name, uuid.uuid4().hex[:8])
    user = env['res.users'].sudo().create({
        'name': '%s tog' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


class _PatchedOperator:
    """把三個 namespace 的 `get_paas_operator_client` 一起換掉。"""

    def __init__(self, client):
        self._patches = [patch(t, return_value=client) for t in _PATCH_TARGETS]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


@tagged('post_install', '-at_install')
class _TenantOrmFixture(HttpCase):
    """W1（alice owner、dave admin、carol user、gary guest）＋ W2（bob owner）。"""

    def setUp(self):
        super().setUp()
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(
            self.env.company.sudo().sh_journal_id,
            'fixture 前提：公司要有 sh_journal_id，否則下單開不出訂閱')

        self.alice, self.alice_login = _new_group_user(self.env, 'alice')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob')
        for u in (self.alice, self.dave, self.carol, self.gary, self.bob):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        Workspace = self.env[_WS]
        self.w1 = Workspace.with_user(self.alice).create({'name': 'W1 tog'})
        self.w2 = Workspace.with_user(self.bob).create({'name': 'W2 tog'})
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.env['woow_paas_platform.workspace_access'].sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})
        self.assertEqual(
            sorted(self.w1.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]),
            'fixture 前提：W1 要同時掛 owner/admin/user/guest 四列')

        self.sa_plan = self._make_plan('SA tog Plan', sa_max_routes=3)
        self.sa_template = self.env[
            'woow_paas_platform.security_access_template'].sudo().create({
                'name': 'SA tog',
                'slug': 'sa-tog-%s' % uuid.uuid4().hex[:6],
                'subscription_plan_ids': [(6, 0, [self.sa_plan.id])],
            })
        self.sh_plan = self._make_plan('SH tog Plan')
        self.sh_template = self.env[
            'woow_paas_platform.smart_home_template'].sudo().create({
                'name': 'HA tog',
                'slug': 'ha-tog-%s' % uuid.uuid4().hex[:6],
                'subscription_plan_ids': [(6, 0, [self.sh_plan.id])],
            })

    def _make_plan(self, name, **extra):
        plan = self.env['sh.subscription.plan'].sudo().create(dict({
            'name': '%s %s' % (name, uuid.uuid4().hex[:4]),
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        }, **extra))
        self.env['product.template'].sudo().create({
            'name': '%s Product' % plan.name,
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id,
            'list_price': 100.0,
        })
        return plan

    # ------------------------------------------------------------------ #
    #  RPC helpers                                                        #
    # ------------------------------------------------------------------ #
    def _post_json(self, url, payload):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': payload})
        resp = self.url_open(url, data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _call_kw(self, login, model, method, args, kwargs=None):
        """以 `login` 的真 session 打 `/web/dataset/call_kw`。"""
        self.authenticate(login, login)
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        })
        resp = self.url_open('/web/dataset/call_kw', data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_denied_by(self, result, needles, msg):
        """否定斷言＋歸因：每個 needle 都要出現在錯誤的型別名／訊息裡。"""
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        for needle in needles:
            self.assertIn(needle, text,
                          msg + '（歸因不符：期望含 %r，回 %r）' % (needle, text))
        return text

    def _assert_rule_denied(self, result, msg):
        """被 ACL／ir.rule 擋下，**不是**本檔新增的任何一道守衛。"""
        text = self._assert_denied_by(result, ['AccessError'], msg)
        for needle in ('OrderGateError', 'EntitySubscriptionBindingError',
                       'LifecycleHaltEscapeError',
                       'Only the workspace owner can archive'):
            self.assertNotIn(needle, text,
                             msg + '（應由 ir.rule 擋，卻是新守衛：%r）' % text)

    def _ok(self, result, msg):
        self.assertNotIn('error', result,
                         msg + '（探針故障或被擋：%s）' % self._error_text(result))
        return result.get('result')

    def _db(self, model, record_id, field_name):
        self.env.flush_all()
        table = self.env[model]._table
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s' % (field_name, table),
            (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, '%s#%s 不在 DB 裡' % (model, record_id))
        return row[0]

    # ------------------------------------------------------------------ #
    #  operator mock                                                      #
    # ------------------------------------------------------------------ #
    def _client(self):
        client = install_publication_receipts(operator_client())
        client.create_tunnel.return_value = {
            'tunnel_id': 'tun-tog-%s' % uuid.uuid4().hex[:8],
            'tunnel_token': 'tok-tog-%s' % uuid.uuid4().hex[:12],
            'tunnel_name': 'name-tog-%s' % uuid.uuid4().hex[:6],
        }
        client.sync_tunnel_routes.side_effect = routes_receipt
        client.check_route_availability.return_value = {
            'status': 'free', 'available': True}
        client.get_tunnel_status.return_value = {'connections': []}
        return client

    # ------------------------------------------------------------------ #
    #  正常下單流程（也是 B 系列的陽性對照：租戶身分的綁定寫入必須通）       #
    # ------------------------------------------------------------------ #
    def _order_sa(self, login, workspace, name):
        self.authenticate(login, login)
        res = self._post_json(
            '/api/workspaces/%d/security-accesses' % workspace.id,
            {'action': 'create', 'name': name,
             'plan_id': self.sa_plan.id, 'template_id': self.sa_template.id})
        payload = self._ok(res, 'SA 下單端點必須可達')
        self.assertTrue(payload.get('success'),
                        '陽性對照：SA 正常下單必須成功（回 %r）' % payload)
        sa = self.env[_SA].sudo().browse(payload['data']['id'])
        self.assertTrue(sa.subscription_id, '前提：下單必須綁上訂閱')
        self.assertEqual(sa.subscription_id.state, 'in_progress')
        return sa

    def _order_sh(self, login, workspace, name):
        self.authenticate(login, login)
        res = self._post_json(
            '/api/workspaces/%d/smarthomes' % workspace.id,
            {'action': 'create', 'name': name,
             'plan_id': self.sh_plan.id, 'template_id': self.sh_template.id})
        payload = self._ok(res, 'SH 下單端點必須可達')
        self.assertTrue(payload.get('success'),
                        '陽性對照：SH 正常下單必須成功（回 %r）' % payload)
        sh = self.env[_SH].sudo().browse(payload['data']['id'])
        self.assertTrue(sh.subscription_id, '前提：下單必須綁上訂閱')
        self.assertEqual(sh.state, 'pending')
        return sh

    def _suspend_sa_with_tunnel(self, sa):
        """平台身分把 SA 擺成「有 tunnel、已停權」——停權逃逸的前提。"""
        _sa_internal_identity(sa.sudo()).write({'tunnel_id': 'tun-tog-live'})
        sa.sudo().write({'state': 'suspended', 'pre_suspend_state': 'active'})
        self.assertEqual(self._db(_SA, sa.id, 'state'), 'suspended')


@tagged('post_install', '-at_install')
class TestSaCreateGate(_TenantOrmFixture):
    """A 系列：SA 只能從平台下單流程建立。"""

    def _assert_gate(self, result, msg):
        self._assert_denied_by(
            result, ['SecurityAccessOrderGateError', 'ordering flow'], msg)

    def test_A1_owner_cannot_create_sa_over_call_kw(self):
        before = self.env[_SA].sudo().search_count([])
        self._assert_gate(
            self._call_kw(self.alice_login, _SA, 'create',
                          [{'name': 'free sa', 'workspace_id': self.w1.id}]),
            'owner 不得裸 create 一筆無訂閱的 SA')
        self.assertEqual(self.env[_SA].sudo().search_count([]), before,
                         '被擋之後不得有任何 SA 落地')

    def test_A2_admin_cannot_create_sa_over_call_kw(self):
        self._assert_gate(
            self._call_kw(self.dave_login, _SA, 'create',
                          [{'name': 'free sa', 'workspace_id': self.w1.id}]),
            'admin 一樣建不出來（不是角色閘）')

    def test_A3_guest_cannot_create_sa_over_call_kw(self):
        self.assertIn('error', self._call_kw(
            self.gary_login, _SA, 'create',
            [{'name': 'guest sa', 'workspace_id': self.w1.id}]),
            'guest 不得 create SA')
        self.assertFalse(
            self.env[_SA].sudo().search([('name', '=', 'guest sa')]))

    def test_A4_forged_context_key_does_not_open_the_gate(self):
        """`call_kw` 換得掉整包 context；JSON 表達不出模組私有 sentinel。"""
        for forged in (True, 1, 'true', [1], {'x': 1}):
            with self.subTest(forged=forged):
                self._assert_gate(
                    self._call_kw(
                        self.alice_login, _SA, 'create',
                        [{'name': 'forged', 'workspace_id': self.w1.id}],
                        {'context': {
                            'woow_security_access_platform_order': forged}}),
                    '偽造的 context key 打不開建立閘')

    def test_A5_owner_and_admin_still_order_via_woow(self):
        """陽性對照：/woow 下單（租戶身分 create ＋ 租戶身分綁訂閱）照常成功。"""
        self._order_sa(self.alice_login, self.w1, 'ordered by owner')
        self._order_sa(self.dave_login, self.w1, 'ordered by admin')


@tagged('post_install', '-at_install')
class TestSubscriptionBindingGuard(_TenantOrmFixture):
    """B 系列：`subscription_id` 不得被租戶清空或改指。"""

    def _assert_binding(self, result, msg):
        self._assert_denied_by(result, ['EntitySubscriptionBindingError'], msg)

    def test_B1_owner_cannot_unbind_sa_subscription(self):
        sa = self._order_sa(self.alice_login, self.w1, 'unbind me')
        sub_id = sa.subscription_id.id
        self._assert_binding(
            self._call_kw(self.alice_login, _SA, 'write',
                          [[sa.id], {'subscription_id': False}]),
            'owner 不得清空 SA 的訂閱（清空＝永久脫離停權 cron）')
        self.assertEqual(self._db(_SA, sa.id, 'subscription_id'), sub_id)

    def test_B2_admin_cannot_rebind_to_a_subscription_already_in_use(self):
        sa1 = self._order_sa(self.alice_login, self.w1, 'payer')
        sa2 = self._order_sa(self.alice_login, self.w1, 'freeloader')
        sa2_sub = sa2.subscription_id.id
        self._assert_binding(
            self._call_kw(self.dave_login, _SA, 'write',
                          [[sa2.id], {'subscription_id': sa1.subscription_id.id}]),
            '一筆訂閱不得同時養兩個 SA')
        self.assertEqual(self._db(_SA, sa2.id, 'subscription_id'), sa2_sub)

    def test_B3_owner_cannot_rebind_to_another_tenants_subscription(self):
        victim = self._order_sa(self.bob_login, self.w2, 'victim')
        mine = self._order_sa(self.alice_login, self.w1, 'mine')
        mine_sub = mine.subscription_id.id
        self._assert_binding(
            self._call_kw(self.alice_login, _SA, 'write',
                          [[mine.id], {'subscription_id': victim.subscription_id.id}]),
            '不得綁別人的訂閱')
        self.assertEqual(self._db(_SA, mine.id, 'subscription_id'), mine_sub)

    def test_B4_owner_cannot_unbind_sh_subscription(self):
        sh = self._order_sh(self.alice_login, self.w1, 'sh unbind')
        sub_id = sh.subscription_id.id
        self._assert_binding(
            self._call_kw(self.alice_login, _SH, 'write',
                          [[sh.id], {'subscription_id': False}]),
            'owner 不得清空 SH 的訂閱')
        self.assertEqual(self._db(_SH, sh.id, 'subscription_id'), sub_id)

    def test_B5_guest_is_still_refused_by_the_record_rule(self):
        sa = self._order_sa(self.alice_login, self.w1, 'guest target')
        self._assert_rule_denied(
            self._call_kw(self.gary_login, _SA, 'write',
                          [[sa.id], {'subscription_id': False}]),
            'guest 由 ir.rule 擋（歸因不被新守衛搶走）')
        self.assertTrue(self._db(_SA, sa.id, 'subscription_id'))

    def test_B6_noop_write_of_the_same_subscription_is_allowed(self):
        """整批送出未修改欄位不得誤擋（比對新值 != 現值）。"""
        sa = self._order_sa(self.alice_login, self.w1, 'noop')
        self._ok(self._call_kw(self.alice_login, _SA, 'write',
                               [[sa.id], {'subscription_id': sa.subscription_id.id,
                                          'name': 'renamed'}]),
                 '同值 no-op write 必須通過')
        self.assertEqual(self._db(_SA, sa.id, 'name'), 'renamed')


@tagged('post_install', '-at_install')
class TestHaltEscapeGuard(_TenantOrmFixture):
    """C 系列：停權中的 SA／SH 不得被租戶帶回服務狀態。"""

    def test_C1_owner_cannot_flip_suspended_sa_back_to_active(self):
        sa = self._order_sa(self.alice_login, self.w1, 'suspended sa')
        self._suspend_sa_with_tunnel(sa)
        self._assert_denied_by(
            self._call_kw(self.alice_login, _SA, 'write',
                          [[sa.id], {'state': 'active'}]),
            ['LifecycleHaltEscapeError'],
            'owner 不得把 suspended 改回 active（之後任一 route 寫入就會推掉 503）')
        self.assertEqual(self._db(_SA, sa.id, 'state'), 'suspended')

    def test_C2_owner_cannot_resync_a_suspended_sa_over_call_kw(self):
        sa = self._order_sa(self.alice_login, self.w1, 'suspended resync')
        self._suspend_sa_with_tunnel(sa)
        client = self._client()
        with _PatchedOperator(client):
            res = self._call_kw(self.alice_login, _SA, 'action_provision',
                                [[sa.id]])
        self._assert_denied_by(res, ['UserError', 'suspended'],
                               '停權中的 SA 不得經 call_kw 重推 route')
        self.assertFalse(client.sync_tunnel_routes.called,
                         '被擋下就不得走到 operator')
        self.assertEqual(self._db(_SA, sa.id, 'state'), 'suspended')

    def test_C3_resync_is_refused_when_the_subscription_is_not_active(self):
        """`_do_suspend` 推 503 失敗會落 `error`：error ＋ 訂閱已停也不得重推。"""
        sa = self._order_sa(self.alice_login, self.w1, 'error + cancelled')
        _sa_internal_identity(sa.sudo()).write({'tunnel_id': 'tun-tog-err'})
        sa.sudo().write({'state': 'error'})
        sa.sudo().subscription_id.sudo().write({'state': 'cancel'})
        client = self._client()
        with _PatchedOperator(client):
            res = self._call_kw(self.alice_login, _SA, 'action_provision',
                                [[sa.id]])
        self._assert_denied_by(res, ['UserError', 'subscription is not active'],
                               '訂閱非 in_progress 時不得重推')
        self.assertFalse(client.sync_tunnel_routes.called)
        self.assertEqual(self._db(_SA, sa.id, 'state'), 'error')

    def test_C4_owner_cannot_flip_suspended_sh_back_into_service(self):
        sh = self._order_sh(self.alice_login, self.w1, 'suspended sh')
        _sh_internal_identity(sh.sudo()).write({'tunnel_id': 'tun-tog-sh'})
        sh.sudo().write({'state': 'suspended'})
        self._assert_denied_by(
            self._call_kw(self.alice_login, _SH, 'write',
                          [[sh.id], {'state': 'error'}]),
            ['LifecycleHaltEscapeError'],
            'owner 不得把 suspended 的 SH 改成 error（再打 provision 就重推 ingress）')
        self.assertEqual(self._db(_SH, sh.id, 'state'), 'suspended')

    def test_C5_guest_is_still_refused_by_the_record_rule(self):
        sa = self._order_sa(self.alice_login, self.w1, 'guest halt')
        self._suspend_sa_with_tunnel(sa)
        self._assert_rule_denied(
            self._call_kw(self.gary_login, _SA, 'write',
                          [[sa.id], {'state': 'active'}]),
            'guest 由 ir.rule 擋')

    def test_C6_platform_suspend_and_resume_are_not_blocked(self):
        """陽性對照：平台身分（cron 的 `_do_suspend`／`_do_resume`）照常運作。"""
        sa = self._order_sa(self.alice_login, self.w1, 'platform resume')
        _sa_internal_identity(sa.sudo()).write({'tunnel_id': 'tun-tog-ok'})
        client = self._client()
        with _PatchedOperator(client):
            sa.sudo()._do_suspend()
            self.assertEqual(self._db(_SA, sa.id, 'state'), 'suspended')
            sa.sudo()._do_resume()
        self.assertNotEqual(self._db(_SA, sa.id, 'state'), 'suspended',
                            '平台的復權不得被停權逃逸守衛誤擋')
        self.assertTrue(client.sync_tunnel_routes.called)

    def test_C7_owner_provision_via_woow_is_unaffected(self):
        """陽性對照：pending SA 經 /woow provision（平台身分）照常成功。"""
        sa = self._order_sa(self.alice_login, self.w1, 'provision ok')
        client = self._client()
        self.authenticate(self.alice_login, self.alice_login)
        with _PatchedOperator(client):
            res = self._post_json(
                '/api/workspaces/%d/security-accesses/%d' % (self.w1.id, sa.id),
                {'action': 'provision'})
        payload = self._ok(res, 'provision 端點必須可達')
        self.assertTrue(payload.get('success'), payload)
        self.assertTrue(client.create_tunnel.called)


@tagged('post_install', '-at_install')
class TestSmartHomeProvisionOrphanGuard(_TenantOrmFixture):
    """D 系列：直打 `action_provision` 不得先在 Cloudflare 開出 tunnel 再炸。"""

    def test_D1_owner_call_kw_provision_fails_before_create_tunnel(self):
        sh = self._order_sh(self.alice_login, self.w1, 'orphan bait')
        client = self._client()
        with _PatchedOperator(client):
            res = self._call_kw(self.alice_login, _SH, 'action_provision',
                                [[sh.id]])
        self._assert_denied_by(res, ['AccessError', 'tunnel_token'],
                               '非平台身分直打 provision 必須被欄位遮罩擋下')
        self.assertFalse(
            client.create_tunnel.called,
            '🔴 本條核心：擋點必須在 create_tunnel 之前（修前這裡已經開出一條'
            '沒有 DB 參照、在計費的 tunnel）')
        self.assertEqual(self._db(_SH, sh.id, 'state'), 'pending')

    def test_D2_guest_call_kw_provision_never_reaches_the_operator(self):
        sh = self._order_sh(self.alice_login, self.w1, 'guest bait')
        client = self._client()
        with _PatchedOperator(client):
            res = self._call_kw(self.gary_login, _SH, 'action_provision',
                                [[sh.id]])
        self.assertIn('error', res, 'guest 不得 provision')
        self.assertFalse(client.create_tunnel.called)

    def test_D3_owner_provision_via_woow_is_unaffected(self):
        """陽性對照：/woow 的平台身分 provision 照常成功、tunnel 錨點落地。"""
        sh = self._order_sh(self.alice_login, self.w1, 'woow provision')
        client = self._client()
        self.authenticate(self.alice_login, self.alice_login)
        with _PatchedOperator(client):
            res = self._post_json(
                '/api/workspaces/%d/smarthomes/%d' % (self.w1.id, sh.id),
                {'action': 'provision'})
        payload = self._ok(res, 'provision 端點必須可達')
        self.assertTrue(payload.get('success'), payload)
        self.assertTrue(client.create_tunnel.called)
        self.assertTrue(self._db(_SH, sh.id, 'tunnel_id'))


@tagged('post_install', '-at_install')
class TestWorkspaceStateOwnerOnly(_TenantOrmFixture):
    """E 系列：工作區封存／還原只有 owner。

    用一個**沒有任何服務**的工作區，與「有服務時拒絕封存」（#1295）的行為脫鉤。
    """

    _OWNER_ONLY = 'Only the workspace owner can archive'

    def test_E1_admin_cannot_archive_by_writing_state(self):
        self._assert_denied_by(
            self._call_kw(self.dave_login, _WS, 'write',
                          [[self.w1.id], {'state': 'archived'}]),
            ['AccessError', self._OWNER_ONLY],
            'admin 不得封存 owner 的工作區')
        self.assertEqual(self._db(_WS, self.w1.id, 'state'), 'active')

    def test_E2_admin_cannot_call_action_archive(self):
        self._assert_denied_by(
            self._call_kw(self.dave_login, _WS, 'action_archive', [[self.w1.id]]),
            ['AccessError', self._OWNER_ONLY],
            'admin 不得經 public method 封存')
        self.assertEqual(self._db(_WS, self.w1.id, 'state'), 'active')

    def test_E3_admin_cannot_call_action_restore(self):
        self.w1.sudo().write({'state': 'archived'})
        self._assert_denied_by(
            self._call_kw(self.dave_login, _WS, 'action_restore', [[self.w1.id]]),
            ['AccessError', self._OWNER_ONLY],
            'admin 不得經 public method 還原')
        self.assertEqual(self._db(_WS, self.w1.id, 'state'), 'archived')

    def test_E4_guest_and_user_are_still_refused_by_the_record_rule(self):
        self.w1.sudo().write({'state': 'archived'})
        self._assert_rule_denied(
            self._call_kw(self.gary_login, _WS, 'action_restore', [[self.w1.id]]),
            'guest 還原由 ir.rule 擋')
        self.w1.sudo().write({'state': 'active'})
        self._assert_rule_denied(
            self._call_kw(self.carol_login, _WS, 'action_archive', [[self.w1.id]]),
            'role=user 封存由 ir.rule 擋')
        self.assertEqual(self._db(_WS, self.w1.id, 'state'), 'active')

    def test_E5_owner_archives_via_woow(self):
        """陽性對照：owner 走產品端點封存照常成功。"""
        self.authenticate(self.alice_login, self.alice_login)
        payload = self._ok(self._post_json(
            '/api/workspaces/%d' % self.w1.id, {'action': 'delete'}),
            'owner 封存端點必須可達')
        self.assertTrue(payload.get('success'), payload)
        self.assertEqual(self._db(_WS, self.w1.id, 'state'), 'archived')

    def test_E6_admin_update_via_woow_is_unaffected(self):
        """陽性對照：admin 改名（/woow `_update_workspace`）不受 state 守衛影響。"""
        self.authenticate(self.dave_login, self.dave_login)
        payload = self._ok(self._post_json(
            '/api/workspaces/%d' % self.w1.id,
            {'action': 'update', 'name': 'renamed by admin'}),
            'admin 更新端點必須可達')
        self.assertTrue(payload.get('success'), payload)
        self.assertEqual(self._db(_WS, self.w1.id, 'name'), 'renamed by admin')

    def test_E7_owner_may_restore_own_workspace(self):
        """刻意放行（產品決策另議）：owner 還原自己封存的工作區沒有跨租戶或計費後果。"""
        self.w1.sudo().write({'state': 'archived'})
        self._ok(self._call_kw(self.alice_login, _WS, 'action_restore',
                               [[self.w1.id]]),
                 'owner 還原目前刻意放行')
        self.assertEqual(self._db(_WS, self.w1.id, 'state'), 'active')

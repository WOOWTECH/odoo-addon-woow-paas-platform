# -*- coding: utf-8 -*-
"""#1294：租戶工作區 owner／admin 要能刪除自己的 Smart Home（與 SA 一致）。

## 缺陷（2026-10-01 正式站實測，18.0.1.0.193）

只掛 `base.group_user` 的工作區 owner 在 `/woow` 刪 SH，端點回
「Failed to delete smart home」，log 是
`Access Denied by ACLs for operation: unlink ... model: woow_paas_platform.smart_home`
（`_delete_smarthome` → `action_delete` → `_assert_unlink_authorized`）。

根因兩層：

1. `access_smart_home_user` 是 `1,1,1,0`（unlink=0）；SA 的是 `1,1,1,1`。
2. #740 的前置檢查以呼叫者身分**同時**驗 route 子表的 unlink 權，而 route 的 ACL
   對 `base.group_user` 是 `unlink=0`（本階段沒有 route CRUD，
   `test_sh_access_acl_768.py` R7 釘住）。

另外一格會在 1／2 修好之後**立刻變成可達**（#859）：`_cleanup_tunnel()` 清錨點那次
write 含 `tunnel_token`（`groups='base.group_system'`），以租戶身分寫會撞欄位 ACL、
被吞成「保留待重試」⇒ tunnel 已經被刪掉，`unlink()` 卻 fail-loud 回
「the tunnel is still live and billed」。

## 修法

- ACL：`access_smart_home_user` → `1,1,1,1`（不動 portal、不動 route ACL）。
- `SmartHome.unlink()`：route 的連帶刪除改以平台身分（`record.sudo().route_ids`）；
  前置檢查只驗主表（「能不能刪這台 SH」）。
- `_cleanup_tunnel()`：清錨點改以平台身分寫（仍需 `_internal_identity` 開閘）。
- record rule **不動**：`smart_home_rule_owner_admin_manage` 是 smart_home 上唯一
  `perm_unlink=True` 的 rule ⇒ unlink 範圍＝自己工作區的 owner/admin。

## 本檔的矩陣（全部走真 HTTP；operator 一律 mock）

| 系列 | 身分 | 入口 | 期望 |
| --- | --- | --- | --- |
| A | owner／admin | 刪除端點、`call_kw unlink` | 成功：列消失、tunnel／Access 照序拆 |
| B | W1 的 user／guest | 刪除端點、`call_kw unlink` | 被拒、零外部呼叫、列原封不動 |
| C | W2 owner（跨工作區） | 刪除端點（兩種路徑形狀）、`call_kw unlink`／`action_delete` | 同上；附陽性對照（他刪得掉自己的） |
| D | owner | 刪除端點 ＋ Cloudflare 失敗 | fail-loud、列與重試錨點保住 |
| E | owner | `call_kw unlink` primary route | 仍刪不掉（route 沒有單獨刪除入口） |

被拒那一側「失敗得乾淨」的強斷言（逐一列名 method、savepoint 沒進、注入紅燈）在
`test_sh_unlink_authz_preflight_740.py`，本檔不重複。
"""
from odoo.tests import tagged

from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorError,
)
from .test_sh_unlink_authz_preflight_740 import _Sh740Fixture

_MODEL = 'woow_paas_platform.smart_home'
_ROUTE_MODEL = 'woow_paas_platform.smart_home_route'


class _Sh1294Fixture(_Sh740Fixture):
    """沿用 #740 的三形態 builder（A 無 route／B 有 route／C 有 Access app）。

    ⚠ 本類別**不放任何 `test_*`**：Odoo loader 只收具體類別 `__dict__` 裡的方法
    （#533），寫在這裡的測試一條都不會被收集。
    """

    def _endpoint_delete(self, login, workspace, home, client=None):
        """以 ``login`` 走真 HTTP 刪除端點。回 (client, 回應 payload, before, after)。"""
        client = client or self._client()
        before = self._db(home.id)
        self.authenticate(login, login)
        with self._patched_operator(client):
            res = self._post_json(
                '/api/workspaces/%d/smarthomes/%d' % (workspace.id, home.id),
                {'action': 'delete'})
        after = self._db(home.id)
        self._report('ENDPOINT %s ws=%s sh=%s' % (login, workspace.id, home.id), {
            'response': res,
            'external_calls_in_order': self._external_calls(client),
            'db_before': before, 'db_after': after,
        })
        return client, res, before, after

    def _rpc(self, login, model, method, ids, client=None):
        client = client or self._client()
        self.authenticate(login, login)
        with self._patched_operator(client):
            res = self._call_kw(model, method, [ids])
        return client, res

    def _assert_endpoint_success(self, res, tag):
        payload = res.get('result', res)
        self.assertNotIn('error', res, '%s：端點回 JSON-RPC error %r' % (tag, res))
        self.assertTrue(
            isinstance(payload, dict) and payload.get('success'),
            '%s：租戶 owner/admin 的刪除必須成功（#1294），回 %r' % (tag, payload))

    def _assert_endpoint_refused(self, res, tag):
        """端點拒絕有兩種合法形狀：`success: False`，或 JSON-RPC error（AccessError）。"""
        if 'error' in res:
            self.assertIn('AccessError', self._error_text(res),
                          '%s：端點回的 error 不是授權類，回 %r' % (tag, res))
            return
        payload = res.get('result', res)
        self.assertIsInstance(payload, dict, '%s：回應不是物件 %r' % (tag, res))
        self.assertFalse(payload.get('success'),
                         '%s：這個身分的刪除必須被拒，回 %r' % (tag, payload))

    def _assert_deleted(self, after, tag):
        self.assertEqual(after['smart_home'], [],
                         '%s：列必須真的被刪掉（裸 SQL 還查得到 %r）'
                         % (tag, after['smart_home']))
        self.assertEqual(after['routes'], [],
                         '%s：route 必須跟著被刪掉（還在 %r）' % (tag, after['routes']))

    def _assert_untouched(self, client, before, after, tag):
        self._assert_no_external_call(client, tag)
        self._assert_row_untouched(before, after, tag)


# ---------------------------------------------------------------------- #
#  A 系列：owner／admin 刪得掉自己的 SH                                    #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh1294OwnerAdminCanDelete(_Sh1294Fixture):
    """修復前四條全紅：前三條端點回「Failed to delete smart home」、A4 回 AccessError。"""

    def test_A1_owner_deletes_sh_without_route_via_endpoint(self):
        """形態 A。這條同時守 #859 的可達格：清錨點若仍以租戶身分寫，會撞
        `tunnel_token` 欄位 ACL ⇒ fail-loud 回「still live and billed」而列留著。"""
        home = self._mk_no_route('1294 A1')
        tunnel_id = home.sudo().tunnel_id
        tunnel_name = home.sudo().tunnel_name
        client, res, _b, after = self._endpoint_delete(
            self.alice_login, self.w1, home)
        self._assert_endpoint_success(res, 'A1')
        self._assert_deleted(after, 'A1')
        client.delete_tunnel.assert_called_once_with(
            tunnel_id, tunnel_name=tunnel_name)

    def test_A2_owner_deletes_protected_sh_via_endpoint(self):
        """形態 C：Access app 要在 tunnel **之前**拆，且帶的是這一列的 app id。"""
        home = self._mk_with_route('1294 A2', protected=True)
        app_id = home.sudo().route_ids.access_app_id
        self.assertTrue(app_id, 'A2 前提：route 上要有 app id')
        client, res, _b, after = self._endpoint_delete(
            self.alice_login, self.w1, home)
        self._assert_endpoint_success(res, 'A2')
        self._assert_deleted(after, 'A2')
        ordered = [c.split('(', 1)[0] for c in self._external_calls(client)]
        self.assertEqual(
            ordered, ['sync_access_app', 'delete_tunnel'],
            'A2：Access teardown 與 tunnel 刪除都要送出，且 Access 在前（實際 %r）'
            % (ordered,))
        payload = client.sync_access_app.call_args.args[0]
        self.assertIs(payload['enabled'], False, 'A2：送出去的必須是 teardown')
        self.assertEqual(payload['existing_app_id'], app_id,
                         'A2：拆的必須是這一列上的那顆 app')

    def test_A3_admin_deletes_sh_via_endpoint(self):
        """role=admin（dave）與 owner 同權——controller gate 與 rule 都是 owner/admin。"""
        home = self._mk_with_route('1294 A3', protected=False)
        tunnel_id = home.sudo().tunnel_id
        client, res, _b, after = self._endpoint_delete(
            self.dave_login, self.w1, home)
        self._assert_endpoint_success(res, 'A3')
        self._assert_deleted(after, 'A3')
        self.assertEqual(
            [c.args[0] for c in client.delete_tunnel.call_args_list],
            [tunnel_id], 'A3：tunnel 必須照常被拆')

    def test_A4_owner_rpc_unlink_is_allowed_at_the_orm_layer(self):
        """ORM 層的陽性對照：B／C 系列的 RPC AccessError 不是「租戶對這張表什麼都
        刪不了」，而是 record rule 的範圍。"""
        home = self._mk_with_route('1294 A4', protected=False)
        home_id = home.id
        client, res = self._rpc(self.alice_login, _MODEL, 'unlink', [home_id])
        self._assert_ok(res, 'A4：owner 以 RPC unlink 自己的 SH 必須成功')
        self._assert_deleted(self._db(home_id), 'A4')
        self.assertTrue(client.delete_tunnel.called,
                        'A4：tunnel 必須照常被拆（沒拆＝這條路徑沒真的跑）')


# ---------------------------------------------------------------------- #
#  B 系列：同工作區的 user／guest 仍被拒                                    #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh1294UserGuestStillDenied(_Sh1294Fixture):
    """陽性對照在 A 系列（同一個 W1、同一種形態，owner/admin 刪得掉）。"""

    def test_B1_user_and_guest_are_refused_by_the_endpoint(self):
        home = self._mk_with_route('1294 B1', protected=True)
        for login, label in ((self.carol_login, 'role=user'),
                             (self.gary_login, 'role=guest')):
            client, res, before, after = self._endpoint_delete(
                login, self.w1, home)
            payload = res.get('result', res)
            self.assertEqual(
                payload, {'success': False, 'error': 'Insufficient permissions'},
                'B1 %s：端點必須以 role gate 拒絕（回 %r）' % (label, res))
            self._assert_untouched(client, before, after, 'B1 %s' % label)

    def test_B2_user_and_guest_rpc_unlink_is_denied_by_the_record_rule(self):
        """繞過端點直打 ORM：擋下來的是 `smart_home_rule_owner_admin_manage`
        （唯一 `perm_unlink=True` 的 rule）。member_read 那條 perm_unlink=False，
        不得把刪除放寬到一般成員。"""
        home = self._mk_with_route('1294 B2', protected=True)
        for login, label in ((self.carol_login, 'role=user'),
                             (self.gary_login, 'role=guest')):
            before = self._db(home.id)
            client, res = self._rpc(login, _MODEL, 'unlink', [home.id])
            self._assert_smart_home_rule_denied(
                res, 'B2 %s：RPC unlink 必須被 record rule 擋下' % label)
            self._assert_untouched(client, before, self._db(home.id),
                                   'B2 %s' % label)


# ---------------------------------------------------------------------- #
#  C 系列：跨工作區一律被拒                                                 #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh1294CrossWorkspaceDenied(_Sh1294Fixture):
    """bob 是 W2 的 owner（租戶身分的上限），不是 W1 的成員。"""

    def test_C1_cross_workspace_endpoint_is_refused_both_shapes(self):
        home = self._mk_with_route('1294 C1', protected=True)
        # 形狀 1：URL 寫 W1（bob 不是成員）
        client, res, before, after = self._endpoint_delete(
            self.bob_login, self.w1, home)
        self._assert_endpoint_refused(res, 'C1/W1-url')
        self._assert_untouched(client, before, after, 'C1/W1-url')
        # 形狀 2：URL 寫 bob 自己的 W2、id 帶 W1 的 SH
        client, res, before, after = self._endpoint_delete(
            self.bob_login, self.w2, home)
        self._assert_endpoint_refused(res, 'C1/W2-url')
        self._assert_untouched(client, before, after, 'C1/W2-url')

    def test_C2_cross_workspace_rpc_is_denied_at_the_orm_layer(self):
        home = self._mk_with_route('1294 C2', protected=True)
        for method in ('unlink', 'action_delete'):
            before = self._db(home.id)
            client, res = self._rpc(self.bob_login, _MODEL, method, [home.id])
            self._assert_smart_home_rule_denied(
                res, 'C2 %s：跨工作區 RPC 必須被 record rule 擋下' % method)
            self._assert_untouched(client, before, self._db(home.id),
                                   'C2 %s' % method)

    def test_C3_positive_control_bob_can_delete_his_own_sh(self):
        """少了它，C1／C2 的「被拒」與「bob 這個帳號什麼都刪不了」不可區分。"""
        client, res, _b, after = self._endpoint_delete(
            self.bob_login, self.w2, self.sh2)
        self._assert_endpoint_success(res, 'C3')
        self._assert_deleted(after, 'C3')
        called = [c.args[0] for c in client.delete_tunnel.call_args_list]
        self.assertEqual(called, [self.b_tunnel_uuid],
                         'C3：拆的必須是 bob 自己那顆 tunnel（實收 %r）' % (called,))


# ---------------------------------------------------------------------- #
#  D 系列：Cloudflare 清理失敗 ⇒ 記錄保留（既有 fail-loud 守線）            #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh1294FailLoudKeepsTheRecord(_Sh1294Fixture):
    """以前只有提權身分走得到這兩格（`test_smart_home.py`、740 E1）；#1294 之後
    租戶 owner 也走得到，必須同樣保住列與重試錨點。"""

    def test_D1_tunnel_delete_failure_keeps_record_and_anchor(self):
        home = self._mk_no_route('1294 D1')
        tunnel_id = home.sudo().tunnel_id
        client = self._client()
        client.delete_tunnel.side_effect = PaaSOperatorError(
            message='upstream 503')
        client, res, _b, after = self._endpoint_delete(
            self.alice_login, self.w1, home, client=client)
        payload = res.get('result', res)
        self.assertFalse(payload.get('success'),
                         'D1：tunnel 拆不掉時刪除必須失敗（回 %r）' % (payload,))
        self.assertIn('Cloudflare cleanup failed', payload.get('error') or '',
                      'D1：必須是 fail-loud 那句話（回 %r）' % (payload,))
        self.assertTrue(after['smart_home'], 'D1：列必須保住當重試錨點')
        self.assertEqual(
            [r['tunnel_id'] for r in after['smart_home']], [tunnel_id],
            'D1：`tunnel_id` 必須保住當重試錨點')

    def test_D2_access_teardown_failure_keeps_record_route_and_app(self):
        home = self._mk_with_route('1294 D2', protected=True)
        app_id = home.sudo().route_ids.access_app_id
        tunnel_id = home.sudo().tunnel_id
        client = self._client()
        client.sync_access_app.side_effect = PaaSOperatorError(
            message='cf access 503')
        client, res, _b, after = self._endpoint_delete(
            self.alice_login, self.w1, home, client=client)
        payload = res.get('result', res)
        self.assertFalse(payload.get('success'),
                         'D2：Access 拆不掉時刪除必須失敗（回 %r）' % (payload,))
        self.assertIn('Cloudflare Access teardown failed',
                      payload.get('error') or '',
                      'D2：必須是 Access fail-loud 那句話（回 %r）' % (payload,))
        self.assertFalse(client.delete_tunnel.called,
                         'D2：Access 拆不掉時不得接著刪 tunnel')
        self.assertEqual(
            [r['tunnel_id'] for r in after['smart_home']], [tunnel_id],
            'D2：列與 `tunnel_id` 必須保住')
        self.assertEqual(
            [r['access_app_id'] for r in after['routes']], [app_id],
            'D2：route 與它的 `access_app_id` 必須保住當重試錨點')


# ---------------------------------------------------------------------- #
#  E 系列：route 仍然沒有單獨的刪除入口                                     #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh1294RouteStillNotTenantDeletable(_Sh1294Fixture):
    """#1294 刻意**不**放寬 route ACL（維持 `unlink=0`）：SH 刪除時 route 由平台身分
    連帶刪除。非 primary 那一格（ACL 本身）由 `test_sh_access_acl_768.py` R7 守。"""

    def test_E1_owner_cannot_rpc_unlink_the_primary_route(self):
        home = self._mk_with_route('1294 E1', protected=False)
        route = home.sudo().route_ids
        client, res = self._rpc(self.alice_login, _ROUTE_MODEL, 'unlink',
                                route.ids)
        self.assertIn('error', res,
                      'E1：owner 不得以 RPC 單獨刪掉 primary route（回 %r）' % (res,))
        self._assert_no_external_call(client, 'E1')
        self.assertEqual(
            [r['id'] for r in self._db(home.id)['routes']], route.ids,
            'E1：被擋之後 route 必須還在')

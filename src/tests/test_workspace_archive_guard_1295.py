# -*- coding: utf-8 -*-
"""#1295：工作區底下還有未刪除的服務時，封存（租戶的「刪除工作區」）必須被拒。

## 問題

`POST /api/workspaces/<id> {"action": "delete"}` → `_delete_workspace` →
`workspace.action_archive()` → `write({'state': 'archived'})`，中間不檢查也不清理
子資源。2026-10-01 正式站實測：工作區內有試用中的 Smart Home 照樣封存成功，SH／
tunnel／DNS／Access app 與 `in_progress` 訂閱全部留著繼續扣點；封存後所有租戶端點
都要求 workspace `state='active'`，也沒有還原端點 ⇒ 租戶再也刪不掉它們。

## 修法（守衛住在 `Workspace.write()`，見 `_check_archivable`）

只要底下還有任何未刪除的 SA／SH／cloud_service（三者「已刪除」＝記錄已 unlink 或
`state='deleting'`），封存一律拒絕：model 層丟 `WorkspaceHasServicesError`，
controller 轉成 `{success: False, code: 'workspace_has_services', data: {services}}`。

## 測試紀律（照抄 #556／#658）

- **走真 HTTP**（產品端點與 `/web/dataset/call_kw`）＋只掛 `base.group_user` 的真實
  租戶帳號。守衛刻意**不**豁免 superuser，`test_G6_*` 另外直接在 su env 驗這一點。
- 否定斷言配陽性對照（空工作區／服務刪除後都能封存）。
- 「被擋之後 DB 長怎樣」的斷言不包在 `assertRaises` 裡（flushing savepoint 回捲會
  假綠，見 `docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md`）——
  HTTP 路徑本來就不經 assertRaises；su 路徑用裸 try/except ＋ `self.fail()`。
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN,
    ROLE_USER,
)
from ..models.cloud_service import TERMINAL_STATES, _internal_provision
from ..models.workspace import (
    ERR_WORKSPACE_HAS_SERVICES,
    WorkspaceHasServicesError,
    _ARCHIVE_GUARD_SERVICE_MODELS,
)
from .smart_home_fixture import sh_model

# `WorkspaceHasServicesError` 經 JSON-RPC 冒出來時 `error.data.name` 的特徵字串。
GUARD_ERROR_NEEDLE = 'WorkspaceHasServicesError'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    login = '%s_%s' % (name, uuid.uuid4().hex[:8])
    user = env['res.users'].sudo().create({
        'name': '%s 1295' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class TestWorkspaceArchiveGuard1295(HttpCase):
    """W（alice owner、dave admin、carol user）＋ 三種服務的建立 helper。"""

    def setUp(self):
        super().setUp()
        self.alice, self.alice_login = _new_group_user(self.env, 'alice1295')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave1295')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol1295')
        for u in (self.alice, self.dave, self.carol):
            self.assertFalse(u.has_group('base.group_system'),
                             'fixture 前提：租戶只能有 base.group_user')

        self.ws = self.env['woow_paas_platform.workspace'].with_user(
            self.alice).create({'name': 'WS 1295 %s' % uuid.uuid4().hex[:6]})
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER)):
            Access.create({'workspace_id': self.ws.id, 'user_id': user.id,
                           'role': role})
        self.assertEqual(self.ws.sudo().owner_id, self.alice)
        self.assertEqual(self.ws.sudo().state, 'active')

    # ------------------------------------------------------------------ #
    #  fixture helpers                                                    #
    # ------------------------------------------------------------------ #
    def _make_sa(self, state='active'):
        sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'SA 1295', 'workspace_id': self.ws.id,
        })
        sa.write({'state': state})
        return sa

    def _make_sh(self, state='active'):
        return sh_model(self.env).sudo().create({
            'name': 'SH 1295', 'workspace_id': self.ws.id, 'state': state,
        })

    def _make_cs(self, state='running'):
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Tmpl 1295', 'slug': 'tmpl-1295-%s' % uuid.uuid4().hex[:6],
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-1295', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        svc = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'CS 1295', 'workspace_id': self.ws.id,
                'template_id': template.id,
            })
        svc.write({'state': state})
        return svc

    # ------------------------------------------------------------------ #
    #  RPC helpers                                                        #
    # ------------------------------------------------------------------ #
    def _post_json(self, url, payload):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': payload})
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _delete_ws(self, login):
        self.authenticate(login, login)
        return self._post_json('/api/workspaces/%d' % self.ws.id,
                               {'action': 'delete'})

    def _call_kw(self, login, method, args):
        self.authenticate(login, login)
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': 'woow_paas_platform.workspace',
                       'method': method, 'args': args, 'kwargs': {}},
        })
        resp = self.url_open('/web/dataset/call_kw', data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _error_text(self, body):
        data = (body.get('error') or {}).get('data') or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_blocked_response(self, body, expected):
        """產品端點的結構化拒絕：`code` ＋ 服務清單（逐筆比對 type/id/name/state）。"""
        self.assertNotIn('error', body, '必須是結構化拒絕而不是 RPC 例外：%r' % body)
        result = body['result']
        self.assertIs(result.get('success'), False, result)
        self.assertEqual(result.get('code'), ERR_WORKSPACE_HAS_SERVICES, result)
        self.assertTrue(result.get('error'), '要有給人看的訊息：%r' % result)
        self.assertEqual(result.get('data', {}).get('services'), expected, result)
        self.assertEqual(self.ws.sudo().state, 'active', '被拒時 state 不得落地')

    @staticmethod
    def _svc(type_key, rec):
        return {'type': type_key, 'id': rec.id, 'name': rec.name,
                'state': rec.state}

    # ------------------------------------------------------------------ #
    #  陽性對照                                                           #
    # ------------------------------------------------------------------ #
    def test_G1_empty_workspace_archives(self):
        """🟢 空工作區：owner 走產品端點封存成功（既有行為不變）。"""
        body = self._delete_ws(self.alice_login)
        self.assertNotIn('error', body, body)
        self.assertTrue(body['result'].get('success'), body)
        self.assertEqual(self.ws.sudo().state, 'archived')

    # ------------------------------------------------------------------ #
    #  未刪除的服務擋下封存（產品端點）                                    #
    # ------------------------------------------------------------------ #
    def test_G2_active_smart_home_blocks_archive(self):
        """正式站實測那一條：試用中（active）的 Smart Home。"""
        sh = self._make_sh('active')
        body = self._delete_ws(self.alice_login)
        self._assert_blocked_response(body, [self._svc('smart_home', sh)])
        self.assertTrue(sh.exists(), 'SH 不得被動到')
        self.assertEqual(sh.state, 'active')

    def test_G3_security_access_blocks_archive(self):
        sa = self._make_sa('active')
        body = self._delete_ws(self.alice_login)
        self._assert_blocked_response(body, [self._svc('security_access', sa)])

    def test_G4_cloud_service_blocks_archive(self):
        """cloud_service（OpenDesign／pi-agent／omnigent／code-server…）同樣擋。"""
        cs = self._make_cs('running')
        body = self._delete_ws(self.alice_login)
        self._assert_blocked_response(body, [self._svc('cloud_service', cs)])

    def test_G5_error_and_suspended_states_still_count_as_undeleted(self):
        """`error`（cleanup 失敗、tunnel 仍活著）與 `suspended` 都不是「已刪除」；
        三種服務一起出現時清單依 SA → SH → CS 排列。"""
        sa = self._make_sa('error')
        sh = self._make_sh('suspended')
        cs = self._make_cs('error')
        body = self._delete_ws(self.alice_login)
        self._assert_blocked_response(body, [
            self._svc('security_access', sa),
            self._svc('smart_home', sh),
            self._svc('cloud_service', cs),
        ])

    # ------------------------------------------------------------------ #
    #  服務刪除之後可以封存                                               #
    # ------------------------------------------------------------------ #
    def test_G6_archive_allowed_after_services_deleted(self):
        """先被擋 → 刪掉服務（SH unlink；SA／CS 落 `deleting` 終態）→ 封存成功。"""
        sa = self._make_sa('active')
        sh = self._make_sh('active')
        cs = self._make_cs('running')
        body = self._delete_ws(self.alice_login)
        self.assertEqual(body['result'].get('code'), ERR_WORKSPACE_HAS_SERVICES)

        sh.sudo().unlink()  # 沒有 tunnel ⇒ teardown no-op，記錄離場
        sa.sudo().write({'state': 'deleting'})
        cs.sudo().write({'state': 'deleting'})

        body = self._delete_ws(self.alice_login)
        self.assertNotIn('error', body, body)
        self.assertTrue(body['result'].get('success'), body)
        self.assertEqual(self.ws.sudo().state, 'archived')

    # ------------------------------------------------------------------ #
    #  ORM 直呼也擋（守衛在 model 層）                                    #
    # ------------------------------------------------------------------ #
    def test_G7_owner_orm_action_archive_blocked(self):
        """owner 經 `/web/dataset/call_kw` 直接呼叫 `action_archive`。"""
        self._make_sh('active')
        body = self._call_kw(self.alice_login, 'action_archive', [[self.ws.id]])
        self.assertIn('error', body, '必須被擋：%r' % body)
        self.assertIn(GUARD_ERROR_NEEDLE, self._error_text(body), body)
        self.assertEqual(self.ws.sudo().state, 'active')

    def test_G8_admin_orm_write_state_blocked(self):
        """admin 經 ORM `write({'state': 'archived'})`——ir.rule 放行 admin 寫入
        （#658 P 系列），所以只守 `action_archive()` 的話這條會整道繞過。"""
        self._make_cs('running')
        body = self._call_kw(self.dave_login, 'write',
                             [[self.ws.id], {'state': 'archived'}])
        self.assertIn('error', body, '必須被擋：%r' % body)
        self.assertIn(GUARD_ERROR_NEEDLE, self._error_text(body), body)
        self.assertEqual(self.ws.sudo().state, 'active')

    def test_G9_admin_orm_write_state_allowed_on_empty_workspace(self):
        """🟢 G8 的陽性對照：空工作區時同一條 ORM write 成功（證明 G8 擋的是本守衛，
        不是 admin 本來就寫不了）。"""
        body = self._call_kw(self.dave_login, 'write',
                             [[self.ws.id], {'state': 'archived'}])
        self.assertNotIn('error', body, body)
        self.assertEqual(self.ws.sudo().state, 'archived')

    def test_G10_superuser_is_not_exempt(self):
        """守衛是資料不變式，不豁免 su（`su-env-exempt-guard-false-green`）。"""
        sh = self._make_sh('active')
        ws = self.ws.sudo()
        try:
            ws.action_archive()
        except WorkspaceHasServicesError as e:
            self.assertEqual(e.code, ERR_WORKSPACE_HAS_SERVICES)
            self.assertEqual(e.services, [self._svc('smart_home', sh)])
        else:
            self.fail('su env 也必須被擋（守衛不得豁免 superuser）')
        self.assertEqual(ws.state, 'active')

    # ------------------------------------------------------------------ #
    #  授權（既有行為不變）                                               #
    # ------------------------------------------------------------------ #
    def test_G11_non_owner_still_cannot_archive_via_endpoint(self):
        """非 owner（admin／user）走產品端點仍是「not found or access denied」，
        不論工作區有沒有服務、也不會拿到服務清單。"""
        for with_service in (False, True):
            if with_service:
                self._make_sh('active')
            for login in (self.dave_login, self.carol_login):
                body = self._delete_ws(login)
                self.assertNotIn('error', body, body)
                result = body['result']
                self.assertIs(result.get('success'), False, result)
                self.assertNotIn('code', result, result)
                self.assertNotIn('data', result, result)
                self.assertEqual(self.ws.sudo().state, 'active')

    def test_G12_member_without_write_gets_ir_rule_error_not_service_list(self):
        """role=user 經 ORM 封存：仍由 ir.rule 擋（AccessError），守衛先做
        `check_access('write')`，不會改拿到本守衛的錯誤／服務名稱。"""
        sh = self._make_sh('active')
        body = self._call_kw(self.carol_login, 'write',
                             [[self.ws.id], {'state': 'archived'}])
        self.assertIn('error', body, '必須被擋：%r' % body)
        text = self._error_text(body)
        self.assertIn('AccessError', text, body)
        self.assertNotIn(GUARD_ERROR_NEEDLE, text, body)
        self.assertNotIn(sh.name, text, '沒有寫入權的人不該看到服務清單')
        self.assertEqual(self.ws.sudo().state, 'active')

    # ------------------------------------------------------------------ #
    #  判準同源                                                           #
    # ------------------------------------------------------------------ #
    def test_G13_cloud_service_deleted_states_match_terminal_states(self):
        """`workspace.py` 為避免 import 順序問題抄了 cloud_service 的「已刪除」判準；
        兩者必須相等（`cloud_service.TERMINAL_STATES` 是配額計數的既有權威）。"""
        guard = {key: states for key, _model, states in _ARCHIVE_GUARD_SERVICE_MODELS}
        self.assertEqual(set(guard), {'security_access', 'smart_home', 'cloud_service'})
        self.assertEqual(tuple(guard['cloud_service']), tuple(TERMINAL_STATES))
        for key, model, states in _ARCHIVE_GUARD_SERVICE_MODELS:
            selection = dict(self.env[model]._fields['state'].selection)
            for state in states:
                self.assertIn(state, selection,
                              '%s 的「已刪除」state %r 不在值集內' % (key, state))

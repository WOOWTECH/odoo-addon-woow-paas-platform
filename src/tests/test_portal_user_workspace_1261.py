# -*- coding: utf-8 -*-
"""#1261 regression: structured product responses without ERP promotion.

2026-10-01 supersedes the temporary Portal product denial: invitations and
existing memberships now work via /woow's private capability seam. Preserve
internal-first duplicate-email selection, no group changes, model-helper deny,
and HA/OAuth's historical deny. Real HTTP still catches raw ACL traceback leaks.
"""
import json
import uuid
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged

PASSWORD = 'Portal1261!pw'


@tagged('post_install', '-at_install')
class TestPortalUserWorkspace1261(HttpCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.suffix = suffix
        Users = self.env['res.users']
        portal_group = self.env.ref('base.group_portal')
        internal_group = self.env.ref('base.group_user')

        # owner：一般 internal 租戶（只掛 base.group_user），不是 admin——
        # 走的是真實租戶會走的 ACL / ir.rule，而不是 group_system 豁免。
        self.owner = Users.create({
            'name': f'Owner 1261 {suffix}',
            'login': f'owner_1261_{suffix}',
            'email': f'owner-1261-{suffix}@test.example',
            'password': PASSWORD,
            'groups_id': [(6, 0, [internal_group.id])],
        })
        self.portal_email = f'portal-1261-{suffix}@test.example'
        self.portal_user = Users.create({
            'name': f'Portal 1261 {suffix}',
            'login': f'portal_1261_{suffix}',
            'email': self.portal_email,
            'password': PASSWORD,
            'groups_id': [(6, 0, [portal_group.id])],
        })
        self.internal_email = f'member-1261-{suffix}@test.example'
        self.internal_user = Users.create({
            'name': f'Member 1261 {suffix}',
            'login': f'member_1261_{suffix}',
            'email': self.internal_email,
            'password': PASSWORD,
            'groups_id': [(6, 0, [internal_group.id])],
        })

        # 鑑別力前提：fixture 的帳號類型真的如名字所說。若哪天預設群組或
        # `share` 的計算改了，這裡大聲失敗，而不是讓下面的斷言靜靜失去意義。
        self.assertTrue(self.portal_user.share, 'fixture 前提：portal 帳號 share=True')
        self.assertFalse(self.owner.share, 'fixture 前提：owner 是 internal')
        self.assertFalse(self.internal_user.share, 'fixture 前提：成員是 internal')

        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        # Workspace.create 會自動補 owner 的 workspace_access(role='owner')。
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': f'WS 1261 {suffix}', 'owner_id': self.owner.id})

    # ── 使用者唯一入口（HTTP JSON-RPC）─────────────────────────────────
    def _post(self, url, params):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        # 🔴 #1261 回歸斷言本體：修正前 portal 呼叫者拿到的是 `error`
        # （AccessError 原文 + traceback），不是 `result`。
        self.assertIn('result', parsed,
                      '必須是結構化 result，不能是 JSON-RPC error：%s' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    def _as(self, user):
        self.authenticate(user.login, PASSWORD)

    def _access_row(self, user):
        self.env.invalidate_all()
        return self.WorkspaceAccess.search([
            ('workspace_id', '=', self.workspace.id),
            ('user_id', '=', user.id),
        ])

    def _invite(self, email):
        return self._post('/api/workspaces/%s/members' % self.workspace.id,
                          {'action': 'invite', 'email': email, 'role': 'user'})

    # ================================================================
    #  入口：邀請
    # ================================================================
    def test_invite_portal_user_succeeds_without_group_changes(self):
        self._as(self.owner)
        result = self._invite(self.portal_email)

        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['user_id'], self.portal_user.id)
        self.assertEqual(self._access_row(self.portal_user).role, 'user')
        self.env.invalidate_all()
        self.assertTrue(self.portal_user.share, '邀請流程不得自動把 portal 升成 internal')
        self._as(self.portal_user)
        listed = self._post('/api/workspaces', {'action': 'list'})
        self.assertTrue(listed['success'], listed)
        self.assertIn(self.workspace.id, [row['id'] for row in listed['data']])

    def test_invite_email_shared_by_portal_and_internal_picks_internal(self):
        """同一個 email 同時有 portal 與 internal 帳號時，邀請落到 internal 那個。

        portal 分身的名字刻意排在前面（res.users 預設 `_order = 'name, login'`），
        舊寫法 `search(..., limit=1)` 會撿到 portal——這條才有鑑別力。
        """
        portal_twin = self.env['res.users'].create({
            'name': f'AAA Portal Twin 1261 {self.suffix}',
            'login': f'portal_twin_1261_{self.suffix}',
            'email': self.internal_email,
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
        })
        first = self.env['res.users'].search(
            [('email', '=ilike', self.internal_email)], limit=1)
        self.assertEqual(first, portal_twin,
                         '前提：預設排序下 limit=1 會先撿到 portal 分身')

        self._as(self.owner)
        result = self._invite(self.internal_email)

        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['user_id'], self.internal_user.id, result)
        self.assertTrue(self._access_row(self.internal_user))
        self.assertFalse(self._access_row(portal_twin))

    def test_invite_internal_user_unchanged(self):
        """陽性對照：internal 帳號照舊邀請成功、回應形狀不變。"""
        self._as(self.owner)
        result = self._invite(self.internal_email)

        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['user_id'], self.internal_user.id)
        self.assertEqual(result['data']['role'], 'user')
        self.assertNotIn('code', result)
        self.assertTrue(self._access_row(self.internal_user))

    def test_invite_unknown_email_keeps_existing_behaviour(self):
        """完全沒有帳號的 email：維持既有文案，不帶新 code、不建任何東西。"""
        self._as(self.owner)
        email = f'nobody-1261-{self.suffix}@test.example'
        result = self._invite(email)

        self.assertFalse(result.get('success'), result)
        self.assertNotIn('code', result)
        self.assertIn(email, result.get('error') or '')
        self.assertEqual(len(self.workspace.access_ids), 1, '只剩 owner 一列')

    # ================================================================
    #  出口：portal 呼叫者
    # ================================================================
    def test_portal_user_workspace_list_is_structured_success(self):
        self._as(self.portal_user)
        result = self._post('/api/workspaces', {'action': 'list'})
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data'], [])

    def test_portal_user_workspace_create_is_structured_success(self):
        self._as(self.portal_user)
        result = self._post('/api/workspaces',
                            {'action': 'create', 'name': f'P WS {self.suffix}'})
        self.assertTrue(result.get('success'), result)
        workspace = self.env['woow_paas_platform.workspace'].browse(result['data']['id'])
        self.assertEqual(workspace.owner_id, self.portal_user)
        self.assertTrue(self.portal_user.share)

    def test_portal_user_with_legacy_membership_can_use_product_bff(self):
        """Existing membership now works in /woow, not model/HA helpers."""
        self.WorkspaceAccess.create({
            'workspace_id': self.workspace.id,
            'user_id': self.portal_user.id,
            'role': 'user',
            'invited_by_id': self.owner.id,
        })
        self.assertFalse(
            self.workspace.sudo().check_user_access(self.portal_user),
            'portal 帳號即使有殘留成員列也不算成員（sudo 呼叫端同樣適用）')

        self._as(self.portal_user)
        ws = self.workspace.id
        for url, params in (
            ('/api/workspaces', {'action': 'list'}),
            ('/api/workspaces/%s' % ws, {'action': 'get'}),
            ('/api/workspaces/%s/members' % ws, {'action': 'list'}),
        ):
            with self.subTest(url=url):
                result = self._post(url, params)
                self.assertTrue(result.get('success'), result)

        result = self._post('/api/workspaces/%s/services' % ws, {'action': 'list'})
        self.assertTrue(result.get('success'), result)

        # Product lists must work with the existing Portal membership.
        for url in ('/api/workspaces/%s/security-accesses' % ws,
                    '/api/workspaces/%s/smarthomes' % ws):
            with self.subTest(url=url):
                result = self._post(url, {'action': 'list'})
                self.assertTrue(result.get('success'), result)
                self.assertEqual(result['data'], [])
                self.assertNotIn('AccessError', result.get('error') or '')
                self.assertNotIn('workspace_access', result.get('error') or '',
                                 '不得把 ACL 原文（model 名）丟給使用者')

        # ai_assistant 的 `_check_workspace_access` 是 sudo search_count：不會丟
        # AccessError，但修正前會算到殘留列而放行。帶 workspace_id 的列表分支
        # 在 portal 分支之外，正是會漏的那條。
        result = self._post('/api/support/projects',
                            {'action': 'list', 'workspace_id': ws})
        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('error'), 'Access denied', result)

    def test_portal_user_with_legacy_membership_ha_api_denied(self):
        """HA 串接 API（OAuth bearer，`auth='none'`）的 sudo 成員查詢同樣不認殘留列。

        `ha_api._get_user_workspaces` / `_check_workspace_access` 都是 sudo
        search，修正前 portal 帳號憑 #1261 之前的殘留成員列就能列 workspace、
        讀 smart home 清單。
        """
        self.WorkspaceAccess.create({
            'workspace_id': self.workspace.id,
            'user_id': self.portal_user.id,
            'role': 'user',
            'invited_by_id': self.owner.id,
        })
        client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')
        Token = self.env['woow_paas_platform.oauth_token'].sudo()

        def make_token(user):
            return Token.create({
                'access_token': f'tok-1261-{user.id}-{self.suffix}',
                'scope': 'workspace:read smarthome:read',
                'expires_at': fields.Datetime.now() + timedelta(hours=1),
                'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
                'client_id': client.id,
                'user_id': user.id,
            })

        def get(url, token):
            return self.url_open(
                url, headers={'Authorization': f'Bearer {token.access_token}'})

        ws = self.workspace.id
        # 陽性對照：同一組端點對 internal owner 照常放行——少了它，「一律
        # 403／空清單」的壞修法（例如 token 本身失效）也會讓下面的陰性斷言全綠。
        owner_token = make_token(self.owner)
        resp = get('/api/smarthome/workspaces', owner_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ws, [w['id'] for w in resp.json()['workspaces']])
        resp = get('/api/smarthome/workspaces/%s/homes' % ws, owner_token)
        self.assertEqual(resp.status_code, 200)

        portal_token = make_token(self.portal_user)
        resp = get('/api/smarthome/workspaces', portal_token)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(ws, [w['id'] for w in resp.json()['workspaces']],
                         '殘留的 portal 成員列不得讓 workspace 出現在清單')
        resp = get('/api/smarthome/workspaces/%s/homes' % ws, portal_token)
        self.assertEqual(resp.status_code, 403)

    # ================================================================
    #  internal 不變（陽性對照）
    # ================================================================
    def test_internal_user_workspace_endpoints_unchanged(self):
        self.assertTrue(self.workspace.sudo().check_user_access(self.owner))

        self._as(self.owner)
        listed = self._post('/api/workspaces', {'action': 'list'})
        self.assertTrue(listed.get('success'), listed)
        self.assertIn(self.workspace.id, [w['id'] for w in listed['data']])
        self.assertNotIn('code', listed)

        detail = self._post('/api/workspaces/%s' % self.workspace.id,
                            {'action': 'get'})
        self.assertTrue(detail.get('success'), detail)

        members = self._post('/api/workspaces/%s/members' % self.workspace.id,
                             {'action': 'list'})
        self.assertTrue(members.get('success'), members)
        self.assertEqual([m['user_id'] for m in members['data']], [self.owner.id])

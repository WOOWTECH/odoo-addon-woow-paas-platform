# -*- coding: utf-8 -*-
"""#1261：Portal 帳號被邀進 workspace「成功」，之後整個平台卻用不了。

## 正式站實測（2026-09-29 瀏覽器 E2E，folight2015@gmail.com）

1. owner 對一個只掛 `User types / Portal` 的帳號（`res.users.share=True`）打
   `POST /api/workspaces/<ws>/members` action=invite → `success: True`，
   `workspace_access` 列真的建了。
2. 該帳號登入 `/woow`，前端第一支 `POST /api/workspaces` action=list 就在
   `WorkspaceAccess.search` 丟 `odoo.exceptions.AccessError`——
   `workspace_access` 的 ir.model.access 只給 `base.group_user` /
   `base.group_system`，portal 一列都沒有——JSON-RPC 回應是 `error`（帶原文
   訊息與 traceback）而不是 `result`。

## 本檔守的三件事

- **入口**：邀請 portal 帳號要被拒絕，回結構化 `code`，**且不得留下授權列**
  （只看回應不看 DB 會漏掉「API 說失敗、列卻在」——#525 A3 的教訓）。
- **出口**：portal 呼叫者打 workspace 端點一律拿到 `result`（結構化
  `success: False`），不是 JSON-RPC `error`。含 #1261 之前邀請殘留的舊成員列
  ——那種列今天正式站上就有一筆。
- **internal 不變**：同一組端點對 internal 使用者的回應與修正前一致（陽性對照；
  少了它，「一律拒絕」的壞修法也會讓陰性斷言全綠）。

## 為什麼走真 HTTP（HttpCase）而不是直呼 controller

缺陷的形狀就是「ORM 例外穿過 controller 變成 JSON-RPC `error`」。直呼 controller
方法時例外會直接在測試裡拋出，看不到框架怎麼序列化它，也驗不到
`auth="user"` 對 portal session 的實際行為（ORM 綠 ≠ endpoint secure）。
`_post` 裡的 `assertIn('result', parsed)` 就是本案的回歸斷言本體。
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import (
    ERR_INVITEE_PORTAL_ACCOUNT,
    ERR_PORTAL_ACCOUNT,
)

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
    def test_invite_portal_user_is_refused_without_access_row(self):
        self._as(self.owner)
        result = self._invite(self.portal_email)

        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('code'), ERR_INVITEE_PORTAL_ACCOUNT, result)
        self.assertIn(self.portal_email, result.get('error') or '',
                      '拒絕訊息要指出是哪個帳號，邀請者才知道要找誰處理')
        self.assertFalse(
            self._access_row(self.portal_user),
            '拒絕邀請就不得留下 workspace_access 列（否則又回到「說失敗、列卻在」）')
        # ⛔ 不自動改群組：拒絕之後對方仍是 portal。
        self.env.invalidate_all()
        self.assertTrue(self.portal_user.share, '邀請流程不得自動把 portal 升成 internal')

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
    def test_portal_user_workspace_list_is_structured_error(self):
        self._as(self.portal_user)
        result = self._post('/api/workspaces', {'action': 'list'})

        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('code'), ERR_PORTAL_ACCOUNT, result)
        self.assertTrue(result.get('error'))
        self.assertNotIn('AccessError', result.get('error'),
                         '不得把 ACL 原文丟給使用者')

    def test_portal_user_workspace_create_is_structured_error(self):
        self._as(self.portal_user)
        Workspace = self.env['woow_paas_platform.workspace']
        before = Workspace.search_count([])
        result = self._post('/api/workspaces',
                            {'action': 'create', 'name': f'P WS {self.suffix}'})

        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('code'), ERR_PORTAL_ACCOUNT, result)
        self.env.invalidate_all()
        self.assertEqual(Workspace.search_count([]), before)

    def test_portal_user_with_legacy_membership_gets_structured_errors(self):
        """#1261 之前邀請殘留的 portal 成員列（正式站上就有一筆）。

        列在，但 portal 用不了 workspace：各端點都要結構化拒絕，而且
        `check_user_access` 不得再憑那一列放行（含 sudo 呼叫端）。
        """
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
                self.assertFalse(result.get('success'), result)
                self.assertEqual(result.get('code'), ERR_PORTAL_ACCOUNT, result)

        # 不在本檔 gate 的 workspace 子端點走 `check_user_access` → 既有的
        # 「Workspace not found or access denied」結構化分支，而不是 AccessError。
        result = self._post('/api/workspaces/%s/services' % ws, {'action': 'list'})
        self.assertFalse(result.get('success'), result)

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

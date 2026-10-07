"""#1516：同名 workspace 的 slug 全域唯一，顯示名稱與租戶隔離不變。

透過真實使用者的 PaaS capability 建立，並用 /woow 的列表 API 觀察結果；
不直接呼叫 slug generator，也不 mock ORM／ir.rule。
"""
import json
import secrets
import uuid
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, tagged

from ..services.paas_capability import _PaaSCapability


@tagged('post_install', '-at_install')
class TestWorkspaceSlug1516(HttpCase):
    def setUp(self):
        super().setUp()
        self.users = {}
        suffix = uuid.uuid4().hex[:10]
        for label in ('a', 'b', 'portal'):
            login = 'slug-1516-%s-%s@example.invalid' % (label, suffix)
            group = 'base.group_portal' if label == 'portal' else 'base.group_user'
            user = self.env['res.users'].with_context(no_reset_password=True).create({
                'name': login, 'login': login, 'email': login, 'password': login,
                'groups_id': [(6, 0, [self.env.ref(group).id])],
            })
            self.assertEqual(user.share, label == 'portal')
            self.assertFalse(user.has_group('base.group_system'))
            self.users[label] = user

    def _capability(self, label):
        env = self.env['res.users'].with_user(self.users[label]).env
        self.assertFalse(env.su, '測試必須以真實使用者身分走 capability')
        return _PaaSCapability(env)

    def _slug_suffixes(self, values):
        # 只固定 slug 的 randomness；ws_hash 的 4-byte 抽值保持真實且獨立。
        values = iter(values)
        token_hex = secrets.token_hex

        def draw(nbytes=None):
            return next(values) if nbytes == 3 else token_hex(nbytes)

        return patch('odoo.addons.woow_paas_platform.models.workspace.secrets.token_hex',
                     side_effect=draw)

    def _listed_workspaces(self, label):
        user = self.users[label]
        self.env.flush_all()
        self.authenticate(user.login, user.login)
        response = self.url_open('/api/workspaces', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': {'action': 'list'},
        }), headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        parsed = response.json()
        self.assertIn('result', parsed, parsed)
        self.assertTrue(parsed['result'].get('success'), parsed)
        return parsed['result']['data']

    def test_internal_users_can_create_same_name_and_list_original_name(self):
        first = self._capability('a').create_workspace('omnigent', '')
        self.assertEqual(first.slug, 'omnigent')
        second_capability = self._capability('b')
        self.assertFalse(second_capability.memberships(),
                         'B 不得是 A 的 workspace 成員')
        with self.env.cr.savepoint():
            second = second_capability.create_workspace('omnigent', '')
        self.assertRegex(second.slug, r'^omnigent-[0-9a-f]{6}$')
        self.assertEqual(second.name, 'omnigent')
        listed = self._listed_workspaces('b')
        self.assertEqual([(row['id'], row['name']) for row in listed],
                         [(second.id, 'omnigent')],
                         'B 的列表應保留 name，且不洩漏 A 的 workspace')

    def test_suffix_collision_is_redrawn_across_users(self):
        capability = self._capability('a')
        self.assertEqual(capability.create_workspace('omnigent', '').slug,
                         'omnigent')
        self.assertEqual(capability.create_workspace('omnigent-a1b2c3', '').slug,
                         'omnigent-a1b2c3')
        with self._slug_suffixes(['a1b2c3', 'd4e5f6']), self.env.cr.savepoint():
            workspace = self._capability('b').create_workspace('omnigent', '')
        self.assertEqual(workspace.slug, 'omnigent-d4e5f6')
        self.assertEqual(workspace.name, 'omnigent')
        self.assertRegex(workspace.ws_hash, r'^[0-9a-f]{8}$')

    def test_eight_suffix_collisions_raise_validation_error(self):
        capability = self._capability('a')
        self.assertEqual(capability.create_workspace('omnigent', '').slug,
                         'omnigent')
        self.assertEqual(capability.create_workspace('omnigent-a1b2c3', '').slug,
                         'omnigent-a1b2c3')
        # 第九個值刻意可用：若越過八次上限，就會成功而使此斷言失敗。
        with self._slug_suffixes(['a1b2c3'] * 8 + ['d4e5f6']):
            # capability 不吞例外；controller 會轉成通用錯誤，故在此斷言型別。
            with self.assertRaises(ValidationError):
                self._capability('b').create_workspace('omnigent', '')

    def test_eighth_suffix_draw_can_still_succeed(self):
        capability = self._capability('a')
        self.assertEqual(capability.create_workspace('omnigent', '').slug,
                         'omnigent')
        self.assertEqual(capability.create_workspace('omnigent-a1b2c3', '').slug,
                         'omnigent-a1b2c3')
        with self._slug_suffixes(['a1b2c3'] * 7 + ['d4e5f6']):
            workspace = self._capability('b').create_workspace('omnigent', '')
        self.assertEqual(workspace.slug, 'omnigent-d4e5f6')

    def test_same_user_can_create_three_workspaces_with_the_same_name(self):
        capability = self._capability('a')
        with self._slug_suffixes(['a1b2c3', 'd4e5f6']):
            workspaces = [capability.create_workspace('omnigent', '')
                          for _ in range(3)]
        self.assertEqual([(ws.slug, ws.name) for ws in workspaces], [
            ('omnigent', 'omnigent'),
            ('omnigent-a1b2c3', 'omnigent'),
            ('omnigent-d4e5f6', 'omnigent'),
        ])

    def test_portal_user_can_create_same_name_and_list_original_name(self):
        first = self._capability('a').create_workspace('omnigent', '')
        self.assertEqual(first.slug, 'omnigent')
        portal_capability = self._capability('portal')
        self.assertTrue(portal_capability.portal)
        self.assertFalse(portal_capability.memberships())
        with self._slug_suffixes(['a1b2c3']):
            second = portal_capability.create_workspace('omnigent', '')
        self.assertEqual((second.slug, second.name),
                         ('omnigent-a1b2c3', 'omnigent'))
        listed = self._listed_workspaces('portal')
        self.assertEqual([(row['id'], row['name']) for row in listed],
                         [(second.id, 'omnigent')])

    def test_archived_workspace_still_reserves_its_normalized_slug(self):
        first = self._capability('a').create_workspace('OmniGENT!', '')
        self.assertEqual(first.slug, 'omnigent')
        first.action_archive()
        self.assertEqual(first.state, 'archived')
        with self._slug_suffixes(['a1b2c3']):
            second = self._capability('b').create_workspace('omnigent', '')
        self.assertEqual((second.slug, second.name),
                         ('omnigent-a1b2c3', 'omnigent'))

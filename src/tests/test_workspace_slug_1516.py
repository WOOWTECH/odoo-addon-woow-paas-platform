"""#1516：同名 workspace 的 slug 全域唯一，顯示名稱與租戶隔離不變。

透過真實使用者的 PaaS capability 建立，並用 /woow 的列表 API 觀察結果；
不直接呼叫 slug generator，也不 mock ORM／ir.rule。
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged

from ..services.paas_capability import _PaaSCapability


@tagged('post_install', '-at_install')
class TestWorkspaceSlug1516(HttpCase):
    def setUp(self):
        super().setUp()
        self.users = {}
        suffix = uuid.uuid4().hex[:10]
        for label in ('a', 'b'):
            login = 'slug-1516-%s-%s@example.invalid' % (label, suffix)
            user = self.env['res.users'].with_context(no_reset_password=True).create({
                'name': login, 'login': login, 'email': login, 'password': login,
                'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
            })
            self.assertFalse(user.share)
            self.assertFalse(user.has_group('base.group_system'))
            self.users[label] = user

    def _capability(self, label):
        env = self.env['res.users'].with_user(self.users[label]).env
        self.assertFalse(env.su, '測試必須以真實使用者身分走 capability')
        return _PaaSCapability(env)

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

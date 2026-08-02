"""#352：allow 型 Access 政策的 include 規則不得「整批放行」。

## 缺陷

Cloudflare Access 的席位是 **per-ACCOUNT** 的全平台共用池（50 席），而且是
**使用者實際認證登入時**才消耗一席。AC 方案按人數計費，人數的計算方式是數
``email_list`` 規則裡的位址——``email_domain``（整個網域放行）與 ``everyone``
（所有人放行）**計 0 人**。

兩者相乘的後果不是「少收一點錢」：租戶用一條規則放進不特定多數人，這些人登入
時照樣吃席位，第 51 人登入當場被擋——**受害的是平台上其他租戶，而他們零
workaround**。

## 兩層都要擋

issue 內文原本寫「UI 已不提供這兩種規則選項」，實查不成立：
``AddPolicyModal.js`` 兩個選項都在，而且 ``email_domain`` **還是新增規則的
預設值**。但就算 UI 修好了也不夠——直接打 API（甚至直接對 rule 表寫入）仍然
寫得進去。所以本檔兩條路徑都測：

1. **model 層直接寫入**：policy.create / rule.create / rule.write。
   特別是 **rule 層單獨寫入**——policy 上的 ``@api.constrains`` 不會因為「只建
   一條 rule」而觸發，必須靠 rule 的 create/write 顯式回呼。漏掉這條的話，
   「先建一個合法 policy、再單獨塞一條 include email_domain」就整條繞過。
2. **controller/API 路徑**：``api_sa_access_policies`` create / update。

## 邊界（刻意不擋的）

- **只擋 include**：include 是 OR（命中即放行），整批放行的入口在這。
  ``require``（AND）與 ``exclude``（NOT）只會**縮小**範圍——「allow 但排除
  evil.com」是正當寫法。
- **bypass 豁免**：bypass 不驗證身分、使用者不經認證 ⇒ 不消耗席位，不是繞過
  路徑；且無差別禁用 ``everyone`` 會與既有的
  ``_check_decision_selector_compat``（bypass 不得用 identity selector）打架，
  讓 bypass 只剩兩種寫法。
- **deny 不受限**：整批**封鎖**一個網域不會放任何人進來。
- 本檢查擋的是繞過寫法，**不等於席數計費成立**——``ac_max_access_seats`` 至今
  沒有任何程式碼在讀（#521）。
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.security_access import (
    SecurityAccessController,
)

#: 兩條呼叫路徑各自的 operator client namespace（缺一就會 fall through 到真實
#: HTTP client）。沿用 test_security_access_subscription_flow 的註解結論。
MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.'
    'get_paas_operator_client'
)
MOCK_GET_CLIENT_MODEL = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


def _patch_sa_request(env):
    return patch(
        'odoo.addons.woow_paas_platform.controllers.security_access.request',
        new=MagicMock(env=env),
    )


class _BulkGrantFixture:
    """共用 fixture：一個 SA ＋ 一條 route，policy 都掛在上面。

    刻意不繼承 ``TransactionCase``——Odoo 的 loader 會把抽象基底也收去跑。
    """

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.Rule = self.env[
            'woow_paas_platform.security_access_access_policy_rule']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Bulk Grant WS', 'owner_id': self.env.user.id,
        })
        self.sa = self.SA.create({
            'name': 'Bulk Grant GW', 'workspace_id': self.workspace.id,
        })
        self.route = self.Route.create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'bulk-grant',
        })

    def _rule(self, group, rule_type, rule_value=False):
        return (0, 0, {'group': group, 'rule_type': rule_type,
                       'rule_value': rule_value})

    def _make_policy(self, name, decision='allow', rules=None):
        return self.Policy.create({
            'route_id': self.route.id, 'name': name, 'decision': decision,
            'rule_ids': rules or [self._rule('include', 'email_list',
                                             'a@acme.com')],
        })

    def _assert_bulk_grant_rejected(self, callable_, expected_selector):
        """斷言被擋，而且擋的理由是「整批放行」而不是別條既有 constraint。

        這裡不只 assertRaises：``_check_at_least_one_include`` /
        ``_check_decision_selector_compat`` / rule 值格式驗證都會丟
        ``ValidationError``，只斷言型別的話，測試會在「因為別的原因紅」時假綠。
        訊息一併釘住被拒的 selector 與替代方案。
        """
        with self.assertRaises(ValidationError) as ctx:
            callable_()
        msg = str(ctx.exception)
        self.assertIn(expected_selector, msg, msg)
        self.assertIn('Email list', msg, msg)
        self.assertIn('seat', msg.lower(), msg)


class TestAllowPolicyBulkGrantModel(_BulkGrantFixture, TransactionCase):
    """model 層：policy 與 rule 兩個寫入面。"""

    allow_inherited_tests_method = True

    # ---------------- policy.create ---------------- #

    def test_allow_include_email_domain_rejected(self):
        """#352 主場景：allow + include email_domain＝不算人頭放進整個網域。"""
        self._assert_bulk_grant_rejected(
            lambda: self._make_policy(
                'domain-allow',
                rules=[self._rule('include', 'email_domain', 'acme.com')]),
            'email_domain',
        )
        self.assertFalse(
            self.Policy.search([('route_id', '=', self.route.id)]),
            "被拒的 policy 不得留下半筆資料。")

    def test_allow_include_everyone_rejected(self):
        """everyone 是同一個缺陷的極端版本。"""
        self._assert_bulk_grant_rejected(
            lambda: self._make_policy(
                'everyone-allow',
                rules=[self._rule('include', 'everyone')]),
            'everyone',
        )

    def test_allow_include_mixed_still_rejected(self):
        """夾帶一條合法 email_list 不能當掩護——include 是 OR，只要有一條整批
        放行就等於整批放行。"""
        self._assert_bulk_grant_rejected(
            lambda: self._make_policy(
                'mixed-allow',
                rules=[self._rule('include', 'email_list', 'a@acme.com'),
                       self._rule('include', 'email_domain', 'acme.com')]),
            'email_domain',
        )

    # ---------------- rule 層單獨寫入 ---------------- #

    def test_standalone_rule_create_rejected(self):
        """先建合法 policy、再單獨塞一條 include email_domain。

        policy 上的 ``@api.constrains`` **不會**因為「只建一條 rule」而觸發，
        所以這條走的是 rule.create 裡的顯式回呼。漏掉它＝整個修法只擋得到走
        ``policy.rule_ids`` 的寫法。
        """
        pol = self._make_policy('legit-then-widen')
        self._assert_bulk_grant_rejected(
            lambda: self.Rule.create({
                'policy_id': pol.id, 'group': 'include',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'}),
            'email_domain',
        )
        self.assertEqual(
            len(pol.rule_ids), 1,
            "被拒的 rule 不得留下——savepoint 應該把它 rollback 掉。")

    def test_rule_write_type_flip_rejected(self):
        """把既有的 include email_list 改成 everyone。"""
        pol = self._make_policy('flip-type')
        rule = pol.rule_ids[0]
        self._assert_bulk_grant_rejected(
            lambda: rule.write({'rule_type': 'everyone', 'rule_value': False}),
            'everyone',
        )

    def test_rule_write_group_move_rejected(self):
        """把合法的 exclude email_domain 搬進 include——同一個繞過的另一個入口。"""
        pol = self._make_policy(
            'move-group',
            rules=[self._rule('include', 'email_list', 'a@acme.com'),
                   self._rule('exclude', 'email_domain', 'evil.com')])
        offender = pol.rule_ids.filtered(lambda r: r.group == 'exclude')
        self._assert_bulk_grant_rejected(
            lambda: offender.write({'group': 'include'}),
            'email_domain',
        )

    def test_policy_decision_flip_to_allow_rejected(self):
        """deny + include email_domain 合法；改成 allow 就不合法。

        判準綁 decision，所以「先用 deny 建好、再翻成 allow」這條路也要擋。
        """
        pol = self._make_policy(
            'deny-then-allow', decision='deny',
            rules=[self._rule('include', 'email_domain', 'acme.com')])
        self._assert_bulk_grant_rejected(
            lambda: pol.write({'decision': 'allow'}),
            'email_domain',
        )

    # ---------------- 正向：不該被誤擋的 ---------------- #

    def test_allow_include_email_list_ok(self):
        """指名道姓的 email_list 是這條規則要引導使用者去用的寫法。"""
        pol = self._make_policy('list-allow')
        self.assertEqual(pol.decision, 'allow')
        self.assertEqual(pol.rule_ids[0].rule_type, 'email_list')

    def test_allow_exclude_email_domain_ok(self):
        """exclude 是 NOT——「allow 名單，但排除 evil.com」只會縮小範圍。"""
        pol = self._make_policy(
            'exclude-domain',
            rules=[self._rule('include', 'email_list', 'a@acme.com'),
                   self._rule('exclude', 'email_domain', 'evil.com')])
        self.assertEqual(len(pol.rule_ids), 2)

    def test_allow_require_email_domain_ok(self):
        """require 是 AND——同樣只會縮小範圍。"""
        pol = self._make_policy(
            'require-domain',
            rules=[self._rule('include', 'email_list', 'a@acme.com'),
                   self._rule('require', 'email_domain', 'acme.com')])
        self.assertEqual(len(pol.rule_ids), 2)

    def test_deny_include_email_domain_ok(self):
        """整批**封鎖**一個網域不會放任何人進來，不是繞過路徑。"""
        pol = self._make_policy(
            'deny-domain', decision='deny',
            rules=[self._rule('include', 'email_domain', 'evil.com')])
        self.assertEqual(pol.decision, 'deny')

    def test_bypass_include_everyone_ok(self):
        """bypass 豁免：不驗證身分 ⇒ 不消耗席位。

        而且無差別禁用 everyone 會與 ``_check_decision_selector_compat``
        打架——那條規定 bypass 不得用 email_domain / email_list，只剩
        ip_range / country / everyone，再拿掉 everyone 就只剩兩種寫法。
        """
        pol = self._make_policy(
            'bypass-everyone', decision='bypass',
            rules=[self._rule('include', 'everyone')])
        self.assertEqual(pol.decision, 'bypass')
        self.assertEqual(pol.rule_ids[0].rule_type, 'everyone')


class TestAllowPolicyBulkGrantEndpoint(_BulkGrantFixture, TransactionCase):
    """controller/API 層：只擋 UI 不夠，直接打 API 也要被擋。"""

    allow_inherited_tests_method = True

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.env.user.id)]):
            Access.create({
                'workspace_id': self.workspace.id,
                'user_id': self.env.user.id, 'role': 'owner',
            })

    def _create_via_api(self, name, include, decision='allow'):
        with patch(MOCK_GET_CLIENT), patch(MOCK_GET_CLIENT_MODEL), \
                _patch_sa_request(self.env):
            return self.controller.api_sa_access_policies(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                action='create', name=name, decision=decision,
                route_id=self.route.id, include=include,
            )

    def _assert_api_rejected(self, res, expected_selector):
        self.assertFalse(res['success'], res)
        err = res['error']
        self.assertIn(expected_selector, err, err)
        # 訊息要說明「為什麼不行」與「該用什麼」，不是只丟一句拒絕。
        self.assertIn('Email list', err, err)
        self.assertIn('seat', err.lower(), err)

    def test_api_create_email_domain_rejected(self):
        res = self._create_via_api(
            'api-domain', [{'rule_type': 'email_domain',
                            'rule_value': 'acme.com'}])
        self._assert_api_rejected(res, 'email_domain')
        self.assertFalse(
            self.Policy.search([('security_access_id', '=', self.sa.id)]),
            "API 被拒後不得留下 policy 資料。")

    def test_api_create_everyone_rejected(self):
        res = self._create_via_api(
            'api-everyone', [{'rule_type': 'everyone', 'rule_value': ''}])
        self._assert_api_rejected(res, 'everyone')

    def test_api_create_email_list_ok(self):
        """正向：API 仍然開得成 email_list 政策（沒有把整條路封死）。"""
        res = self._create_via_api(
            'api-list', [{'rule_type': 'email_list',
                          'rule_value': 'a@acme.com, b@acme.com'}])
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(res['data']['include'][0]['rule_type'], 'email_list')

    def test_api_update_to_email_domain_rejected(self):
        """既有合法政策不能經 API 改成整批放行。"""
        ok = self._create_via_api(
            'api-update-src', [{'rule_type': 'email_list',
                                'rule_value': 'a@acme.com'}])
        self.assertTrue(ok['success'], ok.get('error'))
        policy_id = ok['data']['id']
        with patch(MOCK_GET_CLIENT), patch(MOCK_GET_CLIENT_MODEL), \
                _patch_sa_request(self.env):
            res = self.controller.api_sa_access_policies(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                action='update', policy_id=policy_id,
                include=[{'rule_type': 'email_domain',
                          'rule_value': 'acme.com'}],
            )
        self._assert_api_rejected(res, 'email_domain')
        pol = self.Policy.browse(policy_id)
        self.assertEqual(
            pol.rule_ids.mapped('rule_type'), ['email_list'],
            "被拒的更新不得留下部分寫入（rule_ids 的 (5,0,0) 必須一起 rollback）。")

"""#352 ＋ #521 P-J：allow 型 Access 政策的 include 規則不得「整批放行」。

## 缺陷

Cloudflare Access 的席位是 **per-ACCOUNT** 的全平台共用池，而且是**使用者實際
認證登入時**才消耗一席。AC 方案按人數計費，人數的計算方式是數 ``email_list``
規則裡的位址——``email_domain``（整個網域放行）、``everyone``（所有人放行）、
``ip_range``（整段 IP）與 ``country``（整個國家）**一律計 0 人**。

兩者相乘的後果不是「少收一點錢」：租戶用一條規則放進不特定多數人，這些人登入
時照樣吃席位，池子用完之後下一個人登入當場被擋——**受害的是平台上其他租戶，
而他們零 workaround**。

⚠ 席位池的精確席數是 Cloudflare 方案的外部常數、會過期，刻意不寫在本檔也不寫
進租戶可見訊息（#890，Eugene 2026-09-05 裁「拿掉數字」）；精確值見
``docs/reference/gotchas/cf-zero-trust-seat-semantics.md``。

## 四型是分兩批收齊的

#352（2026-08）收 ``email_domain`` / ``everyone``；``ip_range`` / ``country``
當時被明文標記為「第三、四條繞過路徑，不在本次範圍」，由 **#521 P-J** 補上
（Eugene 2026-09-05 裁決 **(B) 只擋 allow**）。⇒ allow 政策的 include 群組現在
只剩 ``email_list`` 可用，這是刻意的產品收窄。

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
- **bypass 豁免（#521 P-J 裁決 (B)）**：bypass 不驗證身分、使用者不經認證 ⇒
  不消耗席位，不是繞過路徑；bypass + IP 是既有的運維工具（stg 現有
  ``odoo-stg-ip-bypass`` 等規則就是這個形狀）。而且擴到 bypass 會讓 bypass
  政策**一種寫法都不剩**：``_check_decision_selector_compat`` 已禁掉
  email_domain / email_list，本守衛若再禁掉 ip_range / country / everyone，
  五型全禁而「至少一條 include」又是硬規則 ⇒ bypass policy 建不出來。
  ⇒ (B) 在結構上是必需，不是偏好。
- **deny 不受限**：整批**封鎖**一個網域不會放任何人進來。
- 本檢查擋的是繞過寫法，**不等於席數計費成立**——``ac_max_access_seats`` 至今
  沒有任何程式碼在讀（#521）。
"""
import uuid
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

        #521 P-J 之後這條論證更強：``_check_decision_selector_compat`` 已禁掉
        bypass 的 email_domain / email_list，本守衛若再擴到 bypass，剩下的
        ip_range / country / everyone 也全禁 ⇒ 五型全禁、而「至少一條 include」
        是硬規則 ⇒ bypass policy 一種都建不出來。豁免的機制是守衛裡
        ``decision != 'allow'`` 那行早退，不是型別列舉。
        """
        pol = self._make_policy(
            'bypass-everyone', decision='bypass',
            rules=[self._rule('include', 'everyone')])
        self.assertEqual(pol.decision, 'bypass')
        self.assertEqual(pol.rule_ids[0].rule_type, 'everyone')

    # ------------- #521 P-J：ip_range / country（第三、四條繞過路徑）------ #

    def test_allow_include_ip_range_rejected(self):
        """P-J (i)：allow + include ip_range＝不算人頭放進整段 IP。"""
        self._assert_bulk_grant_rejected(
            lambda: self._make_policy(
                'allow-iprange',
                rules=[self._rule('include', 'ip_range', '10.0.0.0/8')]),
            'ip_range')

    def test_allow_include_ip_range_wide_open_rejected(self):
        """P-J：``0.0.0.0/0`` 是同一條路徑的極端版——整個網際網路都放行，
        計 0 人。守衛判的是 rule_type，不判 CIDR 寬窄，所以窄段一樣擋
        （擋的是「席位計數看不見這些人」，不是「這段 IP 有多大」）。"""
        self._assert_bulk_grant_rejected(
            lambda: self._make_policy(
                'allow-iprange-all',
                rules=[self._rule('include', 'ip_range', '0.0.0.0/0')]),
            'ip_range')

    def test_allow_include_country_rejected(self):
        """P-J (ii)：allow + include country＝不算人頭放進整個國家。"""
        self._assert_bulk_grant_rejected(
            lambda: self._make_policy(
                'allow-country',
                rules=[self._rule('include', 'country', 'TW')]),
            'country')

    def test_allow_include_mixed_ip_range_still_rejected(self):
        """合法 email_list 夾帶一條 ip_range 仍被擋：include 是 OR，一條整批
        放行就等於整批放行（與 email_domain 那格同構）。"""
        self._assert_bulk_grant_rejected(
            lambda: self._make_policy(
                'allow-mixed-ip',
                rules=[self._rule('include', 'email_list', 'a@acme.com'),
                       self._rule('include', 'ip_range', '10.0.0.0/8')]),
            'ip_range')

    def test_standalone_rule_create_ip_range_rejected(self):
        """rule.create 的顯式回呼路徑也要涵蓋新型別（policy 的
        @api.constrains 對「單獨建 rule」不會觸發）。"""
        pol = self._make_policy('allow-standalone-ip')
        self._assert_bulk_grant_rejected(
            lambda: self.Rule.create({
                'policy_id': pol.id, 'group': 'include',
                'rule_type': 'ip_range', 'rule_value': '192.0.2.0/24'}),
            'ip_range')
        self.assertEqual(len(pol.rule_ids), 1,
                         '被拒的 rule.create 不得留下列（savepoint 回捲）')

    def test_rule_write_type_flip_to_country_rejected(self):
        """rule.write 把既有 include email_list 改成 country 也是同一個繞過。"""
        pol = self._make_policy('allow-flip-country')
        rule = pol.rule_ids[0]
        self._assert_bulk_grant_rejected(
            lambda: rule.write({'rule_type': 'country',
                                'rule_value': 'TW'}),
            'country')

    def test_bypass_policy_flipped_to_allow_rejected(self):
        """🔴 P-J 唯一擴大既有資料射程的那一格：一條**合法的** bypass +
        ip_range 政策（stg 運維規則的形狀）被翻成 allow 時，當場被擋。

        它在 P-J 之前是放行的——判準綁 ``decision``，所以翻旗就是新的違規
        事件，而不是回頭追殺舊資料。舊資料本身不動（守衛只在寫入時跑）。
        """
        pol = self._make_policy(
            'ops-bypass', decision='bypass',
            rules=[self._rule('include', 'ip_range', '114.32.21.18')])
        self.assertEqual(pol.decision, 'bypass')
        self._assert_bulk_grant_rejected(
            lambda: pol.write({'decision': 'allow'}), 'ip_range')

    # ---------------- P-J 的陽性對照：(B) 只擋 allow ---------------- #

    def test_bypass_include_bare_ip_ok(self):
        """🔴 (iii) 裁決 (B) 的行為錨：bypass + ip_range **不擋**。

        rule_value 刻意用**無 CIDR 後綴**的 ``114.32.21.18``——那正是 stg
        現有 ``odoo-stg-ip-bypass`` 那兩條運維規則的形狀（``ip_network(...,
        strict=False)`` 接受裸 IP，等同 /32）。這一格紅了就代表 P-J 打破了
        stg 的運維通道，而那條通道在別處**沒有任何測試釘住**。
        """
        pol = self._make_policy(
            'bypass-bare-ip', decision='bypass',
            rules=[self._rule('include', 'ip_range', '114.32.21.18')])
        self.assertEqual(pol.decision, 'bypass')
        self.assertEqual(pol.rule_ids[0].rule_type, 'ip_range')
        self.assertEqual(pol.rule_ids[0].rule_value, '114.32.21.18')

    def test_bypass_include_country_ok(self):
        """(iii) 同批：bypass + country 也不擋（同一條 decision 早退）。"""
        pol = self._make_policy(
            'bypass-country', decision='bypass',
            rules=[self._rule('include', 'country', 'TW')])
        self.assertEqual(pol.rule_ids[0].rule_type, 'country')

    def test_deny_include_ip_range_ok(self):
        """deny 不受限：整批**封鎖**一段 IP 不會放任何人進來。"""
        pol = self._make_policy(
            'deny-iprange', decision='deny',
            rules=[self._rule('include', 'ip_range', '10.0.0.0/8')])
        self.assertEqual(pol.decision, 'deny')

    def test_allow_require_ip_range_ok(self):
        """require(AND) ip_range 只**縮小**範圍——「allow 這些人、而且必須從
        公司 IP 進來」是正當寫法，擋它沒有道理。"""
        pol = self._make_policy(
            'allow-require-ip',
            rules=[self._rule('include', 'email_list', 'a@acme.com'),
                   self._rule('require', 'ip_range', '10.0.0.0/8')])
        self.assertEqual(len(pol.rule_ids), 2)

    def test_allow_exclude_country_ok(self):
        """exclude(NOT) country 同理：只縮小範圍。"""
        pol = self._make_policy(
            'allow-exclude-country',
            rules=[self._rule('include', 'email_list', 'a@acme.com'),
                   self._rule('exclude', 'country', 'CN')])
        self.assertEqual(len(pol.rule_ids), 2)

    # ---------------- 租戶身分（非 su）------------------------------ #

    def _tenant_route(self):
        """建一位只有 ``base.group_user`` 的租戶，連同他自己 own 的
        workspace / SA / route，回傳 ``(tenant, route)``。

        workspace 一定要用 ``with_user(tenant).create()``：``Workspace.create()``
        會依 ``owner_id`` 自動 bootstrap 一列 owner 的 ``workspace_access``，
        而 ``_check_owner_count`` 限制一個 workspace 只能有一個 owner ⇒ 事後
        補列會撞「A workspace can only have one owner」（做法同
        ``test_rule_reparent_guard_941``）。
        """
        suffix = uuid.uuid4().hex[:8]
        login = 'bulkgrant_%s' % suffix
        tenant = self.env['res.users'].sudo().create({
            'name': 'Bulk Grant Tenant %s' % suffix,
            'login': login,
            'password': login,
            'email': '%s@test.example' % login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        ws = self.env['woow_paas_platform.workspace'].with_user(tenant).create(
            {'name': 'Bulk Grant Tenant WS %s' % suffix})
        sa = self.SA.sudo().create(
            {'name': 'Tenant GW %s' % suffix, 'workspace_id': ws.id})
        route = self.Route.sudo().create(
            {'security_access_id': sa.id,
             'subdomain_prefix': 'bulk-tenant-%s' % suffix})
        return tenant, route

    def test_tenant_identity_allow_include_ip_range_rejected(self):
        """🔴 守衛在**租戶身分**下一樣擋，而且擋的是 ValidationError。

        ``TransactionCase`` 預設跑在 su env（``env.su=True``），只在 su 下驗過
        的守衛分不出「守衛有效」與「守衛只在 su 下有效」——而 stored related 的
        ``compute_sudo`` 會讓 ``@api.constrains`` 多跑一趟 su，那一趟不吃
        ir.rule，兩者的輸出一模一樣。

        ⚠ **陽性對照必配**：先證明同一位租戶建得起合法的 email_list 政策（守衛
        以外的門——ACL、ir.rule、錨點——都是通的），否則「被擋」可能只是因為他
        根本沒有權限，這一格就會在守衛失效時照樣綠。

        ⚠ ``with_user().create()`` 包在 ``savepoint()`` 裡是刻意的：``_create``
        先 INSERT 再做 post-check，不回捲會讓 transaction 進 aborted 狀態、後續
        查詢全 ``InFailedSqlTransaction``（同 ``test_rule_reparent_guard_941``）。
        """
        tenant, route = self._tenant_route()
        TenantPolicy = self.Policy.with_user(tenant)

        # 陽性對照：合法寫法必須真的建得起來（證明這條租戶路徑是通的）
        legal = TenantPolicy.create({
            'route_id': route.id, 'name': 'tenant-legal', 'decision': 'allow',
            'rule_ids': [self._rule('include', 'email_list', 't@acme.com')]})
        self.assertTrue(legal.id, '前提：租戶必須建得起合法的 allow 政策')

        raised = None
        try:
            with self.env.cr.savepoint():
                TenantPolicy.create({
                    'route_id': route.id, 'name': 'tenant-iprange',
                    'decision': 'allow',
                    'rule_ids': [self._rule('include', 'ip_range',
                                            '10.0.0.0/8')]})
        except Exception as exc:  # noqa: BLE001 —— class 本身就是待量測的量
            raised = exc
        self.assertIsInstance(
            raised, ValidationError,
            '租戶身分下必須被守衛擋（ValidationError）；實際 %r' % (raised,))
        msg = str(raised)
        self.assertIn('ip_range', msg, msg)
        self.assertIn('Email list', msg, msg)
        self.assertIn('seat', msg.lower(), msg)


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
        # #991／#1300：endpoint 經 `_PaaSCapability` 解析 workspace／SA／route／
        # policy，要求「已認證」的 workspace 成員；test superuser（`__system__`，
        # active=False）不算，會在任何守衛之前就回「Workspace not found or access
        # denied」。真實會建／改 policy 的是 workspace owner／admin，所以 harness
        # 以一位 internal admin 成員的身分當 `request.env`。只換呼叫者：受測
        # endpoint、斷言與 production 皆不變，不放寬 capability。
        self.member = self.env['res.users'].with_context(
            no_reset_password=True).create({
                'name': 'Bulk Grant WS Admin',
                'login': 'bulk-grant-admin-%s' % uuid.uuid4().hex[:8],
                'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
            })
        Access.create({
            'workspace_id': self.workspace.id,
            'user_id': self.member.id, 'role': 'admin',
        })

    def _member_request(self):
        """controller 看到的 request：已認證的 workspace admin 成員（見 setUp）。"""
        return _patch_sa_request(self.env(user=self.member))

    def _create_via_api(self, name, include, decision='allow'):
        with patch(MOCK_GET_CLIENT), patch(MOCK_GET_CLIENT_MODEL), \
                self._member_request():
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

    def test_api_create_ip_range_rejected(self):
        """#521 P-J：API 這條路徑也要涵蓋新型別（UI 濾掉了 ≠ 擋住了）。"""
        res = self._create_via_api(
            'api-iprange', [{'rule_type': 'ip_range',
                             'rule_value': '10.0.0.0/8'}])
        self._assert_api_rejected(res, 'ip_range')
        self.assertFalse(
            self.Policy.search([('security_access_id', '=', self.sa.id)]),
            "API 被拒後不得留下 policy 資料。")

    def test_api_create_country_rejected(self):
        res = self._create_via_api(
            'api-country', [{'rule_type': 'country', 'rule_value': 'TW'}])
        self._assert_api_rejected(res, 'country')

    def test_api_create_bypass_ip_range_ok(self):
        """(iii) 的 API 側陽性對照：bypass + ip_range 經 API 仍建得起來——
        stg 的運維規則就是這個形狀，裁決 (B) 明文不擋。"""
        res = self._create_via_api(
            'api-bypass-ip',
            [{'rule_type': 'ip_range', 'rule_value': '114.32.21.18'}],
            decision='bypass')
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(res['data']['include'][0]['rule_type'], 'ip_range')

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
                self._member_request():
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

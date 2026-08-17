# -*- coding: utf-8 -*-
"""#768（SH AC）：SH route 錨定的 ir.rule **第三腿**授權矩陣（真 HTTP RPC）。

## 這個檔守什麼

`security_access_access_policy` 的錨點在本單從二腿（SA route / cloud_service）
擴成三腿（＋`smart_home_route_id`）。`ir.rule` 的 domain **只走得到真欄位**，所以
第三腿是靠 policy 上的 stored related `smart_home_id`（← `smart_home_route_id`）
與子模型上同名的鏡射欄位撐起來的。本檔逐格量出四個 model 的授權矩陣：

| 受測 model | 讀 | 寫 |
| --- | --- | --- |
| `security_access_access_policy`（SH 錨定） | workspace 成員（含 guest） | owner／admin |
| `security_access_access_policy_rule`（SH 錨定） | 同上 | 同上 |
| `smart_home_route` | workspace 成員 | owner／admin（create／unlink 一律拒） |
| policy（SA route／CS 錨定） | **回歸對照**，行為不得改變 | 同左 |

## 漏改的症狀是什麼（兩個方向都會壞，而且都不會有例外）

* **補太少**——漏了 policy 或子模型任一邊的 `smart_home_id`（或忘了 `store=True`）
  ⇒ 三腿全假、**fail-closed**：租戶連自己那台 SH 的 policy 都讀不到，owner 連
  自己那條 selector rule 都建不出來。而丟出來的 `AccessError` 指的是**子表**的
  技術名，沒有人會把它聯想到「parent 換了錨點」。G 系列的陽性對照就是為了讓這
  一格會紅。
* **補太寬**——第三腿忘了 role 條件、或把 `access_ids` 寫成 `access_ids.role`
  點鏈 ⇒ 一個 `role='guest'` 的成員就改得動 Cloudflare 的 allow/deny 閘門。
  **點鏈是跨列匹配**（A 列滿足 `user_id`、B 列滿足 `role`），一個 workspace 只要
  同時掛著 owner 與 guest 兩列，guest 就會通過 ⇒ 等於沒有 role 條件（#556 根
  因之一）。W1 因此刻意掛滿 owner/admin/user/guest **四列**——少一列，兩種寫法
  的行為就一模一樣，這裡的每一條負向斷言都會零鑑別力。

## 為什麼一定要有回歸對照（F 系列）

第三腿是在**既有 domain 上動刀**（`'|'` 從一個變兩個，四條 rule 各改一次）。
只測 SH 那一腿的話，把 SA 腿或 CS 腿寫壞（例如三腿都指向 `smart_home_id`、或
`'|'` 的個數與 leaf 個數對不上導致某一腿被吃掉）**整組 SH 測試照樣全綠**，而受
害的是既有租戶。F 系列因此對 SA route 錨定與 CS 錨定各留一條讀、一條寫。

## TDD 紀律（照抄 #556 / #676 / #744）

* **全部走真 HTTP RPC**（`/web/dataset/call_kw`）。ir.rule 對 `TransactionCase`
  的 superuser env 不生效，直呼 model 也繞開真實授權路徑——「只在 su env 驗過」
  與「這條路徑從來沒成功過」在測試輸出上不可區分（#739 的教訓）。
* fixture user 一律**只掛 `base.group_user`**（setUp 有前提斷言）。
* **每一條「被擋下來」旁邊都有一條「合法路徑會成功」**。沒有它，`AccessError`
  可能來自 fixture 壞掉、欄位不存在、記錄不存在或一個「什麼都擋」的壞 rule，
  而那些情況下所有負向斷言都會為了錯的理由變綠。
* **判定必須留「這個結果不該出現」那一格**：`_assert_denied` 不只斷言「有
  error」，還斷言它**是 `AccessError`**——被 `ValidationError`（constraint）或
  `UserError`（身分錨點守衛／primary 不可刪）擋下來也是「被擋了」，但那證明
  的是別的守線，把 ir.rule 整條拿掉測試照樣綠。
* **不用 `sm_skip_access_sync` 關掉 sync**。fixture 的 SH 沒有 `tunnel_id`，
  route 的 liveness gate（`_access_is_live`）本來就會直接 return ⇒ operator 不會
  被呼叫。用 skip 關掉的話，「錨點分派沒接好」與「fixture 把 sync 關掉了」在輸出
  上不可區分。
* 每次 RPC 後 `invalidate_all()`（helper 內建）：`AccessError` 之前若先讀過同一
  筆記錄，值會被餵進 env cache 而**遮蔽**掉該有的錯誤。
"""
import json
import uuid

from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _internal_provision
from ..models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_USER,
)
from .smart_home_fixture import sh_model

_POLICY = 'woow_paas_platform.security_access_access_policy'
_POLICY_RULE = 'woow_paas_platform.security_access_access_policy_rule'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 768' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _ShAccessAclFixture(HttpCase):
    """W1（alice owner、dave admin、carol user、gary guest）＋ W2（bob owner）。

    W1 底下同時擺三種錨點的 policy（SH route／SA route／cloud_service），
    W2 底下擺一份 SH 錨定的鏡像——跨 workspace 的每一條負向斷言都要有「bob 對
    **自己**那一份做得到」當陽性對照，否則「隔離有效」與「bob 對這個 model
    什麼都做不了」分不出來。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Policy = self.env[_POLICY]
        self.PolicyRule = self.env[_POLICY_RULE]
        self.ShRoute = self.env[_SH_ROUTE]

        self.alice, self.alice_login = _new_group_user(self.env, 'alice768')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob768')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol768')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave768')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary768')

        for u in (self.alice, self.bob, self.carol, self.dave, self.gary):
            self.assertFalse(
                u.has_group('base.group_system'),
                '前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 768'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 768'})
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})
        self.assertEqual(
            sorted(self.w1.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]),
            '前提：W1 必須同時掛 owner/admin/user/guest 四列'
            '（少了 admin 那列，跨列匹配的 domain 寫法就不會被抓到）')
        self.assertFalse(
            self.w2.sudo().access_ids.filtered(
                lambda a: a.user_id in (self.alice | self.carol | self.dave
                                        | self.gary)),
            '前提：W1 的成員不得同時是 W2 的成員（否則跨 workspace 那組全假）')

        # --- 受測主體：SH route 錨定 ---------------------------------- #
        self.sh1 = self._smart_home(self.w1, 'SH of tenant A')
        self.sh2 = self._smart_home(self.w2, 'SH of tenant B')
        self.route_w1 = self._sh_route(self.sh1)
        #: 非 primary route：unlink 那一格**必須**打在它身上。primary 的
        #: `unlink()` 會先丟 `UserError`（不可刪），打在 primary 上等於量到
        #: 另一道守線，ACL 的 `unlink=0` 就算被改成 1 也不會紅。
        self.route_w1_extra = self._sh_route(
            self.sh1, is_primary=False,
            subdomain_prefix='extra-%s' % uuid.uuid4().hex[:6])
        self.route_w2 = self._sh_route(self.sh2)

        # --- 回歸對照：SA route 錨定 ＋ CS 錨定（皆在 W1 底下）--------- #
        self.sa_w1 = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA W1 768', 'workspace_id': self.w1.id})
        self.sa_route_w1 = self.env[
            'woow_paas_platform.security_access_route'].sudo().create({
                'security_access_id': self.sa_w1.id,
                'subdomain_prefix': 'acl768-%s' % uuid.uuid4().hex[:6]})
        self.svc_w1 = self._service(self.w1, 'svc-w1-768')

        self.sh_policy = self._policy(smart_home_route_id=self.route_w1.id)
        self.sh_policy_w2 = self._policy(smart_home_route_id=self.route_w2.id)
        self.sa_policy = self._policy(route_id=self.sa_route_w1.id)
        self.cs_policy = self._policy(cloud_service_id=self.svc_w1.id)

        # 🔴 前提斷言：ir.rule 第三腿 walk 的是 policy 上的 stored related
        # `smart_home_id`，不是 `smart_home_route_id`。這一格若是空的，member
        # 讀取那組會 fail-closed 而紅（好），但**跨 workspace 那組會為了錯的
        # 理由變綠**（誰都讀不到）。所以在測試開始前先量它一次。
        self.assertEqual(
            self.sh_policy.sudo().smart_home_id, self.sh1.sudo(),
            '前提：policy 的 stored related smart_home_id 必須物化到 SH '
            '（空的話跨 workspace 那組會因為「誰都讀不到」而假綠）')
        self.assertEqual(
            self.sh_policy.sudo().rule_ids.mapped('smart_home_id'),
            self.sh1.sudo(),
            '前提：子模型的 smart_home_id 鏡射也必須物化')

    # ------------------------------------------------------------------ #
    #  fixture builders（一律 sudo：fixture 不該被受測的 rule 擋住）        #
    # ------------------------------------------------------------------ #
    def _smart_home(self, workspace, name):
        """建一台 SH，並把開閘 context **洗掉**再交出去。

        🔴 `with_env(self.env)` 不是排版：`sh_model()` 回的 handle 同時開了建立
        閘與身分錨點閘，而 `with_context` 是 merge、`.sudo()` / `.browse()` 都
        保留它 ⇒ 不洗掉的話 fixture 會把受測守衛一起關掉
        （`test_smart_home_authz_524.py:153-168` 的同一條教訓）。

        刻意**不設** `tunnel_id`：route 的 `_access_is_live()` 因此恆為 False，
        本檔所有 create/write 都不會真的打到 operator。用 `sm_skip_access_sync`
        達成同一件事是錯的——那會讓「錨點分派沒接好」與「fixture 把 sync 關掉
        了」不可區分。
        """
        return sh_model(self.env).sudo().create({
            'name': name,
            'workspace_id': workspace.id,
            # subdomain 有 DB UNIQUE，且 primary route 的 hostname 由它 compute。
            'subdomain': 'paas-sm-768-%s' % uuid.uuid4().hex[:8],
        }).with_env(self.env)

    def _sh_route(self, sh, **extra):
        """route 一律用 sudo 建——本檔不是在測 route 的建立路徑。"""
        vals = {'smart_home_id': sh.id, 'is_primary': True}
        vals.update(extra)
        return self.ShRoute.sudo().create(vals).with_env(self.env)

    def _service(self, workspace, name):
        template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'ACL 768 tmpl',
                'slug': 'acl-768-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/acl768',
                # ⚠ 必須釘版，否則 fixture 會死在供給閘上，而那個 error 長得
                # 跟「rule 擋住了」一模一樣。
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': name,
                'workspace_id': workspace.id,
                'template_id': template.id,
                'state': 'running',
                'subdomain': '%s-%s' % (name, uuid.uuid4().hex[:6]),
            })

    def _policy(self, **anchor):
        """一條 include ＋ 一條 exclude。

        第二條不是裝飾：子模型的 unlink 陽性對照要刪掉一條 rule 而 policy 仍
        滿足「至少一條 include」，否則 owner 那一格會撞上 `ValidationError`
        ——被擋是被擋了，但擋的是別的守線。
        """
        vals = {
            'name': 'acl768-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [
                (0, 0, {'group': 'include', 'rule_type': 'email_list',
                        'rule_value': 'a@example.com'}),
                (0, 0, {'group': 'exclude', 'rule_type': 'email_domain',
                        'rule_value': 'evil.com'}),
            ],
        }
        vals.update(anchor)
        return self.Policy.sudo().create(vals).with_env(self.env)

    # ------------------------------------------------------------------ #
    #  RPC helper                                                         #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        })
        resp = self.url_open(
            '/web/dataset/call_kw', data=body,
            headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    # --- policy ------------------------------------------------------- #
    def _read_policy(self, policy):
        return self._call_kw(_POLICY, 'read', [[policy.id], ['name']])

    def _search_read_policy(self, policy):
        return self._call_kw(
            _POLICY, 'search_read', [[['id', '=', policy.id]], ['name']])

    def _rename_policy(self, policy, new_name='renamed-768'):
        return self._call_kw(
            _POLICY, 'write', [[policy.id], {'name': new_name}])

    def _unlink_policy(self, policy):
        return self._call_kw(_POLICY, 'unlink', [[policy.id]])

    def _create_policy(self, **anchor):
        vals = {
            'name': 'rpc-768-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [[0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'b@example.com'}]],
        }
        vals.update(anchor)
        return self._call_kw(_POLICY, 'create', [vals])

    # --- policy rule（子模型）------------------------------------------ #
    def _create_selector_rule(self, policy):
        return self._call_kw(_POLICY_RULE, 'create', [{
            'policy_id': policy.id, 'group': 'exclude',
            'rule_type': 'email_domain', 'rule_value': 'evil2.com'}])

    def _exclude_rule(self, policy):
        """policy 底下那條 exclude rule（刪它不會破壞「至少一條 include」）。"""
        rule = policy.sudo().rule_ids.filtered(
            lambda r: r.group == 'exclude')[:1]
        self.assertTrue(
            rule, '前提：fixture policy 必須有一條 exclude rule 可供刪除測試')
        return rule

    def _rename_selector_rule(self, rule, value='renamed768.com'):
        return self._call_kw(
            _POLICY_RULE, 'write', [[rule.id], {'rule_value': value}])

    def _unlink_selector_rule(self, rule):
        return self._call_kw(_POLICY_RULE, 'unlink', [[rule.id]])

    def _search_read_selector_rules(self, policy):
        return self._call_kw(
            _POLICY_RULE, 'search_read',
            [[['policy_id', '=', policy.id]], ['group']])

    # --- smart_home_route ---------------------------------------------- #
    def _read_route(self, route):
        return self._call_kw(
            _SH_ROUTE, 'read', [[route.id], ['hostname', 'is_protected']])

    def _search_read_route(self, route):
        return self._call_kw(
            _SH_ROUTE, 'search_read',
            [[['id', '=', route.id]], ['hostname', 'is_protected']])

    def _set_route_protected(self, route, value=True):
        return self._call_kw(
            _SH_ROUTE, 'write', [[route.id], {'is_protected': value}])

    def _create_route(self, sh):
        """刻意 `is_primary=False`：primary 會先撞上 model 自己的唯一性守衛
        （`ValidationError`，在 `super().create()` 的 ACL 檢查**之前**），那樣
        量到的就不是 ACL 的 `create=0`。"""
        return self._call_kw(_SH_ROUTE, 'create', [{
            'smart_home_id': sh.id, 'is_primary': False,
            'subdomain_prefix': 'rpc-%s' % uuid.uuid4().hex[:6]}])

    def _unlink_route(self, route):
        return self._call_kw(_SH_ROUTE, 'unlink', [[route.id]])

    # ------------------------------------------------------------------ #
    #  斷言 helper                                                         #
    # ------------------------------------------------------------------ #
    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)
        return result.get('result')

    def _assert_denied(self, result, msg):
        """被擋下來，**而且是被 ir.rule／ACL 擋的**。

        第三格（`AccessError`）才是判準的重點：`ValidationError`（arc／quota／
        「至少一條 include」）與 `UserError`（身分錨點、primary 不可刪）也都會
        讓「有沒有 error」那一格通過，但它們證明的是別的守線 —— 只驗「被擋了」
        的話，把本檔要守的 rule 整條刪掉，測試照樣全綠。
        """
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn(
            'AccessError', text,
            msg + '（歸因：必須是 ir.rule／ACL 這一層擋的，'
                  '不是 constraint 或身分錨點守衛；回 %r）' % text)

    def _assert_empty_read(self, result, msg):
        """讀取方向被擋：`read` 回 `AccessError`、`search_read` 回空集。

        兩種都算「讀不到」，但要**明確涵蓋**——只認其中一種的話，另一種行為會
        讓斷言為了不同的理由變綠。第三格是「回了非空結果」＝一定不通過。
        """
        if 'error' in result:
            self.assertIn(
                'AccessError', self._error_text(result),
                msg + '（歸因：讀取被擋必須是 AccessError，回 %r）'
                % self._error_text(result))
        else:
            self.assertEqual(
                result.get('result'), [],
                msg + '（search_read 回了非空結果 %r）' % result.get('result'))


class TestShAnchoredPolicyAcl(_ShAccessAclFixture):
    """E 系列：SH route 錨定 policy 的 read／write 切分（第三腿本體）。"""

    # ---------------------------------------------------------------- #
    #  陽性對照——先證明這些操作在該通的身分下真的做得到                  #
    # ---------------------------------------------------------------- #
    def test_E1_owner_can_manage_sh_anchored_policy(self):
        """owner 讀／改／建／刪四向全通（**這是 E3-E6 的陽性對照**）。

        四向都失敗的世界（第三腿整條漏掉、或 `smart_home_id` 忘了 `store=True`）
        才是本單最可能的翻車形狀：fail-closed 沒有例外、沒有 log，租戶只會回報
        「Access 分頁點不開」。
        """
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_ok(self._read_policy(self.sh_policy),
                        'owner 讀不到 SH 錨定的 policy（第三腿漏了？）')
        self._assert_ok(self._rename_policy(self.sh_policy),
                        'owner 改不動 SH 錨定的 policy')
        self._assert_ok(
            self._create_policy(smart_home_route_id=self.route_w1.id),
            'owner 建不了 SH 錨定的 policy')
        self._assert_ok(self._unlink_policy(self.sh_policy),
                        'owner 刪不掉 SH 錨定的 policy')

    def test_E2_admin_can_manage_sh_anchored_policy(self):
        """admin 與 owner 同權。少了這條，把 role 條件寫成 `['owner']` 不會紅。"""
        self.authenticate(self.dave_login, self.dave_login)
        self._assert_ok(self._rename_policy(self.sh_policy),
                        'admin 改不動 SH 錨定的 policy')
        self._assert_ok(
            self._create_policy(smart_home_route_id=self.route_w1.id),
            'admin 建不了 SH 錨定的 policy')

    # ---------------------------------------------------------------- #
    #  負向——同 workspace 的低權角色                                     #
    # ---------------------------------------------------------------- #
    def test_E3_member_user_can_read_but_not_write(self):
        """role=user：讀得到（BFF 的 list/get 需要），寫入三向全擋。"""
        self.authenticate(self.carol_login, self.carol_login)
        self._assert_ok(
            self._read_policy(self.sh_policy),
            'role=user 應該讀得到 SH 錨定的 policy'
            '（讀不到＝member_read 那條的第三腿漏了，租戶的 Access 分頁全空）')
        self._assert_denied(
            self._rename_policy(self.sh_policy),
            'role=user 改得動 SH 的 Access policy'
            '⇒ 一般成員可以自行放寬 Cloudflare 的 allow/deny 閘門')
        self._assert_denied(
            self._create_policy(smart_home_route_id=self.route_w1.id),
            'role=user 建得了 SH 的 Access policy ⇒ 可自行新增一條 allow')
        self._assert_denied(
            self._unlink_policy(self.sh_policy),
            'role=user 刪得掉 SH 的 Access policy ⇒ 可自行拆掉別人設的限制')
        self.assertTrue(
            self.sh_policy.sudo().exists(),
            '被擋之後 policy 不得消失（「回 error 但其實刪掉了」）')

    def test_E4_guest_can_read_but_not_write(self):
        """guest 這一格是**跨列匹配**的判別式。

        把第三腿寫成 `smart_home_id.workspace_id.access_ids.role` 點鏈的話，
        W1 同時掛著 owner 那一列 ⇒ gary 會用**別人**那列的 role 過關，等於這條
        rule 完全沒有 role 條件。
        """
        self.authenticate(self.gary_login, self.gary_login)
        self._assert_ok(self._read_policy(self.sh_policy),
                        'guest 應該讀得到 SH 錨定的 policy')
        self._assert_denied(
            self._rename_policy(self.sh_policy),
            'guest 改得動 SH 的 Access policy ⇒ 第三腿的 role 條件形同虛設')
        self._assert_denied(
            self._create_policy(smart_home_route_id=self.route_w1.id),
            'guest 建得了 SH 的 Access policy ⇒ 第三腿的 role 條件形同虛設')

    # ---------------------------------------------------------------- #
    #  負向——跨 workspace                                                #
    # ---------------------------------------------------------------- #
    def test_E5_cross_workspace_read_is_denied_both_shapes(self):
        """W2 的 owner 對 W1 的 SH policy：`read` 與 `search_read` 都讀不到。

        兩種形狀是**不同的失敗模式**：`read` 被擋噴 `AccessError`，`search` 被
        rule 過濾則是**安靜地少幾列**。只測其中一種的話，一條「只掛
        `perm_read=False` 卻沒有 domain」之類的錯誤形態會讓另一條路徑繼續通。
        """
        self.authenticate(self.bob_login, self.bob_login)
        self._assert_empty_read(
            self._read_policy(self.sh_policy),
            '跨 workspace 用 read 拿得到 W1 的 SH Access policy')
        self._assert_empty_read(
            self._search_read_policy(self.sh_policy),
            '跨 workspace 用 search_read 枚舉得到 W1 的 SH Access policy')
        # 陽性對照：bob 對**自己** workspace 那筆讀得到——否則上面兩條可能只是
        # 「bob 對這個 model 什麼都讀不到」（第三腿整條壞掉也是這個輸出）。
        rows = self._assert_ok(
            self._search_read_policy(self.sh_policy_w2),
            'bob 讀不到自己 workspace 的 SH Access policy')
        self.assertTrue(
            rows,
            '陽性對照：bob 必須讀得到自己那筆'
            '（讀不到＝第三腿整條失效，上面兩條的綠燈毫無資訊）')

    def test_E6_cross_workspace_write_is_denied(self):
        self.authenticate(self.bob_login, self.bob_login)
        self._assert_denied(
            self._rename_policy(self.sh_policy),
            '跨 workspace 改得動 W1 的 SH Access policy')
        self.assertNotEqual(
            self.sh_policy.sudo().name, 'renamed-768',
            '被擋之後值不得改變（「回 error 但其實寫進去了」）')
        # 陽性對照：bob 對自己 workspace 那筆照樣改得動
        self._assert_ok(
            self._rename_policy(self.sh_policy_w2),
            'bob 對自己 workspace 的 SH policy 應該改得動')

    def test_E7_cannot_anchor_a_new_policy_on_someone_elses_sh_route(self):
        """把新 policy 錨在別人的 SH route 上 ⇒ 擋。

        create 方向的 domain 是對**新列**評估的（而第三腿走的是 stored related
        `smart_home_id`，那一格是 create 當下才算出來的）。這條證明第三腿在
        create 方向沒有寫成永真——永真的話任何人都能往別人家的智慧宅塞一條
        allow 規則。
        """
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_denied(
            self._create_policy(smart_home_route_id=self.route_w2.id),
            'W1 的 owner 把 policy 錨在 W2 的 SH route 上竟然成功')


class TestShAnchoredPolicyRuleAcl(_ShAccessAclFixture):
    """G 系列：selector rule 子模型必須與 parent 對等（第三腿的鏡射）。

    [[unbound-project-acl-dual-leg]] 明列「sub-model 必須對等改」。兩個方向都要
    測：漏改 ⇒ 租戶連自己那條 include 都建不出來（**fail-closed**，而錯誤訊息
    指的是子表的技術名，沒人會聯想到 parent 換了錨點）；改太寬 ⇒ guest 可以自己
    加一條 include 把受保護的 hostname 打開，而 parent 那層的 role 條件完全擋不到
    （它擋的是 policy 表，不是 rule 表）。
    """

    def test_G1_owner_can_create_a_selector_rule_on_sh_anchored_policy(self):
        """🔴 **這是「漏改子模型」唯一會紅的那一格。**

        子模型的 `smart_home_id` 若沒鏡射（或忘了 `store=True`），這裡回的是
        `AccessError`——而系統其餘部分完全正常，租戶只會回報「規則存不起來」。
        """
        self.authenticate(self.alice_login, self.alice_login)
        before = self.PolicyRule.sudo().search_count(
            [('policy_id', '=', self.sh_policy.id)])
        self._assert_ok(
            self._create_selector_rule(self.sh_policy),
            'owner 在 SH 錨定的 policy 底下建不了 selector rule '
            '（子模型的 smart_home_id 鏡射漏了？）')
        self.assertEqual(
            self.PolicyRule.sudo().search_count(
                [('policy_id', '=', self.sh_policy.id)]),
            before + 1,
            'RPC 回成功但列沒落地 ⇒ 測到的是「沒 error」而不是「真的建出來」')

    def test_G2_owner_can_write_and_unlink_a_selector_rule(self):
        """寫入方向的陽性對照（G4 的對照組）。"""
        self.authenticate(self.alice_login, self.alice_login)
        rule = self._exclude_rule(self.sh_policy)
        self._assert_ok(self._rename_selector_rule(rule),
                        'owner 改不動 SH 錨定 policy 的 selector rule')
        self._assert_ok(self._unlink_selector_rule(rule),
                        'owner 刪不掉 SH 錨定 policy 的 selector rule')
        self.assertFalse(
            rule.sudo().exists(),
            'RPC 回成功但列還在 ⇒ 測到的是「沒 error」而不是「真的刪掉」')

    def test_G3_member_user_can_read_rules_but_not_write_them(self):
        self.authenticate(self.carol_login, self.carol_login)
        rows = self._assert_ok(
            self._search_read_selector_rules(self.sh_policy),
            'role=user 應該讀得到 SH 錨定 policy 的 selector rule')
        self.assertTrue(
            rows,
            'role=user 讀到空集 ⇒ 子模型 member_read 的第三腿漏了'
            '（租戶的 Access 規則清單會整片空白）')
        rule = self._exclude_rule(self.sh_policy)
        self._assert_denied(
            self._rename_selector_rule(rule),
            'role=user 改得動 selector rule ⇒ 可繞過 parent 的 role 條件'
            '（parent 擋的是 policy 表，不是 rule 表）')
        self._assert_denied(
            self._unlink_selector_rule(rule),
            'role=user 刪得掉 selector rule ⇒ 可自行拆掉 exclude 限制')
        self.assertTrue(rule.sudo().exists(), '被擋之後列不得消失')

    def test_G4_guest_cannot_add_a_selector_rule(self):
        self.authenticate(self.gary_login, self.gary_login)
        self._assert_denied(
            self._create_selector_rule(self.sh_policy),
            'guest 在 SH 錨定的 policy 底下建得了 selector rule '
            '⇒ 可自行 include 自己、打開受保護的 hostname')

    def test_G5_cross_workspace_cannot_read_selector_rules(self):
        self.authenticate(self.bob_login, self.bob_login)
        self._assert_empty_read(
            self._search_read_selector_rules(self.sh_policy),
            '跨 workspace 讀得到 W1 的 SH policy selector rule')
        # 陽性對照：自己 workspace 的讀得到
        rows = self._assert_ok(
            self._search_read_selector_rules(self.sh_policy_w2),
            'bob 讀不到自己 workspace 的 selector rule')
        self.assertTrue(
            rows,
            '陽性對照：bob 必須讀得到自己那組'
            '（讀不到＝子模型第三腿整條失效，上面的空集毫無資訊）')


class TestSmartHomeRouteAcl(_ShAccessAclFixture):
    """R 系列：`smart_home_route` 這張表自己的 ACL ＋ ir.rule。

    這張表的切分與 policy 那兩張**不同**，兩層各擋不同的東西：

    * `ir.model.access`：`group_user` = `read=1, write=1, create=0, unlink=0`
      ——本階段沒有 route CRUD，路由由平台在 provision 當下建立、隨 SH 一起消失。
    * `ir.rule`：member 讀、owner／admin 寫（`smart_home_id.workspace_id` walk）。

    ⚠ create／unlink 那兩格刻意打在 **owner** 身上：owner 是租戶身分的上限，
    她被擋才證明「一律被拒」；打在 guest 身上的話，ACL 被改成 `create=1` 也不會
    紅（rule 的 role 條件會頂上去，變成量到另一層）。
    """

    # ---------------------------------------------------------------- #
    #  讀                                                                #
    # ---------------------------------------------------------------- #
    def test_R1_workspace_members_can_read_their_own_route(self):
        """owner／admin／user／guest 全體成員都讀得到（與父 model 同一組切分）。"""
        for login, label in ((self.alice_login, 'owner'),
                             (self.dave_login, 'admin'),
                             (self.carol_login, 'role=user'),
                             (self.gary_login, 'role=guest')):
            self.authenticate(login, login)
            rows = self._assert_ok(
                self._read_route(self.route_w1),
                '%s 讀不到自己 workspace 的 smart_home_route '
                '（Access 分頁會完全打不開）' % label)
            self.assertEqual(rows[0]['id'], self.route_w1.id)

    def test_R2_cross_workspace_read_is_denied_both_shapes(self):
        self.authenticate(self.bob_login, self.bob_login)
        self._assert_empty_read(
            self._read_route(self.route_w1),
            '跨 workspace 用 read 拿得到 W1 的 smart_home_route')
        self._assert_empty_read(
            self._search_read_route(self.route_w1),
            '跨 workspace 用 search_read 枚舉得到 W1 的 smart_home_route')
        # 陽性對照：bob 讀得到自己那條
        rows = self._assert_ok(
            self._search_read_route(self.route_w2),
            'bob 讀不到自己 workspace 的 smart_home_route')
        self.assertTrue(
            rows,
            '陽性對照：bob 必須讀得到自己那條'
            '（讀不到＝這兩條 rule 整組失效，上面的空集毫無資訊）')

    # ---------------------------------------------------------------- #
    #  寫（is_protected＝Cloudflare Access 的總開關）                     #
    # ---------------------------------------------------------------- #
    def test_R3_owner_and_admin_can_toggle_protection(self):
        """陽性對照：R4／R5／R6 的「被擋」不是因為租戶對這張表什麼都做不了。"""
        for login, label, value in ((self.alice_login, 'owner', True),
                                    (self.dave_login, 'admin', False)):
            self.authenticate(login, login)
            self._assert_ok(
                self._set_route_protected(self.route_w1, value),
                '%s 改不動自己 route 的 is_protected '
                '（owner_admin_manage 那條的 walk 寫錯了？）' % label)
            self.route_w1.invalidate_recordset()
            self.assertEqual(
                self.route_w1.sudo().is_protected, value,
                '%s 的寫入必須真的落地（不是被吞掉回 True）' % label)

    def test_R4_member_user_and_guest_cannot_toggle_protection(self):
        """一般成員關掉保護＝把智慧宅整台曝到公網，這一格是本表最貴的一格。"""
        for login, label in ((self.carol_login, 'role=user'),
                             (self.gary_login, 'role=guest')):
            self.authenticate(login, login)
            self._assert_denied(
                self._set_route_protected(self.route_w1, True),
                '%s 改得動 route 的 is_protected ⇒ 可自行開關 Access 閘門'
                % label)
            self.route_w1.invalidate_recordset()
            self.assertFalse(
                self.route_w1.sudo().is_protected,
                '%s 被擋之後值不得改變' % label)

    def test_R5_cross_workspace_write_is_denied(self):
        self.authenticate(self.bob_login, self.bob_login)
        self._assert_denied(
            self._set_route_protected(self.route_w1, True),
            '跨 workspace 改得動 W1 的 smart_home_route')
        # 陽性對照：bob 對自己那條改得動
        self._assert_ok(
            self._set_route_protected(self.route_w2, True),
            'bob 對自己 workspace 的 route 應該改得動')

    # ---------------------------------------------------------------- #
    #  建／刪：ACL 那一層（create=0 / unlink=0），對任何租戶身分一律拒     #
    # ---------------------------------------------------------------- #
    def test_R6_tenant_cannot_create_a_route_even_as_owner(self):
        """route 由平台在 provision 當下建立，租戶沒有 CRUD 入口。

        陽性對照在 R3（同一個 owner、同一張表，write 走得通）——沒有它，這裡的
        `AccessError` 與「owner 對 smart_home_route 整張表都沒權限」不可區分。
        """
        self.authenticate(self.alice_login, self.alice_login)
        before = self.ShRoute.sudo().search_count(
            [('smart_home_id', '=', self.sh1.id)])
        result = self._create_route(self.sh1)
        self._assert_denied(
            result, 'owner 用裸 RPC 建得出 smart_home_route')
        self.assertNotIn(
            'ValidationError', self._error_text(result),
            '歸因：擋下來的必須是 ACL 的 create=0，不是 primary 唯一性守衛'
            '（撞到後者代表這一格量錯了對象，ACL 被改成 create=1 也不會紅）')
        self.assertEqual(
            self.ShRoute.sudo().search_count(
                [('smart_home_id', '=', self.sh1.id)]),
            before,
            '被擋之後不得有任何 route 落地')

    def test_R7_tenant_cannot_unlink_a_route_even_as_owner(self):
        """打在**非 primary** 的那條上——primary 會先撞 `UserError`（不可刪），
        那是另一道守線，量它等於讓 ACL 的 `unlink=0` 永遠測不到。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._unlink_route(self.route_w1_extra)
        self._assert_denied(result, 'owner 用裸 RPC 刪得掉 smart_home_route')
        self.assertNotIn(
            'UserError', self._error_text(result),
            '歸因：擋下來的必須是 ACL 的 unlink=0，不是 primary 不可刪守衛')
        self.assertTrue(
            self.route_w1_extra.sudo().exists(),
            '被擋之後 route 不得消失（連同它的 Access app 會變成 CF 孤兒）')


class TestOtherAnchorsRegression(_ShAccessAclFixture):
    """F 系列：SA route 錨定與 CS 錨定那兩腿的回歸鎖。

    第三腿是在既有 domain 上動刀（四條 rule 各多一個 `'|'` 與一個 leaf）。
    `'|'` 的個數與 leaf 個數對不上時，Odoo **不會報錯**——多餘的 leaf 會被
    當成 AND 或被吃掉，症狀是既有租戶悄悄失去（或多出）權限。只測 SH 那一腿的
    話，把 SA 腿或 CS 腿寫壞會全綠。
    """

    def test_F1_owner_still_manages_sa_route_anchored_policy(self):
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_ok(self._read_policy(self.sa_policy),
                        'owner 讀不到 SA route 錨定的 policy（第一腿被吃掉了？）')
        self._assert_ok(self._rename_policy(self.sa_policy),
                        'owner 改不動 SA route 錨定的 policy')

    def test_F2_member_user_still_blocked_on_sa_route_anchored_policy(self):
        self.authenticate(self.carol_login, self.carol_login)
        self._assert_ok(self._read_policy(self.sa_policy),
                        'role=user 應該讀得到 SA route 錨定的 policy')
        self._assert_denied(
            self._rename_policy(self.sa_policy),
            'role=user 改得動 SA route 錨定的 policy '
            '⇒ 第一腿的 role 條件在加第三腿時被放寬了')

    def test_F3_owner_still_manages_cs_anchored_policy(self):
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_ok(self._read_policy(self.cs_policy),
                        'owner 讀不到 CS 錨定的 policy（第二腿被吃掉了？）')
        self._assert_ok(self._rename_policy(self.cs_policy),
                        'owner 改不動 CS 錨定的 policy')

    def test_F4_member_user_still_blocked_on_cs_anchored_policy(self):
        self.authenticate(self.carol_login, self.carol_login)
        self._assert_ok(self._read_policy(self.cs_policy),
                        'role=user 應該讀得到 CS 錨定的 policy')
        self._assert_denied(
            self._rename_policy(self.cs_policy),
            'role=user 改得動 CS 錨定的 policy '
            '⇒ 第二腿的 role 條件在加第三腿時被放寬了')

    def test_F5_cross_workspace_isolation_holds_on_the_other_two_anchors(self):
        """跨 workspace 那一格也要回歸：第三腿不得把隔離放寬。

        `'|'` 多接一腿最典型的寫壞法是把某一腿寫成永真（例如 leaf 打錯欄位名時
        Odoo 會 raise，但把 `('smart_home_id', '!=', False)` 這種寫上去不會）。
        那個世界裡 SH 那組全綠，露餡的只有這一格。
        """
        self.authenticate(self.bob_login, self.bob_login)
        self._assert_empty_read(
            self._search_read_policy(self.sa_policy),
            '跨 workspace 讀得到 W1 的 SA route 錨定 policy')
        self._assert_empty_read(
            self._search_read_policy(self.cs_policy),
            '跨 workspace 讀得到 W1 的 CS 錨定 policy')
        # 陽性對照：bob 對自己 workspace 的那筆讀得到（他只有 SH 錨定那一份）
        rows = self._assert_ok(
            self._search_read_policy(self.sh_policy_w2),
            'bob 讀不到自己 workspace 的 policy')
        self.assertTrue(
            rows,
            '陽性對照：bob 必須讀得到自己那筆'
            '（讀不到的話上面兩條空集只證明「bob 什麼都讀不到」）')

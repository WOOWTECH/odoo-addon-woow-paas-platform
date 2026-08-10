# -*- coding: utf-8 -*-
"""#556：`workspace_access` 授權重構——三層根因（ACL `unlink=0`、ir.rule 的
`create`/`write` 對 `base.group_user` 自我滿足、model 完全無
`create()`/`write()`/`unlink()` 守衛）讓非成員可經裸 ORM 自助加入、既有成員
可自我升級、admin 可經 0-owner 視窗把自己升成 owner；同時 `_remove_member`
端點今天本身就是壞的（ACL `unlink=0` 讓合法 owner 連清除入侵列都做不到）。

權威 spec：`/tmp/woow-556/analysis.md`（Opus 唯讀分析 + 方案 B 設計，本檔的
class/method 命名直接對應其 §5.2 攻擊向編號 A1/A1'/A2/A3/A4/A5 與 §5.3 陽性
對照編號 P1-P8）。有疑義以該檔為準，不在此重述設計理由的全文。

## TDD 紀律（analysis.md §5，一字不可省）

- **全部走真 HTTP RPC**：ORM 層攻擊（A1/A1'/A2/A3/A4）走
  `/web/dataset/call_kw`；controller 層（A5/P1-P4/P6-P7）走
  `/api/workspaces/...`；P8 走 `/api/ha-paas/provision`。**禁止
  `TransactionCase`**——它預設 `su=True`，ir.rule 不套用、model guard 的
  deny 分支結構上不可達，恆綠且與修復好壞無關。
- fixture user 一律只掛 `base.group_user`（Alice/Bob/Carol/Mallory/Dave）。
  P7 是刻意的例外：它測的正是 `group_system` 的 admin bypass 通道本身，
  必須用 `authenticate('admin','admin')`（uid 2、`group_system`、非
  superuser）。混用會讓 guard 的 `has_group('base.group_system')` 豁免
  對租戶攻擊向零鑑別力。

## 🔴 A1 的 payload 不可被「對齊」成 A1'

`A1`（Mallory 對 W1 裸 ORM `create({..., 'role': 'admin'})`）是本組測試的
**鑑別關鍵**：純 ir.rule 重構（analysis.md 方案 A，未補 model guard）擋得住
`role='user'`／省略 role（A1'）但擋不住 `role='admin'`——`create` 的 rule
檢查發生在 INSERT **之後**（`odoo/models.py:5292`），`'any'` 子條件被攻擊者
剛插入的那一列自己滿足（EXISTS 現讀）。若有人「順手把 A1 的 payload 對齊
A1'」，整組測試對 #556 核心攻擊向就會歸零，而清單看起來仍是全綠（analysis.md
§4 理由 1、§0.1）。**不可修改 A1 的 role 值，也不可刪掉 A1' 這組對照。**

## write-pivot（獨立驗收 verify556 找到的殘留洞，A1 的第二條 payload）

`create` 方向的自助加入（A1）關掉了，但 `write` 方向留了一條等價繞道：任何
`base.group_user` 都能先合法自建一個 workspace（P6 設計行為，她在那裡是
owner），再把自己那列 access row 用
`write([row_id], {'workspace_id': 受害workspace, 'role': 'admin'})` **搬進**
受害 workspace——ir.rule 的 write 檢查只查 **pre-write** 狀態（她在來源 ws
確實是 owner，放行）、寫完不重查目的地；`_check_assignable_role` 原本也只用
`rec.workspace_id.id`（**來源**、非 vals 裡的目的地）判定。`TestWorkspace
AccessWritePivotGuard` 是這個洞的永久回歸測試，改寫自 verify556 的探針
（`/tmp/wt556v/probe/test_v556_pivot.py`）。修法採 identity anchor：
`workspace_id`／`user_id` 建立後不可變（非 superuser 且非 group_system 不得
在 write 中改變這兩個欄位的值），因為沒有任何合法租戶流程需要重指派一列
access row 屬於哪個 workspace／哪個 user。
"""
import json
import uuid
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_OWNER,
    ROLE_USER,
)


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。

    刻意不用 `admin`／uid 2：guard 的 `has_group('base.group_system')` 豁免
    會讓那個身分對本檔要驗的租戶攻擊向零鑑別力（P7 是唯一刻意的例外）。
    """
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 556' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _WorkspaceAccess556Fixture(HttpCase):
    """W1（Alice owner、Carol role=user 成員）＋ W2（Bob owner）＋ Mallory
    （不屬任何 workspace）。四人皆只掛 `base.group_user`。
    """

    def setUp(self):
        super().setUp()
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Workspace = self.env['woow_paas_platform.workspace']

        self.alice, self.alice_login = _new_group_user(self.env, 'alice556')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob556')
        self.mallory, self.mallory_login = _new_group_user(self.env, 'mallory556')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol556')

        for u in (self.alice, self.bob, self.mallory, self.carol):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        # setUp 走 HttpCase 預設的 superuser env——workspace.create() 的
        # owner-access bootstrap（陷阱2）在這裡本來就不受本 issue 影響，
        # 用它單純只是為了搭 fixture，不是本檔要驗的路徑。
        self.ws1 = self.Workspace.create({'name': 'WS1-556', 'owner_id': self.alice.id})
        self.ws2 = self.Workspace.create({'name': 'WS2-556', 'owner_id': self.bob.id})
        self.alice_access = self.WorkspaceAccess.search([
            ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.alice.id)])
        self.bob_access = self.WorkspaceAccess.search([
            ('workspace_id', '=', self.ws2.id), ('user_id', '=', self.bob.id)])
        self.carol_access = self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws1.id, 'user_id': self.carol.id, 'role': ROLE_USER})

    # ------------------------------------------------------------------ #
    #  使用者唯一入口：真 HTTP JSON-RPC                                    #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊（create/write/unlink/read）
        必須走這條真實 RPC 路徑，不可用 `TransactionCase` + `with_user()`
        模擬（見檔頭 TDD 紀律）。"""
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

    def _post_raw(self, url, params):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
        resp = self.url_open(url, data=body, headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _post_api(self, url, params):
        parsed = self._post_raw(url, params)
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    # ------------------------------------------------------------------ #
    #  斷言 helper                                                        #
    # ------------------------------------------------------------------ #
    def _assert_access_error(self, result, msg):
        self.assertIn('error', result, msg + '（回 %r）' % result)
        name = result['error'].get('data', {}).get('name', '')
        self.assertIn('AccessError', name,
                      msg + '（期望 AccessError，回 %r）' % result)

    def _assert_call_kw_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)


@tagged('post_install', '-at_install')
class TestWorkspaceAccessAttackVectors(_WorkspaceAccess556Fixture):
    """analysis.md §5.2：A1/A1'/A2/A3/A4 必須 raise AccessError；A5 是既有
    功能性 bug（`_remove_member` 對非 superuser 必拋框架層錯誤）的正向修復。
    """

    # ---------------------------------------------------------------- #
    #  A1 — 非成員自助加入（核心鑑別測試，見檔頭警告）                      #
    # ---------------------------------------------------------------- #
    def test_A1_non_member_self_join_as_admin_must_be_denied(self):
        """🔴 role 必須是 'admin'——不可換成 'user'，理由見檔頭。

        pre-fix RED：`workspace_access_rule_create` 是 `[(1,'=',1)]` 全開、
        ACL create=1、model 無任何守衛 ⇒ Mallory 直接拿到 W1 的 admin 列。
        post-fix GREEN：model guard `_check_assignable_role` 查 Mallory 在
        W1 沒有任何既有 access 列（grantor_role=None）⇒ allowed=[] ⇒
        `AccessError`，且發生在 `super().create()`（INSERT）**之前**——單靠
        ir.rule（方案 A）擋不住這個 payload，因為 rule 的 create 檢查在
        INSERT 之後才跑，`'any'` 子條件被攻擊者剛插入的那一列自己滿足。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': self.mallory.id,
              'role': ROLE_ADMIN}])
        self._assert_access_error(
            result, 'Mallory 不是 W1 成員，不得以 role=admin 自助加入')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', self.ws1.id),
                ('user_id', '=', self.mallory.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    def test_A1_prime_weak_payload_control_role_user(self):
        """A1' 對照（弱 payload，role='user'）——保留來證明 A1 不是靠 role
        值碰巧擋住：純 ir.rule 重構（方案 A）本來就擋得住這個弱 payload，
        擋不住的是 A1 的 'admin' payload。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': self.mallory.id,
              'role': ROLE_USER}])
        self._assert_access_error(
            result, 'Mallory 不是 W1 成員，role=user 弱 payload 也不得放行')

    def test_A1_prime_weak_payload_control_role_omitted(self):
        """A1' 對照（省略 role，落到 Selection default 'user'）——同上。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': self.mallory.id}])
        self._assert_access_error(
            result, 'Mallory 省略 role（落到 default user）也不得放行')

    # ---------------------------------------------------------------- #
    #  A2 — 既有 user 成員自我升級                                       #
    # ---------------------------------------------------------------- #
    def test_A2_existing_user_member_self_escalation_denied(self):
        """Carol（role=user）把自己那列改成 admin。

        pre-fix RED：`workspace_access_rule_member_access` 的 domain 完全
        沒有 role 條件，只要求「是該 ws 的成員」——任何成員可 write 該 ws
        任何一列（含自己）的 role。
        post-fix GREEN：新 rule 的 domain 以**呼叫者的既有角色**判定（pre-
        write 語意在這個方向對防守方有利：改動前 Carol 仍是 user，不在
        [owner,admin] ⇒ manage rule 不命中）；model guard 的
        `_check_assignable_role` 對呼叫者是 user 的情況 `allowed=[]`，同樣
        擋下。
        """
        self.authenticate(self.carol_login, self.carol_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.carol_access.id], {'role': ROLE_ADMIN}])
        self._assert_access_error(result, 'Carol（role=user）不得自我升級成 admin')
        self.carol_access.invalidate_recordset()
        self.assertEqual(self.carol_access.role, ROLE_USER, '拒絕後 role 不得變動')

    # ---------------------------------------------------------------- #
    #  A3 — 第三方植入                                                   #
    # ---------------------------------------------------------------- #
    def test_A3_third_party_injection_existing_user_denied(self):
        """Mallory 把 Bob（W2 owner、與 W1 無關）植入 W1 當 admin。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': self.bob.id,
              'role': ROLE_ADMIN}])
        self._assert_access_error(result, 'Mallory 不得把 Bob 植入 W1')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.bob.id)]),
            '拒絕後 Bob 在 W1 不得有任何 access 列')

    def test_A3_third_party_injection_reserved_uid_1_denied(self):
        """Mallory 把 uid 1（`__system__`／OdooBot）植入 W1——目標
        `user_id` 是誰不影響判定，guard 只看呼叫者自己在該 ws 的既有角色。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': 1, 'role': ROLE_ADMIN}])
        self._assert_access_error(result, 'Mallory 不得把 uid 1 植入 W1')

    # ---------------------------------------------------------------- #
    #  A4 — admin 降級 owner → 自我升 owner（方案 B 才關得掉）             #
    # ---------------------------------------------------------------- #
    def test_A4_admin_cannot_demote_owner_row(self):
        """admin 降級 owner → 0-owner 視窗 → 自我升 owner 兩步鏈的第一步。

        純 ir.rule 重構（方案 A）擋不住：manage rule 只看**呼叫者**在
        pre-write 當下是否 owner/admin——Carol（已被升為 admin）對 Alice
        的 owner 列一樣放行，rule 從不管**目標列**的角色。只有 model guard
        的『非 owner 呼叫者不得動 owner 列』（B2）才擋得住這一步。
        """
        self.carol_access.sudo().write({'role': ROLE_ADMIN})
        self.authenticate(self.carol_login, self.carol_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.alice_access.id], {'role': ROLE_USER}])
        self._assert_access_error(
            result,
            'Carol（admin）不得把 Alice 的 owner 列降級——0-owner 視窗鏈的第一步')
        self.alice_access.invalidate_recordset()
        self.assertEqual(
            self.alice_access.role, ROLE_OWNER,
            '拒絕後 Alice 仍必須是 owner——不得留下 0-owner 視窗')

    def test_A4_admin_cannot_unlink_owner_row(self):
        """同一防護的 unlink 方向：非 owner 不得 unlink 一列 role==owner
        的記錄（若能刪掉 owner 列，同樣會製造 0-owner 視窗）。"""
        self.carol_access.sudo().write({'role': ROLE_ADMIN})
        self.authenticate(self.carol_login, self.carol_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'unlink',
            [[self.alice_access.id]])
        self._assert_access_error(result, 'Carol（admin）不得 unlink Alice 的 owner 列')
        self.assertTrue(self.alice_access.exists(), '拒絕後 Alice 的 owner 列必須仍在')

    # ---------------------------------------------------------------- #
    #  A5 — `_remove_member` 今天本身是壞的；修復後才是正向清除入侵列       #
    # ---------------------------------------------------------------- #
    def test_A5_owner_can_clear_an_injected_row_via_remove_member_endpoint(self):
        """`_remove_member`（`paas.py:826-872`）對任何非 superuser 呼叫者
        今天必拋**框架層** `AccessError`（ACL `unlink=0`，該行外無
        try/except）——不是「回 success:False」，是連 JSON-RPC envelope 都
        沒有 `result` 只有 `error`。

        pre-fix RED：即使 Alice 是合法 owner，也連清除一列 sudo 植入的入侵
        access 都做不到。
        post-fix GREEN：ACL unlink=1（C4）＋
        `workspace_access_rule_owner_admin_manage`（perm_unlink=True，owner
        呼叫者命中）放行；目標列 role='user'（非 owner）不觸發 B2 的
        owner-列保護。
        """
        injected = self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws1.id, 'user_id': self.mallory.id,
            'role': ROLE_USER})
        self.authenticate(self.alice_login, self.alice_login)
        parsed = self._post_raw(
            '/api/workspaces/%s/members/%s' % (self.ws1.id, injected.id),
            {'action': 'remove'})
        self.assertIn(
            'result', parsed,
            '_remove_member 修復後必須回 JSON-RPC result（不是框架層 error）'
            '（回 %r）' % parsed)
        result = parsed['result']
        self.assertTrue(result.get('success'),
                        'owner 移除入侵列應成功（回 %r）' % result)
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', self.ws1.id),
                ('user_id', '=', self.mallory.id)]),
            '成功回應後該列必須真的消失')


@tagged('post_install', '-at_install')
class TestWorkspaceAccessPositiveControls(_WorkspaceAccess556Fixture):
    """analysis.md §5.3 P1-P7：防 over-correction——修復不得誤傷既有合法流程。
    """

    def test_P1_owner_can_invite_user_and_admin(self):
        self.authenticate(self.alice_login, self.alice_login)
        for role in (ROLE_USER, ROLE_ADMIN):
            invitee, _ = _new_group_user(self.env, 'p1invitee%s556' % role)
            result = self._post_api('/api/workspaces/%s/members' % self.ws1.id, {
                'action': 'invite', 'email': invitee.email, 'role': role})
            self.assertTrue(
                result.get('success'),
                'owner 邀請 role=%s 應成功（回 %r）' % (role, result))

    def test_P2_admin_can_invite_admin(self):
        """鎖 analysis.md §3 方案 B 偏離#1：`ASSIGNABLE_ROLES_BY_ROLE[admin]`
        含 'admin'——workspace_access 刻意偏離 project_access 樣板之處（保
        現行 controller『admin 可邀 admin』行為），不可被『對齊
        project_access』改掉。"""
        self.carol_access.sudo().write({'role': ROLE_ADMIN})
        self.authenticate(self.carol_login, self.carol_login)
        invitee, _ = _new_group_user(self.env, 'p2invitee556')
        result = self._post_api('/api/workspaces/%s/members' % self.ws1.id, {
            'action': 'invite', 'email': invitee.email, 'role': ROLE_ADMIN})
        self.assertTrue(result.get('success'),
                        'admin 邀請 role=admin 應成功（回 %r）' % result)

    def test_P3_owner_can_update_role_and_remove_non_owner_member(self):
        self.authenticate(self.alice_login, self.alice_login)
        result = self._post_api(
            '/api/workspaces/%s/members/%s' % (self.ws1.id, self.carol_access.id),
            {'action': 'update_role', 'role': ROLE_GUEST})
        self.assertTrue(result.get('success'), 'owner 改成員 role 應成功（回 %r）' % result)
        self.carol_access.invalidate_recordset()
        self.assertEqual(self.carol_access.role, ROLE_GUEST)

        result = self._post_api(
            '/api/workspaces/%s/members/%s' % (self.ws1.id, self.carol_access.id),
            {'action': 'remove'})
        self.assertTrue(result.get('success'),
                        'owner 移除非 owner 成員應成功（回 %r）' % result)
        self.assertFalse(self.carol_access.exists())

    def test_P4_user_member_can_still_list_members(self):
        self.authenticate(self.carol_login, self.carol_login)
        result = self._post_api('/api/workspaces/%s/members' % self.ws1.id,
                                {'action': 'list'})
        self.assertTrue(result.get('success'), result)
        emails = {m['email'] for m in result['data']}
        self.assertIn(self.alice.email, emails)
        self.assertIn(self.carol.email, emails)

    def test_P5_user_member_can_still_read_cloud_service(self):
        """`workspace_rule_...`（cloud_service 側的既有 ir.rule）與本次修改
        無關，不該被誤傷。"""
        template = self.env.ref('woow_paas_platform.cloud_app_odoo')
        service = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': 'p5-svc-556', 'workspace_id': self.ws1.id,
            'template_id': template.id,
            'helm_release_name': 'p5-556', 'helm_namespace': 'paas-ws-p5-556'})
        self.authenticate(self.carol_login, self.carol_login)
        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'read', [[service.id], ['name']])
        self._assert_call_kw_ok(
            result, 'Carol（W1 成員）讀 W1 的 cloud_service 不應受本次修改影響')

    def test_P6_fresh_group_user_can_bootstrap_own_workspace(self):
        """鎖 C6 的 `.sudo()` bootstrap：Plan B 收緊 create 方向的規則後，
        `workspace.py:81` 若不加 `.sudo()`，全新使用者對『自己那個全新
        workspace』完全沒有任何既有 access 列（grantor=None）⇒ model guard
        會把自己的 owner-bootstrap 一起擋死（analysis.md 陷阱2 的雞生蛋）。
        對照既有 `test_workspace_create_rollback_525.py:140-152`——那條用
        `admin`／`group_system` 身分跑，鎖不住這裡（group_system 直接
        bypass guard，測不到 sudo 有沒有補上）。
        """
        dave, dave_login = _new_group_user(self.env, 'dave556')
        self.authenticate(dave_login, dave_login)
        result = self._post_api('/api/workspaces', {
            'action': 'create', 'name': 'Dave WS 556'})
        self.assertTrue(result.get('success'),
                        'group_user 建立 workspace 應成功（回 %r）' % result)
        ws_id = result['data']['id']
        rows = self.WorkspaceAccess.sudo().search([('workspace_id', '=', ws_id)])
        self.assertEqual(
            rows.mapped('role'), [ROLE_OWNER],
            '新建 workspace 必須恰產生一列 owner access（回 %r）' % rows.mapped('role'))
        self.assertEqual(rows.user_id.id, dave.id)

    def test_P7_group_system_admin_has_full_crud_on_foreign_workspace(self):
        """鎖 C5 的 ACL admin 列 ＋ `workspace_access_rule_admin_full`：
        `authenticate('admin','admin')`（uid 2、`group_system`、非
        superuser）對 W1（admin 既非成員也非 owner）的 access 列必須有完整
        CRUD。缺任一則既有 `test_invite_member_rollback_525.py` 也會紅
        （它同樣以 admin 身分跑，只是剛好每次都在自己是 owner 的 workspace
        內操作，測不到這條 group_system bypass）。
        """
        self.authenticate('admin', 'admin')

        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'read',
            [[self.alice_access.id], ['role']])
        self._assert_call_kw_ok(result, 'admin 應能 read 非自家 workspace 的 access 列')

        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.carol_access.id], {'role': ROLE_GUEST}])
        self._assert_call_kw_ok(result, 'admin 應能 write 非自家 workspace 的 access 列')

        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': self.bob.id, 'role': ROLE_USER}])
        self._assert_call_kw_ok(result, 'admin 應能 create 非自家 workspace 的 access 列')
        new_id = result['result']

        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'unlink', [[new_id]])
        self._assert_call_kw_ok(result, 'admin 應能 unlink 非自家 workspace 的 access 列')


@tagged('post_install', '-at_install')
class TestWorkspaceAccessHaPaasBootstrap(HttpCase):
    """P8：HA PaaS provision（`ha_paas.py`，全程 `.sudo()` 的 server-
    authoritative 路徑）修復後仍能開通並建出 owner access 列。Fixture 抄自
    `test_ha_paas_provision.py` 的 token/scope 建法（同套 oauth_client）。
    """

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'name': 'P8 Prov User 556', 'login': 'p8prov556@test',
            'email': 'p8prov556@test'})
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_app')
        self.token = self.env['woow_paas_platform.oauth_token'].create({
            'access_token': 'ha-provision-test-token-556', 'user_id': self.user.id,
            'client_id': self.client.id, 'scope': 'ha:provision',
            'expires_at': fields.Datetime.now() + timedelta(hours=1)})

    def test_P8_ha_paas_provision_still_creates_owner_access_row(self):
        resp = self.url_open(
            '/api/ha-paas/provision', data='{}',
            headers={'Authorization': 'Bearer ha-provision-test-token-556',
                     'Content-Type': 'application/json'})
        self.assertEqual(resp.status_code, 202, resp.text[:300])
        WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        rows = WorkspaceAccess.sudo().search([
            ('user_id', '=', self.user.id), ('role', '=', ROLE_OWNER)])
        self.assertTrue(rows, 'HA PaaS provision 必須仍能建出 owner access 列')


@tagged('post_install', '-at_install')
class TestWorkspaceAccessWritePivotGuard(_WorkspaceAccess556Fixture):
    """write-pivot（見檔頭「write-pivot」段）——A1 的第二條 payload，由獨立
    驗收 agent（verify556）發現，analysis.md 與最初的攻擊向清單都沒列到。

    `workspace_id`／`user_id` 是建立後不可變的 identity anchor：任何
    `base.group_user` 都能合法自建 workspace（P6）成為 owner，若允許她把
    自己那列 access row 的 `workspace_id` 改指到受害 workspace，等同繞過
    A1 擋下的 create 方向、從 write 方向重新拿到同一個結果。
    """

    def _own_ws_as_mallory(self, name):
        """Mallory 用完全合法的 P6 路徑自建 workspace，取得 owner 列。"""
        result = self._post_api('/api/workspaces', {'action': 'create', 'name': name})
        self.assertTrue(result.get('success'), 'P6 自建 workspace 應成功（回 %r）' % result)
        ws_id = result['data']['id']
        row = self.WorkspaceAccess.sudo().search([
            ('workspace_id', '=', ws_id), ('user_id', '=', self.mallory.id)])
        self.assertEqual(row.role, ROLE_OWNER, 'fixture 前提：Mallory 是自己 ws 的 owner')
        return ws_id, row

    def test_pivot_self_owned_row_write_into_victim_workspace_denied(self):
        """一步 pivot：`write({'workspace_id': W1, 'role': 'admin'})`——
        改前 guard 的 `_check_assignable_role` 用 `rec.workspace_id.id`
        （來源 ws，Mallory 在那裡是 owner）判定，角色檢查整個問錯了
        workspace；ir.rule 的 write 檢查同樣只查 pre-write（來源）狀態。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        _ws_id, row = self._own_ws_as_mallory('Mallory WS pivot-1')
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[row.id], {'workspace_id': self.ws1.id, 'role': ROLE_ADMIN}])
        self._assert_access_error(
            result, 'Mallory 不得把自己的 owner 列 pivot 進 W1 並升級成 admin')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.mallory.id)]),
            'pivot 被拒後，Mallory 在 W1 不得有任何 access 列')

    def test_pivot_third_party_row_write_into_victim_workspace_denied(self):
        """A3 的 pivot 繞道版：Mallory 在自己的 ws 建一列給 Bob（第三方），
        再把那一列 write 搬進 W1——只改 `workspace_id`，不改 `role`。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        ws_id, _row = self._own_ws_as_mallory('Mallory WS pivot-2')
        create_result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': ws_id, 'user_id': self.bob.id, 'role': ROLE_ADMIN}])
        self._assert_call_kw_ok(
            create_result, 'Mallory 在自己的 ws 建一列給 Bob 應成功（fixture 前提）')
        bob_row_id = create_result['result']
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[bob_row_id], {'workspace_id': self.ws1.id}])
        self._assert_access_error(result, 'Mallory 不得把 Bob 的列 pivot 進 W1')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.bob.id)]),
            'pivot 被拒後，Bob 在 W1 不得有任何 access 列')

    def test_pivot_user_id_reassignment_denied(self):
        """`user_id` 是另一半 identity anchor：Mallory 把自己 ws 裡的 owner
        列直接 write `user_id` 改成 Bob——若通過，Bob 會在完全沒經過 invite
        /transfer_ownership 的情況下無聲變成該 ws 的 owner。與 workspace_id
        方向對稱，同一道 guard 守。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        _ws_id, row = self._own_ws_as_mallory('Mallory WS pivot-3')
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[row.id], {'user_id': self.bob.id}])
        self._assert_access_error(result, 'Mallory 不得把自己的列 user_id 改指向 Bob')
        row.invalidate_recordset()
        self.assertEqual(row.user_id.id, self.mallory.id, '拒絕後 user_id 不得變動')

    def test_pivot_admin_group_system_can_still_repoint_anchors(self):
        """陽性對照：`group_system` bypass 通道必須維持暢通——identity
        anchor guard 不可誤傷既有的 group_system 全權維護路徑（鎖 C5／
        `workspace_access_rule_admin_full` 的既有語意，防 over-correction）。
        """
        self.authenticate('admin', 'admin')
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.carol_access.id], {'workspace_id': self.ws2.id}])
        self._assert_call_kw_ok(
            result, 'group_system 呼叫者 repoint workspace_id 不應被 identity anchor guard 誤擋')

    def test_pivot_control_direct_create_still_denied(self):
        """陽性對照：同一個 Mallory 直接 create 進 W1 依然被擋（與 A1 同一
        條斷言，這裡重申只是為了證明本測試組的 fixture／身分沒有壞掉——
        如果連這條都綠，代表壞的是 fixture 不是 pivot guard 生效）。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': self.mallory.id, 'role': ROLE_ADMIN}])
        self._assert_access_error(
            result, 'FIXTURE 前提：直接 create 進 W1 必須仍被擋（不是本測試組的重點，是陽性對照）')

# -*- coding: utf-8 -*-
"""#657：`project_access`（專案成員 ACL）補三個 model 層授權缺口——照抄
`woow_paas_platform.workspace_access` 的 #556 樣板（`test_workspace_access_authz_556.py`），
但 gap 2 的修法與 issue #657 本文寫的不同：不是 `vals.get('role') or ROLE_USER`，而是用 Odoo
自己的 `_add_missing_default_values(vals)` 一次解析全欄位（見
`src/models/project_access.py::ProjectAccess.create`）。

## 第二輪修訂（PR #666 留言 6583，verify657 獨立對抗式覆核）

第一輪的 gap 2 修法只把 `role` 用手動重演 Odoo 解析順序的方式補齊，`project_id` 半邊仍是
raw `vals.get('project_id')`——攻擊者只要把 `project_id` 也放進 context 的
`default_project_id`，整道守衛照樣被跳過（verify657 稱為 **H1**，並在本分支上實測完成
admin → owner 的完整提權鏈）。本檔新增 H1a/H1b/H1c 三種 vals/context 分佈形態的攻擊測試
＋單一差異變數對照（見 `test_X4_prime_admin_explicit_role_owner_denied_control`）。

同一輪還發現／修正：
- **`user_id` 也要錨定**（X3b、X9）：原本排除 `user_id` 的理由（「它是 stored compute，
  不是身分輸入」）被實測推翻——沒有 `inverse` 的 stored compute 照樣可被公開 `write()`
  改動，而且 `user_id` 才是本模組每一條 ir.rule 與 grantor lookup 實際使用的授權鍵，
  `partner_id` 只是顯示欄位。
- **X8 的 cmd4 半邊對本 PR 的守衛零鑑別力**（develop 上也是 ir.rule 擋的）——已移除，改成
  帶可達性陽性對照的 o2m cmd0/1/2/5 battery（`test_X8_*`），先證明攻擊者（P1 admin）真的
  driveable 得了 `project.project` 這個 o2m 通道（需先被加為 follower——`project_
  rule_workspace_member` 對 unbound project 只認 follower／create_uid，不認
  `project_access` 成員資格），再打攻擊 payload。
- **P6 的用詞更正**：`HttpCase` 的 `self.env` 本身就是 uid=1，直接 `.sudo()` 是 no-op，
  測到的其實是「本來就是 superuser」而非「`.sudo()` 這個切換動作」；改用
  `with_user(mallory).sudo()` 才是真正驗到 `env.is_superuser()` 分支。

## TDD 紀律（照抄 #556，一字不可省）

- **全部走真 HTTP RPC**：`/web/dataset/call_kw`。**禁止 `TransactionCase`**——它預設
  `su=True`，ir.rule 不套用、model guard 的 deny 分支結構上不可達，恆綠且與修復好壞
  無關。本檔僅一條例外走直呼 ORM（`test_P6_env_is_superuser_exemption_unrestricted`），
  且該條測的正是 `env.is_superuser()` 這個 HTTP 結構上不可能觸發的豁免分支本身，不是
  假綠（第一輪的 `test_X8_prime_o2m_command4_shape_is_effective_control` 已移除，見上方
  「第二輪修訂」）。
- fixture user 一律只掛 `base.group_user`（alice/bob/carol/dave/mallory/eve）。
  P5 是刻意的例外：它測的正是 `group_system` 的 admin bypass 通道本身，必須用
  `authenticate('admin','admin')`（uid 2、`group_system`、非 superuser）。

## 三個缺口與本檔對應

- **Gap 1**（identity anchor）：`project_id`/`partner_id`/`user_id` 建立後不可變。X1
  （自己那列 pivot：project_id + role）、X2（第三方列 pivot：只改 project_id）、X3
  （partner_id 重指派）、X3b（user_id 重指派）、X7（`copy()` 繞道，底層走 `create()`）、
  X8（`project.project` 的 o2m `project_access_ids` inverse command 繞道，帶可達性
  陽性對照）、X9（admin 用 user_id 重指派鎖住真 owner 的完整攻擊鏈）。
- **Gap 2**（role／project_id 缺省整道守衛被跳過）：X4（`with_context(default_role=
  'owner')` + vals 不放 role，攻擊者是 admin）、X4'（對照組，vals 明寫 `role='owner'`）、
  H1a/H1b/H1c（三種 vals/context 分佈形態，見上方「第二輪修訂」）。
- **Gap 3**（無 owner 列保護）：X5（admin 降級 owner 列）、X6（admin unlink owner
  列，斷言拿到的是 **AccessError** 不是 ValidationError）。

## 陽性對照（防 over-correction）

P1（owner 邀請 user/guest）、P2（admin 邀請 user 成功／邀請 admin 必須被拒）、P3（owner
改角色／移除非 owner 成員）、P4（owner 對自己那列送出 no-op anchor 值＋改 role 不被
誤擋）、P-BOOT（乾淨 `base.group_user` 自建 project 仍拿到 owner 列）、P5（`group_system`
全權 CRUD 含改 anchor）、P6（`env.is_superuser()` 豁免不受任何新 guard 影響）、P7（user_id
compute 在 create() 當下仍正確填上，anchor guard 沒打壞它）。
"""
import uuid

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.project_access import (
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_OWNER,
    ROLE_USER,
)

import json


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。

    刻意不用 `admin`／uid 2：guard 的 `has_group('base.group_system')` 豁免會讓那個
    身分對本檔要驗的租戶攻擊向零鑑別力（P5 是唯一刻意的例外）。
    """
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 657' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _ProjectAccess657Fixture(HttpCase):
    """P1（alice owner、carol role=user、dave role=admin）＋ P2（bob owner）＋
    mallory（不屬任何 project）＋ eve（第三方／共犯，不屬任何 project）。六人皆只掛
    `base.group_user`。
    """

    def setUp(self):
        super().setUp()
        self.ProjectAccess = self.env['woow_paas_platform.project_access']
        self.Project = self.env['project.project']

        self.alice, self.alice_login = _new_group_user(self.env, 'alice657')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob657')
        self.mallory, self.mallory_login = _new_group_user(self.env, 'mallory657')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol657')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave657')
        self.eve, self.eve_login = _new_group_user(self.env, 'eve657')

        for u in (self.alice, self.bob, self.mallory, self.carol, self.dave, self.eve):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        # 用 `with_user(alice)` 而非 SUPERUSER + context hint 建立：
        # `project_project.py::create()` 的 SUPERUSER 分支只認
        # `bootstrap_owner_partner_id` context hint，但那樣建出來的
        # `project.project` 記錄 `create_uid` 會是 uid=1（superuser），不是
        # alice——`project_rule_workspace_member` 對 unbound project 的 ir.rule
        # 只認 follower 或 `create_uid`，不認 `project_access` 成員資格，
        # 所以那樣建的話 alice 雖然是 project_access 的 owner，卻連 P1 這筆
        # `project.project` 記錄本身都讀不到（第二輪修訂踩到，見
        # `test_X8_reachability_positive_controls` 的可達性設計）。
        # `with_user(alice)` 落入 create() 的 ELSE 分支（`self.env.user.
        # partner_id`），效果相同（她一樣拿到 project_access 的 owner 列）但
        # `create_uid` 正確地是她自己，之後才能被加為 follower／驗證可達性。
        self.p1 = self.Project.with_user(self.alice).create({'name': 'P1-657'})
        self.p2 = self.Project.with_user(self.bob).create({'name': 'P2-657'})
        self.alice_access = self.ProjectAccess.sudo().search([
            ('project_id', '=', self.p1.id), ('partner_id', '=', self.alice.partner_id.id)])
        self.bob_access = self.ProjectAccess.sudo().search([
            ('project_id', '=', self.p2.id), ('partner_id', '=', self.bob.partner_id.id)])
        self.assertEqual(self.alice_access.role, ROLE_OWNER, 'fixture 前提：alice 是 P1 owner')
        self.assertEqual(self.bob_access.role, ROLE_OWNER, 'fixture 前提：bob 是 P2 owner')
        self.carol_access = self.ProjectAccess.sudo().create({
            'project_id': self.p1.id, 'partner_id': self.carol.partner_id.id, 'role': ROLE_USER})
        self.dave_access = self.ProjectAccess.sudo().create({
            'project_id': self.p1.id, 'partner_id': self.dave.partner_id.id, 'role': ROLE_ADMIN})

    # ------------------------------------------------------------------ #
    #  使用者唯一入口：真 HTTP JSON-RPC                                    #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊（create/write/unlink/read/copy）必須
        走這條真實 RPC 路徑，不可用 `TransactionCase` + `with_user()` 模擬（見檔頭
        TDD 紀律）。"""
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

    def _own_project_as(self, user, name):
        """`user` 用完全合法的 P-BOOT 路徑自建 project，取得 owner 列。呼叫前
        caller 必須已 `self.authenticate(user 對應的 login, ...)`。"""
        result = self._call_kw('project.project', 'create', [{'name': name}])
        self._assert_call_kw_ok(
            result, '%s 自建 project 應成功（回 %r）' % (user.login, result))
        project_id = result['result']
        row = self.ProjectAccess.sudo().search([
            ('project_id', '=', project_id), ('partner_id', '=', user.partner_id.id)])
        self.assertEqual(
            row.role, ROLE_OWNER, 'fixture 前提：%s 是自己 project 的 owner' % user.login)
        return project_id, row

    def _subscribe_dave_to_p1(self):
        """o2m 攻擊測試的可達性前提（PR #666 留言 6583 §5.4）：
        `project_rule_workspace_member` 對 unbound project（P1 沒有
        cloud_service_ids）只認 follower 或 create_uid，**不認**
        `project_access` 成員資格——dave 雖然是 P1 的 `project_access` admin，
        預設連 P1 這筆 `project.project` 記錄都讀不到。必須先由 alice（owner，
        對 P1 有完整權限）把 dave 加為 follower，o2m 攻擊測試才有意義。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'project.project', 'message_subscribe',
            [[self.p1.id]], {'partner_ids': [self.dave.partner_id.id]})
        self._assert_call_kw_ok(
            result, 'alice 把 dave 加為 P1 follower 應成功（fixture 前提，回 %r）' % result)

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
class TestProjectAccessAttackVectors657(_ProjectAccess657Fixture):
    """工單 §5「攻擊向量」表 X1-X9（含 X4'／H1a-c 對照）。"""

    # ---------------------------------------------------------------- #
    #  Gap 1 — identity anchor（X1/X2/X3/X3b/X7/X8/X9）                  #
    # ---------------------------------------------------------------- #
    def test_X1_attacker_self_owned_row_pivot_into_victim_project_denied(self):
        """攻擊者自建 project（成 owner）→ write(自己那列, {project_id: 受害
        project, role: 'admin'})。

        pre-fix RED：write() 完全沒有 identity anchor 檢查；改前的
        `_check_assignable_role` 只在 `'role' in vals` 時執行，且讀的是
        `rec.project_id`（pre-write／來源 project，攻擊者在那裡是 owner）⇒
        `ASSIGNABLE_ROLES_BY_ROLE[owner]` 含 admin ⇒ 放行。ir.rule 的 write 檢查
        同樣只查 pre-write 狀態（攻擊者在來源 project 確實是 owner）⇒ 放行。
        payload 用 role='admin'：project_access 的表裡 admin 不可授予 admin，且
        攻擊者在受害 project 完全不是成員 ⇒ 對 gap 1 有鑑別力。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        _own_pid, own_row = self._own_project_as(self.mallory, 'Mallory P657 pivot-1')
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[own_row.id], {'project_id': self.p1.id, 'role': ROLE_ADMIN}])
        self._assert_access_error(
            result, 'Mallory 不得把自己的 owner 列 pivot 進 P1 並升級成 admin')
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.mallory.partner_id.id)]),
            'pivot 被拒後，Mallory 在 P1 不得有任何 access 列')

    def test_X2_third_party_row_pivot_into_victim_project_denied(self):
        """反方向 pivot：攻擊者在自己的 project 建一列給第三方（eve），再把「那一
        列」write 搬進受害 project——只改 `project_id`，不改 `role`。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        own_pid, _own_row = self._own_project_as(self.mallory, 'Mallory P657 pivot-2')
        create_result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'project_id': own_pid, 'partner_id': self.eve.partner_id.id, 'role': ROLE_ADMIN}])
        self._assert_call_kw_ok(
            create_result, 'Mallory 在自己的 project 建一列給 Eve 應成功（fixture 前提）')
        eve_row_id = create_result['result']
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[eve_row_id], {'project_id': self.p1.id}])
        self._assert_access_error(result, 'Mallory 不得把 Eve 的列 pivot 進 P1')
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.eve.partner_id.id)]),
            'pivot 被拒後，Eve 在 P1 不得有任何 access 列')

    def test_X3_partner_id_reassignment_denied(self):
        """身分錨點之一：攻擊者把自己那列的 `partner_id` 改指向 eve——若通過，
        eve 會在完全沒經過 invite 的情況下無聲變成攻擊者自己 project 的 owner。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        _pid, row = self._own_project_as(self.mallory, 'Mallory P657 pivot-3')
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[row.id], {'partner_id': self.eve.partner_id.id}])
        self._assert_access_error(result, 'Mallory 不得把自己的列 partner_id 改指向 Eve')
        row.invalidate_recordset()
        self.assertEqual(row.partner_id.id, self.mallory.partner_id.id, '拒絕後 partner_id 不得變動')

    def test_X3b_user_id_reassignment_denied(self):
        """身分錨點的第三個欄位（F2，PR #666 留言 6583 §5.1）：攻擊者把自己那列的
        `user_id` 改指向 eve。`user_id` 是 `@api.depends('partner_id')` 的
        stored compute，但沒有 `inverse` 的 stored compute 照樣可被公開 `write()`
        改動；而且它才是本模組每一條 ir.rule 與 grantor lookup 實際使用的授權鍵——
        若通過，eve 會無聲取得攻擊者原本的存取權，畫面上顯示的成員（partner_id）
        卻沒變。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        _pid, row = self._own_project_as(self.mallory, 'Mallory P657 pivot-user_id')
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[row.id], {'user_id': self.eve.id}])
        self._assert_access_error(result, 'Mallory 不得把自己的列 user_id 改指向 Eve')
        row.invalidate_recordset()
        self.assertEqual(row.user_id.id, self.mallory.id, '拒絕後 user_id 不得變動')

    def test_X7_copy_bypass_denied(self):
        """`copy()` 繞道：`copy()` 底層走 `create()`，理論上早該被 gap 2 的
        create-guard 擋住（copy_data 一定會帶出 role 值，這裡再明寫 override 成
        'admin' 確保不是靠 default 巧合）；這裡驗證真的沒有另一條路。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        _pid, row = self._own_project_as(self.mallory, 'Mallory P657 pivot-copy')
        result = self._call_kw(
            'woow_paas_platform.project_access', 'copy',
            [[row.id], {'project_id': self.p1.id, 'role': ROLE_ADMIN}])
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.mallory.partner_id.id)]),
            'copy() 繞道成功：Mallory 用 copy 進了 P1（回 %r）' % result)

    def test_X8_o2m_cmd1_own_row_pivot_via_project_write_denied(self):
        """不直接寫 access 列，改從 `project.project` 的 one2many
        `project_access_ids` 下 inverse command `(1, id, vals)`（Odoo 的
        `Command.UPDATE`，底層真的會呼叫 comodel 的公開 `write()` ——見
        `odoo/fields.py::_RelationalMulti.write_real`）。這半條不需要額外的
        可達性設定：mallory 操作的是**自己**的 project（她本來就 create_uid，
        `project_rule_workspace_member` 對她永遠可達），繞道對象是她自己那列
        access——與 `_subscribe_dave_to_p1` 開的那組（見 `test_X8_reachability_*`
        / `test_X8_cmd*`）是不同的可達性前提，兩組互補。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        own_pid, row = self._own_project_as(self.mallory, 'Mallory P657 pivot-o2m')

        result = self._call_kw(
            'project.project', 'write',
            [[own_pid], {'project_access_ids': [[1, row.id, {'project_id': self.p1.id}]]}])
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.mallory.partner_id.id)]),
            'o2m command 1 繞道成功（回 %r）' % result)

    def test_X8_reachability_positive_controls(self):
        """o2m 攻擊 battery 的可達性陽性對照（PR #666 留言 6583 §4 E2 段）：先證明
        dave 被加為 follower 後真的能讀到 P1、且 o2m 通道本身對他是可驅動的（用一個
        「安全」payload：role='user' 是 dave 身為 admin 本來就能授予的角色）。沒有
        這兩條，後面攻擊測試若沒被擋，看不出是守衛擋下還是攻擊者根本連不到這個
        通道（探針壞在取樣層 vs 判準層，同一批坑見
        `feedback_negative_verification_needs_positive_control`）。"""
        self._subscribe_dave_to_p1()
        self.authenticate(self.dave_login, self.dave_login)

        read_result = self._call_kw('project.project', 'read', [[self.p1.id], ['name']])
        self._assert_call_kw_ok(
            read_result, '可達性對照：dave 成為 follower 後應能 read P1（回 %r）' % read_result)

        safe_cmd0 = self._call_kw(
            'project.project', 'write',
            [[self.p1.id], {'project_access_ids': [
                [0, 0, {'partner_id': self.eve.partner_id.id, 'role': ROLE_USER}]]}])
        self._assert_call_kw_ok(
            safe_cmd0,
            '通道有效性對照：dave 用 o2m cmd0 建一列合法的 role=user 應成功（回 %r）' % safe_cmd0)
        self.assertTrue(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.eve.partner_id.id)]),
            'safe payload 沒有真的落地 ⇒ o2m 通道對 dave 其實不可驅動，後面的攻擊測試零鑑別力')

    def test_X8_cmd0_context_default_role_owner_denied(self):
        """o2m 版的 gap 2/H1：dave 透過 o2m command 0（建立並連結）+ context
        `default_role='owner'` 想幫共犯 eve 鑄一列 owner。O2M 的 CREATE 命令處理
        會強制把 `inverse` 欄位（`project_id`）設成 p1.id（見
        `write_real`：`dict(command[2], **{inverse: record.id})`），所以這條測的
        純粹是「role 缺省守衛在 o2m 呼叫路徑下依然有效」，不是 H1 本身（H1 是
        `project_id` 那半，o2m 這條路徑結構上碰不到，因為 project_id 由 Odoo
        自己填死）。"""
        self._subscribe_dave_to_p1()
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'project.project', 'write',
            [[self.p1.id], {'project_access_ids': [[0, 0, {'partner_id': self.eve.partner_id.id}]]}],
            {'context': {'default_role': ROLE_OWNER}})
        self._assert_access_error(
            result, 'dave 不得用 o2m cmd0 + context default_role trick 幫共犯建出 owner 列')
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.eve.partner_id.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    def test_X8_cmd1_demote_owner_row_denied(self):
        """o2m command 1（UPDATE）試圖降級 owner 列——底層走 comodel 的公開
        `write()`，應觸發 `_check_owner_row_protected`。"""
        self._subscribe_dave_to_p1()
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'project.project', 'write',
            [[self.p1.id], {'project_access_ids': [[1, self.alice_access.id, {'role': ROLE_USER}]]}])
        self._assert_access_error(result, 'dave 不得透過 o2m cmd1 把 alice 的 owner 列降級')
        self.alice_access.invalidate_recordset()
        self.assertEqual(self.alice_access.role, ROLE_OWNER, '拒絕後 alice 仍必須是 owner')

    def test_X8_cmd2_delete_owner_row_denied(self):
        """o2m command 2（DELETE）試圖刪除 owner 列——底層走 comodel 的公開
        `unlink()`，應觸發 `_check_owner_row_protected`。"""
        self._subscribe_dave_to_p1()
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'project.project', 'write',
            [[self.p1.id], {'project_access_ids': [[2, self.alice_access.id, 0]]}])
        self._assert_access_error(result, 'dave 不得透過 o2m cmd2 刪除 alice 的 owner 列')
        self.assertTrue(self.alice_access.exists(), '拒絕後 alice 的 owner 列必須仍在')

    def test_X8_cmd5_clear_all_denied(self):
        """o2m command 5（CLEAR）試圖清空 P1 的所有 access 列——`project_id`
        是 `ondelete='cascade'`，CLEAR 對這種欄位形狀等同對每一列跑 unlink，
        owner 那一列一樣要過 `_check_owner_row_protected`。"""
        self._subscribe_dave_to_p1()
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'project.project', 'write',
            [[self.p1.id], {'project_access_ids': [[5, 0, 0]]}])
        self._assert_access_error(result, 'dave 不得透過 o2m cmd5 清空 P1 的所有 access 列')
        self.assertTrue(self.alice_access.exists(), '拒絕後 alice 的 owner 列必須仍在')

    def test_X9_admin_cannot_repoint_member_user_id_to_lock_out_owner(self):
        """F2 的完整攻擊鏈（PR #666 留言 6583 §5.1 G1-G3）：dave（admin）把 carol
        的列 `user_id` 改指向 alice（真 owner）。grantor lookup 用 `limit=1` +
        `_order='role desc, create_date desc'`（字串降序 `user > owner > guest >
        admin`）——若通過，這第二列會贏過 alice 原本的 owner 列，把 alice 鎖在
        自己的 project 外（她自己的 grantor lookup 會先撈到這列 role='user' 的
        「複本」）。"""
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[self.carol_access.id], {'user_id': self.alice.id}])
        self._assert_access_error(
            result, 'dave（admin）不得把 carol 的列 user_id 改指向 alice——會鎖住真 owner')
        self.carol_access.invalidate_recordset()
        self.assertEqual(self.carol_access.user_id.id, self.carol.id, '拒絕後 user_id 不得變動')

    # ---------------------------------------------------------------- #
    #  Gap 2 — role/project_id 缺省整道守衛被跳過（X4/X4'/H1a-c）          #
    # ---------------------------------------------------------------- #
    def test_X4_admin_context_default_role_owner_bypass_denied(self):
        """dave（P1 admin）用 `with_context(default_role='owner')` 建立一列給共犯
        eve，vals 裡刻意不放 `role`（project_id 仍在 vals 裡）。

        pre-fix RED：`_check_assignable_role` 的 early-return
        `if not target_role or not project_id: return` 在 `vals.get('role')`
        是 `None` 時直接放行；Odoo 的 `super().create()` 隨後透過
        `_add_missing_default_values` → `default_get` 用 context 的
        `default_role` 補值 ⇒ Eve 拿到 `role='owner'`。🔴 這是 issue #657 本文的
        修法（`vals.get('role') or ROLE_USER`）擋不住的那個 payload——它一樣只
        看 vals，不看 Odoo 實際會落地的值。
        """
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'project_id': self.p1.id, 'partner_id': self.eve.partner_id.id}],
            {'context': {'default_role': ROLE_OWNER}})
        self._assert_access_error(
            result, 'admin 不得用 default_role context trick 幫共犯建出 owner 列')
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.eve.partner_id.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    def test_X4_prime_admin_explicit_role_owner_denied_control(self):
        """X4 對照組（同時也是 H1a/H1b/H1c 的單一差異變數對照——role 與
        project_id 全都明寫在 vals 裡，不透過任何 context default）。這條在修改
        前**應該已經被擋**（`_check_assignable_role` 有跑：dave 是 admin，
        `ASSIGNABLE_ROLES_BY_ROLE[admin]` 不含 owner）。X4 與 X4' 的差異就是
        gap 2 的存在證明——兩者攻擊意圖相同，唯一差異是 role 有沒有出現在 vals
        裡；H1a/H1b/H1c 與這條的差異則是 project_id 有沒有出現在 vals 裡。"""
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'project_id': self.p1.id, 'partner_id': self.eve.partner_id.id, 'role': ROLE_OWNER}])
        self._assert_access_error(
            result, 'admin 明寫 role=owner 的 create 應早已被擋（陽性對照）')

    def test_H1a_admin_context_default_project_id_only_bypass_denied(self):
        """H1 shape 1（PR #666 留言 6583 §1 payload A1）：vals={partner_id,
        role='owner'}，`project_id` 只放在 context 的 `default_project_id`。

        pre-fix RED（第二輪修訂之前的版本）：`create()` 傳給
        `_check_assignable_role` 的第二個參數是 raw `vals.get('project_id')`，
        vals 沒有這個 key ⇒ `None` ⇒ 命中 `if not project_id: return` 直接放行
        ——role='owner' 的守衛整道沒跑，`super().create()` 才從 context 補上
        project_id。與 `test_X4_prime_...`（project_id 也在 vals 裡）的唯一差異
        就是 project_id 在 vals 還是 context——這就是 H1 的存在證明。
        """
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'partner_id': self.eve.partner_id.id, 'role': ROLE_OWNER}],
            {'context': {'default_project_id': self.p1.id}})
        self._assert_access_error(
            result, 'dave 不得用「project_id 只放 context」的方式繞過角色檢查（H1 shape 1）')
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.eve.partner_id.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    def test_H1b_admin_context_default_project_id_and_role_bypass_denied(self):
        """H1 shape 2（PR #666 留言 6583 §1 payload A2）：vals={partner_id}，
        `project_id` 與 `role` 都放在 context。"""
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'partner_id': self.eve.partner_id.id}],
            {'context': {'default_project_id': self.p1.id, 'default_role': ROLE_OWNER}})
        self._assert_access_error(
            result, 'dave 不得用「project_id + role 都放 context」的方式繞過（H1 shape 2）')
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.eve.partner_id.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    def test_H1c_admin_context_default_all_fields_bypass_denied(self):
        """H1 shape 3（PR #666 留言 6583 §1 payload A3）：vals 完全空，
        `project_id`／`role`／`partner_id` 全部放在 context。"""
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{}],
            {'context': {
                'default_project_id': self.p1.id,
                'default_role': ROLE_OWNER,
                'default_partner_id': self.eve.partner_id.id,
            }})
        self._assert_access_error(
            result, 'dave 不得用「全欄位皆放 context」的方式繞過（H1 shape 3）')
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.eve.partner_id.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    # ---------------------------------------------------------------- #
    #  Gap 3 — 無 owner 列保護（X5/X6）                                  #
    # ---------------------------------------------------------------- #
    def test_X5_admin_cannot_demote_owner_row(self):
        """dave（P1 admin）把 alice 的 owner 列降級成 user——0-owner 視窗鏈的
        第一步。

        pre-fix RED：ir.rule 的 write 檢查只看**呼叫者**（dave）在 P1 是否
        owner/admin ⇒ 放行；改前的 `_check_assignable_role('user', p1)` 對
        grantor_role=admin 而言，'user' 在 `ASSIGNABLE_ROLES_BY_ROLE[admin]`
        裡 ⇒ 同樣放行。兩層都沒守到目標列本身的角色。
        """
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[self.alice_access.id], {'role': ROLE_USER}])
        self._assert_access_error(
            result, 'dave（admin）不得把 alice 的 owner 列降級——0-owner 視窗鏈的第一步')
        self.alice_access.invalidate_recordset()
        self.assertEqual(self.alice_access.role, ROLE_OWNER, '拒絕後 alice 仍必須是 owner')

    def test_X6_admin_unlink_owner_row_must_be_AccessError_not_ValidationError(self):
        """Gap 3，且要證明順序 (a) 授權拒絕 AccessError 先於 (b) 不變式拒絕
        ValidationError：fixture 刻意讓受害 project 有**兩個** owner
        （alice + frank），這樣既有的『移除最後一個 owner』不變式檢查
        （`unlink()` 尾端的 `other_owners` 計數）根本不會觸發——如果 dave 的
        unlink 請求不是被 AccessError 擋下，代表授權層沒有獨立作用，只是巧合被
        不變式檢查救到（本 fixture 特地讓那個巧合失效）。
        """
        frank, _frank_login = _new_group_user(self.env, 'frank657')
        project6 = self.Project.with_context(
            bootstrap_owner_partner_id=self.alice.partner_id.id
        ).create({'name': 'X6-657-two-owner-project'})
        owner_row_1 = self.ProjectAccess.sudo().search([
            ('project_id', '=', project6.id), ('partner_id', '=', self.alice.partner_id.id)])
        self.ProjectAccess.sudo().create({
            'project_id': project6.id, 'partner_id': frank.partner_id.id, 'role': ROLE_OWNER})
        self.ProjectAccess.sudo().create({
            'project_id': project6.id, 'partner_id': self.dave.partner_id.id, 'role': ROLE_ADMIN})

        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'unlink', [[owner_row_1.id]])
        self._assert_access_error(
            result, 'dave（admin）不得 unlink alice 的 owner 列——即使專案還有另一個 owner')
        self.assertTrue(owner_row_1.exists(), '拒絕後 alice 的 owner 列必須仍在')


@tagged('post_install', '-at_install')
class TestProjectAccessPositiveControls657(_ProjectAccess657Fixture):
    """工單 §5「陽性對照」表 P1-P7 + P-BOOT：防 over-correction——修復不得誤傷既有
    合法流程。
    """

    def test_P1_owner_can_invite_user_and_guest(self):
        self.authenticate(self.alice_login, self.alice_login)
        for role in (ROLE_USER, ROLE_GUEST):
            invitee, _login = _new_group_user(self.env, 'p1invitee%s657' % role)
            result = self._call_kw(
                'woow_paas_platform.project_access', 'create',
                [{'project_id': self.p1.id, 'partner_id': invitee.partner_id.id, 'role': role}])
            self._assert_call_kw_ok(
                result, 'owner 邀請 role=%s 應成功（回 %r）' % (role, result))

    def test_P2_admin_can_invite_user_but_not_admin(self):
        """鎖工單 §3.1：`ASSIGNABLE_ROLES_BY_ROLE[admin]` 是 `[user, guest]`——
        project_access 維持嚴格表，不可被『對齊 workspace_access（admin 可邀
        admin）』改掉。"""
        self.authenticate(self.dave_login, self.dave_login)
        invitee, _login = _new_group_user(self.env, 'p2invitee657')
        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'project_id': self.p1.id, 'partner_id': invitee.partner_id.id, 'role': ROLE_USER}])
        self._assert_call_kw_ok(result, 'admin 邀請 role=user 應成功（回 %r）' % result)

        invitee2, _login2 = _new_group_user(self.env, 'p2invitee2657')
        result2 = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'project_id': self.p1.id, 'partner_id': invitee2.partner_id.id, 'role': ROLE_ADMIN}])
        self._assert_access_error(
            result2, 'admin 邀請 role=admin 必須被拒——project_access 維持嚴格表')

    def test_P3_owner_can_update_role_and_remove_non_owner_member(self):
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[self.carol_access.id], {'role': ROLE_GUEST}])
        self._assert_call_kw_ok(result, 'owner 改成員 role 應成功（回 %r）' % result)
        self.carol_access.invalidate_recordset()
        self.assertEqual(self.carol_access.role, ROLE_GUEST)

        result = self._call_kw(
            'woow_paas_platform.project_access', 'unlink', [[self.carol_access.id]])
        self._assert_call_kw_ok(result, 'owner 移除非 owner 成員應成功（回 %r）' % result)
        self.assertFalse(self.carol_access.exists())

    def test_P4_owner_noop_anchor_values_write_on_own_row_not_blocked(self):
        """陽性對照（防過度修正）：`project_id`/`partner_id`/`user_id` 帶**原值**
        送出（UI 整批送欄位常見）＋同時送 `role`（原值，owner→owner 對
        grantor=owner 自己合法），必須成功——guard 若寫成『key 在 vals 裡就擋』
        而非『值真的改變才擋』，這條會紅。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[self.alice_access.id],
             {'project_id': self.p1.id, 'partner_id': self.alice.partner_id.id,
              'user_id': self.alice.id, 'role': ROLE_OWNER}])
        self._assert_call_kw_ok(
            result, 'owner 對自己那列送出 no-op anchor 值不應被誤擋（回 %r）' % result)

    def test_P_BOOT_fresh_group_user_can_bootstrap_own_project(self):
        """鎖 `project_project.py:341` 的 sudo bootstrap：既有
        `test_FPA14_non_root_sudo_create_self_owns` 驗的是 `.sudo().create()`
        分支選擇；這裡走的是本檔攻擊測試同一條路徑（plain group_user 直接
        `project.project.create()`，非 sudo），確認 gap 1/gap 2 的新 guard 沒有
        把這條自舉流程一起擋死。"""
        frank, frank_login = _new_group_user(self.env, 'pboot657')
        self.authenticate(frank_login, frank_login)
        result = self._call_kw(
            'project.project', 'create', [{'name': 'Frank P657 bootstrap'}])
        self._assert_call_kw_ok(result, 'group_user 自建 project 應成功（回 %r）' % result)
        project_id = result['result']
        rows = self.ProjectAccess.sudo().search([('project_id', '=', project_id)])
        self.assertEqual(
            rows.mapped('role'), [ROLE_OWNER],
            '新建 project 必須恰產生一列 owner access（回 %r）' % rows.mapped('role'))
        self.assertEqual(rows.partner_id.id, frank.partner_id.id)

    def test_P5_group_system_admin_has_full_crud_incl_anchor(self):
        """鎖 `project_access_rule_admin_full`：`authenticate('admin','admin')`
        （uid 2、`group_system`、非 superuser）對 P1（admin 既非成員也非 owner）
        的 access 列必須有完整 CRUD，含改 identity anchor。"""
        self.authenticate('admin', 'admin')

        result = self._call_kw(
            'woow_paas_platform.project_access', 'read',
            [[self.alice_access.id], ['role']])
        self._assert_call_kw_ok(result, 'group_system 應能 read 非自家 project 的 access 列')

        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[self.carol_access.id], {'project_id': self.p2.id}])
        self._assert_call_kw_ok(result, 'group_system 應能 repoint project_id anchor')
        self.carol_access.invalidate_recordset()
        self.assertEqual(self.carol_access.project_id.id, self.p2.id)

        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'project_id': self.p1.id, 'partner_id': self.bob.partner_id.id, 'role': ROLE_USER}])
        self._assert_call_kw_ok(result, 'group_system 應能 create 非自家 project 的 access 列')
        new_id = result['result']

        result = self._call_kw(
            'woow_paas_platform.project_access', 'unlink', [[new_id]])
        self._assert_call_kw_ok(result, 'group_system 應能 unlink 非自家 project 的 access 列')

    def test_P6_env_is_superuser_exemption_unrestricted(self):
        """陽性對照：`env.is_superuser()` 豁免（涵蓋任何 `.sudo()` recordset，
        不限 root uid）不受任何新 guard 影響。

        🔴 與原始版本不同的地方（PR #666 留言 6583 §2 C4／§4）：`HttpCase` 的
        `self.env` 本身就是 uid=1，`.sudo()` 在那個 env 上是 no-op
        （`BaseModel.sudo()`：`if flag == self.env.su: return self`），所以直呼
        `self.carol_access.sudo().write(...)` 測到的其實是「本來就是
        superuser」，不是「`.sudo()` 這個切換動作本身」。改用 `with_user
        (mallory)` 先把 uid 切到非 root，再 `.sudo()` 切回 `su=True`（call
        order 見 `test_FPA14_non_root_sudo_create_self_owns` 的註解：反過來
        或對本來就是 uid=1 的 env 呼叫 `.sudo()` 都會讓 `sudo()` 變 no-op），
        才是真正驗到 `env.is_superuser()` 這個豁免分支，而不只是巧合地在
        superuser env 下操作。"""
        non_root_sudo_row = self.ProjectAccess.with_user(self.mallory).sudo().browse(
            self.carol_access.id)
        non_root_sudo_row.write({'partner_id': self.bob.partner_id.id})
        self.assertEqual(
            self.carol_access.sudo().partner_id.id, self.bob.partner_id.id,
            'env.is_superuser() 豁免（非 root uid + .sudo()）不應被新 guard 擋下')

    def test_P7_user_id_compute_still_populated_on_create(self):
        """F2 陽性對照：`user_id` anchor 不影響 `create()` 當下的 compute
        populate——新建一列給「有對應 res.users 的 partner」，`user_id` 必須被
        填上。compute 的落地走的是 Odoo 內部 flush 機制（`_flush()` 直接發
        SQL），不經過本檔新加的 `write()` public override，所以 anchor guard
        不會攔到它（若真的攔到，代表判斷錯了，見 §4「不做」清單的停下來回報
        指示）。"""
        self.authenticate(self.alice_login, self.alice_login)
        frank, _login = _new_group_user(self.env, 'p7frank657')
        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'project_id': self.p1.id, 'partner_id': frank.partner_id.id, 'role': ROLE_USER}])
        self._assert_call_kw_ok(result, 'owner 邀請新成員應成功（回 %r）' % result)
        new_id = result['result']
        row = self.ProjectAccess.sudo().browse(new_id)
        self.assertEqual(
            row.user_id.id, frank.id,
            'user_id compute 必須被正確填上，未被 anchor guard 打壞')

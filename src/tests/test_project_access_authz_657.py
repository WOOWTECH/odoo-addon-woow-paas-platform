# -*- coding: utf-8 -*-
"""#657：`project_access`（專案成員 ACL）補三個 model 層授權缺口——照抄
`woow_paas_platform.workspace_access` 的 #556 樣板（`test_workspace_access_authz_556.py`），
但 gap 2 的修法與 issue #657 本文寫的不同：不是 `vals.get('role') or ROLE_USER`，而是
逐字重現 Odoo 的 `default_get` 解析順序（見 `src/models/project_access.py::create`）。
本檔的 class / method 命名對應派工 leader 的工單（scratchpad
`woow-657-workorder-impl.md` §2 / §5）攻擊向編號 X1-X8（含 X4' 對照）與陽性對照編號
P1-P6 / P-BOOT。

## TDD 紀律（照抄 #556，一字不可省）

- **全部走真 HTTP RPC**：`/web/dataset/call_kw`。**禁止 `TransactionCase`**——它預設
  `su=True`，ir.rule 不套用、model guard 的 deny 分支結構上不可達，恆綠且與修復好壞
  無關。
- fixture user 一律只掛 `base.group_user`（alice/bob/carol/dave/mallory/eve）。
  P5 是刻意的例外：它測的正是 `group_system` 的 admin bypass 通道本身，必須用
  `authenticate('admin','admin')`（uid 2、`group_system`、非 superuser）。

## 三個缺口與本檔對應

- **Gap 1**（identity anchor）：`project_id`/`partner_id` 建立後不可變。X1（自己那列
  pivot：project_id + role）、X2（第三方列 pivot：只改 project_id）、X3（partner_id
  重指派）、X7（`copy()` 繞道，底層走 `create()`）、X8（`project.project` 的 o2m
  `project_access_ids` inverse command 繞道，含 command 4 有效性陽性對照）。
- **Gap 2**（role 缺省整道守衛被跳過）：X4（`with_context(default_role='owner')` +
  vals 不放 role，攻擊者是 admin）、X4'（對照組，vals 明寫 `role='owner'`——這條
  修改前應已被擋，兩者的差異就是 gap 2 的存在證明）。
- **Gap 3**（無 owner 列保護）：X5（admin 降級 owner 列）、X6（admin unlink owner
  列，且斷言拿到的是 **AccessError** 不是 ValidationError——fixture 刻意讓受害
  project 有兩個 owner，讓『移除最後一個 owner』的不變式檢查根本不會觸發，藉此把
  授權判定與不變式判定的鑑別力分開）。

## 陽性對照（防 over-correction）

P1（owner 邀請 user/guest）、P2（admin 邀請 user 成功／邀請 admin 必須被拒——鎖
`ASSIGNABLE_ROLES_BY_ROLE` 的嚴格表，不可對齊 workspace_access 那份）、P3（owner
改角色／移除非 owner 成員）、P4（owner 對自己那列送出 no-op anchor 值＋改 role 不被
誤擋）、P-BOOT（乾淨 `base.group_user` 自建 project 仍拿到 owner 列——
`project_project.py:341` 的 sudo bootstrap 沒被打壞）、P5（`group_system` 全權
CRUD 含改 anchor）、P6（`sudo()` 路徑不受限）。
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

        # setUp 走 HttpCase 預設的 superuser env（uid=1）——project.project.create()
        # 的 owner-access bootstrap（project_project.py:278-341）在 SUPERUSER 分支
        # 只認 context 的 bootstrap_owner_partner_id hint，不是本次要驗的攻擊路徑，
        # 這裡單純用它搭 fixture。
        self.p1 = self.Project.with_context(
            bootstrap_owner_partner_id=self.alice.partner_id.id
        ).create({'name': 'P1-657'})
        self.p2 = self.Project.with_context(
            bootstrap_owner_partner_id=self.bob.partner_id.id
        ).create({'name': 'P2-657'})
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
    """工單 §5「攻擊向量」表 X1-X8（含 X4' 對照）。"""

    # ---------------------------------------------------------------- #
    #  Gap 1 — identity anchor（X1/X2/X3/X7/X8）                        #
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
        """身分錨點的另一半：攻擊者把自己那列的 `partner_id` 改指向 eve——若通過，
        eve 會在完全沒經過 invite 的情況下無聲變成攻擊者自己 project 的 owner。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        _pid, row = self._own_project_as(self.mallory, 'Mallory P657 pivot-3')
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[row.id], {'partner_id': self.eve.partner_id.id}])
        self._assert_access_error(result, 'Mallory 不得把自己的列 partner_id 改指向 Eve')
        row.invalidate_recordset()
        self.assertEqual(row.partner_id.id, self.mallory.partner_id.id, '拒絕後 partner_id 不得變動')

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

    def test_X8_o2m_inverse_command_bypass_denied(self):
        """不直接寫 access 列，改從 `project.project` 的 one2many
        `project_access_ids` 下 inverse command——command `(1, id, vals)` 是「改該
        列」、command `(4, id)` 是「連結該列」，兩者底層都會呼叫到 comodel 的
        `write()`，應該同樣被 anchor guard 擋。command 4 的形態是否真的對本
        model 有效，由 `test_X8_prime_o2m_command4_shape_is_effective_control`
        陽性對照鎖住——沒有它，「沒位移」有可能只是命令形態本身無效，不能當作
        守住的證據。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        own_pid, row = self._own_project_as(self.mallory, 'Mallory P657 pivot-o2m')

        result_a = self._call_kw(
            'project.project', 'write',
            [[own_pid], {'project_access_ids': [[1, row.id, {'project_id': self.p1.id}]]}])
        self.assertFalse(
            self.ProjectAccess.sudo().search([
                ('project_id', '=', self.p1.id), ('partner_id', '=', self.mallory.partner_id.id)]),
            'o2m command 1 繞道成功（回 %r）' % result_a)

        result_b = self._call_kw(
            'project.project', 'write',
            [[own_pid], {'project_access_ids': [[4, self.carol_access.id]]}])
        self.carol_access.invalidate_recordset()
        self.assertEqual(
            self.carol_access.project_id.id, self.p1.id,
            'o2m command 4 繞道成功：Carol 的列被拉出 P1（回 %r）' % result_b)
        self._assert_access_error(
            result_b,
            'command 4 沒有位移但也沒被拒 ⇒ 可能整條被靜默忽略，本測試對此路徑零鑑別力')

    def test_X8_prime_o2m_command4_shape_is_effective_control(self):
        """陽性對照（鎖上一條測試的鑑別力）：同樣的 `(4, id)` 命令形態，改由 sudo
        下，必須真的把列搬過去。若這條也沒效果，代表上一條的「沒位移」只是命令
        形態本身無效（探針壞在取樣層），不能當成守住的證據。"""
        project8 = self.Project.sudo().create({'name': 'Ctrl P657 pivot-o2m'})
        project8.sudo().write({'project_access_ids': [(4, self.carol_access.id)]})
        self.carol_access.invalidate_recordset()
        self.assertEqual(
            self.carol_access.project_id.id, project8.id,
            'sudo 下 command 4 也沒搬動 ⇒ 這個命令形態在本 model 上無效，上一條測試對'
            '該路徑沒有鑑別力')

    # ---------------------------------------------------------------- #
    #  Gap 2 — role 缺省整道守衛被跳過（X4/X4'）                          #
    # ---------------------------------------------------------------- #
    def test_X4_admin_context_default_role_owner_bypass_denied(self):
        """dave（P1 admin）用 `with_context(default_role='owner')` 建立一列給共犯
        eve，vals 裡刻意不放 `role`。

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
        """X4 對照組：同樣的攻擊意圖，但 vals 明寫 `role='owner'`（不透過
        context）。這條在修改前**應該已經被擋**（`_check_assignable_role` 有
        跑：dave 是 admin，`ASSIGNABLE_ROLES_BY_ROLE[admin]` 不含 owner）。X4 與
        X4' 的差異就是 gap 2 的存在證明——兩者攻擊意圖相同，唯一差異是 role 有沒
        有出現在 vals 裡。"""
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'create',
            [{'project_id': self.p1.id, 'partner_id': self.eve.partner_id.id, 'role': ROLE_OWNER}])
        self._assert_access_error(
            result, 'admin 明寫 role=owner 的 create 應早已被擋（陽性對照）')

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
    """工單 §5「陽性對照」表 P1-P6 + P-BOOT：防 over-correction——修復不得誤傷既有
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
        """陽性對照（防過度修正）：`project_id`/`partner_id` 帶**原值**送出
        （UI 整批送欄位常見）＋同時送 `role`（原值，owner→owner 對 grantor=owner
        自己合法），必須成功——guard 若寫成『key 在 vals 裡就擋』而非『值真的
        改變才擋』，這條會紅。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.project_access', 'write',
            [[self.alice_access.id],
             {'project_id': self.p1.id, 'partner_id': self.alice.partner_id.id, 'role': ROLE_OWNER}])
        self._assert_call_kw_ok(
            result, 'owner 對自己那列送出 no-op anchor 值不應被誤擋（回 %r）' % result)

    def test_P_BOOT_fresh_group_user_can_bootstrap_own_project(self):
        """鎖 `project_project.py:341` 的 sudo bootstrap：既有
        `test_FPA14_non_root_sudo_create_self_owns` 驗的是 `.sudo().create()`
        分支選擇；這裡走的是本檔攻擊測試同一條路徑（plain group_user 直接
        `project.project.create()`，非 sudo），確認 gap 2/gap 1 的新 guard沒有
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

    def test_P6_sudo_path_still_unrestricted(self):
        """陽性對照：平台級 sudo 路徑（superuser）不受任何新 guard 影響——資料
        修復／未來若有需要仍可換位。"""
        self.carol_access.sudo().write({'partner_id': self.bob.partner_id.id})
        self.assertEqual(self.carol_access.sudo().partner_id.id, self.bob.partner_id.id)

# -*- coding: utf-8 -*-
"""#732：owner 把**自己那列**的 role 改成非 owner ⇒ workspace 永久 0 owner。

三道既有守衛都涵蓋不到這條 write() 路徑（動工前已逐一在原始碼複驗）：

- `_check_owner_row_protected`：docstring 明言只擋 **non-owner caller**——
  呼叫者就是 owner ⇒ 直接 return 放行；
- `unlink()` 的 last-owner 不變式（(b) 段）：只掛在 unlink，write 不經過；
- `_check_owner_count`（`@api.constrains`）：只在**新值是 owner** 時觸發，
  新值 admin/user/guest 整道不跑。

write 路徑的 `_check_assignable_role` 也放行（owner 可指派 admin）。產品端點
`_update_member_role`（`paas.py`）在 controller 層擋掉 owner 列 ⇒ 缺陷路徑是
**裸 ORM `call_kw`**——issue 實測正是 alice（owner）對自己那列
`write({'role': 'admin'})` 回 OK True、DB 落地 0 owner，且
`ASSIGNABLE_ROLES_BY_ROLE['admin']` 不含 owner ⇒ 租戶側永久不可回復。

## TDD 紀律（承 #556 檔頭，一字不可省）

全部走真 HTTP RPC（`/web/dataset/call_kw`）。**禁止 `TransactionCase`**——它
預設 `su=True`，而新守衛與本檔既有守衛同族、豁免
`is_superuser()`/`base.group_system`，TransactionCase 下 deny 分支結構上
不可達，恆綠且與修復好壞無關。fixture user 只掛 `base.group_user`；
group_system／sudo 兩條豁免各有專屬陽性對照（見下）。

## 守衛形狀（比對記錄，不比對呼叫者）

判準是「這次 write 會不會讓該 workspace 剩 0 個 owner」：demote 一列現值
owner 的記錄時，數**這次呼叫之外**還有沒有別的 owner 列。**不是**「owner 列
一律不得 demote」——`TestLastOwnerDemotePositiveControls` 的雙 owner 對照
用 SQL 造出 ORM 不可達的雙 owner 態來釘這個形狀（守衛若寫成 blanket-deny，
該條會紅）。

## 豁免形狀：group_system 無條件＋sentinel opt-in；**裸 sudo() 不豁免**

`_check_owner_count` 是 `@api.constrains` ⇒ 對 su 也生效 ⇒ promote-first
的轉移順序（先把別人升 owner）在既有碼下就被擋死；若 demote-first 也無條件
擋，**所有 ORM 層 ownership 轉移對所有呼叫者同時不可達**（含平台
group_system 修資料、含未來喚醒 `transfer_ownership()`），只剩裸 SQL——
這會打死 issue 驗收第 2 條「合法轉移流程不得被新守衛擋住」。所以要有豁免；
但豁免**不給裸 `is_superuser()`**：#711 的教訓正是「sudo() 路徑豁免守衛、
後來被 controller 的 sudo() 包裝變成可利用洞」——本檔既有兩道 AccessError
家族守衛都豁免 su，未來哪個 controller 順手 `sudo().write` 就同時繞光三道。
豁免兩通道：

- `base.group_system`（人類平台修復通道；經 UI／RPC 設不進物件身分 context）
  ——demote-then-promote 是今天唯一可運作的轉移路徑，由
  `test_group_system_full_transfer_sequence_still_works` 整段釘住；
- 私有 sentinel context key（#519/#522/#536/#744 同族形狀：值是模組私有物件、
  判準 `is`、登記 `INTERNAL_CTX_KEY_REGISTRY`）——未來平台碼（含喚醒
  `transfer_ownership()`）的顯式開閘；JSON 表達不出物件身分 ⇒ 租戶 RPC
  偽造不了。裸 `sudo()` 包裝 fail-loud，由
  `test_bare_sudo_wrap_demote_denied` 釘住（該條即 #711 那格的反向測試）。

實測補強（動工前枚舉）：src/ 生產碼**沒有任何**用 sudo 寫既有 access 列
role 的路徑（bootstrap／ha_paas 都是 create 方向；`_update_member_role`
以 request user 跑且 controller 先擋 owner 列）⇒ 拿掉裸 su 豁免零誤傷。
"""
import json
import uuid

from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_OWNER,
    ROLE_USER,
)


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 732' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _LastOwner732Fixture(HttpCase):
    """W1（Alice owner、Carol role=user 成員）。兩人皆只掛 `base.group_user`。"""

    def setUp(self):
        super().setUp()
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Workspace = self.env['woow_paas_platform.workspace']

        self.alice, self.alice_login = _new_group_user(self.env, 'alice732')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol732')
        for u in (self.alice, self.carol):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.ws1 = self.Workspace.create(
            {'name': 'WS1-732', 'owner_id': self.alice.id})
        self.alice_access = self.WorkspaceAccess.search([
            ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.alice.id)])
        self.assertEqual(self.alice_access.role, ROLE_OWNER,
                         'fixture 前提：bootstrap 必須產生 Alice 的 owner 列')
        self.carol_access = self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws1.id, 'user_id': self.carol.id,
            'role': ROLE_USER})

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

    def _post_api(self, url, params):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
        resp = self.url_open(url, data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    # ------------------------------------------------------------------ #
    #  斷言 helper（issue 驗收：斷言用例外型別，不比對文案片段）              #
    # ------------------------------------------------------------------ #
    def _assert_validation_error(self, result, msg):
        self.assertIn('error', result, msg + '（回 %r）' % result)
        name = result['error'].get('data', {}).get('name', '')
        self.assertIn('ValidationError', name,
                      msg + '（期望 ValidationError，回 %r）' % result)

    def _assert_call_kw_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)

    def _owner_count(self, ws):
        return self.WorkspaceAccess.sudo().search_count([
            ('workspace_id', '=', ws.id), ('role', '=', ROLE_OWNER)])

    def _force_role_sql(self, access, role):
        """用裸 SQL 造 ORM 不可達的狀態（雙 owner 由 `_check_owner_count`
        擋死、0-owner 由本次新守衛擋死）——測試要驗的正是守衛在這些狀態下
        的**形狀**，所以造態必須繞過守衛本身，否則是循環論證。"""
        self.env.cr.execute(
            "UPDATE woow_paas_platform_workspace_access SET role = %s "
            "WHERE id = %s", (role, access.id))
        self.env.invalidate_all()


@tagged('post_install', '-at_install')
class TestLastOwnerDemoteGuard(_LastOwner732Fixture):
    """缺陷向：pre-fix 全 RED（write 回 OK True、DB 落地 0 owner）。"""

    def test_owner_self_demote_to_admin_denied(self):
        """issue #732 的原始 payload：alice（owner）對自己那列
        `write({'role': 'admin'})`。pre-fix：OK True、workspace 剩 0 owner、
        租戶側不可回復（`ASSIGNABLE_ROLES_BY_ROLE['admin']` 不含 owner）。
        post-fix：ValidationError（invariant 家族，與 unlink 的 last-owner
        不變式同型），且列不得變動。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.alice_access.id], {'role': ROLE_ADMIN}])
        self._assert_validation_error(
            result, '唯一 owner 自我降級成 admin 必須被拒——否則 0-owner 永久化')
        self.alice_access.invalidate_recordset()
        self.assertEqual(self.alice_access.role, ROLE_OWNER,
                         '拒絕後 Alice 必須仍是 owner')
        self.assertEqual(self._owner_count(self.ws1), 1,
                         '拒絕後 W1 必須仍恰有 1 個 owner')

    def test_owner_self_demote_to_guest_denied(self):
        """形狀對照：目標值不是 'admin' 也一樣擋——守衛的判準是「降級後剩
        0 owner」，不是特定目標 role 值（防守衛被寫成只擋 admin）。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.alice_access.id], {'role': ROLE_GUEST}])
        self._assert_validation_error(
            result, '唯一 owner 自我降級成 guest 也必須被拒')
        self.alice_access.invalidate_recordset()
        self.assertEqual(self.alice_access.role, ROLE_OWNER)

    def test_owner_self_demote_via_o2m_command_denied(self):
        """繞道對照：不直接寫 access 列，改從 workspace 的 one2many
        `access_ids` 下 command `(1, id, {'role': 'admin'})`——底層走
        comodel 的 `write()`，必須被同一道守衛擋（同 #556 檔的 o2m 繞道
        測試形態）。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.ws1.id],
             {'access_ids': [[1, self.alice_access.id, {'role': ROLE_ADMIN}]]}])
        self._assert_validation_error(
            result, 'o2m command 1 繞道自我降級也必須被拒')
        self.alice_access.invalidate_recordset()
        self.assertEqual(self.alice_access.role, ROLE_OWNER,
                         '拒絕後 Alice 必須仍是 owner（o2m 路徑不得留下 0-owner）')

    def test_forged_context_does_not_open_the_hatch(self):
        """forged-context 反測（su-env-exempt-guard-false-green.md 列為必備）：
        sentinel escape hatch 的 key 是公開字串，`call_kw` 又把呼叫端 context
        **整包**換進 env——租戶在 RPC payload 塞任何 JSON 表達得出的 truthy
        值都不得開門（#522 在 18.0 實測過裸真值版真的會被打開並 commit）。
        真 sentinel 的正向對照在 PositiveControls（物件身分 JSON 帶不動，
        只有 server 端 in-process 碼設得進去）。"""
        self.authenticate(self.alice_login, self.alice_login)
        for forged in (True, 1, 'true', [1], {'x': 1}):
            result = self._call_kw(
                'woow_paas_platform.workspace_access', 'write',
                [[self.alice_access.id], {'role': ROLE_ADMIN}],
                kwargs={'context': {'woow_ws_last_owner_demote': forged}})
            self._assert_validation_error(
                result,
                '偽造 context（%r）不得打開 last-owner demote 逃生口' % (forged,))
            self.alice_access.invalidate_recordset()
            self.assertEqual(self.alice_access.role, ROLE_OWNER,
                             '偽造 context（%r）被拒後 role 不得變動' % (forged,))

    def test_bare_sudo_wrap_demote_denied(self):
        """#711 那格的反向測試：伺服器端碼把租戶操作包進 `sudo()`（未帶
        sentinel opt-in）demote 最後一個 owner，必須 fail-loud。

        刻意走直接 ORM 而非 HTTP RPC——這條測的不是 RPC 攻擊面，是「未來
        某個 controller 順手 `sudo().write`」的伺服器端形狀；`with_user
        (alice).sudo()` 正是那個形狀（su=True、user 非 group_system）。
        pre-fix RED：write 直接成功、0-owner 落地——本檔既有兩道守衛都
        豁免 su，整條無人攔。"""
        with self.assertRaises(ValidationError):
            self.alice_access.with_user(self.alice).sudo().write(
                {'role': ROLE_ADMIN})
        self.assertEqual(self.alice_access.sudo().role, ROLE_OWNER,
                         '拒絕後 Alice 必須仍是 owner')


@tagged('post_install', '-at_install')
class TestLastOwnerDemotePositiveControls(_LastOwner732Fixture):
    """陰陽對照：防過度修正。pre-fix 這些本來就綠；post-fix 必須仍綠——
    紅了代表守衛擋過頭（blanket-deny／誤傷非 demote 寫入／打死唯一可運作
    的平台側轉移路徑）。"""

    def test_second_owner_present_self_demote_succeeds(self):
        """派工單指定的陽性對照：**尚有另一位 owner 時**自我降級必須成功。

        雙 owner 態經 ORM 不可達（`_check_owner_count` 對 su 也生效），用
        SQL 直造——這不是測不存在的產品流程，是釘守衛的形狀：判準必須是
        「數這次呼叫之外剩幾個 owner」，寫成「owner 列一律不得 demote」
        這條就紅。同時它涵蓋 legacy／手修資料真的存在雙 owner 的情形。"""
        self._force_role_sql(self.carol_access, ROLE_OWNER)
        self.assertEqual(self._owner_count(self.ws1), 2,
                         'fixture 前提：SQL 直造後 W1 必須有 2 個 owner')
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.alice_access.id], {'role': ROLE_ADMIN}])
        self._assert_call_kw_ok(
            result, '尚有另一位 owner（Carol）時，Alice 自我降級必須成功')
        self.alice_access.invalidate_recordset()
        self.assertEqual(self.alice_access.role, ROLE_ADMIN)
        self.assertEqual(self._owner_count(self.ws1), 1,
                         '降級後 W1 仍有 Carol 這個 owner——不變式未破')

    def test_owner_noop_role_write_succeeds(self):
        """no-op 對照：owner 對自己那列送 `{'role': 'owner'}`（UI 整批送
        欄位常見）不得被擋——目標值是 owner ⇒ 不是 demote。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.alice_access.id], {'role': ROLE_OWNER}])
        self._assert_call_kw_ok(result, 'no-op role write 不得被 last-owner 守衛誤擋')
        self.alice_access.invalidate_recordset()
        self.assertEqual(self.alice_access.role, ROLE_OWNER)

    def test_owner_nonrole_write_on_own_row_succeeds(self):
        """非 role 欄位對照：owner 寫自己那列的其他欄位（vals 無 'role'）
        不得觸發守衛。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.alice_access.id], {'invited_date': '2026-08-22 00:00:00'}])
        self._assert_call_kw_ok(result, '不含 role 的 write 不得觸發 last-owner 守衛')

    def test_member_role_change_still_works_via_endpoint(self):
        """產品端點對照：owner 走 `update_role` 改**非 owner 成員**的 role
        （carol user→guest）必須照常成功——日常成員管理不經過守衛的觸發
        條件（目標列不是 owner）。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._post_api(
            '/api/workspaces/%s/members/%s' % (self.ws1.id, self.carol_access.id),
            {'action': 'update_role', 'role': ROLE_GUEST})
        self.assertTrue(result.get('success'),
                        'owner 改非 owner 成員 role 應成功（回 %r）' % result)
        self.carol_access.invalidate_recordset()
        self.assertEqual(self.carol_access.role, ROLE_GUEST)

    def test_group_system_full_transfer_sequence_still_works(self):
        """豁免陽性對照（group_system，uid 2、非 superuser）：平台側
        demote-then-promote 是今天**唯一可運作**的 ownership 轉移路徑
        （promote-first 被 `_check_owner_count` 擋、租戶端點擋 owner 列、
        `transfer_ownership()` 是死碼）——整段必須不被新守衛打死。
        紅了代表豁免被拿掉＝所有 ORM 轉移路徑同時死光、平台修資料只剩
        裸 SQL。"""
        self.authenticate('admin', 'admin')
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.alice_access.id], {'role': ROLE_ADMIN}])
        self._assert_call_kw_ok(
            result, 'group_system demote 現任 owner（轉移第一步）應成功')
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'write',
            [[self.carol_access.id], {'role': ROLE_OWNER}])
        self._assert_call_kw_ok(
            result, 'group_system promote 新 owner（轉移第二步）應成功')
        self.assertEqual(self._owner_count(self.ws1), 1,
                         '轉移完成後 W1 恰有 1 個 owner')
        self.carol_access.invalidate_recordset()
        self.assertEqual(self.carol_access.role, ROLE_OWNER,
                         '轉移完成後 owner 是 Carol')

    def test_zero_owner_promote_repair_path_works(self):
        """修復路徑陽性對照：workspace 真的落在 0-owner 態（legacy 資料，
        SQL 直造）時，promote 方向（把某成員升 owner）必須照常可用——
        守衛只擋 demote，0-owner 的**修復**不需要任何豁免（0 其他 owner ⇒
        `_check_owner_count` 放行）。"""
        self._force_role_sql(self.alice_access, ROLE_ADMIN)
        self.assertEqual(self._owner_count(self.ws1), 0,
                         'fixture 前提：SQL 直造後 W1 是 0-owner 態')
        self.alice_access.sudo().write({'role': ROLE_OWNER})
        self.assertEqual(self._owner_count(self.ws1), 1,
                         'promote 修復後 W1 恢復恰 1 個 owner')

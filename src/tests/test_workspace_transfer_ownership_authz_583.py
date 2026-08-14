# -*- coding: utf-8 -*-
"""#583：`transfer_ownership` 的授權守衛檢查「被操作那筆記錄」而非呼叫者身分。

`workspace_access.py` 的守衛原文是 `if self.role != ROLE_OWNER: raise
ValidationError(...)`——`self` 是呼叫端指定的 access 列，攻擊者只要挑一筆
`role == owner` 的記錄當 `self`（也就是現任 owner 自己那筆），守衛就通過，
它從頭到尾沒有問過 `env.user` 是誰。

## 可達性實證（2026-08-14，`origin/develop` f55eccc6 / manifest 18.0.1.0.136）

真 HTTP RPC 打過（`/web/session/authenticate` + `/web/dataset/call_kw`，
攻擊者是只掛 `base.group_user` 的真 guest 成員）。結論分兩半，本檔的測試設計
直接建立在這兩半的差別上：

- **守衛確實被通過**：guest 拿 owner 那筆當 `self`、`new_owner_id` 給一個不存在
  的 uid ⇒ 回的是**後段商業驗證**的 `ValidationError`（"The new owner must be an
  existing member of the workspace."），不是守衛的
  `ValidationError`（"Only the current owner can transfer ownership."）。
  ⇒ 執行流真的越過了守衛那一行。
- **但完整奪權鏈不可達**：攻擊步驟走到「把現任 owner 降成 admin」時被 #556 那批
  修法加進 `write()` 的 `_check_owner_row_protected` 擋下（`AccessError`）。
  ⇒ 嚴重性下修，缺陷本體仍成立。

判別式矩陣（同一個 payload、只換 `self` 與呼叫者身分，三種身分輸出**逐字相同**）：

    呼叫者        self 是哪一筆   結果
    mallory(guest)  owner 那筆    ValidationError: The new owner must be...
    mallory(guest)  自己那筆      ValidationError: Only the current owner can transfer ownership.
    dave(admin)     owner 那筆    ValidationError: The new owner must be...
    dave(admin)     自己那筆      ValidationError: Only the current owner can transfer ownership.
    alice(owner)    owner 那筆    ValidationError: The new owner must be...
    alice(owner)    自己那筆      ValidationError: Only the current owner can transfer ownership.

⇒ 結果完全由 `self` 決定、與呼叫者身分零相關。**守衛對身分的鑑別力是 0。**

## 🔴 為什麼不能照 issue 字面寫成「斷言 AccessError」

issue 的驗收要求第一條是「以 guest 身分呼叫 `transfer_ownership`，斷言被拒」。
照字面寫（guest → owner 那筆 → 斷言 `AccessError`）**修法前後都綠**：今天它已經
是 `AccessError`，只是那個 deny 來自下游的 `_check_owner_row_protected`，與本單
要修的守衛無關。那種測試對本單的修法**零鑑別力**。

本檔的 RED 來源是 `TestTransferOwnershipGateIsCallerScoped` 那三條：
**共用同一個 payload（`new_owner_id` 給不存在的 uid），只換呼叫者身分**。

    測試              修法前            修法後
    guest             ValidationError   AccessError    🔴 RED
    admin             ValidationError   AccessError    🔴 RED
    owner（陽性對照）   ValidationError   ValidationError  綠 → 綠

通過與否只可能來自 `env.user` ⇒ 這正是要驗的性質。
🔴 **第三條（owner）不可刪**：沒有它，「方法整個壞掉、所有人都拿 AccessError」
也會讓前兩條變綠 —— 那時整組測試會靜默退化成「這個 method 還活著嗎」的檢查。

## 🔴 前提斷言與攻擊斷言必須寫在同一條測試裡

record rule 一收窄（例如日後把 `workspace_access_rule_member_read` 收成只看得到
自己那列），攻擊者就再也讀不到 owner 那筆，所有攻擊斷言會**靜默退化成恆真**而仍然
全綠。所以每一條攻擊測試都先斷言「攻擊者確實讀得到 owner 那筆記錄」。
**不要把它抽成獨立的一條測試**——對照與攻擊一旦分離，日後有人動壞了對照那條，
攻擊測試會變成零鑑別力的綠燈而沒有任何訊號（#668 第 ② 項的同型前車之鑑）。

## 測試手段的紀律

- **全部走真 HTTP RPC**（`/web/dataset/call_kw`）。禁止 `TransactionCase`：它預設
  `su=True`，ir.rule 不套用、guard 的 deny 分支結構上不可達，恆綠。
- fixture user 一律只掛 `base.group_user`（guard 對 `group_system` 有豁免，混用會
  讓整組測試對租戶攻擊向零鑑別力）。
- **斷言用例外型別**（`error.data.name` 的 fully-qualified class name），不比對文案
  片段——文案會改，比對文案的測試是脆的。
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_OWNER,
    ROLE_USER,
)

ACCESS_ERROR = 'odoo.exceptions.AccessError'
VALIDATION_ERROR = 'odoo.exceptions.ValidationError'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 583' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _TransferOwnership583Fixture(HttpCase):
    """W1（alice owner、carol user、mallory guest、dave admin）
    ＋ W2（bob owner，用來當 W1 的非成員攻擊者）。五人皆只掛 `base.group_user`。
    """

    def setUp(self):
        super().setUp()
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Workspace = self.env['woow_paas_platform.workspace']

        self.alice, self.alice_login = _new_group_user(self.env, 'alice583')
        self.mallory, self.mallory_login = _new_group_user(self.env, 'mallory583')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave583')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol583')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob583')

        for u in (self.alice, self.mallory, self.dave, self.carol, self.bob):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        # setUp 走 HttpCase 預設的 superuser env——只是為了搭 fixture，
        # 不是本檔要驗的路徑（要驗的路徑一律走下面的 _call_kw）。
        self.ws1 = self.Workspace.create({'name': 'WS1-583', 'owner_id': self.alice.id})
        self.ws2 = self.Workspace.create({'name': 'WS2-583', 'owner_id': self.bob.id})

        self.alice_access = self.WorkspaceAccess.search([
            ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.alice.id)])
        self.assertEqual(
            self.alice_access.role, ROLE_OWNER,
            'fixture 前提：workspace.create() 必須把 owner 那列 bootstrap 出來')

        self.mallory_access = self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws1.id, 'user_id': self.mallory.id,
            'role': ROLE_GUEST})
        self.dave_access = self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws1.id, 'user_id': self.dave.id,
            'role': ROLE_ADMIN})
        self.carol_access = self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws1.id, 'user_id': self.carol.id,
            'role': ROLE_USER})

        # 一個保證不存在的 uid：讓 transfer_ownership 的**後段商業驗證**
        # （"The new owner must be an existing member"）成為「閘門有沒有放行」
        # 的觀測窗。刻意不用寫死的 999999——那在別的 DB 上可能真的存在。
        self.absent_uid = self.env['res.users'].sudo().search(
            [], order='id desc', limit=1).id + 1000
        self.assertFalse(
            self.env['res.users'].sudo().browse(self.absent_uid).exists(),
            'fixture 前提：absent_uid 必須真的不存在')

    # ------------------------------------------------------------------ #
    #  使用者唯一入口：真 HTTP JSON-RPC                                    #
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
        # 切身分／每次呼叫之間清掉測試端 env 的 ORM cache：被高權限讀過的記錄
        # 若留在 cache 裡，之後的低權限讀可能命中 cache 而不觸發 AccessError。
        self.env.invalidate_all()
        return resp.json()

    def _transfer(self, access_id, new_owner_id):
        return self._call_kw(
            'woow_paas_platform.workspace_access', 'transfer_ownership',
            [[access_id], new_owner_id])

    # ------------------------------------------------------------------ #
    #  斷言 helper——比對例外**型別**（fully-qualified class name），不比文案  #
    # ------------------------------------------------------------------ #
    def _assert_exception(self, result, expected, msg):
        self.assertIn('error', result,
                      msg + '（期望 %s，但呼叫成功了：%r）' % (expected, result))
        name = result['error'].get('data', {}).get('name', '')
        self.assertEqual(name, expected, msg + '（回 %r）' % result)

    def _assert_reads_owner_row(self, msg):
        """前提斷言：當前身分**確實讀得到** owner 那筆記錄。

        沒有這條，record rule 一收窄，下面的攻擊斷言就會靜默退化成恆真。
        """
        rows = self._call_kw(
            'woow_paas_platform.workspace_access', 'search_read',
            [[['workspace_id', '=', self.ws1.id], ['role', '=', ROLE_OWNER]],
             ['id', 'role']])
        self.assertNotIn('error', rows, msg + '（讀取本身就失敗了：%r）' % rows)
        self.assertEqual(
            [r['id'] for r in rows['result']], [self.alice_access.id],
            msg + '（讀到的是 %r）' % rows)

    def _assert_ownership_untouched(self, msg):
        """奪權必須沒有發生：access 列的 role 與 workspace.owner_id 都不變。"""
        self.env.invalidate_all()
        roles = {
            a.user_id.id: a.role
            for a in self.WorkspaceAccess.sudo().search(
                [('workspace_id', '=', self.ws1.id)])
        }
        self.assertEqual(roles.get(self.alice.id), ROLE_OWNER,
                         msg + '：alice 不再是 owner（%r）' % roles)
        self.assertEqual(roles.get(self.mallory.id), ROLE_GUEST,
                         msg + '：mallory 的 role 被改動了（%r）' % roles)
        self.assertEqual(roles.get(self.dave.id), ROLE_ADMIN,
                         msg + '：dave 的 role 被改動了（%r）' % roles)
        self.assertEqual(
            self.ws1.sudo().owner_id.id, self.alice.id,
            msg + '：workspace.owner_id 被改寫了')


@tagged('post_install', '-at_install')
class TestTransferOwnershipGateIsCallerScoped(_TransferOwnership583Fixture):
    """🔴 本組是本單的 RED —— 三條共用同一個 payload，只換呼叫者身分。

    payload 固定為 `transfer_ownership([alice 的 owner 列], <不存在的 uid>)`：

    - 閘門**放行** ⇒ 執行流走到後段的 new_owner 查詢 ⇒ `ValidationError`
    - 閘門**擋下** ⇒ `AccessError`

    ⇒ 例外型別就是「閘門有沒有問過你是誰」的觀測窗，且**不需要比對文案**。
    修法前三條都回 `ValidationError`（守衛只看 `self.role`，而 `self` 是 owner
    那筆）；修法後只有 owner 那條還是 `ValidationError`。
    """

    def test_guest_is_denied_at_the_gate_not_at_business_validation(self):
        self.authenticate(self.mallory_login, self.mallory_login)
        # 前提斷言（必須與攻擊斷言同在一條測試裡）
        self._assert_reads_owner_row(
            'mallory 必須讀得到 owner 那筆，否則本條攻擊斷言是恆真的')
        self._assert_exception(
            self._transfer(self.alice_access.id, self.absent_uid),
            ACCESS_ERROR,
            'guest 必須在授權閘被擋下（AccessError），而不是通過閘門、'
            '倒在後段的 new_owner 商業驗證（ValidationError）')

    def test_admin_is_denied_at_the_gate_not_at_business_validation(self):
        """admin 是非 owner 裡權限最高的 —— 它也必須被擋在閘門。"""
        self.authenticate(self.dave_login, self.dave_login)
        self._assert_reads_owner_row(
            'dave 必須讀得到 owner 那筆，否則本條攻擊斷言是恆真的')
        self._assert_exception(
            self._transfer(self.alice_access.id, self.absent_uid),
            ACCESS_ERROR,
            'admin 必須在授權閘被擋下（AccessError）')

    def test_owner_passes_the_gate_positive_control(self):
        """🔴 陽性對照，不可刪。

        同一個 payload 由 owner 打出去必須**通過閘門**，觀測窗是它倒在後段的
        商業驗證（`ValidationError`）而不是授權（`AccessError`）。

        沒有這一條，「這個 method 對所有人都壞掉、一律回 AccessError」也會讓上面
        兩條變綠 —— 整組測試會從「閘門看不看呼叫者」退化成「method 還活著嗎」。
        """
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_exception(
            self._transfer(self.alice_access.id, self.absent_uid),
            VALIDATION_ERROR,
            'owner 必須通過授權閘、倒在後段的 new_owner 商業驗證；'
            '若回 AccessError 表示閘門把合法 owner 也擋掉了')


@tagged('post_install', '-at_install')
class TestTransferOwnershipAttackVectors(_TransferOwnership583Fixture):
    """issue 的驗收要求 ①②：guest 直接打、以及先自我提權再打。

    ⚠ 這兩條在修法**前後都是 AccessError**（修法前的 deny 來自下游
    `_check_owner_row_protected`，與本單的閘門無關）⇒ 它們對本次修法的鑑別力
    是 0，鑑別力在上面那組。保留它們的理由是**回歸**：它們鎖住的是
    「完整奪權鏈不可達」這個今天成立的性質，日後任何一層被放寬都會在這裡變紅。
    """

    def test_guest_transfer_owner_row_to_self_is_denied(self):
        """驗收①：guest 拿 owner 那筆當 `self`，把自己指定為新 owner。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        self._assert_reads_owner_row(
            'mallory 必須讀得到 owner 那筆（issue 攻擊步驟 1），'
            '否則本條攻擊斷言是恆真的')
        self._assert_exception(
            self._transfer(self.alice_access.id, self.mallory.id),
            ACCESS_ERROR, 'guest 不得經 transfer_ownership 奪走 workspace')
        self._assert_ownership_untouched('guest 直接奪權')

    def test_guest_self_promotion_then_transfer_is_denied(self):
        """驗收②：**先自我提權**再打。

        issue 的修法方向警告：「授權閘的強度等於它依賴欄位的最弱寫入保護」。
        本條把那個依賴面一起鎖住 —— 自我提權那一步本身必須先被擋（第一段），
        閘門才不會被「改資料再過閘」繞過（第二段）。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._assert_reads_owner_row(
            'mallory 必須讀得到 owner 那筆，否則第二段攻擊斷言是恆真的')

        # 第一段：自我提權到 owner —— 必須被擋
        self._assert_exception(
            self._call_kw(
                'woow_paas_platform.workspace_access', 'write',
                [[self.mallory_access.id], {'role': ROLE_OWNER}]),
            ACCESS_ERROR, 'guest 不得把自己那列寫成 owner')
        # 第一段（弱 payload）：提權到 admin 也必須被擋
        self._assert_exception(
            self._call_kw(
                'woow_paas_platform.workspace_access', 'write',
                [[self.mallory_access.id], {'role': ROLE_ADMIN}]),
            ACCESS_ERROR, 'guest 不得把自己那列寫成 admin')
        self.env.invalidate_all()
        self.assertEqual(
            self.mallory_access.sudo().role, ROLE_GUEST,
            '自我提權被擋之後，mallory 那列必須仍是 guest')

        # 第二段：提權失敗後再打閘門
        self._assert_exception(
            self._transfer(self.alice_access.id, self.mallory.id),
            ACCESS_ERROR, '自我提權失敗後，transfer_ownership 仍必須被擋')
        self._assert_ownership_untouched('guest 自我提權後奪權')

    def test_non_member_cannot_even_reach_the_row(self):
        """非成員（bob 只是 W2 的 owner）打 W1 的 owner 列。

        ⚠ 這一條的 deny 來自 **record rule 那一層**（`bob` 讀不到 W1 的任何
        access 列），不是本單的閘門 —— 前提斷言在這裡是**反向**的：先斷言
        「他確實讀不到」，這條測試的語意才明確。
        """
        self.authenticate(self.bob_login, self.bob_login)
        rows = self._call_kw(
            'woow_paas_platform.workspace_access', 'search_read',
            [[['workspace_id', '=', self.ws1.id]], ['id', 'role']])
        self.assertNotIn('error', rows, '讀取本身不該報錯，只該回空集合：%r' % rows)
        self.assertEqual(
            rows['result'], [],
            'bob 不是 W1 成員，record rule 必須讓他一列都看不到（回 %r）' % rows)

        self._assert_exception(
            self._transfer(self.alice_access.id, self.bob.id),
            ACCESS_ERROR, '非成員不得經 transfer_ownership 奪走 workspace')
        self._assert_ownership_untouched('非成員奪權')

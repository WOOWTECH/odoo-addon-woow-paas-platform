# -*- coding: utf-8 -*-
"""#525 A3：`_invite_member` 吞掉例外 → 被邀請者其實已入群，API 卻說邀請失敗。

`try` 內唯一的寫入是 `WorkspaceAccess.create()`。例外被 `except Exception` 吃掉、
以 HTTP 200 + `success: False` 返回 → 不傳播到框架層 → request 正常結束 →
**transaction 照常 commit** → 那一列授權留了下來：**被邀請者實際已取得該
workspace 的成員權限**，而 API 說失敗。

加上 `unique_user_workspace`（`UNIQUE(workspace_id, user_id)`）與 controller 自己
在 create 之前的「已是成員」前置檢查，使用者會被鎖在一個自相矛盾的狀態裡：

    第一次邀請 → 顯示「邀請失敗」（殘留已落地）
    再邀一次   → 顯示「User is already a member of this workspace」

## 可達性（重要，先讀）

**今天的規則與 constraint 下，這條路徑不是穩定可重現的**——#525 A3 的可達性註記
已經盤過，本檔覆核後同意：

- `_check_owner_count`（唯一的 `@api.constrains`）只在 `role == 'owner'` 時拋，而
  controller 的 `ASSIGNABLE_ROLES`（admin/user/guest）擋掉了 owner。
- `unique_user_workspace` 撞車拋的是 psycopg2 `IntegrityError`，交易當場毀損，
  最終是框架層錯誤而非「靜默殘留」。
- `check_access('create')` 的 `AccessError` 發生在 INSERT **之前**，零殘留；
  而 `workspace_access_rule_create` 目前對 `base.group_user` 全開。

真正產生殘留的視窗在 `models.py` 的「INSERT 已下 → `_validate_fields` 才跑」之間。
本檔用 patch 在那個視窗注入例外來取得可達的 RED——**這是模擬一個今天還不存在的
constrains**，不是重現今天就會發生的線上事故。結構缺陷成立且與 #520／A1 同構，
規則或 constrains 一收緊（#525 A3 明說的情境）殘留立即可達。

**刻意沒有做的事**：不放寬 `ASSIGNABLE_ROLES`、不改 `ir.rule`。那是拿放寬安全
設定去換一個綠燈，換到的紅燈也不是真的。

## 為什麼注入點要挑在 `_validate_fields`，而且要模仿 `@api.constrains` 的欄位過濾

`_validate_fields` 迭代的 `self._constraint_methods` 存的是 **registry setup 當下
抓下來的 function 物件**，所以 patch `_check_owner_count` 這個類別屬性**不會生效**
（測試會靜默地什麼都沒注入）。掛在 `_validate_fields` 本身才是走一般屬性查找、
patch 得到的位置，而它正是 INSERT 之後的第一站：`models.py` 的 `_create` 尾端
`records._validate_fields(...)` → `records.check_access('create')` → `return`。

注入**必須帶欄位過濾**（只在 `role`／`workspace_id` 被寫入時才拋），否則不忠實：
`WorkspaceAccess` 有兩個 stored related 欄位（`user_name`／`user_email`），它們的
重算被延到 request 尾端 `cr.flush()`，並在 `_compute_field_value` 裡對**那兩個
欄位**再跑一次 `_validate_fields`。無差別觸發的注入會在那裡**第二次**拋例外，而那
時已經離開 controller 的 `try` ⇒ 例外逸出到框架 → 整筆交易被框架回捲 → 殘留歸零、
回應變成框架層錯誤。真正的 `@api.constrains('role', 'workspace_id')` 因為欄位不
相交而不會被那次重算叫到，所以只會在 `_create` 內觸發一次、殘留得以留存。

（初版注入沒有欄位過濾，`test_injection_fires_after_the_access_row_is_inserted`
先紅並指出回應是框架錯誤而非 `success: False` —— 這就是那條前提斷言存在的理由。）

## 測試結構

依 #525 comment 的要求，「沒有殘留」的斷言配一條把**前提**釘成斷言的測試：
`test_injection_fires_after_the_access_row_is_inserted` 證明注入觸發時該列已經
拿到 id（＝INSERT 已下）。少了它，`WorkspaceAccess.create` 一被重排，殘留不再
產生，「沒有殘留」就全部退化成恆真。
"""
import json

from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import WorkspaceAccess

_INVITEE_EMAIL = 'invitee-525-a3@test.example'


@tagged('post_install', '-at_install')
class TestInviteMemberLeavesNoAccessRow(HttpCase):
    """邀請失敗時不得留下授權列，使用者要能重邀。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 525 A3', 'owner_id': self.admin_user.id})
        self.invitee = self.env['res.users'].sudo().create({
            'name': 'Invitee 525 A3',
            'login': 'invitee-525-a3',
            'email': _INVITEE_EMAIL})

    # ── 使用者唯一入口（HTTP JSON-RPC）─────────────────────────────────
    def _post(self, params):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
        resp = self.url_open(
            '/api/workspaces/%s/members' % self.workspace.id,
            data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    def _invite(self, role='user'):
        return self._post({'action': 'invite', 'email': _INVITEE_EMAIL,
                           'role': role})

    def _listed_member_emails(self):
        result = self._post({'action': 'list'})
        self.assertTrue(result.get('success'), result)
        return [m.get('email') for m in result['data']]

    # ── 探針：被邀請者到底有沒有拿到成員權限 ───────────────────────────
    def _invitee_access(self):
        """`check_user_access` 是每一支 workspace 端點用來放行的那道閘。

        直接問它，等於問「這個人現在進不進得來」——比數 row 更貼近危害本身。
        """
        self.env.invalidate_all()
        return self.workspace.sudo().check_user_access(self.invitee)

    # ── 注入：在 INSERT 之後、_validate_fields 當下拋例外 ──────────────
    def _fail_post_insert_validation(self, recorder=None):
        original = WorkspaceAccess._validate_fields

        def validate_then_boom(model_self, field_names, excluded_names=()):
            # 先物化：`_create` 傳進來的是 generator，餵給 original 之後就空了。
            names = set(field_names)
            original(model_self, names, excluded_names)
            # 模仿 `@api.constrains('role', 'workspace_id')` 的欄位過濾（見檔頭）：
            # 少了這一步，request 尾端 stored related 欄位的重算會二次觸發，例外
            # 就跑到 controller 的 try 之外去了。
            if names.isdisjoint({'role', 'workspace_id'}):
                return
            if recorder is not None:
                recorder.append(list(model_self.ids))
            raise ValidationError('injected: post-INSERT validation failed')
        return patch.object(
            WorkspaceAccess, '_validate_fields', validate_then_boom)

    # ================================================================
    #  陽性對照：成功邀請時兩個探針都看得見
    # ================================================================
    def test_successful_invite_grants_membership(self):
        self.assertFalse(self._invitee_access(),
                         'fixture 前提：被邀請者一開始不是成員')

        result = self._invite()

        self.assertTrue(result.get('success'), '邀請應成功（回 %r）' % result)
        self.assertTrue(
            self._invitee_access(),
            '成功邀請後 `check_user_access` 必須放行——探針有鑑別力的證明')
        self.assertIn(
            _INVITEE_EMAIL, self._listed_member_emails(),
            '成功邀請後成員清單必須看得到他')

    # ================================================================
    #  前提：注入確實發生在授權列 INSERT 之後
    # ================================================================
    def test_injection_fires_after_the_access_row_is_inserted(self):
        """注入點若跑到 INSERT 之前，殘留根本不會產生，下面兩條「沒有殘留」
        就退化成恆真——而假綠與真綠的輸出一模一樣。"""
        seen = []
        with self._fail_post_insert_validation(recorder=seen):
            result = self._invite()

        self.assertFalse(result.get('success'),
                         '注入後應失敗（回 %r）' % result)
        self.assertEqual(
            len(seen), 1,
            '`_validate_fields` 必須被呼叫到——沒被呼叫代表注入根本沒生效')
        self.assertTrue(
            seen[0] and all(isinstance(i, int) for i in seen[0]),
            '注入觸發時該授權列必須已經拿到 id（＝INSERT 已下），'
            '「沒有殘留」的斷言才有守到東西；實際拿到 %r' % (seen[0],))

    # ================================================================
    #  殘留：授權列
    # ================================================================
    def test_failed_invite_does_not_grant_membership(self):
        with self._fail_post_insert_validation():
            result = self._invite()

        self.assertFalse(result.get('success'),
                         '注入後應失敗（回 %r）' % result)
        self.assertFalse(
            self._invitee_access(),
            'API 回「邀請失敗」，被邀請者就不得實際取得成員權限——殘留的授權列'
            '會讓他直接讀寫該 workspace 的服務與專案')

    # 註：`_create_workspace` 的「回應組裝也被包進 try」在 A2 有對應測試
    # （`test_response_assembly_failure_does_not_leave_a_workspace_behind`），
    # 這裡**沒有**同名測試是刻意的：`_invite_member` 的回應組裝只讀
    # `new_access.id`（純記憶體、不碰 DB）與已 sudo 取得的 `target_user`，造不出
    # 可達的失敗。controller 那邊仍把組裝移出 try——那是與 A2 對齊的結構防護，
    # 不是修掉一個今天測得出來的缺陷。（初版曾寫過一條，注入其實沒讓組裝失敗、
    # 端點照樣回 `success: True`，已移除而非改成硬湊的紅燈。）

    # ================================================================
    #  使用者要能自我修復：重邀
    # ================================================================
    def test_user_can_retry_the_invite_after_a_failure(self):
        """失敗 → 排除原因 → 再邀一次，必須成功。

        殘留的授權列會撞上 controller 在 create 之前的「已是成員」前置檢查
        （以及 `unique_user_workspace`），使用者被鎖在「顯示邀請失敗、再邀說
        已是成員」的矛盾狀態，兩邊都不是真的。
        """
        with self._fail_post_insert_validation():
            first = self._invite()
        self.assertFalse(first.get('success'),
                         '第一次應失敗（回 %r）' % first)

        retry = self._invite()          # 排除原因後重邀

        self.assertTrue(
            retry.get('success'),
            '排除原因後重邀必須成功；失敗代表使用者被自己上一次的殘留擋住，'
            '無法自救（回 %r）' % retry)

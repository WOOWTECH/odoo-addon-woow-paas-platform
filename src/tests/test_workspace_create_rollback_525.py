# -*- coding: utf-8 -*-
"""#525 A2：`_create_workspace` 吞掉例外 → 留下一筆使用者永遠看不到、也刪不掉的孤兒。

controller 只呼叫一次 `Workspace.create()`，但 model 覆寫（`workspace.py` 的
`create`）把它拆成**兩段非原子寫入**：

    super().create()                    → INSERT 主表列
    workspace_access.create({owner})    → 建 owner 的授權列

第二段拋 Python 層例外時，主表列已經進去了。例外被 controller 的
`except Exception` 吃掉、以 HTTP 200 + `success: False` 返回 → 不傳播到框架層
→ request 正常結束 → **transaction 照常 commit** → 孤兒落地。

孤兒的兩個後果都不會讓使用者察覺，只會累積：

1. **完全不可見**——`_list_workspaces` 是**從 workspace_access 反查** workspace
   的，而 `ir.rule` `workspace_rule_member_read` 的 domain 是
   `[('access_ids', 'any', [('user_id', '=', user.id)])]`。沒有 access 列的
   workspace 對非系統管理員的非成員不可見。#1678 起 `base.group_system`
   可在後台透過原始 ORM 讀全部 Workspace；產品 list 仍從成員列反查，
   不因此向沒有成員列的呼叫者顯示孤兒。
   （#658 之前這條叫 `workspace_rule_member_access`、讀寫合一；#1678
   只增加系統管理員的全域 read，不增加 write/unlink。）
2. **永久佔住原本的 slug**——`unique_slug` 是 DB 層 constraint，**不看 ir.rule**。
   #1516 後 `_generate_slug` 以 sudo 全域查重，包含使用者看不到的孤兒；同名重試
   會成功並取得 6 碼隨機 hex 後綴，但原本的 base slug 仍被孤兒佔住。

   歷史 RED（2026-08-02）中，舊產生器以使用者身分查重、看不到孤兒，回傳同一個
   slug，INSERT 撞 `unique_slug`，交易毀損後以框架層 500 收場。#1516 已修正這個
   重試後果，但不能取代 #525 的 savepoint：不可見且刪不掉的孤兒仍不該被留下。

第二個殘留來源在同一段：原本的 `except Exception` 一路包到**純讀取的回應組裝**，
連組裝時的例外都會被歸類成「建立失敗」，但那時 workspace 其實已完整建成（含
owner access）——使用者收到 `success: False`，東西卻真的在那裡。

## 測試結構（#525 comment「斷言沒有殘留要連前提一起斷言」）

「沒有殘留」的斷言，有效性依賴一個前提：**那條路徑本來會產生殘留**。前提若只
寫在註解裡，`Workspace.create` 一被重排（例如改成先建 access 再建主表列），殘留
不再產生、所有「沒有殘留」的斷言**全部退化成恆真**，假綠與真綠的輸出一模一樣。
所以本檔：

- `test_injection_fires_after_the_workspace_row_is_inserted` 把前提釘成斷言：
  注入點觸發的當下，主表列**已經** INSERT 進去了。
- 兩條 `test_orphan_*` 用**直接造出來的**孤兒（不經缺陷）把上述兩個後果釘成
  事實——它們在修好之後仍然成立，是「為什麼這個殘留有害」的長期文件，也讓
  slug／可見性兩個探針有鑑別力。
- `test_successful_create_*` 是四個探針的陽性對照。
"""
import json

from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace import Workspace
from odoo.addons.woow_paas_platform.models.workspace_access import WorkspaceAccess

_WS_NAME = 'WS 525 A2'
_WS_SLUG = 'ws-525-a2'


@tagged('post_install', '-at_install')
class TestCreateWorkspaceLeavesNoOrphan(HttpCase):
    """建立失敗時不得留下孤兒 workspace，使用者要能用同名重試。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.Workspace = self.env['woow_paas_platform.workspace']

    # ── 使用者唯一入口（HTTP JSON-RPC）─────────────────────────────────
    def _post(self, url, params):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.env.invalidate_all()
        return parsed

    def _create(self, name=_WS_NAME, description=''):
        """回**整包** JSON-RPC 回應：有一條測試要分辨「回 result」與「回 error」。"""
        return self._post('/api/workspaces', {
            'action': 'create', 'name': name, 'description': description})

    def _listed_names(self):
        """使用者實際看得到的 workspace 名單（走 list 端點，非直接查 DB）。"""
        parsed = self._post('/api/workspaces', {'action': 'list'})
        self.assertIn('result', parsed, 'list 端點應回 result: %s' % parsed)
        return [w['name'] for w in parsed['result']['data']]

    # ── 探針 ───────────────────────────────────────────────────────────
    def _rows(self, name=_WS_NAME):
        """DB 實體探針。測試 env 是 superuser，看得到孤兒——使用者看不到。"""
        self.env.invalidate_all()
        return self.Workspace.sudo().with_context(active_test=False).search(
            [('name', '=', name)])

    # ── 注入：讓 `Workspace.create()` 的第二段失敗 ─────────────────────
    def _fail_owner_access_create(self, recorder=None):
        """讓建立 owner 授權列那一段失敗，主表列則已經 INSERT 完成。

        真實對應：`workspace_access` 的 create ACL／`ir.rule` 一收緊，Odoo 在
        `create()` 開頭的 `check_access` 就 raise `AccessError`——**在 INSERT 之
        前**，所以殘留正好是 #525 A2 描述的孤兒形狀（主表列在、access_ids 空）。
        #525 A3 的可達性註記也指向同一件事：規則一改，殘留立即可達。

        `recorder` 給的話，觸發當下把「主表列是否已經在 DB 裡」記進去。
        """
        def boom(model_self, vals_list):
            if recorder is not None:
                recorder.append(
                    model_self.env['woow_paas_platform.workspace'].sudo()
                    .search_count([('name', '=', _WS_NAME)]))
            raise AccessError(
                'You are not allowed to create Workspace User Access records.')
        return patch.object(WorkspaceAccess, 'create', boom)

    def _break_response_assembly(self):
        """workspace 完整建成（含 owner access），但**回應組裝**炸掉。

        代表 #525 A2 點名的第二個殘留來源：原本的 `except Exception` 包到純讀取
        的回應組裝，於是「組裝出錯」被回報成「建立失敗」，東西卻真的建好了。
        這裡用「回傳一個指向不存在 id 的 recordset」讓組裝時的 `.name` 拋
        `MissingError`——它站在「組裝區任何例外」的位置上。
        """
        original = Workspace.create

        def create_then_return_missing(model_self, vals_list):
            original(model_self, vals_list)
            return model_self.browse(2 ** 31 - 1)
        return patch.object(Workspace, 'create', create_then_return_missing)

    # ================================================================
    #  陽性對照：成功路徑上四個探針都看得見
    # ================================================================
    def test_successful_create_produces_workspace_with_owner_access(self):
        parsed = self._create()

        self.assertIn('result', parsed, '建立應回 result: %s' % parsed)
        result = parsed['result']
        self.assertTrue(result.get('success'), '建立應成功（回 %r）' % result)
        self.assertEqual(result['data']['slug'], _WS_SLUG,
                         '第一次建立應拿到乾淨的 slug（回 %r）' % result)
        rows = self._rows()
        self.assertEqual(len(rows), 1, '成功建立的 workspace 必須查得到')
        self.assertEqual(
            rows.access_ids.mapped('role'), ['owner'],
            '成功建立必須連帶產生 owner 授權列——這是「孤兒」的反面')
        self.assertIn(_WS_NAME, self._listed_names(),
                      'list 端點必須看得到它——可見性探針有鑑別力的證明')

    # ================================================================
    #  孤兒不可見且佔住 base slug；#1516 後同名重試須改用隨機後綴
    # ================================================================
    def test_orphan_workspace_is_invisible_via_the_list_endpoint(self):
        visible = self._create()
        self.assertTrue(visible['result']['success'], visible)

        orphan = self.Workspace.sudo().create({
            'name': 'WS 525 A2 orphan', 'owner_id': self.admin_user.id})
        # #556 B2：workspace_access.unlink() 現在守「不得移除最後一個
        # owner」，連 .sudo() 呼叫都擋（照抄 project_access.unlink() 的
        # 不變式，無 sudo 豁免）——ORM unlink() 造不出孤兒了。production
        # 的孤兒本來就不是「先建好再刪掉」，是 owner-access create 那段
        # 從未插入過（見本檔檔頭）；改走 DB 層直接 DELETE，繞過 Python
        # unlink() override，忠實重現「這一列從未存在」的真實孤兒形狀。
        self.env.cr.execute(
            "DELETE FROM woow_paas_platform_workspace_access "
            "WHERE workspace_id = %s", (orphan.id,))
        self.env.invalidate_all()
        self.assertFalse(orphan.access_ids, 'fixture 前提：孤兒沒有任何授權列')

        names = self._listed_names()
        # 陽性對照先行：正常的那筆看得到 ⇒ 下面的「看不到」不是探針壞掉。
        self.assertIn(_WS_NAME, names, 'list 端點應看得到正常 workspace')
        self.assertNotIn(
            'WS 525 A2 orphan', names,
            '孤兒在產品 list 不可見（從 workspace_access 反查），'
            '不因 #1678 的管理員後台全域 read 而出現在產品列表')

    def test_orphan_workspace_holds_the_slug_it_is_invisible_in(self):
        orphan = self.Workspace.sudo().create({
            'name': _WS_NAME, 'owner_id': self.admin_user.id})
        # #556 B2：見上一條測試的註解——ORM unlink() 現在擋最後一個
        # owner，連 sudo 都擋，改走 DB 層直接 DELETE 繞過它。
        self.env.cr.execute(
            "DELETE FROM woow_paas_platform_workspace_access "
            "WHERE workspace_id = %s", (orphan.id,))
        self.env.invalidate_all()
        self.assertEqual(orphan.slug, _WS_SLUG, 'fixture 前提：孤兒佔住該 slug')

        # DB 層看得到（unique_slug 是 SQL constraint，不看 ir.rule）
        self.assertEqual(
            self.Workspace.sudo().search_count([('slug', '=', _WS_SLUG)]), 1,
            'slug 確實被佔住——unique_slug 是 DB 層約束')
        # #1678：管理員後台可全域讀；一般非成員仍受 ir.rule 限縮。
        administrator_workspace = self.Workspace.with_user(self.admin_user)
        self.assertFalse(administrator_workspace.env.su)
        self.assertEqual(
            administrator_workspace.search_count([('slug', '=', _WS_SLUG)]), 1,
            '系統管理員不必加入工作區即可透過原始 ORM 查看孤兒')
        nonmember = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': '525 nonmember',
            'login': 'workspace525-slug-nonmember',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.assertTrue(nonmember.has_group('base.group_user'))
        self.assertFalse(nonmember.has_group('base.group_system'))
        self.assertFalse(orphan.access_ids.filtered(lambda access: access.user_id == nonmember))
        nonmember_workspace = self.Workspace.with_user(nonmember)
        self.assertFalse(nonmember_workspace.env.su)
        self.assertEqual(
            nonmember_workspace.search_count([('slug', '=', _WS_SLUG)]), 0,
            '一般非成員看不到孤兒，不代表 sudo 的 slug 產生器看不到它')

        retry = self._create()
        self.assertIn('result', retry, retry)
        self.assertTrue(retry['result'].get('success'), retry)
        self.assertRegex(retry['result']['data']['slug'],
                         r'^ws-525-a2-[0-9a-f]{6}$',
                         '孤兒佔住 base slug 時，同名重試應成功並取得隨機後綴')
        self.assertEqual(retry['result']['data']['name'], _WS_NAME)
        listed = self._post('/api/workspaces', {'action': 'list'})
        self.assertTrue(listed['result'].get('success'), listed)
        self.assertEqual([row['id'] for row in listed['result']['data']],
                         [retry['result']['data']['id']],
                         '同名重試可見，但孤兒仍不可出現在使用者列表')

    # ================================================================
    #  前提：注入確實發生在主表列 INSERT 之後
    # ================================================================
    def test_injection_fires_after_the_workspace_row_is_inserted(self):
        """沒有這條，下面兩條「沒有孤兒」會在 `Workspace.create` 一被重排時
        （例如改成先建 access 再建主表列）退化成恆真——殘留根本沒產生過，
        「沒有殘留」當然為真，而假綠與真綠的輸出一模一樣。"""
        seen = []
        with self._fail_owner_access_create(recorder=seen):
            parsed = self._create()

        self.assertIn('result', parsed, parsed)
        self.assertFalse(parsed['result'].get('success'),
                         '注入後應失敗（回 %r）' % parsed['result'])
        self.assertEqual(
            seen, [1],
            '注入必須落在主表列 INSERT **之後**；否則「沒有孤兒」不是在守什麼'
            '東西，只是孤兒從來沒被造出來過')

    # ================================================================
    #  殘留：孤兒 workspace
    # ================================================================
    def test_failed_create_leaves_no_workspace_row(self):
        with self._fail_owner_access_create():
            parsed = self._create()

        self.assertFalse(parsed['result'].get('success'),
                         '注入後應失敗（回 %r）' % parsed['result'])
        self.assertFalse(
            self._rows(),
            '建立失敗不得留下 workspace 主表列——沒有 access 列的孤兒對使用者'
            '不可見、刪不掉，而且永久佔住 slug')

    # ================================================================
    #  使用者要能自我修復：同名重試拿回原本的 slug
    # ================================================================
    def test_user_can_retry_with_the_same_name_and_get_the_intended_slug(self):
        """失敗 → 排除原因 → 用同一個名字再建一次，必須拿到乾淨的 slug。

        #525 的 savepoint 撤掉失敗建立，不留下孤兒 ⇒ base slug 仍可用。
        若有孤兒佔位，#1516 雖會讓重試成功，卻只能拿到帶後綴的 slug；因此這條
        仍須斷言乾淨的 slug，不能只看「建立成功」而漏掉殘留。
        """
        with self._fail_owner_access_create():
            first = self._create()
        self.assertFalse(first['result'].get('success'),
                         '第一次應失敗（回 %r）' % first['result'])

        retry = self._create()          # 排除原因後重試

        self.assertIn('result', retry, '重試不應以框架層錯誤收場：%s' % retry)
        self.assertTrue(retry['result'].get('success'),
                        '同名重試必須成功（回 %r）' % retry['result'])
        self.assertEqual(
            retry['result']['data']['slug'], _WS_SLUG,
            '重試必須拿回原本要的 slug；拿到 %r 代表殘留孤兒還佔著位子'
            % retry['result']['data']['slug'])

    # ================================================================
    #  第二個殘留來源：回應組裝出錯被誤報成「建立失敗」
    # ================================================================
    def test_response_assembly_failure_does_not_leave_a_workspace_behind(self):
        """組裝回應時出錯 → 不得變成「回失敗、東西卻建好了」。

        原本的 `except Exception` 一路包到 314-327 的純讀取回應組裝。組裝屬於
        「已經成功之後」的工作，把它算進「建立失敗」會製造出第二種不一致：
        使用者收到 `success: False`，workspace 卻完整存在（連 owner access 都有，
        所以還看得到）。
        """
        with self._break_response_assembly():
            parsed = self._create()

        self.assertFalse(
            self._rows(),
            '回應組裝失敗時，不得留下一個「使用者被告知建立失敗、實際卻存在」'
            '的 workspace（回 %s）' % json.dumps(parsed)[:300])

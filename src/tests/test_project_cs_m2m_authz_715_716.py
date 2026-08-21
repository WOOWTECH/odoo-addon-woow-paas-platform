# -*- coding: utf-8 -*-
"""#715 / #716：#705 post-merge 獨立複驗留下的旁路與三個殘留缺口。

兩張單必須一起修：#715 的修法（把 `project_id` 納入守衛的觸發欄位）如果掛錯
邊，會把 #716 ① 的批次 create 誤擋缺口放大成「每一次 task create 都要
owner／admin」。分開修會互踩，所以守線也寫在同一個檔。

| 缺口 | 守什麼 | class |
| --- | --- | --- |
| #715 | task 換 project ⇒ `primary_cloud_service_id` 靜默換綁，守衛整道跳過 | `TestTaskMoveRebindsPrimary715` |
| #716 ① | 批次 create 對 `vals_list` 取 key 聯集 ⇒ F2 的 false positive 復發 | `TestBatchCreateUnion716` |
| #716 ② | 反向端候選集漏掉 archived project（`active_test` 預設過濾） | `TestReverseCandidateArchivedProject716` |
| #716 ③ | `(0, 0, vals)` 例外的 docstring 理由是假的（機制釘樁） | `TestNestedCreateCommand0Claim716` |

## ⚠ 本輪實測推翻了 #716 ② 的一半敘述（寫在最前面，免得下一個人照抄）

原文：「已封存 project 的綁定被 `(5)` 解掉，卻沒進候選集」。**前半是假的**——
`Many2many.write_real` 算 `old_relation` 用的是同一個 `record[fname]`，
`active_test` 對它一樣生效 ⇒ `(3)` / `(5)` / `(6)` 全都動不到已封存的那一列
（`B5` 一手實測）。⇒ 舊實作沒出事是「框架兩邊一起瞎」，不是「候選集是超集」。
修法仍然要做，但價值換成「讓超集這句話自己成立、不掛在框架某個沒寫下來的
行為上」，而不是「補上一個可被利用的漏洞」。

## #715 這條為什麼是「界線宣稱為假」而不只是 UX

`cloud_service_binding_guard` 檔頭寫著「guest／user 連自己 workspace 的
service 也不能綁」。而 `task.write({'project_id': ...})` **不指名任何載體
欄位** ⇒ `_cs_binding_diff_fields` 回空 tuple ⇒ 守衛連 snapshot 都不做；但
`primary_cloud_service_id` 是 `@api.depends('project_id.cloud_service_ids')`
的 stored compute，換 project 會讓它重算成新 project 的第一顆 service。
那是一次貨真價實的綁定變更，完全沒有經過授權檢查。

⚠ 影響面**限於同一個 workspace 內部**（guest 只搬得動他有 write 的 project；
跨租戶那格是 `project` 自己的 ir.rule 擋的，不是本守衛——#715 的
`test_H3` 做過歸因）。所以嚴重度 Medium，但界線那句話在 move 這個入口上為假。

## 這一輪刻意**不**擴大守的範圍（寫下來，免得下一輪當成漏洞再開一單）

同一個 guest 在 P2 **直接建** task 仍然會拿到 `primary = svc_a2`，而且合法
（#705 F2 的修法：create 側只為呼叫者指名的值負責）。⇒ 修完之後界線是
「guest 動不了**既有**綁定、也塞不進自己挑的 service」，不是「guest 名下不會
出現任何 service 關聯」。`M6` 就是這一格的釘樁。

## TDD 紀律

沿用 #698／#705 的樣板：全部走真 HTTP RPC（`/web/dataset/call_kw`）、否定性
斷言一律配陽性對照、主斷言落在不變式（relation／compute 實際變成什麼）而不是
「走了哪條 code path」。#716 ② 用的是 F3 那把「強制否決」判準，理由同 F3：
能走到那一格的呼叫者本來就對那些 project 有 write ⇒ 用「有沒有被擋」當判準
會恆綠。
"""
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged

from .test_project_cs_m2m_authz_698 import _Binding698Fixture

_CS = 'woow_paas_platform.cloud_service'
_PROJECT = 'project.project'
_TASK = 'project.task'


@tagged('post_install', '-at_install')
class TestTaskMoveRebindsPrimary715(_Binding698Fixture):
    """🔴 #715：guest 用「搬 task」讓 `primary_cloud_service_id` 靜默換綁。"""

    def _wa_service(self, name):
        return self._service(self.template, self.wa, name)

    def _bound_project(self, name, service):
        """建一個 project 並由 wa 的 owner 合法綁上 `service`（回 project id）。"""
        pid = self._new_project_as(self.aowner_login, name)
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, service.id)]}]),
            '前提：owner 必須綁得上自己 workspace 的 service（%s）' % name)
        return pid

    def _task_in(self, login, pid, name, expect_primary):
        """以 `login` 的身分在 `pid` 建一個 task，並確認 compute 導出的 primary。"""
        self.authenticate(login, login)
        created = self._call_kw(_TASK, 'create', [{'name': name, 'project_id': pid}])
        self._assert_ok(created, '前提：%s 必須建得起 task（%s）' % (login, name))
        tid = created['result']
        self.assertEqual(
            self.Task.sudo().browse(tid).primary_cloud_service_id, expect_primary,
            '前提：primary 必須先被 compute 成 %s（否則下面的 move 沒東西可比）'
            % expect_primary.name)
        return tid

    def test_M1_guest_move_cannot_silently_rebind_primary(self):
        """🔴 攻擊路徑本體：guest 把 task 從 P1 搬到 P2 ⇒ primary 從 svc_a
        變成 svc_a2，全程沒經過「只有 owner／admin 才能變更綁定」。

        斷言三件事：被**本守衛**擋下（歸因到 service id，不是 project 的
        ir.rule 順手擋的）、task 沒搬過去、primary 沒被換掉（交易回滾）。
        """
        svc_a2 = self._wa_service('SVC wa #2 715')
        p1 = self._bound_project('715 M1 P1', self.svc_a)
        p2 = self._bound_project('715 M1 P2', svc_a2)
        tid = self._task_in(self.gary_login, p1, 'M1 task', self.svc_a)

        moved = self._call_kw(_TASK, 'write', [[tid], {'project_id': p2}])
        self._assert_guard_denied(moved, svc_a2, 'M1')
        task = self.Task.sudo().browse(tid)
        self.assertEqual(task.project_id.id, p1,
                         'M1：整筆交易必須回滾，task 不得留在 P2')
        self.assertEqual(task.primary_cloud_service_id, self.svc_a,
                         'M1：primary 不得被換成 P2 的 service')

    def test_M2_owner_move_is_allowed_and_rebinds(self):
        """陽性對照：owner 搬同一個 task 必須成功，且 primary **確實改變**。

        缺這一格，M1 的紅可以靠「把 move 整個擋死」達成；而「primary 確實
        改變」那一句同時證明這條路徑真的有 diff 可比——綠燈不是「沒東西比」
        換來的。
        """
        svc_a2 = self._wa_service('SVC wa #2 715 M2')
        p1 = self._bound_project('715 M2 P1', self.svc_a)
        p2 = self._bound_project('715 M2 P2', svc_a2)
        tid = self._task_in(self.aowner_login, p1, 'M2 task', self.svc_a)

        moved = self._call_kw(_TASK, 'write', [[tid], {'project_id': p2}])
        self._assert_ok(moved, 'M2：owner 搬 task 是合法操作，不得被誤擋')
        task = self.Task.sudo().browse(tid)
        self.assertEqual(task.project_id.id, p2, 'M2：move 必須真的落地')
        self.assertEqual(task.primary_cloud_service_id, svc_a2,
                         'M2：綁定確實隨 move 改變（守衛有東西可比，卻放行）')

    def test_M3_admin_move_is_allowed(self):
        """界線是 owner **與 admin**（`ROLE_HIERARCHY`），不是只有 owner。"""
        svc_a2 = self._wa_service('SVC wa #2 715 M3')
        p1 = self._bound_project('715 M3 P1', self.svc_a)
        p2 = self._bound_project('715 M3 P2', svc_a2)
        tid = self._task_in(self.wamate_login, p1, 'M3 task', self.svc_a)

        moved = self._call_kw(_TASK, 'write', [[tid], {'project_id': p2}])
        self._assert_ok(moved, 'M3：admin 搬 task 不得被誤擋')
        self.assertEqual(
            self.Task.sudo().browse(tid).primary_cloud_service_id, svc_a2,
            'M3：綁定確實隨 move 改變')

    def test_M4_move_without_binding_change_is_not_blocked(self):
        """兩個 project 綁**同一顆** service ⇒ 綁定沒有 diff ⇒ guest 搬得動。

        判準是「實際綁定有沒有變」，不是「vals 有沒有提到 project_id」。缺這
        一格，M1 轉綠可以靠「只要動 project_id 就一律要求 owner／admin」達成，
        而那會把同 service 的 move 一起誤擋。
        """
        p1 = self._bound_project('715 M4 P1', self.svc_a)
        p2 = self._bound_project('715 M4 P2', self.svc_a)
        tid = self._task_in(self.gary_login, p1, 'M4 task', self.svc_a)

        moved = self._call_kw(_TASK, 'write', [[tid], {'project_id': p2}])
        self._assert_ok(
            moved, 'M4：綁定沒有變化的 move 不得被擋（回 %r）' % moved)
        task = self.Task.sudo().browse(tid)
        self.assertEqual(task.project_id.id, p2, 'M4：move 必須真的落地')
        self.assertEqual(task.primary_cloud_service_id, self.svc_a,
                         'M4：primary 不變（同一顆 service 仍在新 project 的集合裡）')

    def test_M5_guest_move_into_unbound_project_is_blocked(self):
        """移除方向：guest 把 task 搬進自己建的 unbound project ⇒ primary 被
        清空。#698 E7 的判準是「新增與移除套同一條線」，這一格守的是移除側。
        """
        p1 = self._bound_project('715 M5 P1', self.svc_a)
        p0 = self._new_project_as(self.gary_login, '715 M5 guest own')
        tid = self._task_in(self.gary_login, p1, 'M5 task', self.svc_a)

        moved = self._call_kw(_TASK, 'write', [[tid], {'project_id': p0}])
        self._assert_guard_denied(moved, self.svc_a, 'M5')
        task = self.Task.sudo().browse(tid)
        self.assertEqual(task.project_id.id, p1, 'M5：整筆交易必須回滾')
        self.assertEqual(task.primary_cloud_service_id, self.svc_a,
                         'M5：primary 不得被清空')

    def test_M6_project_id_is_not_a_create_side_trigger(self):
        """🔴 修法的爆炸半徑鎖：`project_id` 只掛 **write** 側觸發器。

        每一次 task create 都會指名 `project_id`。把它掛成兩側通吃的觸發器
        ⇒ create 退回全比對 ⇒ #705 F2 原樣復發（在已合法綁定的 project 裡
        建 task 會被要求 owner／admin）。這一格與 `Y4a` 形狀相同，但守的是
        **本次修法**的爆炸半徑，不可因為「705 已經有一條」而刪掉。
        """
        pid = self._bound_project('715 M6 P', self.svc_a)
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_TASK, 'create', [{'name': 'M6 task', 'project_id': pid}])
        self._assert_ok(
            res, 'M6：create 側不得因為 vals 有 project_id 就退回全比對')
        self.assertEqual(
            self.Task.sudo().browse(res['result']).primary_cloud_service_id,
            self.svc_a,
            'M6：compute 仍然導出綁定（證明這一格不是靠「沒有綁定變化」過關）')


@tagged('post_install', '-at_install')
class TestBatchCreateUnion716(_Binding698Fixture):
    """🟠 #716 ①：批次 create 對 `vals_list` 取 key 聯集，F2 換到批次路徑復發。

    舊實作 `fnames.update(...)` 掃完整個 `vals_list` 才決定要比對哪些欄位，
    再把結果套在**整批** records 上 ⇒ 只要有任何一筆指名了載體欄位，其他每
    一筆的 compute 導出值也會被送去授權。
    """

    def _bound_project_of_wa(self, name):
        pid = self._new_project_as(self.aowner_login, name)
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：owner 先合法綁上自己 workspace 的 service')
        return pid

    def test_B1_batch_create_union_does_not_widen(self):
        """🔴 缺口本體（#716 ① 的逐字 payload）：t1 明寫
        `primary_cloud_service_id: false`、t2 什麼都不寫 ⇒ t2 的 compute 導出值
        被 t1 的 key 拖去授權，guest 建不了 t2。
        """
        pid = self._bound_project_of_wa('716 B1')
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_TASK, 'create', [[
            {'name': 'B1 t1', 'project_id': pid, 'primary_cloud_service_id': False},
            {'name': 'B1 t2', 'project_id': pid},
        ]])
        self._assert_ok(
            res, 'B1：t2 沒有指名任何載體欄位，不得被 t1 的 key 拖去授權')
        tasks = self.Task.sudo().search([('name', 'in', ['B1 t1', 'B1 t2'])])
        self.assertEqual(len(tasks), 2, 'B1：兩筆都必須落地（回 %r）' % res)
        self.assertEqual(
            tasks.filtered(lambda t: t.name == 'B1 t2').primary_cloud_service_id,
            self.svc_a,
            'B1：t2 的 primary 仍由 compute 導出（證明這一格不是靠「沒有綁定」過關）')

    def test_B2_batch_second_vals_explicit_cross_tenant_still_blocked(self):
        """🔴 逐筆化**不得**讓後面幾筆漏檢。

        跨租戶綁定明寫在**第二筆**：只看 `vals_list[0]` 的實作會靜默放行。
        """
        pid = self._bound_project_of_wa('716 B2')
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_TASK, 'create', [[
            {'name': 'B2 t1', 'project_id': pid},
            {'name': 'B2 t2', 'project_id': pid,
             'extra_cloud_service_ids': [(4, self.svc_v.id)]},
        ]])
        self._assert_guard_denied(res, self.svc_v, 'B2')
        self.assertFalse(
            self.Task.sudo().search([('name', 'in', ['B2 t1', 'B2 t2'])]),
            'B2：整批必須回滾（跨租戶綁定連同 t1 一起不得落地）')

    def test_B3_batch_first_vals_explicit_cross_tenant_still_blocked(self):
        """對照組：同樣的綁定改寫在**第一筆**。兩格一起才涵蓋得到「只檢查某
        一個位置」這一族實作錯誤。
        """
        pid = self._bound_project_of_wa('716 B3')
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_TASK, 'create', [[
            {'name': 'B3 t1', 'project_id': pid,
             'extra_cloud_service_ids': [(4, self.svc_v.id)]},
            {'name': 'B3 t2', 'project_id': pid},
        ]])
        self._assert_guard_denied(res, self.svc_v, 'B3')
        self.assertFalse(
            self.Task.sudo().search([('name', 'in', ['B3 t1', 'B3 t2'])]),
            'B3：整批必須回滾')


@tagged('post_install', '-at_install')
class TestReverseCandidateArchivedProject716(_Binding698Fixture):
    """🟡 #716 ②：反向端候選集漏掉 archived project。

    `self.sudo().project_ids` 是一次 ORM 讀取，`active_test` 預設 `True`
    ⇒ `Many2many.convert_to_record_multi` 會把 `active=False` 的 comodel
    record 濾掉（`Many2many.read` 自己雖然帶 `active_test=False` 撈 SQL，
    過濾發生在轉成 recordset 那一步）⇒ relation 那一列還在，候選集卻沒有它。

    🔑 **本輪實測更正了 #716 ② 的一半敘述**：原文寫「已封存 project 的綁定被
    `(5)` 解掉，卻沒進候選集」。前半是假的——`Many2many.write_real` 算
    `old_relation` 時用的是**同一個** `record[fname]`，所以它也看不到已封存
    的那一列 ⇒ `(3)` / `(5)` / `(6)` 全都**動不到**它（`B5` 一手實測：同一次
    `(5, 0, 0)` 把未封存的解掉、已封存的原封不動）。⇒ 舊實作沒出事，是因為
    框架兩邊一起瞎，不是因為候選集真的是超集。

    ⇒ 修法的價值是**讓「候選集 ⊇ 實際變動」自己成立**，不再掛在框架某個沒寫
    下來的行為上；方向只會過寬，符合 `_assert_reverse_binding_authorized`
    docstring 對「可以取代它的寫法」訂的那條規則。

    判準＝**強制否決**（與 #705 F3 同一把，理由也同）：能走到那一格的呼叫者
    必然對那些 project 有 write，用「有沒有被擋」當判準會恆綠。這裡不共用 F3
    的 helper——那組 helper 綁死了「非封存」的前提敘述，各留一份比抽象化清楚。
    """

    def _forced_denial(self):
        """把 `project.project.check_access('write')` 變成無條件 raise。

        read 方向照常委派——否則整個 RPC 在抵達受測步驟之前就死了，而那個
        錯誤長得跟「守衛擋住了」一模一樣。
        """
        cls = type(self.Project)
        original = cls.check_access
        calls = []

        def fake(records, operation):
            calls.append((operation, tuple(records.ids)))
            if operation == 'write':
                raise AccessError('716 forced denial')
            return original(records, operation)

        return patch.object(cls, 'check_access', fake), calls

    def _archived_bound_project(self, name):
        pid = self._new_project_as(self.aowner_login, name)
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：owner 先把自己的 service 綁上自己的 project')
        self.Project.sudo().browse(pid).write({'active': False})
        self.assertFalse(
            self.Project.sudo().browse(pid).active, '前提：project 必須真的被封存')
        self.assertEqual(
            self._bound(pid), {self.svc_a.id},
            '前提：封存不動 relation 那一列（否則本格測到的是別的東西）')
        return pid

    def test_B4_archived_project_reaches_candidate_set(self):
        """🔴 缺口本體：`(5, 0, 0)` 解掉的是**已封存** project 的綁定，而候選集
        只靠「寫入前的 `self.project_ids`」涵蓋移除方向 ⇒ 整條沒被檢查過。
        """
        pid = self._archived_bound_project('716 B4 archived')
        self.authenticate(self.aowner_login, self.aowner_login)
        patcher, calls = self._forced_denial()
        with patcher:
            res = self._call_kw(_CS, 'write', [
                [self.svc_a.id], {'project_ids': [(5, 0, 0)]}])
        self.assertIn(
            'error', res,
            'B4：強制否決沒生效 ⇒ 已封存的 project 沒進候選集（calls=%r，回 %r）'
            % (calls, res))
        self.assertTrue(
            any(pid in ids for op, ids in calls if op == 'write'),
            'B4：check_access(write) 必須涵蓋已封存的 project #%s（calls=%r）'
            % (pid, calls))
        self.assertEqual(
            self._bound(pid), {self.svc_a.id},
            'B4：綁定必須還在（強制否決 ⇒ 整個交易回滾）')

    def _live_bound_project(self, name):
        pid = self._new_project_as(self.aowner_login, name)
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：owner 先把自己的 service 綁上這個（未封存的）project')
        return pid

    def test_B5_archived_row_is_out_of_reach_and_owner_is_not_blocked(self):
        """陽性對照 ＋ 缺口為什麼不可利用的一手實證（不 patch）。

        同一次 `(5, 0, 0)` 同時作用在「未封存」與「已封存」兩個 project 上：

        1. owner 的合法解綁**不得**被誤擋（候選集擴大只會多檢查，不該多擋人）；
        2. 未封存那個**真的被解掉**（證明 `(5)` 有效，不是整個指令沒作用）；
        3. 已封存那個**原封不動**——`Many2many.write_real` 算 `old_relation`
           用的是同一個 `record[fname]`，它也看不到已封存的那一列。

        第 3 點就是「候選集漏掉它今天不可利用」的機制，也是 #716 ② 原文
        「已封存 project 的綁定被 `(5)` 解掉」那半句被推翻的地方。
        """
        archived = self._archived_bound_project('716 B5 archived')
        live = self._live_bound_project('716 B5 live')
        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_CS, 'write', [
            [self.svc_a.id], {'project_ids': [(5, 0, 0)]}])
        self._assert_ok(res, 'B5：owner 的合法解綁不得因為候選集擴大而被誤擋')
        self.assertEqual(
            self._bound(live), set(),
            'B5 對照：未封存的那個必須真的被解掉（否則本格零鑑別力）')
        self.assertEqual(
            self._bound(archived), {self.svc_a.id},
            'B5：已封存那一列 Odoo 自己的 write_real 也動不到 ⇒ 候選集漏掉它'
            '今天不可利用（修法是把「候選集 ⊇ 實際變動」變成自己成立）')


@tagged('post_install', '-at_install')
class TestNestedCreateCommand0Claim716(_Binding698Fixture):
    """🟡 #716 ③：`(0, 0, vals)` 例外的 docstring 理由是**假的**。

    舊 docstring 寫「那個 project 是呼叫者自己建的，且**它的 create 會走
    project 側的 mixin**」。第二句在授權上不是 load-bearing：
    `Many2many.write_real` 先 `comodel.create(vals)`，relation 那一列是
    **之後**用直接 SQL insert 完成的——不是「建出來的 project 收到帶
    `cloud_service_ids` 的 vals」。所以巢狀 vals 本身不含載體 key 時，project
    側的 mixin 看不到任何要比對的欄位。

    本 class 把**機制**釘住（B6），並把兩個「今天為什麼不可利用」的實證留成
    回歸鎖（B7／B8）。修正後的 docstring 只能宣稱這三格證得到的事。
    """

    def _examined(self):
        """記錄 `_assert_binding_authorized` 收到的 service id 集合。

        判準落在「有沒有被送去授權」，不是「有沒有被擋」——這一格的呼叫者
        本來就對 svc_a 有權，用「有沒有被擋」問不出東西。
        """
        cls = type(self.Service)
        original = cls._assert_binding_authorized
        seen = []

        def fake(records, service_ids):
            seen.append(set(service_ids))
            return original(records, service_ids)

        return patch.object(cls, '_assert_binding_authorized', fake), seen

    def test_B6_cmd0_nested_create_is_not_examined_by_project_mixin(self):
        """🔴 機制釘樁：`(0, 0, {'name': ...})` 建出來的 project 綁上了 svc_a，
        而 project 側的 mixin **一次都沒有**把它送去授權。

        前兩個斷言是前提（project 真的建出來、relation 那一列真的落地），
        沒有它們，`examined == []` 可能只代表「整條路徑根本沒跑」。
        """
        self.authenticate(self.aowner_login, self.aowner_login)
        patcher, seen = self._examined()
        with patcher:
            res = self._call_kw(_CS, 'write', [
                [self.svc_a.id], {'project_ids': [(0, 0, {'name': 'B6 nested'})]}])
        self._assert_ok(
            res, 'B6 前提：owner 必須做得到就地新建 project 並綁上自己的 service')
        project = self.Project.sudo().search([('name', '=', 'B6 nested')])
        self.assertEqual(len(project), 1, 'B6 前提：巢狀 create 必須真的建出 project')
        self.assertEqual(
            set(project.cloud_service_ids.ids), {self.svc_a.id},
            'B6 前提：relation 那一列必須真的落地（否則下面的斷言零鑑別力）')
        self.assertFalse(
            [s for s in seen if self.svc_a.id in s],
            'B6：project 側 mixin 一次都沒把這筆綁定送去授權（examined=%r）'
            '⇒ 舊 docstring「它的 create 會走 project 側的 mixin」在授權上為假'
            % seen)

    def test_B7_cmd0_nested_vals_naming_service_is_blocked(self):
        """巢狀 vals **自己**帶載體 key 時，project 側 mixin 就看得見 ⇒ 擋得住。

        這一格是 B6 的分界線：不可利用的窄縫僅限「巢狀 vals 不含載體 key」。

        ⚠ 必須**陪綁一顆自己的 service**（與 `X1c` 同一個理由）：只寫受害者
        的 service 時，`BaseModel.create` 內部的 post-create ir.rule 檢查會先
        擋下來（訊息是 Odoo 的 `doesn't have 'create' access to Project`），
        守衛連開口的機會都沒有 ⇒ 那樣的綠燈是**歸因錯誤**，測到的是 project
        的 rule 不是本守衛。陪綁一顆 wa 的 service 讓 rule 的 bound 分支成立，
        才問得到「守衛怎麼說」。
        """
        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_CS, 'write', [
            [self.svc_a.id],
            {'project_ids': [(0, 0, {'name': 'B7 nested',
                                     'cloud_service_ids': [
                                         (4, self.svc_a.id),
                                         (4, self.svc_v.id)]})]}])
        self._assert_guard_denied(res, self.svc_v, 'B7')
        self.assertFalse(
            self.Project.sudo().search([('name', '=', 'B7 nested')]),
            'B7：整筆交易必須回滾')

    def test_B8_cmd0_nested_create_inherits_default_context(self):
        """巢狀 create 繼承外層 context ⇒ `default_` 那族由 #705 F1 的
        `_add_missing_default_values` 接住，同樣擋得住。

        陪綁理由同 `B7`。
        """
        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(
            _CS, 'write',
            [[self.svc_a.id], {'project_ids': [(0, 0, {'name': 'B8 nested'})]}],
            {'context': {'default_cloud_service_ids': [[4, self.svc_a.id],
                                                       [4, self.svc_v.id]]}})
        self._assert_guard_denied(res, self.svc_v, 'B8')
        self.assertFalse(
            self.Service.sudo().browse(self.svc_v.id).project_ids.filtered(
                lambda p: p.name == 'B8 nested'),
            'B8：跨租戶綁定不得落地')

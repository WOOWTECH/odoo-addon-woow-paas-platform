# -*- coding: utf-8 -*-
"""#705 覆核 findings 的守線（F1 / F2 / F3 / F6）。

`test_project_cs_m2m_authz_698.py` 守的是「#698 那條攻擊被擋住」。本檔守的是
**獨立覆核在那份修法上找到的四個缺口**——它們共用同一組 fixture，但斷言的是
另一批不變式，所以拆成獨立檔案（跑 RED/GREEN 時可單獨選中）。

| finding | 守什麼 | class |
| --- | --- | --- |
| F1 | `default_<field>` context ／ 其他「值不在 raw vals 裡」的來源不得繞過 create 守衛 | `TestCreateValueSourcesF1` |
| F1 | 第三條路徑的實測結果（`ir.default`、`copy()`） | `TestCreateOtherValueSourcesF1` |
| F2 | compute 導出的綁定**不是**呼叫者新增的 ⇒ 不得因此拒絕合法操作 | `TestCreateComputeFalsePositiveF2` |
| F3 | 反向端候選集必須真的是超集（含移除方向的舊值） | `TestReverseCandidateSetF3` |
| F6 | cross-project move **會**改動綁定集合（行為端已由 #715 反轉，見該 class） | `TestCrossProjectMoveReasonF6` |

## 為什麼 F1 需要「值不在 raw vals 裡」這一整族

守衛的 create 攔截點必須用**框架解析後**的 vals 決定要不要跑。原始實作看的是
呼叫者送進來的 raw vals，而 `_add_missing_default_values`（`_prepare_create_values`
→ `BaseModel.create` 內部）是在 `super().create()` **底下**才把 `default_*`
context ／ `ir.default` 展開成欄位值的 ⇒ key 不在 vals 裡 ⇒ 守衛整道跳過。

⚠ 本檔刻意**不**逐一重演「哪些來源會補值」——那正是 #657 的形態。這裡的做法是
對每一個**已知**來源各留一條測試當回歸鎖，而修法本身走
`self._add_missing_default_values(vals)`（框架自己的解析），所以未來多出來的來源
不需要回頭改守衛。

## F3 為什麼只能用「強制否決」判別

反向端漏掉的候選 id（`(5,0,0)` / `(6,0,[])` 的移除方向）**今天不可利用**：能走到
那一格的呼叫者必然是該 service workspace 的 owner／admin（#676），而
`project_rule_workspace_member` 的 bound 分支（`cloud_service_ids.workspace_id.
access_ids.user_id`，**無 role 條件**）讓他對任何綁著那顆 service 的 project 都有
write ⇒ 漏檢的那些就算檢查了也會通過。⇒ 用「有沒有被擋」當判準會**三格全綠**，
那是假綠。

所以判準改成：把 `project.project.check_access('write')` patch 成對 write 方向
無條件 raise。斷言落在守衛的**契約**上——「這次寫入實際改動到的 project，每一個
都要經過 `check_access('write')`」——而不是落在「走了哪一行」。陽性對照（`Z1`）
證明這個判準在候選集**有**撈到 id 時確實會紅。
"""
import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged

from .test_project_cs_m2m_authz_698 import _Binding698Fixture

_CS = 'woow_paas_platform.cloud_service'
_PROJECT = 'project.project'
_TASK = 'project.task'


@tagged('post_install', '-at_install')
class TestCreateValueSourcesF1(_Binding698Fixture):
    """🔴 F1：把 payload 從 `args` 搬到 `kwargs.context` 就繞過 create 守衛。"""

    def _create_with_context(self, model, vals, context):
        return self._call_kw(model, 'create', [vals], {'context': context})

    def test_X1a_control_explicit_vals_is_blocked(self):
        """陽性對照：同一筆綁定**明寫在 vals 裡**時守衛擋得住。

        沒有它，X1b–X1f 的紅燈可能只是「create 帶 x2many 本來就不 work」。
        """
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'create', [{
            'name': 'X1a explicit',
            'cloud_service_ids': [(4, self.svc_a.id)],
        }])
        self._assert_guard_denied(res, self.svc_a, 'X1a')

    def test_X1b_default_context_same_workspace(self):
        """`default_cloud_service_ids` 綁**自己 workspace** 的 service。

        這一格是 create 守衛的鑑別力來源（與 698 的 `C1` 同理）：guest 對
        `project_rule_workspace_member` 的 bound 分支成立 ⇒ post-create 的
        rule 檢查放行 ⇒ 只剩守衛擋得住。
        """
        self.authenticate(self.gary_login, self.gary_login)
        res = self._create_with_context(
            _PROJECT, {'name': 'X1b default ctx'},
            {'default_cloud_service_ids': [[4, self.svc_a.id]]})
        self.assertIn(
            'error', res,
            'X1b：guest 不得經由 default_ context 繞過 create 守衛（回 %r）' % res)
        self.assertFalse(
            self.Service.sudo().browse(self.svc_a.id).project_ids.filtered(
                lambda p: p.name == 'X1b default ctx'),
            'X1b：綁定不得落地（不變式）')
        self._assert_guard_denied(res, self.svc_a, 'X1b')

    def test_X1c_default_context_cross_tenant(self):
        """跨租戶版本：陪綁一顆自己的 service 以滿足 rule 的 bound 分支
        （to-many 的 ANY 語意，只要**有一顆** service 的 workspace 有你就成立），
        受害者的 service 一起夾帶進去。"""
        self.authenticate(self.gary_login, self.gary_login)
        res = self._create_with_context(
            _PROJECT, {'name': 'X1c default ctx cross'},
            {'default_cloud_service_ids': [[4, self.svc_a.id],
                                           [4, self.svc_v.id]]})
        self.assertIn('error', res,
                      'X1c：跨租戶 service 不得經由 default_ context 綁上'
                      '（回 %r）' % res)
        self.assertFalse(
            self.Service.sudo().browse(self.svc_v.id).project_ids.filtered(
                lambda p: p.name == 'X1c default ctx cross'),
            'X1c：受害者 service 不得被綁上（不變式）')

    def test_X1d_downstream_workspace_ids_not_polluted(self):
        """下游：`workspace_ids` 是 `compute_sudo` 的 stored compute ⇒ 綁上
        就會吸收受害者 workspace，進 reach 集合。這條下游與 X1c 是**不同**的
        觀測面（X1c 看 relation，這裡看 compute 的產物）。"""
        self.authenticate(self.gary_login, self.gary_login)
        self._create_with_context(
            _PROJECT, {'name': 'X1d default ctx'},
            {'default_cloud_service_ids': [[4, self.svc_a.id],
                                           [4, self.svc_v.id]]})
        polluted = self.Project.sudo().search(
            [('name', '=', 'X1d default ctx')]).filtered(
                lambda p: self.wv in p.workspace_ids)
        self.assertFalse(
            polluted,
            'X1d：workspace_ids 不得吸收受害者 workspace（reach 集合被污染）')

    def test_X1e_task_default_context(self):
        """task 的兩個載體欄位各有一個 `default_` key。"""
        pid = self._new_project_as(self.gary_login)
        self.authenticate(self.gary_login, self.gary_login)
        extra = self._create_with_context(
            _TASK, {'name': 'X1e extra', 'project_id': pid},
            {'default_extra_cloud_service_ids': [[4, self.svc_v.id]]})
        self.assertFalse(
            self.Task.sudo().search([('name', '=', 'X1e extra')])
                .mapped('extra_cloud_service_ids'),
            'X1e-extra：default_ context 不得綁上跨租戶 service（回 %r）' % extra)

        primary = self._create_with_context(
            _TASK, {'name': 'X1e primary', 'project_id': pid},
            {'default_primary_cloud_service_id': self.svc_v.id})
        self.assertFalse(
            self.Task.sudo().search([('name', '=', 'X1e primary')])
                .mapped('primary_cloud_service_id'),
            'X1e-primary：default_ context 不得寫上跨租戶 service（回 %r）'
            % primary)

    def test_X1f_reverse_side_default_context(self):
        """反向端（`cloud_service.create` 帶 `default_project_ids`）。

        後果與 `V1` 同：把自己的 service 推進**任意** project ⇒ 那個 project
        落進攻擊者的 bound 分支 ⇒ 對受害者 project 取得 read＋write。
        """
        victim_pid = self._new_project_as(self.victim_login, 'X1f victim')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [victim_pid], {'cloud_service_ids': [(4, self.svc_v.id)]}]),
            '前提：受害者先綁自己的 service')

        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_CS, 'create', [{
            'name': 'X1f pushed',
            'workspace_id': self.wa.id,
            'template_id': self.template.id,
            'helm_namespace': 'paas-ws-test-698',
            'helm_release_name': 'svc-x1f-%s' % uuid.uuid4().hex[:6],
        }], {'context': {'default_project_ids': [[4, victim_pid]]}})
        self.assertNotIn(
            self.wa,
            self.Project.sudo().browse(victim_pid).workspace_ids,
            'X1f：受害者 project 的 workspace_ids 不得吸收攻擊者 workspace'
            '（回 %r）' % res)
        self.assertFalse(
            self.Service.sudo().search([('name', '=', 'X1f pushed')])
                .mapped('project_ids').filtered(lambda p: p.id == victim_pid),
            'X1f：反向端不得經由 default_ context 把自己的 service 推進別人的'
            ' project（回 %r）' % res)


@tagged('post_install', '-at_install')
class TestCreateOtherValueSourcesF1(_Binding698Fixture):
    """F1 的同族：其他「值不出現在呼叫者 raw vals 裡」的來源。

    每一條都是**實測**的結果，不是讀原始碼推的——F1 本身就是「讀起來像守住了」
    的產物。
    """

    def test_X2a_ir_default_is_not_a_bypass(self):
        """`ir.default`（使用者自訂預設值）是 `default_get` 的第二個來源。

        它與 `default_` context 走同一個解析步驟，但**載體完全不同**（DB 裡的
        一列，不是這次 RPC 的 payload）⇒ 只看 payload 的守衛看不見它。
        """
        self.env['ir.default'].sudo().set(
            _PROJECT, 'cloud_service_ids', [self.svc_a.id],
            user_id=self.gary.id)
        self.env.invalidate_all()
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'create', [{'name': 'X2a ir_default'}])
        self.assertFalse(
            self.Service.sudo().browse(self.svc_a.id).project_ids.filtered(
                lambda p: p.name == 'X2a ir_default'),
            'X2a：ir.default 不得成為 create 守衛的旁路（回 %r）' % res)

    def test_X2b_copy_of_bound_project_is_guarded(self):
        """`copy()`：M2M 預設 `copy=True` ⇒ `copy_data` 把載體欄位放進 create
        的 vals ⇒ 守衛看得見。

        這一格**在覆核之前沒有任何測試守著**——未來有人把 `copy=False`，或
        把 create 守衛改回只看 raw vals，它就靜默打開。
        """
        pid = self._new_project_as(self.aowner_login, 'X2b source')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：owner 先把 service 綁上（沒綁就沒東西可複製）')

        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'copy', [[pid]])
        copies = self.Project.sudo().search(
            [('id', '!=', pid), ('cloud_service_ids', 'in', [self.svc_a.id])])
        self.assertFalse(
            copies,
            'X2b：guest 不得經由 copy() 複製出一個帶綁定的 project（回 %r）'
            % res)

    def test_X2d_load_import_path_is_guarded(self):
        """`load()`（base_import 走的那條）有自己的一套 vals 組裝
        （`_convert_records`），是與 `create` 直呼**不同**的入口。

        它最後仍然呼叫 `create`，載體欄位的 key 會出現在 vals 裡 ⇒ 守衛看得見。
        這一格是那個推論的**實測**，不是推論本身。
        """
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'load', [
            ['name', 'cloud_service_ids/.id'],
            [['X2d loaded', str(self.svc_a.id)]]])
        self.assertFalse(
            self.Service.sudo().browse(self.svc_a.id).project_ids.filtered(
                lambda p: p.name == 'X2d loaded'),
            'X2d：guest 不得經由 load() 匯入一個帶綁定的 project（回 %r）' % res)

    def test_X2e_control_owner_load_still_works(self):
        """陽性對照：owner 走同一條 `load()` 必須成功且綁定落地。

        沒有它，X2d 的「沒有落地」可能只是 `load()` 的欄位規格寫錯了——那樣
        X2d 會恆綠、零鑑別力。
        """
        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_PROJECT, 'load', [
            ['name', 'cloud_service_ids/.id'],
            [['X2e loaded', str(self.svc_a.id)]]])
        self.assertTrue(
            self.Service.sudo().browse(self.svc_a.id).project_ids.filtered(
                lambda p: p.name == 'X2e loaded'),
            'X2e：owner 必須 load 得起來且綁定落地（回 %r）' % res)

    def test_X2c_control_owner_copy_still_works(self):
        """陽性對照：owner 複製同一個 project 必須成功。

        沒有它，X2b 的「沒有副本」可能只是 `copy()` 這條路整個壞掉。
        """
        pid = self._new_project_as(self.aowner_login, 'X2c source')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：owner 先把 service 綁上')
        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_PROJECT, 'copy', [[pid]])
        self._assert_ok(res, 'X2c：owner 必須複製得起來')
        copies = self.Project.sudo().search(
            [('id', '!=', pid), ('cloud_service_ids', 'in', [self.svc_a.id])])
        self.assertTrue(copies, 'X2c：副本必須帶著綁定（證明這條路真的會寫綁定）')


@tagged('post_install', '-at_install')
class TestCreateComputeFalsePositiveF2(_Binding698Fixture):
    """🟠 F2：create 守衛把「compute 自動導出的綁定」當成呼叫者新增的。

    `task.primary_cloud_service_id` 是 `@api.depends('project_id.
    cloud_service_ids')` 的 stored compute：在一個**已合法綁定**的 project 裡
    建 task，它會被自動算成該 project 的第一顆 service。呼叫者什麼都沒綁，
    卻被要求 owner／admin。

    觸發條件是「vals 裡出現受保護欄位的 key」——**值是空的也算**，而
    `extra_cloud_service_ids` 就在 `src/views/project_task_views.xml` 的 form
    裡，任何把它一起送出的 client 都會踩到。
    """

    def _bound_project_of_wa(self):
        pid = self._new_project_as(self.aowner_login, 'F2 shared')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：owner 先合法綁上自己 workspace 的 service')
        return pid

    def test_Y4a_control_task_create_without_key(self):
        """對照：不帶受保護 key 時 guest 建得起 task，且 primary **確實**被
        自動算成該 project 的 svc_a。

        這一格同時是 F2 的前提斷言——沒有「compute 真的會導出綁定」，
        Y4b/Y4c 就無從談起。
        """
        pid = self._bound_project_of_wa()
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_TASK, 'create', [
            {'name': 'Y4a no key', 'project_id': pid}])
        self._assert_ok(res, 'Y4a：guest 必須建得起 task')
        task = self.Task.sudo().browse(res['result'])
        self.assertEqual(
            task.primary_cloud_service_id, self.svc_a,
            'Y4a：compute 必須真的把 primary 導成該 project 的第一顆 service')

    def test_Y4b_empty_extra_key_does_not_break_task_create(self):
        """🔴 帶一個**空的** `extra_cloud_service_ids` 就炸 ⇒ 功能性回歸。

        被拒的 service 是他自己 workspace 的、他本來就看得到——訊息會讓人
        完全摸不著頭緒。
        """
        pid = self._bound_project_of_wa()
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_TASK, 'create', [
            {'name': 'Y4b empty key', 'project_id': pid,
             'extra_cloud_service_ids': []}])
        self._assert_ok(
            res,
            'Y4b：呼叫者沒有新增任何綁定（空 list），不得被守衛拒絕')
        task = self.Task.sudo().browse(res['result'])
        self.assertEqual(
            set(task.extra_cloud_service_ids.ids), set(),
            'Y4b：extra 必須是空的（證明呼叫者確實什麼都沒綁）')

    def test_Y4c_user_role_also_unaffected(self):
        """role=`user` 與 guest 同側——F2 的影響面不只 guest。"""
        pid = self._bound_project_of_wa()
        self.authenticate(self.carol_login, self.carol_login)
        res = self._call_kw(_TASK, 'create', [
            {'name': 'Y4c empty key', 'project_id': pid,
             'extra_cloud_service_ids': []}])
        self._assert_ok(res, 'Y4c：role=user 同樣不得被守衛拒絕')

    def test_Y4d_project_side_same_shape(self):
        """project 側同型：帶空的 `cloud_service_ids` 建 project 必須成功。

        界定範圍用——project 側沒有 compute 導出的綁定，所以它本來就該綠；
        它變紅代表修法把 create 的 key gate 改成了無條件。
        """
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'create', [
            {'name': 'Y4d empty key', 'cloud_service_ids': []}])
        self._assert_ok(res, 'Y4d：空 list 不得被守衛拒絕')

    def test_Y4e_web_client_shaped_payload(self):
        """web client 的實際形狀：兩個受保護欄位**都**送出，m2o 送 `False`。

        Y4b 只帶一個 key，證不到「明寫 `primary=False` 時 compute 會不會
        照樣回填」。這一格把兩個 key 一起送，是 F2 在真實 client 下的形狀。
        """
        pid = self._bound_project_of_wa()
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_TASK, 'create', [
            {'name': 'Y4e web shape', 'project_id': pid,
             'extra_cloud_service_ids': [],
             'primary_cloud_service_id': False}])
        self._assert_ok(res, 'Y4e：web client 形狀的 payload 不得被守衛拒絕')

    def test_Y4f_explicit_binding_in_same_payload_still_blocked(self):
        """🔴 F2 的修法**不得**把 F1 那條線一起放掉。

        同一個形狀的 payload，只把 `extra_cloud_service_ids` 從空 list 換成
        真的綁一顆跨租戶 service ⇒ 必須被擋。沒有這一格，「Y4b 轉綠」可以
        靠「create 守衛整個關掉」達成。
        """
        pid = self._bound_project_of_wa()
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_TASK, 'create', [
            {'name': 'Y4f explicit', 'project_id': pid,
             'extra_cloud_service_ids': [(4, self.svc_v.id)],
             'primary_cloud_service_id': False}])
        self.assertFalse(
            self.Task.sudo().search([('name', '=', 'Y4f explicit')])
                .mapped('extra_cloud_service_ids'),
            'Y4f：明寫的跨租戶綁定必須仍被擋（回 %r）' % res)
        self._assert_guard_denied(res, self.svc_v, 'Y4f')


@tagged('post_install', '-at_install')
class TestReverseCandidateSetF3(_Binding698Fixture):
    """🟡 F3：反向端候選 id 收集必須是**真的**超集。

    判準＝強制否決（見檔頭）。不變式：**這次寫入實際改動到的 project，每一個
    都必須經過 `check_access('write')`**；候選集漏掉誰，誰就完全沒被檢查過。
    """

    def _svc_a_bound_to_own_project(self):
        pid = self._new_project_as(self.aowner_login, 'F3 own')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：owner 先把自己的 service 綁上自己的 project')
        return pid

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
                raise AccessError('F3 forced denial')
            return original(records, operation)

        return patch.object(cls, 'check_access', fake), calls

    def _assert_reached(self, res, calls, pid, msg):
        self.assertIn(
            'error', res,
            '%s：強制否決沒生效 ⇒ 守衛沒對這筆 project 呼叫 check_access'
            '（calls=%r，回 %r）' % (msg, calls, res))
        self.assertEqual(
            self._bound(pid), {self.svc_a.id},
            '%s：綁定必須還在（強制否決 ⇒ 整個交易回滾）' % msg)
        self.assertTrue(
            any(pid in ids for op, ids in calls if op == 'write'),
            '%s：check_access(write) 必須涵蓋 project #%s（calls=%r）'
            % (msg, pid, calls))

    def test_Z1_control_cmd3_reaches_candidate_set(self):
        """陽性對照：`(3, pid)` 的 id 明寫在指令裡 ⇒ 候選集撈得到 ⇒ 強制否決
        會紅。沒有它，Z2/Z3 的綠燈可能只是「強制否決這個判準壞了」。"""
        pid = self._svc_a_bound_to_own_project()
        self.authenticate(self.aowner_login, self.aowner_login)
        patcher, calls = self._forced_denial()
        with patcher:
            res = self._call_kw(_CS, 'write', [
                [self.svc_a.id], {'project_ids': [(3, pid)]}])
        self._assert_reached(res, calls, pid, 'Z1')

    def test_Z2_cmd5_clear_reaches_candidate_set(self):
        """🔴 `(5, 0, 0)`（`Command.clear()`）：`command[1]` 是 `0`（falsy）、
        `command[2]` 是 `0`（非 list）⇒ 舊實作的候選集**空**。"""
        pid = self._svc_a_bound_to_own_project()
        self.authenticate(self.aowner_login, self.aowner_login)
        patcher, calls = self._forced_denial()
        with patcher:
            res = self._call_kw(_CS, 'write', [
                [self.svc_a.id], {'project_ids': [(5, 0, 0)]}])
        self._assert_reached(res, calls, pid, 'Z2')

    def test_Z3_cmd6_empty_reaches_candidate_set(self):
        """🔴 `(6, 0, [])`：`command[2]` 是空 list ⇒ 舊實作的候選集**空**。

        同一個指令在 project 側是**擋得住**的（`R3`，after-diff 看得見移除）
        ——兩端的不對稱由這一格與 `R3` 一起記錄。
        """
        pid = self._svc_a_bound_to_own_project()
        self.authenticate(self.aowner_login, self.aowner_login)
        patcher, calls = self._forced_denial()
        with patcher:
            res = self._call_kw(_CS, 'write', [
                [self.svc_a.id], {'project_ids': [(6, 0, [])]}])
        self._assert_reached(res, calls, pid, 'Z3')

    def test_Z4_reverse_positive_control_survives(self):
        """陽性對照（不 patch）：候選集加進「寫入前的舊值」之後，owner 對
        自己的 project 做同樣三個指令必須**仍然成功**。

        沒有它，Z1–Z3 可以靠「把所有人都擋住」達成——而那會讓合法的解綁
        全面失效。
        """
        pid = self._svc_a_bound_to_own_project()
        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_CS, 'write', [
            [self.svc_a.id], {'project_ids': [(6, 0, [])]}])
        self._assert_ok(res, 'Z4：owner 必須解得掉自己的綁定')
        self.assertEqual(self._bound(pid), set(), 'Z4：解綁必須真的落地')


@tagged('post_install', '-at_install')
class TestCrossProjectMoveReasonF6(_Binding698Fixture):
    """🟢 F6：cross-project move **會**改動綁定集合。

    follow-ups 的 F3 曾把「move 不觸發守衛」的理由寫成「它比對的是綁定集合，
    而綁定沒變」——那是假的：`primary_cloud_service_id` 是
    `@api.depends('project_id.cloud_service_ids')` 的 stored compute，move
    **會**改綁定集合。

    🔴 **本 class 的結論在 #715 被推翻了一半（2026-08-19）。**
    當時寫的第二句「真正的理由是守衛的 key gate：`project_id` 不在受保護欄位
    裡 ⇒ 連 snapshot 都不做」在機制上正確，但那正是 #715 的旁路——guest 靠搬
    task 就能讓 primary 靜默換綁，繞掉「只有 owner／admin 才能變更綁定」。
    `project_id` 現在是 task 的 **write 側觸發器**（`_cs_binding_write_triggers`），
    move 會走守衛、由角色決定放不放行。

    ⇒ 本 class 保留仍然成立的那一半（**綁定確實會變**，所以這條路徑有東西可
    比、綠燈不是「沒 diff」換來的），並把行為端改成 owner 的陽性對照；
    「guest 被擋」那一格在 `test_project_cs_m2m_authz_715_716.py` 的 `M1`，
    「不是任何 move 都擋」在 `M4`。
    """

    def _wa_service(self, name):
        return self._service(self.template, self.wa, name)

    def test_X3b_move_changes_binding_and_is_authorized_for_owner(self):
        """owner 把 task 從 P1 移到 P2（兩個 project 各綁一顆 wa 的 service）。

        斷言兩件事，缺一不可：
        1. move **成功**（守衛參與，但 owner 通過 ⇒ 合法操作沒被誤擋）；
        2. `primary_cloud_service_id` **變了**（證明「綁定沒變」是假的，同時
           證明這條路徑真的有 diff 可比）。
        """
        svc_a2 = self._wa_service('SVC attacker 698 #2')
        p1 = self._new_project_as(self.aowner_login, 'F6 P1')
        p2 = self._new_project_as(self.aowner_login, 'F6 P2')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [p1], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：P1 綁 svc_a')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [p2], {'cloud_service_ids': [(4, svc_a2.id)]}]),
            '前提：P2 綁 svc_a2')

        self.authenticate(self.aowner_login, self.aowner_login)
        created = self._call_kw(_TASK, 'create', [
            {'name': 'F6 task', 'project_id': p1}])
        self._assert_ok(created, '前提：owner 建得起 P1 的 task')
        tid = created['result']
        self.assertEqual(
            self.Task.sudo().browse(tid).primary_cloud_service_id, self.svc_a,
            '前提：primary 先被 compute 成 svc_a')

        moved = self._call_kw(_TASK, 'write', [[tid], {'project_id': p2}])
        self._assert_ok(
            moved,
            'F6：owner 的 cross-project move 是合法操作，不得被誤擋（回 %r）' % moved)
        self.assertEqual(
            self.Task.sudo().browse(tid).primary_cloud_service_id, svc_a2,
            'F6：綁定集合**確實會變**——「綁定沒變」那句宣稱是假的')

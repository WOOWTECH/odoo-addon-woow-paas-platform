"""#849 RED-A：寫 policy 的 stored related 投影欄位 `security_access_id` 會怎樣。

⛔ **本檔是量測，不是守線。** RED-A 的任務是把「到底發生什麼」量清楚，
不是證明任何一方的推論。任何一格紅了都要看**紅在哪一句斷言上**才知道它在說什麼。

## #849 原文的機制主張與它的問題

原文說這兩個 stored related 帶 `inverse` 委派 ⇒ 寫它們會寫到**來源列**
（`route.security_access_id`）。理由寫的是「`readonly` 不在 `related_attrs` 繼承清單
裡（無 `_related_readonly`）⇒ class 預設 `False`」。

**那條推導在 related 欄位上不適用**（宿主 checkout `odoo-18.0/odoo/fields.py`，
判別事實是「哪個區塊裡有這行」而不是行號）：`Field._get_attrs()` 的
``if attrs.get('related'):`` 區塊裡有 ``attrs['readonly'] = attrs.get('readonly', True)``
⇒ **related 欄位的 `readonly` 預設就是 `True`**，`related_attrs` 那條路徑根本輪不到。

⇒ `setup_related()` 裝 inverse 的條件 ``if self.inherited or not (self.readonly or field.readonly)``
在兩欄上都不成立 ⇒ **`inverse` 不裝** ⇒ 寫它們**不會**寫到來源列。
F 系列把這件事量成端到端事實（不只讀碼）。

## 那值會去哪——這才是本單真正的形狀

`store=True` ⇒ 欄位有真 column。`Field.write()` 的第一件事是
``records.env.remove_to_compute(self, records)``（註解逐字：discard recomputation of
self on records），接著 ``dirty = self.store and any(records._ids)``
⇒ **直寫 stored related 不但落地，還會把該列該欄的待重算取消掉。**
`check_field_access_rights('write', ...)` 只看欄位 `groups=`（`is_accessible`），
**不看 `readonly`** ⇒ `readonly=True` 攔不住 ORM 寫入。

而那根 column **正是 `ir.rule` 判歸屬用的**（`security/ir_rules.xml` 的 policy
member-read 與 owner/admin-manage 兩條 domain，三條腿走
`security_access_id` / `cloud_service_id` / `smart_home_id`，**不走** `route_id`）。
⇒ 可能出現**歸屬分裂**：實際掛在 X 的 route 上、而 `ir.rule` 認為屬於 Y。

## 三道既有防線對這個 vals **全部不 fire**

* `_reject_identity_anchor_writes`（#798）只看 `_POLICY_IDENTITY_ANCHOR_FIELDS`（`cf_policy_id`）。
* `_assert_anchor_move_authorised`（#848）迴圈跑 `_ANCHOR_FIELDS`（三個真錨點）。
* `if resync:` 內那個 `check_access('read')` 排序迴圈（#786）——`resync` 的觸發集合是
  ``('name','decision','sequence','rule_ids') + _ANCHOR_FIELDS``，**不含 `security_access_id`**
  ⇒ `resync` 為 False，那個迴圈**連跑都不跑**。

🔴 **順帶一個結構事實**：policy 上租戶寫得動的欄位只有
``route_id / cloud_service_id / smart_home_route_id / smart_home_id /
security_access_id / name / decision / rule_ids / sequence / cf_policy_id``。
扣掉三個真錨點（#848 守）、`cf_policy_id`（#798 守）、以及 name/decision/rule_ids/sequence
（全在 resync 觸發集合裡），**剩下不觸發任何一道防線的就只有這兩個投影欄位**。
⇒ 繞過不是巧合，這兩欄是唯一的無守線出口。

## 兩個方向都要跑（雞生蛋）

攻擊者要對那列下 `write`，而**那列一開始 ir.rule 判給誰，正是由他想寫的那個欄位決定的**：

* **D1（奪取）**：目標列一開始就歸受害者 ⇒ `write` 在門口就被擋 ⇒ **證明不了分裂**。
  D1 的陽性對照（寫合法欄位 `name`）會**一起紅** —— 那正是「這帳號對這列根本寫不動」
  的證據，也正是 D1 零鑑別力的原因。
* **D2（推給別人）**：目標列一開始歸攻擊者 ⇒ 他把投影欄位寫成受害者的
  ⇒ 這列被「送」給受害者、而 `route_id` 還指著自己。

**哪個方向能成立本身就是結論的一部分**，不是前置條件。

## 判準的形狀

⛔ 不做「通過／不通過」兩格——三種可能結果（靜默丟棄／落地／落地後被 recompute 蓋回）
**輸出不同但都不報錯**，兩格會把三種都塞進「通過」。所以：

* DB 值量**兩次**（寫入後立刻、觸發一次 recompute 之後），且**繞過 ir.rule**（raw SQL）。
  ⚠ 用攻擊者身分經 ORM 讀、而列已被判給別人 ⇒ 讀到空
  ⇒ **那看起來像「寫入被丟棄」，其實是「你讀不到」**。
* ir.rule 歸屬量 **2 身分 × 2 時點 = 4 格**，read 與 write **分開量**
  （read 可見而 write 不可是合法狀態）。判別式是**表上有格子翻面**，
  不是「攻擊後某人看得到」。
* 每一次量測都**一起記 `route_id` 的當下值**——分裂的定義是
  「ir.rule 判給的對象 ≠ `route_id` 那條 route 所屬的 workspace」，沒有它就沒有對照物。
  `route_id` 四次讀數若有任何一次變動 ⇒ 探針／fixture 出事，整組作廢。

🔴 依 #822（`docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md`）：
任何「預期例外 ＋ 之後驗 DB 狀態」的格子一律用裸 `try/except` ＋ `else: self.fail(...)`，
**不用 `assertRaises`**——它把 with 區塊包進 `env.cr.savepoint()`，例外之後的 DB 斷言
量到的是回捲後的世界。本檔的攻擊格走真 RPC（例外變成 JSON payload，不進 Python），
所以主要受影響的是 in-process 的那幾格。

⚠ 本檔**不 import 也不 patch 任何尚未存在的符號**：RED 的紅必須落在量測斷言上，
`AttributeError` 是另一種紅。⇒ 預測 **0 ERROR**；出現 ERROR ⇒ 探針壞了，先修探針。
"""

import json
import logging
import uuid

from odoo.tests import HttpCase, tagged

from ..models.security_access import _skip_route_sync

_logger = logging.getLogger(__name__)

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'

def _new_group_user(env, name):
    """只有 `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 849' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _Projection849Fixture(HttpCase):
    """wsA（Mallory 是 owner）／wsB（Bob 是 owner、Mallory 只是 `role='user'`）。

    兩個 SA 都刻意**不給 `tunnel_id`**、錨點刻意**不給訂閱**、policy 刻意**不帶
    `cf_policy_id`**——三個前提各自關掉一道「不是本單要驗的門」，而且都斷言成
    fixture 前提（照 #848 的形狀）。

    🔴 「錨點無訂閱」對本單**特別關鍵**：`_anchor()` 讀的正是被攻擊的那個投影欄位
    （`cloud_service_id or security_access_id or smart_home_id`），
    `@api.constrains('security_access_id', ...) _check_policy_quota` 會跟著它走
    ⇒ 有訂閱時配額炸掉會被誤讀成「守衛擋住了攻擊」。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]

        self.mallory, self.mallory_login = _new_group_user(self.env, 'mallory849')
        self.assertFalse(
            self.mallory.has_group('base.group_system'),
            '前提：攻擊者只能有 base.group_user（%s 撞了）' % self.mallory.login)
        self.bob, self.bob_login = _new_group_user(self.env, 'bob849')

        # wsA —— Mallory 是 owner（攻擊者自己的 workspace）
        self.ws_a = self._workspace('WS-A 849', self.mallory)
        # wsB —— Bob 是 owner（受害者），Mallory 只是**普通成員**
        self.ws_b = self._workspace('WS-B 849', self.bob, {self.mallory: 'user'})

        self.sa_a, self.route_a = self._sa_route(self.ws_a, 'a')
        self.sa_b, self.route_b = self._sa_route(self.ws_b, 'b')

        # D2 的標的：一開始歸攻擊者（route 在 wsA）
        self.pol_a = self._policy(self.route_a, 'a')
        # D1 的標的：一開始歸受害者（route 在 wsB）
        self.pol_b = self._policy(self.route_b, 'b')

        # 前提：兩列的投影欄位一開始都與各自的 route 一致（＝尚未分裂）
        self.assertEqual(
            self._db(_POLICY, self.pol_a.id, 'security_access_id'), self.sa_a.id,
            '前提：pol_a 的投影欄位一開始必須 = SA_A')
        self.assertEqual(
            self._db(_POLICY, self.pol_b.id, 'security_access_id'), self.sa_b.id,
            '前提：pol_b 的投影欄位一開始必須 = SA_B')

    # ------------------------------------------------------------------ #
    #  Fixture helpers                                                    #
    # ------------------------------------------------------------------ #
    def _workspace(self, name, owner, extra_members=None):
        """owner 只能由 `with_user()` 決定（`_check_owner_count` 限制一個 owner）。"""
        ws = self.Workspace.with_user(owner).create(
            {'name': '%s %s' % (name, uuid.uuid4().hex[:6])})
        self.assertEqual(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', ws.id), ('role', '=', 'owner')]).user_id,
            owner,
            '前提：bootstrap 出來的 owner 必須是 %s' % owner.login)
        for user, role in (extra_members or {}).items():
            self.WorkspaceAccess.sudo().create({
                'workspace_id': ws.id, 'user_id': user.id, 'role': role})
        return ws.sudo()

    def _sa_route(self, workspace, tag):
        sa = self.SA.sudo().create(
            {'name': 'SA 849 %s' % tag, 'workspace_id': workspace.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        self.assertFalse(
            sa.sudo().subscription_id,
            '前提：SA 不得有訂閱，否則 `_anchor_plan()` 非空 ⇒ '
            '`_check_policy_quota` 會變成一道我沒預期的門（本單尤其關鍵：'
            '`_anchor()` 讀的正是被攻擊的欄位）')
        return sa, self._route(sa, tag)

    def _route(self, sa, tag):
        return _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r849%s-%s' % (tag, uuid.uuid4().hex[:6]),
        }).with_context(sa_skip_route_sync=False)

    def _policy(self, route, tag):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            'route_id': route.id,
            'name': 'pol-849-%s' % tag,
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        })
        self.assertFalse(
            pol.cf_policy_id,
            '前提：policy 不得帶 cf_policy_id，否則 `_apply_anchor_move()` 會去碰'
            ' admin-only 的 cf_access_policy_reap，多出一道我沒預期的門')
        self.assertFalse(
            pol._anchor_plan(),
            '前提：`_anchor_plan()` 必須為空 ⇒ `_check_policy_quota` 直接 continue')
        return pol.with_context(sa_skip_route_sync=False)

    # ------------------------------------------------------------------ #
    #  量測 helpers                                                       #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw` —— 本檔所有租戶面操作都走這條真實 RPC。"""
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        })
        resp = self.url_open(
            '/web/dataset/call_kw', data=body,
            headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.status_code, resp.json()

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _db(self, model_name, record_id, field_name):
        """直接讀 Postgres 的那一格——**繞過 ir.rule**。

        用 ORM 讀會把「這一列已經被判給別人、我讀不到」誤讀成「寫入被丟棄」。
        先 `flush_all()`：「沒有例外」與「沒有寫入」是兩個主張。
        """
        self.env.flush_all()
        self.assertIn(field_name, self.env[model_name]._fields)
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s'
            % (field_name, self.env[model_name]._table), (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, '記錄 %s#%s 不在 DB 裡' % (model_name, record_id))
        return row[0]

    def _measure_cell(self, login, pid, tag):
        """一格：`read`（search 看不看得到）／`write`（寫回原值）／當下 `route_id`。

        read 與 write 分開量——read 可見而 write 不可是**合法狀態**，
        混在一格會分不出來。`route_id` 每格都記，否則整張表只在講可見性、
        沒有對照物。

        另外量「撐著這次可見性的是哪一條腿」：以 domain 把 SA 腿分別窄化到 wsA
        與 wsB 各試一次。ir.rule 與 domain 是 AND，所以哪一邊回 1，
        就是那一邊的 workspace 在撐（另兩條腿在本 fixture 恆空）。
        """
        self.authenticate(login, login)

        # read
        st_r, res_r = self._call_kw(_POLICY, 'search', [[('id', '=', pid)]])
        can_read = (st_r == 200 and 'error' not in res_r
                    and pid in (res_r.get('result') or []))
        read_err = None if 'error' not in res_r else self._error_text(res_r)

        # 哪一條腿在撐
        legs = {}
        for leg_tag, ws in (('wsA', self.ws_a), ('wsB', self.ws_b)):
            st_l, res_l = self._call_kw(
                _POLICY, 'search',
                [[('id', '=', pid),
                  ('security_access_id.workspace_id', '=', ws.id)]])
            legs[leg_tag] = (st_l == 200 and 'error' not in res_l
                             and pid in (res_l.get('result') or []))

        # write（寫回原本的 name 值——無害，但仍走完整 write 路徑）
        current_name = self.env[_POLICY].sudo().browse(pid).name
        st_w, res_w = self._call_kw(
            _POLICY, 'write', [[pid], {'name': current_name}])
        can_write = (st_w == 200 and 'error' not in res_w)
        write_err = None if can_write else self._error_text(res_w)

        cell = {
            'tag': tag, 'login': login,
            'read': can_read, 'read_err': read_err,
            'write': can_write, 'write_err': write_err,
            'legs': legs,
            'route_id': self._db(_POLICY, pid, 'route_id'),
            'security_access_id': self._db(_POLICY, pid, 'security_access_id'),
        }
        _logger.info('[849][CELL:%s] %r', tag, cell)
        return cell

    def _attack(self, pid, new_sa_id, tag):
        """攻擊：只寫 `security_access_id` **一個欄位**（少一個變數）。"""
        status, result = self._call_kw(
            _POLICY, 'write', [[pid], {'security_access_id': new_sa_id}])
        landed = {
            'policy.security_access_id': self._db(_POLICY, pid, 'security_access_id'),
            'policy.route_id': self._db(_POLICY, pid, 'route_id'),
        }
        _logger.info(
            '[849][%s] RAW projection-write RPC -> http=%s error=%r landed=%r',
            tag, status,
            self._error_text(result) if 'error' in result else None, landed)
        return status, result, landed

    def _positive_control(self, pid, tag):
        """同一條 RPC、同一列、租戶合法欄位——攻擊格前面的必備陽性對照。

        少了它，「攻擊被擋」與「這個帳號對這一列根本寫不動」在輸出上不可區分。
        **回傳**而不是斷言：D1 那格的陽性對照**預期會紅**，而那個紅正是結論。
        """
        new_name = 'renamed-by-tenant-849-%s' % tag
        status, result = self._call_kw(_POLICY, 'write', [[pid], {'name': new_name}])
        ok = (status == 200 and 'error' not in result)
        landed = self._db(_POLICY, pid, 'name')
        _logger.info(
            '[849][PC:%s] positive control write name -> http=%s ok=%s '
            'error=%r landed_name=%r', tag, status, ok,
            None if ok else self._error_text(result), landed)
        return ok, landed, new_name, result


class TestProjectionFrameworkFacts849(_Projection849Fixture):
    """F：框架事實——`inverse` 到底有沒有裝。**這一組是 leader3 推論的判別式。**"""

    def test_F1_projection_fields_are_readonly_true(self):
        """related 欄位的 `readonly` 預設是 True（`_get_attrs` 的 related 區塊）。"""
        for fname in ('security_access_id', 'smart_home_id'):
            field = self.Policy._fields[fname]
            self.assertTrue(
                field.readonly,
                '#849：`%s` 的 readonly 必須是 True（related 預設）；實際 %r。'
                '若為 False 代表有人加了 readonly=False，本單結論要整組重驗'
                % (fname, field.readonly))

    def test_F2_projection_fields_have_no_inverse(self):
        """🔴 `inverse` 沒裝 ⇒ #849 原文的「寫它會寫到來源列」**機制不成立**。

        這一格若紅（inverse 真的有裝），leader3 讀框架讀錯了，
        #849 已發出的留言與給 issue-leader 的報告都要更正。
        """
        for fname in ('security_access_id', 'smart_home_id'):
            field = self.Policy._fields[fname]
            self.assertIsNone(
                field.inverse,
                '#849：`%s` 不應裝 inverse（`self.readonly` 為 True ⇒ '
                '`setup_related` 那個條件不成立）；實際 %r'
                % (fname, field.inverse))

    def test_F3_source_field_is_not_readonly(self):
        """條件依賴：本單結論建立在「兩欄沒宣告 readonly=False」上。

        來源欄位 `route.security_access_id` 的 readonly 是 False——所以決定
        inverse 裝不裝的是**投影欄位自己**的 readonly。日後若有人給投影欄位加
        `readonly=False`，來源欄位的 readonly 就變成決定性的，結論要重驗。
        """
        self.assertFalse(
            self.Route._fields['security_access_id'].readonly,
            '來源欄位 route.security_access_id 的 readonly 應為 False')

    def test_F4_projection_fields_are_stored_columns(self):
        """`store=True` ⇒ 有真 column ⇒ 值可以直接落在投影欄位上。"""
        for fname in ('security_access_id', 'smart_home_id'):
            field = self.Policy._fields[fname]
            self.assertTrue(field.store, '`%s` 必須是 stored' % fname)
            self.assertTrue(
                field.related, '`%s` 必須是 related' % fname)

    def test_F5_projection_fields_are_outside_the_848_guard_scope(self):
        """陰性對照：#848 守衛的 `_ANCHOR_FIELDS` 不含這兩欄 ⇒ 它不 fire。"""
        from ..models.security_access_access_policy import _ANCHOR_FIELDS
        for fname in ('security_access_id', 'smart_home_id'):
            self.assertNotIn(
                fname, _ANCHOR_FIELDS,
                '前提：#848 守衛跑 _ANCHOR_FIELDS=%r，本單兩欄不在其中'
                % (_ANCHOR_FIELDS,))
        self.assertIn(
            'route_id', _ANCHOR_FIELDS,
            '陽性對照：探針要抓得到確實在名單裡的那一個，否則 assertNotIn 零鑑別力')


class TestSeizeDirection849(_Projection849Fixture):
    """D1（奪取）：目標列一開始就歸受害者，攻擊者想把它搶過來。"""

    def test_D1_seize_direction(self):
        """預測：門口就被擋，**而且陽性對照會一起紅** ⇒ D1 對分裂零鑑別力。

        ⚠ 第一輪這一格漏了 `authenticate()`，整組 RPC 回的是
        `SessionExpiredException` 而不是 `AccessError` —— 而斷言照樣**綠**
        （值沒變，因為請求根本沒進到 ORM）。**探針壞掉時的輸出與「守衛擋住了」
        不可區分**，抓到它的是陽性對照的 `error` 文字，不是斷言。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        pc_ok, pc_landed, pc_want, pc_result = self._positive_control(
            self.pol_b.id, 'd1')
        _logger.info(
            '[849][D1] positive_control_ok=%s（False = 這帳號對 pol_b 根本寫不動 '
            '⇒ D1 的紅不是「守衛擋住攻擊」）', pc_ok)

        status, result, landed = self._attack(
            self.pol_b.id, self.sa_a.id, 'D1')

        self.assertEqual(
            landed['policy.security_access_id'], self.sa_b.id,
            '#849 D1：wsB 的普通成員不得把 wsB 的 policy 的投影欄位改指自己的 SA'
            '——實際落地 %r（SA_B=%r、SA_A=%r）'
            % (landed['policy.security_access_id'], self.sa_b.id, self.sa_a.id))

        # 探針前提：紅的類型必須是授權相關，`SessionExpired` 代表根本沒登入。
        for label, res in (('positive_control', pc_result), ('attack', result)):
            text = self._error_text(res) if 'error' in res else ''
            self.assertNotIn(
                'SessionExpired', text,
                'D1 探針前提：%s 回的是 SessionExpired ⇒ RPC 沒有帶認證，'
                '這一格量到的東西沒有意義（第一輪實撞）' % label)

        # 🔴「不該出現」那一格：陽性對照過了、攻擊卻被擋
        # ⇒ 代表有一道只擋 `security_access_id` 的守衛，我沒讀到它。
        if pc_ok and 'error' in result:
            self.fail(
                '#849 D1 🔴 不該出現：合法欄位寫得動（陽性對照過）但投影欄位被擋'
                '——代表存在一道我沒讀到的、只針對 `security_access_id` 的守衛。'
                'error=%r' % self._error_text(result))


class TestPushDirection849(_Projection849Fixture):
    """D2（推給別人）：目標列一開始歸攻擊者，他把投影欄位寫成受害者的 SA。"""

    def test_D2a_positive_control_tenant_can_write_own_policy(self):
        """陽性對照：攻擊者對自己 workspace 的 policy 有合法寫入權。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        ok, landed, want, result = self._positive_control(self.pol_a.id, 'd2')
        self.assertTrue(
            ok, '陽性對照：攻擊者對自己的 policy 必須寫得動，否則整組零鑑別力'
                '（error=%r）' % self._error_text(result))
        self.assertEqual(
            landed, want, '陽性對照：合法欄位必須真的落地')

    def test_D2b_projection_write_must_not_land(self):
        """🔴 本單主格：投影欄位的值不得落地。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        ok, _landed, _want, _res = self._positive_control(self.pol_a.id, 'd2b')
        self.assertTrue(ok, '前置陽性對照必須先通過')

        status, result, landed = self._attack(
            self.pol_a.id, self.sa_b.id, 'D2')

        self.assertEqual(
            landed['policy.security_access_id'], self.sa_a.id,
            '#849 D2：投影欄位不得被寫成別人的 SA——實際落地 %r'
            '（SA_A=%r、SA_B=%r），而 route_id 仍是 %r'
            % (landed['policy.security_access_id'], self.sa_a.id,
               self.sa_b.id, landed['policy.route_id']))

    def test_D2c_rejection_must_be_loud(self):
        """靜默丟棄與大聲拒絕不同：呼叫端要知道自己失敗了。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        status, result, landed = self._attack(self.pol_a.id, self.sa_b.id, 'D2c')
        self.assertIn(
            'error', result,
            '#849 D2：投影欄位的越權寫入必須**大聲**拒絕；實際 HTTP %s、'
            'DB 落地值 %r' % (status, landed['policy.security_access_id']))

    def test_D2d_source_row_must_not_change(self):
        """🔴 「不該出現」那一格：來源列被改動 ⇒ inverse 其實有裝。

        這一格紅代表 #849 原文的機制**成立**，兩張單的定位都要改。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        before = self._db(_ROUTE, self.route_a.id, 'security_access_id')
        self._attack(self.pol_a.id, self.sa_b.id, 'D2d')
        after = self._db(_ROUTE, self.route_a.id, 'security_access_id')
        _logger.info(
            '[849][D2d] route_a.security_access_id before=%r after=%r',
            before, after)
        self.assertEqual(
            after, before,
            '#849：來源列 route_a.security_access_id 不應被動到（before=%r '
            'after=%r）。若動了 ⇒ inverse 委派**確實存在**，'
            'leader3 的框架判讀要更正' % (before, after))

    def test_D2e_route_id_must_not_change(self):
        """`route_id`（真錨點）不應被動到——它是分裂的對照物。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        _s, _r, landed = self._attack(self.pol_a.id, self.sa_b.id, 'D2e')
        self.assertEqual(
            landed['policy.route_id'], self.route_a.id,
            '`route_id` 必須全程不變（實際 %r）' % (landed['policy.route_id'],))


    def test_D2f_target_sa_needs_no_visibility_to_the_attacker(self):
        """🔴 目標 SA 需不需要是攻擊者看得到的？

        #848 量到的可利用性是**共同成員**限定（要讀得到目標錨點）。本條路徑寫的
        是一個 Many2one 的 **id 整數**，而 `Field.write` 對目標**不做任何檢查**，
        三道防線又都不 fire ⇒ 若連「完全不是成員的第三個 workspace 的 SA」都寫得
        進去，本單的可利用性就比 #848 **寬一格**。

        wsC 由第三個帳號持有，攻擊者**不是** wsC 的任何角色。
        """
        carol, _carol_login = _new_group_user(self.env, 'carol849')
        ws_c = self._workspace('WS-C 849', carol)
        sa_c, _route_c = self._sa_route(ws_c, 'c')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', ws_c.id),
                ('user_id', '=', self.mallory.id)]),
            '前提：攻擊者不得是 wsC 的任何角色，否則這一格量的還是共同成員')

        self.authenticate(self.mallory_login, self.mallory_login)
        # 陽性對照：攻擊者對 wsC 的 SA 本來就讀不到（證明「不是成員」是真的）
        st_r, res_r = self._call_kw(_SA, 'search', [[('id', '=', sa_c.id)]])
        visible = (st_r == 200 and 'error' not in res_r
                   and sa_c.id in (res_r.get('result') or []))
        _logger.info(
            '[849][D2f] 攻擊者看得到 wsC 的 SA 嗎 = %s（應為 False）', visible)
        self.assertFalse(
            visible,
            '前提：攻擊者必須讀不到 wsC 的 SA，否則這一格零鑑別力')

        ok, _l, _w, _r = self._positive_control(self.pol_a.id, 'd2f')
        self.assertTrue(ok, '前置陽性對照必須先通過')

        status, result, landed = self._attack(self.pol_a.id, sa_c.id, 'D2f')
        self.assertEqual(
            landed['policy.security_access_id'], self.sa_a.id,
            '#849 D2f：投影欄位不得被寫成一個攻擊者完全讀不到的 SA 的 id'
            '——實際落地 %r（SA_A=%r、wsC 的 SA=%r），route_id 仍是 %r。'
            '若落地 ⇒ 本單可利用性比 #848 寬一格（#848 要共同成員）'
            % (landed['policy.security_access_id'], self.sa_a.id, sa_c.id,
               landed['policy.route_id']))


class TestOwnershipSplit849(_Projection849Fixture):
    """S：ir.rule 歸屬 —— 2 身分 × 2 時點 = 4 格，read／write 分開量。"""

    def _matrix(self):
        pid = self.pol_a.id
        before_att = self._measure_cell(self.mallory_login, pid, 'BEFORE/attacker')
        before_vic = self._measure_cell(self.bob_login, pid, 'BEFORE/victim')

        self.authenticate(self.mallory_login, self.mallory_login)
        status, result, landed = self._attack(pid, self.sa_b.id, 'S')

        after_att = self._measure_cell(self.mallory_login, pid, 'AFTER/attacker')
        after_vic = self._measure_cell(self.bob_login, pid, 'AFTER/victim')

        matrix = {'before_attacker': before_att, 'before_victim': before_vic,
                  'after_attacker': after_att, 'after_victim': after_vic,
                  'attack_http': status,
                  'attack_error': (self._error_text(result)
                                   if 'error' in result else None),
                  'attack_landed': landed}
        _logger.info('[849][MATRIX] %r', matrix)
        return matrix

    def test_S0_route_id_stable_across_all_four_cells(self):
        """V0：`route_id` 四次讀數必須全等，否則整組作廢（不是缺口的證據）。"""
        m = self._matrix()
        seen = {k: v['route_id'] for k, v in m.items() if isinstance(v, dict)
                and 'route_id' in v}
        self.assertEqual(
            set(seen.values()), {self.route_a.id},
            '探針前提：`route_id` 四格必須全等於 %r，實際 %r'
            % (self.route_a.id, seen))

    def test_S1_no_cell_may_flip(self):
        """🔴 分裂的判別式：**表上不得有格子翻面**（兩個方向都算）。"""
        m = self._matrix()
        flips = []
        for who, before_key, after_key in (
                ('attacker', 'before_attacker', 'after_attacker'),
                ('victim', 'before_victim', 'after_victim')):
            for op in ('read', 'write'):
                b, a = m[before_key][op], m[after_key][op]
                if b != a:
                    flips.append('%s.%s: %s -> %s' % (who, op, b, a))
        self.assertEqual(
            flips, [],
            '#849：寫投影欄位不得改變 ir.rule 的歸屬判定。實際翻面：%r；'
            '而 route_id 全程 = %r ⇒ **歸屬分裂**'
            % (flips, m['after_attacker']['route_id']))

    def test_S2_ir_rule_owner_must_match_route_owner(self):
        """🔴 分裂的直接定義：ir.rule 判給的 workspace 必須等於 route 的 workspace。"""
        m = self._matrix()
        route_ws = self.route_a.sudo().security_access_id.workspace_id
        legs = m['after_attacker']['legs']
        _logger.info(
            '[849][S2] route 所屬 workspace=%r(%s)；攻擊後撐著可見性的腿=%r',
            route_ws.id, route_ws.name, legs)
        self.assertFalse(
            legs.get('wsB'),
            '#849：攻擊後這一列不得經 wsB 的 SA 腿被判給 wsB——'
            'route_id 指的 route 屬於 workspace %r，legs=%r ⇒ 歸屬分裂'
            % (route_ws.id, legs))

    def test_S3_victim_must_not_gain_control(self):
        """受害者不應因為別人寫了一個欄位就取得一列他沒建立的 policy。"""
        m = self._matrix()
        self.assertFalse(
            m['after_victim']['read'],
            '#849：受害者不應看得到一列 route 不屬於他的 policy（'
            'route_id=%r、legs=%r）'
            % (m['after_victim']['route_id'], m['after_victim']['legs']))


class TestRecomputeWindow849(_Projection849Fixture):
    """R：落地之後撐多久——「落地但短命」與「落地且持久」對安全意義不同。"""

    def test_R1_recompute_restores_true_value(self):
        """觸發一次 recompute 之後，值應回到 `route.security_access_id`。

        **觸發點**：`policy.modified(['route_id'])` + `flush_all()`。
        **為什麼會觸發**：投影欄位的 related 路徑是 `route_id.security_access_id`
        ⇒ `route_id` 是它的依賴。
        **陽性對照（R1c）**：flush 前直接檢查 `env.records_to_compute(field)`
        含不含這一列——直接觀測「觸發有沒有把欄位排進佇列」，
        而不是靠「值有沒有變」反推（值沒變時「沒觸發」與「觸發了但算出同一個值」
        在輸出上不可區分）。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        _s, _r, landed = self._attack(self.pol_a.id, self.sa_b.id, 'R1')
        first = landed['policy.security_access_id']

        pol = self.env[_POLICY].sudo().browse(self.pol_a.id)
        field = self.Policy._fields['security_access_id']

        # 陰性對照：觸發之前佇列裡不該有它
        pre_queued = self.pol_a.id in self.env.records_to_compute(field).ids
        pol.modified(['route_id'])
        # R1c 陽性對照：觸發之後佇列裡必須有它
        post_queued = self.pol_a.id in self.env.records_to_compute(field).ids
        _logger.info(
            '[849][R1c] records_to_compute 含本列：觸發前=%s 觸發後=%s',
            pre_queued, post_queued)
        self.assertTrue(
            post_queued,
            'R1c 陽性對照：`modified([\'route_id\'])` 必須把投影欄位排進重算佇列，'
            '否則 R1 的第二次讀數零意義（前=%s 後=%s）'
            % (pre_queued, post_queued))

        self.env.flush_all()
        second = self._db(_POLICY, self.pol_a.id, 'security_access_id')
        true_value = self._db(_ROUTE, self.route_a.id, 'security_access_id')
        _logger.info(
            '[849][R1] DB 兩次讀數：寫入後立刻=%r、recompute 後=%r；'
            'route 的真值=%r', first, second, true_value)

        self.assertEqual(
            second, true_value,
            '#849：recompute 之後投影欄位應等於來源真值 %r，實際 %r'
            '（寫入後立刻的值是 %r）' % (true_value, second, first))

    def test_R2_who_can_trigger_the_realistic_recompute(self):
        """現實形態的觸發：UI 形狀的 `route_id` no-op write（UI 整批送欄位）。

        這一格量的是**誰做得到這個觸發**——若兩個租戶身分都做不到，
        窗口對租戶面就是永久的。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._attack(self.pol_a.id, self.sa_b.id, 'R2')

        outcomes = {}
        for tag, login in (('attacker', self.mallory_login),
                           ('victim', self.bob_login)):
            self.authenticate(login, login)
            st, res = self._call_kw(
                _POLICY, 'write',
                [[self.pol_a.id], {'route_id': self.route_a.id}])
            outcomes[tag] = {
                'http': st,
                'ok': st == 200 and 'error' not in res,
                'error': None if 'error' not in res else self._error_text(res),
                'db_after': self._db(_POLICY, self.pol_a.id,
                                     'security_access_id'),
            }
        # superuser 對照：這條路徑修得掉嗎
        self.env[_POLICY].sudo().browse(self.pol_a.id).write(
            {'route_id': self.route_a.id})
        outcomes['superuser'] = {
            'http': None, 'ok': True, 'error': None,
            'db_after': self._db(_POLICY, self.pol_a.id, 'security_access_id'),
        }
        _logger.info('[849][R2] 誰觸發得了自我修復：%r', outcomes)

        self.assertTrue(
            outcomes['attacker']['ok'] or outcomes['victim']['ok'],
            '#849 R2：至少要有一個租戶身分能觸發自我修復，否則窗口對租戶面是'
            '**永久**的（實際 %r）' % outcomes)


class TestGuardBypassSideBySide849(_Projection849Fixture):
    """X：並排對照——同一攻擊者、同一列，真錨點被擋 vs 投影欄位。"""

    def test_X1_real_anchor_write_is_blocked_by_848_guard(self):
        """陽性對照：寫真錨點 `route_id` 會被 #848 守衛擋下。

        這一格**綠**才代表 #848 的守衛在這個沙盒裡是活著的；
        它綠而 D2b 紅，就是「繞過」的並排實證。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        st, res = self._call_kw(
            _POLICY, 'write', [[self.pol_a.id], {'route_id': self.route_b.id}])
        landed = self._db(_POLICY, self.pol_a.id, 'route_id')
        _logger.info(
            '[849][X1] real-anchor write -> http=%s error=%r landed_route_id=%r',
            st, self._error_text(res) if 'error' in res else None, landed)
        self.assertIn(
            'error', res,
            '#848 守衛必須擋住真錨點搬移（實際 HTTP %s、落地 route_id=%r）'
            % (st, landed))
        self.assertEqual(
            landed, self.route_a.id,
            '#848 守衛擋下之後 route_id 必須原封不動（實際 %r）' % (landed,))

    def test_X2_noop_route_id_write_by_victim_before_and_after(self):
        """leader3 (b)：R2 推論鏈的獨立、較便宜的證偽路徑。

        推論鏈是：`route_id` 進 vals ⇒ `resync=True` ⇒ 那個
        `check_access('read')` 排序迴圈對 **wsA 的 route** 打 read ⇒ 受害者
        不是 wsA 成員 ⇒ `AccessError` ⇒ savepoint 整段回捲。

        ⚠ **攻擊前**打這一槍，受害者本來就連 policy 都讀不到 ⇒ 紅的理由是
        **policy 自己的 ir.rule**，不是那個排序迴圈。兩個原因都給 `AccessError`
        ⇒ 「有沒有紅」在這裡**零鑑別力**。

        ⇒ 有鑑別力的是**錯誤訊息指名哪個 model**：攻擊前應指向 policy，
        攻擊後（受害者已取得 policy 的 write）應指向 **route**。
        本格把兩個時點的原始錯誤文字都量出來，讓「紅的理由變了」看得見。
        """
        probes = {}
        for tag in ('before', 'after'):
            if tag == 'after':
                self.authenticate(self.mallory_login, self.mallory_login)
                self._attack(self.pol_a.id, self.sa_b.id, 'X2')
            self.authenticate(self.bob_login, self.bob_login)
            st, res = self._call_kw(
                _POLICY, 'write',
                [[self.pol_a.id], {'route_id': self.route_a.id}])
            text = self._error_text(res) if 'error' in res else None
            probes[tag] = {
                'http': st,
                'ok': st == 200 and 'error' not in res,
                'error': text,
                'names_policy_model': bool(text and _POLICY in text),
                'names_route_model': bool(text and _ROUTE in text),
                'db_security_access_id': self._db(
                    _POLICY, self.pol_a.id, 'security_access_id'),
                'db_route_id': self._db(_POLICY, self.pol_a.id, 'route_id'),
            }
        _logger.info('[849][X2] victim no-op route_id write：%r', probes)

        # 陽性對照：探針要能分辨兩種紅，否則整格零鑑別力。
        self.assertTrue(
            probes['before']['error'] or probes['after']['error'],
            'X2 探針前提：至少一個時點要有錯誤文字可讀，否則分不出理由（%r）'
            % probes)
        # 主張：攻擊後受害者仍不得寫這一列（不論被哪一層擋）。
        self.assertFalse(
            probes['after']['ok'],
            '#849 X2：攻擊之後受害者不應寫得動一列 route 不屬於他的 policy（%r）'
            % probes['after'])

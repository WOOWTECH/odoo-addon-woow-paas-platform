"""#849 GREEN：policy／rule 的 stored related 投影欄位不得直寫（守衛 ＋ 誤擋對照）。

RED 那支（`test_policy_related_anchor_projection_849.py`）量的是**缺口**：租戶打
一次 `/web/dataset/call_kw`，把自己一列 policy 的 `security_access_id` 寫成任意
整數，`route_id` 一動也沒動，而 `ir.rule` 的歸屬判定跟著翻面 ⇒ **歸屬分裂**、
受害者取得跨租戶 read。本檔量的是**守衛**，分兩半：

* **G 系列**——五個投影欄位（policy 2 ＋ rule 3）的直寫一律被**大聲**拒絕，
  含 superuser。每一格都配一個**同一條 RPC、同一列、合法欄位**的陽性對照，
  否則「守衛擋住了」與「這個帳號對這一列根本寫不動」在輸出上不可區分。
* **L 系列**——🔴 **這一半才是本檔存在的理由**。守衛擋的是「投影欄位出現在
  `vals` 裡」，而框架自己的 related 重算**天天都在改這幾根 column**。若那條路
  也被擋，症狀是「每次搬 route／換錨點都炸」——非常明顯，但**要在測試裡看到它
  不炸才算數**，讀碼推論不算。L 系列把每一條合法路徑都真的跑一次。

  理論是：stored related 的重算走 `Field.compute_value` → cache → `flush`
  → 低階 `_write()`，**不經過**掛守衛的公開 `write()`。L 系列是這條理論的量測。

* **C 系列**——create 側的三個載體。`_strip_identity_anchors`（#753）逐字記過：
  `super().create()` 內部的 `_add_missing_default_values()` 會把「vals 裡缺席」
  的欄位從三個載體補回來，其中 `default_<field>` context 與 `ir.default` 的列
  **都由租戶決定**。⇒ 只看 raw vals 的 create 守衛涵蓋不到後兩個載體。
  本系列**不主張機制**，只主張不變量：**投影欄位的落地值必須等於錨點算出來的
  真值**——不論它是被守衛擋掉、被壓值、還是被框架的重算蓋回去。同時把「create
  剛回來時的原始值」記進 log，讓實際走的是哪一種看得見。

⚠ 依 #822：任何「預期例外 ＋ 之後驗 DB 狀態」的格子一律用裸 `try/except` ＋
`else: self.fail(...)`，**不用 `assertRaises`**——它把 with 區塊包進
`env.cr.savepoint()`，例外之後的 DB 斷言量到的是回捲後的世界。

⚠ fixture 沿用 RED 那支的三個前提（SA 無 tunnel／錨點無訂閱／policy 無
`cf_policy_id`），各自關掉一道「不是本檔要驗的門」。「錨點無訂閱」對本檔一樣
關鍵：`_anchor()` 讀的正是被守的那幾個欄位。
"""

import json
import logging
import uuid

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from ..models.security_access import _skip_route_sync
from ..models.smart_home_route import _sm_skip_access_sync
from .smart_home_fixture import sh_model

_logger = logging.getLogger(__name__)

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'
_RULE = 'woow_paas_platform.security_access_access_policy_rule'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'


def _new_group_user(env, name):
    """只有 `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 849g' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _ProjectionGuard849Fixture(HttpCase):
    """wsA（Mallory 是 owner，兩條 route ＋ 兩個 smart home）／wsB（Bob 是 owner）。

    Mallory 在 wsB 只是 `role='user'`——RED 那支量到的可利用性形狀。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]
        self.Rule = self.env[_RULE]

        self.mallory, self.mallory_login = _new_group_user(self.env, 'mallory849g')
        self.assertFalse(
            self.mallory.has_group('base.group_system'),
            '前提：租戶只能有 base.group_user，否則 ir.rule 整個不生效（假綠）')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob849g')

        self.ws_a = self._workspace('WS-A 849g', self.mallory)
        self.ws_b = self._workspace('WS-B 849g', self.bob, {self.mallory: 'user'})

        self.sa_a, self.route_a = self._sa_route(self.ws_a, 'a')
        self.sa_a2, self.route_a2 = self._sa_route(self.ws_a, 'a2')
        self.sa_b, self.route_b = self._sa_route(self.ws_b, 'b')

        self.pol_a = self._policy(self.route_a, 'a')
        self.pol_a2 = self._policy(self.route_a2, 'a2')
        self.rule_a = self.pol_a.sudo().rule_ids[0]

        # 前提：投影欄位一開始與錨點一致（＝尚未分裂）
        self.assertEqual(
            self._db(_POLICY, self.pol_a.id, 'security_access_id'), self.sa_a.id,
            '前提：pol_a 的投影欄位一開始必須 = SA_A')
        self.assertEqual(
            self._db(_RULE, self.rule_a.id, 'security_access_id'), self.sa_a.id,
            '前提：rule_a 的投影欄位一開始必須 = SA_A（兩跳 related）')

    # ------------------------------------------------------------------ #
    #  Fixture helpers                                                    #
    # ------------------------------------------------------------------ #
    def _workspace(self, name, owner, extra_members=None):
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
            {'name': 'SA 849g %s' % tag, 'workspace_id': workspace.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        self.assertFalse(
            sa.sudo().subscription_id,
            '前提：SA 不得有訂閱，否則 `_anchor_plan()` 非空 ⇒ 配額會變成一道'
            '我沒預期的門（`_anchor()` 讀的正是被守的那個欄位）')
        return sa, self._route(sa, tag)

    def _route(self, sa, tag):
        return _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'g849%s-%s' % (tag, uuid.uuid4().hex[:6]),
        }).with_context(sa_skip_route_sync=False)

    def _sh_route(self, workspace, tag):
        sh = sh_model(self.env).create({
            'name': 'sh-849g-%s-%s' % (tag, uuid.uuid4().hex[:6]),
            'workspace_id': workspace.id,
            'state': 'active',
        }).with_env(self.env)
        return _sm_skip_access_sync(self.env[_SH_ROUTE].sudo()).create(
            {'smart_home_id': sh.id, 'is_primary': True})

    def _policy(self, route, tag, anchor_field='route_id'):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            anchor_field: route.id,
            'name': 'pol-849g-%s' % tag,
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
            '前提：`_anchor_plan()` 必須為空 ⇒ 配額檢查直接 continue')
        return pol.with_context(sa_skip_route_sync=False)

    # ------------------------------------------------------------------ #
    #  量測 helpers                                                       #
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
        self.env.invalidate_all()
        return resp.status_code, resp.json()

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _db(self, model_name, record_id, field_name):
        """直接讀 Postgres 的那一格——**繞過 ir.rule**，且先 `flush_all()`。

        用 ORM 讀會把「這一列已經被判給別人、我讀不到」誤讀成「寫入被丟棄」。
        """
        self.env.flush_all()
        return self._db_noflush(model_name, record_id, field_name)

    def _db_noflush(self, model_name, record_id, field_name):
        """同上但**不 flush**——用來看「還沒重算之前 column 上是什麼」。"""
        self.assertIn(field_name, self.env[model_name]._fields)
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s'
            % (field_name, self.env[model_name]._table), (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, '記錄 %s#%s 不在 DB 裡' % (model_name, record_id))
        return row[0]

    def _rpc_write(self, model, rec_id, vals, tag):
        status, result = self._call_kw(model, 'write', [[rec_id], vals])
        _logger.info(
            '[849G][%s] RPC write %s#%s %r -> http=%s error=%r',
            tag, model, rec_id, vals, status,
            self._error_text(result) if 'error' in result else None)
        return status, result

    def _positive_control(self, model, rec_id, tag):
        """同一條 RPC、同一列、租戶合法欄位——每個守衛格前面的必備陽性對照。

        少了它，「守衛擋住」與「這個帳號對這一列根本寫不動」在輸出上不可區分。
        """
        field = 'name' if model == _POLICY else 'rule_value'
        value = ('renamed-849g-%s' % tag if field == 'name'
                 else 'pc-849g-%s@example.com' % tag)
        status, result = self._call_kw(model, 'write', [[rec_id], {field: value}])
        ok = (status == 200 and 'error' not in result)
        landed = self._db(model, rec_id, field)
        _logger.info(
            '[849G][PC:%s] %s.%s -> http=%s ok=%s error=%r landed=%r',
            tag, model, field, status, ok,
            None if ok else self._error_text(result), landed)
        self.assertTrue(
            ok, '陽性對照：租戶對自己的 %s 必須寫得動合法欄位，否則同組守衛格'
                '零鑑別力（error=%r）' % (model, self._error_text(result)))
        self.assertEqual(landed, value, '陽性對照：合法欄位必須真的落地')
        return ok


class TestProjectionGuardBlocksTenantWrites849(_ProjectionGuard849Fixture):
    """G：五個投影欄位的租戶直寫一律被大聲拒絕，而且值不落地。"""

    def _assert_blocked(self, model, rec_id, field, new_id, tag):
        before = self._db(model, rec_id, field)
        status, result = self._rpc_write(model, rec_id, {field: new_id}, tag)
        after = self._db(model, rec_id, field)
        # 大聲：呼叫端必須知道自己失敗了（靜默丟棄與拒絕不同）
        self.assertIn(
            'error', result,
            '#849：`%s.%s` 的直寫必須**大聲**拒絕；實際 HTTP %s、DB 值 %r'
            % (model, field, status, after))
        # 訊息要指名是哪個欄位——否則分不出擋它的是本守衛還是別的 constraint
        text = self._error_text(result)
        self.assertIn(
            field, text,
            '#849：拒絕訊息必須指名被拒的欄位 `%s`，否則無法分辨擋它的是本守衛'
            '還是別的 constraint（實際 %r）' % (field, text))
        self.assertNotIn(
            'SessionExpired', text,
            '探針前提：回的是 SessionExpired ⇒ RPC 沒帶認證，這一格零意義（%r）'
            % text)
        # 不落地
        self.assertEqual(
            after, before,
            '#849：`%s.%s` 被拒之後值必須原封不動（before=%r after=%r）'
            % (model, field, before, after))
        return text

    def test_G1_policy_security_access_id_write_is_rejected(self):
        """🔴 主格：RED 那支量到的那一條路徑，現在必須被擋。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        self._positive_control(_POLICY, self.pol_a.id, 'g1')
        self._assert_blocked(
            _POLICY, self.pol_a.id, 'security_access_id', self.sa_b.id, 'G1')
        # 真錨點必須全程不變——它是分裂的對照物
        self.assertEqual(
            self._db(_POLICY, self.pol_a.id, 'route_id'), self.route_a.id,
            '`route_id` 必須全程不變')

    def test_G2_policy_smart_home_id_write_is_rejected(self):
        """policy 的第二個投影欄位（smart home 那條腿）。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        self._positive_control(_POLICY, self.pol_a.id, 'g2')
        sh_route_b = self._sh_route(self.ws_b, 'g2b')
        self._assert_blocked(
            _POLICY, self.pol_a.id, 'smart_home_id',
            sh_route_b.smart_home_id.id, 'G2')

    def test_G3_rule_three_projection_fields_are_rejected(self):
        """🔴 rule model 三欄——RED 那支零覆蓋的那一半。

        三欄逐一量（⛔ 不是量一欄然後外推）：同名不同性質，
        `cloud_service_id` 在 policy 上是**真錨點**、在 rule 上是**投影**。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._positive_control(_RULE, self.rule_a.id, 'g3')
        sh_route_b = self._sh_route(self.ws_b, 'g3b')
        targets = {
            'security_access_id': self.sa_b.id,
            'cloud_service_id': 12345,
            'smart_home_id': sh_route_b.smart_home_id.id,
        }
        for field, new_id in targets.items():
            self._assert_blocked(_RULE, self.rule_a.id, field, new_id,
                                 'G3:%s' % field)
        self.assertEqual(
            self._db(_RULE, self.rule_a.id, 'policy_id'), self.pol_a.id,
            '真錨點 `policy_id` 必須全程不變')

    def test_G4_superuser_is_not_exempt(self):
        """契約：**沒有 su 豁免**。

        `env.su` 豁免會在每個 `TransactionCase` 與每條 `sudo()` 路徑上靜默停用
        守衛（見 `_INTERNAL_IDENTITY_WRITE` 註解與
        `docs/decisions/patterns/su-env-exempt-guard-false-green.md`）。投影欄位
        **沒有**合法的直寫路徑 ⇒ 任何 su 豁免都是洞。

        ⚠ 這一格同時是「L 系列在 sudo 下跑也有意義」的前提：守衛在 su 下仍然
        活著，所以 L 系列量到的「重算沒被擋」不是因為 sudo 把守衛關掉了。
        """
        before = self._db(_POLICY, self.pol_a.id, 'security_access_id')
        try:
            self.env[_POLICY].sudo().browse(self.pol_a.id).write(
                {'security_access_id': self.sa_b.id})
        except UserError as exc:
            self.assertIn('security_access_id', str(exc))
        else:
            self.fail(
                '#849：superuser 也必須被擋（su 豁免＝洞）。DB 值 before=%r '
                'after=%r' % (before,
                              self._db(_POLICY, self.pol_a.id,
                                       'security_access_id')))
        self.assertEqual(
            self._db(_POLICY, self.pol_a.id, 'security_access_id'), before,
            '被拒之後值必須原封不動')


class TestProjectionRecomputeNotFalselyBlocked849(_ProjectionGuard849Fixture):
    """🔴 L：框架自己的 related 重算**不得**被守衛誤擋。

    每一格的判準都是兩件事一起成立：**(a) 不 raise**、**(b) 投影欄位真的被算成
    新值**。只驗 (a) 會把「守衛沒擋、但值也沒更新」放行——那同樣是壞的，而且
    症狀是安靜的（歸屬停在舊 workspace）。
    """

    def _assert_projection(self, model, rec_id, field, expected, tag):
        actual = self._db(model, rec_id, field)
        _logger.info('[849L][%s] %s#%s.%s = %r（期望 %r）',
                     tag, model, rec_id, field, actual, expected)
        self.assertEqual(
            actual, expected,
            'L%s：重算之後 `%s.%s` 必須 = %r，實際 %r ⇒ 若相等於舊值代表重算被'
            '守衛吃掉了（守衛沒 raise、但值也沒更新，症狀是安靜的）'
            % (tag, model, field, expected, actual))

    def test_L1_route_moved_to_another_sa_recomputes_projection(self):
        """route 搬到別的 SA ⇒ 底下 policy／rule 的投影欄位必須重算成功。

        走 `sudo()` 是刻意的，而且**有效**：本守衛沒有 su 豁免（G4 已把這件事
        斷言成契約）⇒ 它若會誤擋，在這裡照樣會炸。
        """
        self.Route.sudo().browse(self.route_a.id).write(
            {'security_access_id': self.sa_a2.id})
        self._assert_projection(
            _POLICY, self.pol_a.id, 'security_access_id', self.sa_a2.id, '1p')
        self._assert_projection(
            _RULE, self.rule_a.id, 'security_access_id', self.sa_a2.id, '1r')

    def test_L2_policy_anchor_move_recomputes_projection(self):
        """policy 換錨點（#848 的**授權**路徑，真租戶身分）⇒ 投影欄位重算。"""
        pol = self.Policy.with_user(self.mallory).browse(self.pol_a.id)
        pol.write({'route_id': self.route_a2.id})
        self._assert_projection(
            _POLICY, self.pol_a.id, 'security_access_id', self.sa_a2.id, '2p')
        self._assert_projection(
            _RULE, self.rule_a.id, 'security_access_id', self.sa_a2.id, '2r')

    def test_L3_policy_create_computes_projection(self):
        """建立時投影欄位被算出來（沒有它，新 policy 對誰都不可見）。"""
        pol = self._policy(self.route_a2, 'L3')
        self._assert_projection(
            _POLICY, pol.id, 'security_access_id', self.sa_a2.id, '3')

    def test_L4_rule_moved_to_another_policy_recomputes_three_fields(self):
        """rule 搬到另一個 policy ⇒ 三個投影欄位一起重算（真租戶身分）。

        ⚠ 搬的是**第二條** include rule：把 pol_a 唯一那條搬走會撞
        `_check_at_least_one_include`（實撞過），那是另一道不變量、與本格無關。
        """
        extra = _skip_route_sync(self.Rule.sudo()).create({
            'policy_id': self.pol_a.id,
            'group': 'include',
            'rule_type': 'email_list',
            'rule_value': 'l4@example.com',
        })
        self._assert_projection(
            _RULE, extra.id, 'security_access_id', self.sa_a.id, '4pre')
        self.Rule.with_user(self.mallory).browse(extra.id).write(
            {'policy_id': self.pol_a2.id})
        self._assert_projection(
            _RULE, extra.id, 'security_access_id', self.sa_a2.id, '4sa')
        # 另外兩欄在 route 錨定下恆空——一起量，證明它們也走完了重算而不是被擋
        self._assert_projection(
            _RULE, extra.id, 'cloud_service_id', None, '4cs')
        self._assert_projection(
            _RULE, extra.id, 'smart_home_id', None, '4sh')

    def test_L5_rule_create_computes_three_projection_fields(self):
        """建立 rule 時三個投影欄位被算出來。"""
        rule = _skip_route_sync(self.Rule.sudo()).create({
            'policy_id': self.pol_a2.id,
            'group': 'include',
            'rule_type': 'email_list',
            'rule_value': 'l5@example.com',
        })
        self._assert_projection(
            _RULE, rule.id, 'security_access_id', self.sa_a2.id, '5sa')
        self._assert_projection(
            _RULE, rule.id, 'cloud_service_id', None, '5cs')
        self._assert_projection(
            _RULE, rule.id, 'smart_home_id', None, '5sh')

    def test_L6_smart_home_leg_recomputes_projection(self):
        """第三條腿：policy 換 `smart_home_route_id` ⇒ `smart_home_id` 重算。

        這條腿與 route 腿走的是**不同的來源欄位**，⛔ 不可從 L1／L2 外推。
        """
        sh_route_1 = self._sh_route(self.ws_a, 'L6a')
        sh_route_2 = self._sh_route(self.ws_a, 'L6b')
        pol = self._policy(sh_route_1, 'L6', anchor_field='smart_home_route_id')
        rule = pol.sudo().rule_ids[0]
        self._assert_projection(
            _POLICY, pol.id, 'smart_home_id', sh_route_1.smart_home_id.id, '6a')
        self._assert_projection(
            _RULE, rule.id, 'smart_home_id', sh_route_1.smart_home_id.id, '6ra')

        self.Policy.with_user(self.mallory).browse(pol.id).write(
            {'smart_home_route_id': sh_route_2.id})
        self._assert_projection(
            _POLICY, pol.id, 'smart_home_id', sh_route_2.smart_home_id.id, '6b')
        self._assert_projection(
            _RULE, rule.id, 'smart_home_id', sh_route_2.smart_home_id.id, '6rb')


class TestProjectionCreateCarriers849(_ProjectionGuard849Fixture):
    """C：create 側三個載體。不變量＝**落地值必須等於錨點算出來的真值**。

    ⛔ 本系列刻意**不主張機制**（守衛擋掉／壓值／框架重算蓋回，三種輸出不同但
    都滿足不變量）。要主張機制就要另外量，而那不是本單的守備範圍——
    `_strip_identity_anchors`（#753）的教訓是：主張錯的機制比不主張更糟。

    🔴 **量到的結果（2026-08-26，本檔第一次跑）：三個載體都是「框架自己中和掉」**
    ——create 回來時**連 raw column 上都已經是錨點真值**（log 逐格記著
    `raw=… landed=… route 真值=…` 三個數字，三格全等）。所以 **create 側沒有加
    守衛**，這是量出來的決定不是省略：

    * `odoo/models.py::create()` 只把「可編輯的 computed 欄位」放進 `protected`
      （條件是 `field.compute and (not field.readonly or field.precompute)`）。
      投影欄位 `readonly=True` ＋ 非 precompute ⇒ **不在** `protected` 裡
      ⇒ `_create()` 尾端的 `records.modified(self._fields, create=True)` 照樣把
      它排進重算佇列，而同一個方法尾端的 `records.check_access('create')` 會
      觸發 flush ⇒ create 還沒回來，值就被算回真值了。
    * 這正是它與 `cf_policy_id`（#798 用 `_strip_identity_anchors` 壓值）的差別：
      那是一個**沒有 compute 的 Char**，框架不會替它自我修復。

    ⚠ 這個結論的前提是投影欄位維持 `readonly=True`。踩斷它的 tripwire 已經在
    RED 那支裡：`test_F1_projection_fields_are_readonly_true`。若哪天有人加了
    `readonly=False`，F1 先紅，本系列的結論要整組重驗。
    """

    def _assert_create_carrier_neutralised(self, pol, tag):
        raw = self._db_noflush(_POLICY, pol.id, 'security_access_id')
        landed = self._db(_POLICY, pol.id, 'security_access_id')
        truth = self._db(_ROUTE, self.route_a.id, 'security_access_id')
        _logger.info(
            '[849C][%s] create 後 raw=%r、flush 後 landed=%r、route 真值=%r、'
            '攻擊者想種的=%r', tag, raw, landed, truth, self.sa_b.id)
        self.assertEqual(
            landed, truth,
            '#849 C%s：create 側載體 %s 不得讓投影欄位偏離錨點真值 %r'
            '（實際 %r，攻擊者想種的是 %r）'
            % (tag, tag, truth, landed, self.sa_b.id))
        self.assertNotEqual(
            landed, self.sa_b.id,
            '#849 C%s：投影欄位落地成攻擊者選的 SA ⇒ 歸屬分裂' % tag)

    def test_C1_explicit_projection_value_in_create_vals(self):
        """載體 3：直接把投影欄位寫進 create 的 vals。"""
        try:
            pol = self._policy_with(
                {'security_access_id': self.sa_b.id}, 'C1')
        except UserError as exc:
            _logger.info('[849C][C1] create 被守衛擋下：%s', exc)
            self.assertIn('security_access_id', str(exc))
            return
        self._assert_create_carrier_neutralised(pol, '1')

    def test_C2_default_context_carrier(self):
        """載體 1：`default_<field>` context——`/web/dataset/call_kw` 把呼叫端
        context 整包換進 env（`odoo/api.py::call_kw`），租戶完全可控。"""
        try:
            pol = self._policy_with(
                {}, 'C2', ctx={'default_security_access_id': self.sa_b.id})
        except UserError as exc:
            _logger.info('[849C][C2] create 被守衛擋下：%s', exc)
            return
        self._assert_create_carrier_neutralised(pol, '2')

    def test_C3_ir_default_row_carrier(self):
        """載體 2：`ir.default` 的列——租戶對它有完整 CRUD
        （base 的 `access_ir_default_group_user` = 1,1,1,1）。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        st, res = self._call_kw(
            'ir.default', 'set',
            [_POLICY, 'security_access_id', self.sa_b.id, True])
        _logger.info('[849C][C3] ir.default.set -> http=%s error=%r', st,
                     self._error_text(res) if 'error' in res else None)
        stored = self.env['ir.default'].sudo().search([
            ('field_id.model', '=', _POLICY),
            ('field_id.name', '=', 'security_access_id'),
            ('user_id', '=', self.mallory.id)])
        if not stored:
            self.skipTest(
                'C3 探針前提不成立：租戶的 ir.default.set 沒有落庫（http=%s '
                'error=%r）⇒ 這條載體在本樹上打不開，這一格量不到東西'
                % (st, self._error_text(res) if 'error' in res else None))
        self.assertIn(
            json.dumps(self.sa_b.id), stored.mapped('json_value'),
            '陽性對照：落庫的值必須是攻擊者選的那個，否則本格零鑑別力')
        try:
            pol = self.Policy.with_user(self.mallory)
            pol = self._policy_with({}, 'C3', model=pol)
        except UserError as exc:
            _logger.info('[849C][C3] create 被守衛擋下：%s', exc)
            return
        self._assert_create_carrier_neutralised(pol, '3')

    def _policy_with(self, extra_vals, tag, ctx=None, model=None):
        handle = _skip_route_sync(model if model is not None
                                  else self.Policy.sudo())
        if ctx:
            handle = handle.with_context(**ctx)
        vals = {
            'route_id': self.route_a.id,
            'name': 'pol-849g-%s' % tag,
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'c@example.com'})],
        }
        vals.update(extra_vals)
        return handle.create(vals)

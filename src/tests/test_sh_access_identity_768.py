"""#768：`smart_home_route` 身分錨點對「預設值載體」的攻防（#753 同型）。

## 為什麼需要這一檔

`smart_home_route.access_app_id` 決定「本列的 teardown／sync 會動到**哪一個**
Cloudflare Access application」。把它換掉不是「改一個設定」，是把整列重新瞄準別人
的資源，而該列仍保有自己 workspace 的 ir.rule 祝福。

守衛（`_strip_sm_route_identity_anchors` / `_reject_sm_route_identity_writes`）在
PR #771 就寫好了，而且寫的是 #753 的正解（**壓伺服器端值**而不是 `vals.pop()`）。
但 #771 的驗收指出：**沒有任何測試證明它擋得住**。這正是 #753 那條教訓的更根本版
——那次是「守衛看起來有測試、實際上從未被執行」，這次是測試根本沒被寫。本檔補上。

## 三個載體與它們對 SH route 的實際可達性（不要照抄 SA 的結論）

`super().create()` 內部的 `_add_missing_default_values(vals)` 會把「vals 裡**缺席**」
的欄位從三個載體補回來（`odoo/models.py::default_get` 的順位）：

1. `default_<field>` **context** —— 🔴 **在 SH route 上是真的可達的**。route 的唯一
   建立點 `smart_home._ensure_primary_route()` 走 `.sudo()`，而 **`.sudo()` 不會洗掉
   context**；它是由租戶的 `action_provision` 請求觸發的，所以租戶 RPC 帶進來的
   `default_access_app_id` 會一路流到 `Route.sudo().create()`。B 系列打的就是這條
   **生產路徑**，不是一個假想的呼叫。
2. `ir.default` 的列 —— 可達性**比 SA 那邊窄**：`_get_model_defaults` 收的是
   `user_id = env.uid` 或 `user_id IS NULL` 的列，而平台建立路徑走 sudo（uid=1）
   ⇒ 租戶自己那一列收不到，**全域列（`user_id=False`）才收得到**。C 系列因此用
   全域列打，並在同一個方法裡用非 anchor 欄位證明這個載體真的有到——否則「anchor
   沒被種進去」與「這個載體在這條路徑上根本不作用」分不出來，而後者會讓本檔在守衛
   被拿掉之後**仍然是綠的**。
3. 欄位自身的 `default=` —— 伺服器端程式碼，呼叫端碰不到。`access_app_id` 沒有
   欄位級 default ⇒ 壓 `False`（見 `_server_owned_anchor_value`）。

⚠ 另一件不可照抄 SA 的事：**租戶對 `smart_home_route` 的 ACL 是 `1,1,0,0`**
（`create=0`），所以「租戶直接 RPC create 一條 route」在 ACL 那層就被擋掉了
（E1 把這件事釘住）。這**不代表 model 層守衛可以省**——上面第 1 點那條經平台碼
的路徑繞過了 ACL（它是 sudo 的），而那正是這道守衛存在的理由。

## 每一格都要能分辨「守住了」與「探針壞了」

- A 系列：錨點清單本身（anti-vacuity floor）。清單打錯字會讓底下每一個迴圈靜默
  no-op，而那些測試會**全綠**。
- B／C 系列：每一格都配一個**同機制、非 anchor** 的對照欄位（`session_duration`）。
- D 系列：write 側拒絕 ＋ 偽造 context 值迴圈。
- E 系列：陽性對照——真 sentinel 仍寫得進（`assertIs` 比對**物件身分**）、`sudo()`
  本身打不開、以及 🔴 **SH 的 fixture 開閘不得順便打開 route 的閘**（那是本 PR 把
  兩把 sentinel 分開的全部理由；共用一把的話所有沿用 `sh_model()` 的既有 fixture
  會連帶把這道守衛關掉）。
"""
import json
import uuid

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.smart_home_route import (
    _INTERNAL_SM_ROUTE_IDENTITY_WRITE,
    _SM_ROUTE_IDENTITY_ANCHOR_FIELDS,
    _SM_ROUTE_IDENTITY_CTX_KEY,
    _internal_sm_route_identity,
)
from .smart_home_fixture import sh_model

_ROUTE = 'woow_paas_platform.smart_home_route'

#: anti-vacuity floor：本檔的迴圈全部跑 `_SM_ROUTE_IDENTITY_ANCHOR_FIELDS`，所以
#: 那個常數若被清空或打錯字，每一條負向斷言都會變成 no-op 而**全綠**。這個 floor
#: 是「至少要蓋到這些」的下界——新增錨點時迴圈自動涵蓋，不必改測試；但**移除**
#: 錨點會在這裡紅。
_ANCHOR_FLOOR = {'access_app_id'}

#: 「JSON 表達得出來」的每一種偽造值。sentinel 的全部價值就是這幾個都打不開；
#: 少了這條迴圈，把守衛改回裸真值（`if ctx.get(KEY):`）不會有任何測試變紅。
_FORGED_CTX_VALUES = (True, 1, 'true', 'True', [1], {'x': 1}, 'internal')


class _ShRouteAnchor768Fixture(TransactionCase):
    """model 層 fixture。刻意跑在 `TransactionCase`（＝su env）。

    守衛若寫成 `if not self.env.su:` 這一整組會靜默恆綠
    （`docs/decisions/patterns/su-env-exempt-guard-false-green.md`），所以 setUp
    有一條前提斷言把「我們真的在 su env 裡」釘住。
    """

    def setUp(self):
        super().setUp()
        self.assertTrue(
            self.env.su,
            '前提：本組必須跑在 su env 才有鑑別力——守衛若對 su 豁免，'
            '在非 su env 下每一條負向斷言都會為了錯的理由變綠')
        self.Route = self.env[_ROUTE]
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SH anchor WS 768'})
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        # 受害者真的存在的 CF Access app id——攻擊者要搶的就是這個。
        self.victim_app = 'victim-app-%s' % uuid.uuid4().hex[:8]

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #
    def _smart_home(self, name='sh-anchor-768', subdomain=None):
        """一台已 provision（有 subdomain）但**還沒有 route** 的 Smart Home。

        用 `sh_model()` 開 SH 的兩把閘建立——這是刻意的：E3 要證明「SH 的閘打開
        不等於 route 的閘也打開」，而其餘每一格都在這個前提下跑，代表它們量到的
        不是「fixture 剛好沒開閘」。
        """
        sh = sh_model(self.env).create({
            'name': name, 'workspace_id': self.workspace.id,
        }).with_env(self.env)
        sh.sudo().write({
            'subdomain': subdomain or 'paas-sm-768-%s' % uuid.uuid4().hex[:8],
        })
        return sh

    def _db(self, record_id, field_name):
        """直接讀 Postgres 的那一格。

        `flush_all()` 先把尚未 flush 的寫入推下去，才不會讓「值其實被接受了」
        讀起來像沒變——「沒有例外」與「沒有寫入」是兩個不同的主張。
        """
        self.env.flush_all()
        # 欄位名一律來自 model 自己的 _fields，不來自使用者輸入。
        self.assertIn(field_name, self.Route._fields)
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s'
            % (field_name, self.Route._table), (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, 'route#%s 不在 DB 裡' % record_id)
        return row[0]

    def _forged_value(self, field):
        """A value the field's type can parse; the subject here is the guard."""
        if self.Route._fields[field].type == 'datetime':
            return '2000-01-01 00:00:00'
        return self.victim_app

    def _anchor_ctx_defaults(self, value):
        """`{'default_<每一個錨點>': value}`——迴圈跑宣告式清單，新錨點自動涵蓋。"""
        return {'default_%s' % f: value
                for f in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS}


class TestShRouteAnchorDeclaration(_ShRouteAnchor768Fixture):
    """A：錨點清單本身（anti-vacuity）。"""

    def test_A1_anchor_list_is_not_empty_and_covers_the_floor(self):
        self.assertTrue(
            _SM_ROUTE_IDENTITY_ANCHOR_FIELDS,
            '錨點清單是空的 ⇒ 本檔每一個迴圈都變成 no-op 而全綠')
        missing = _ANCHOR_FLOOR - set(_SM_ROUTE_IDENTITY_ANCHOR_FIELDS)
        self.assertFalse(
            missing,
            '錨點清單少了 %s ——這些欄位決定本列驅動的是哪一個 Cloudflare '
            '物件，從清單移除等於靜默取消守衛' % sorted(missing))

    def test_A2_every_declared_anchor_is_a_real_field(self):
        """打錯字的欄位名會讓守衛對它「什麼都不做」，而且不會有任何錯誤。"""
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            self.assertIn(
                field, self.Route._fields,
                '錨點清單裡的 %r 不是 %s 的欄位（打錯字＝該錨點無人看守）'
                % (field, _ROUTE))


class TestShRouteAnchorDefaultContext(_ShRouteAnchor768Fixture):
    """B：`default_<field>` context —— SH route 上**真的可達**的那條。"""

    def test_B1_platform_create_path_ignores_caller_supplied_defaults(self):
        """🔴 主格：走**生產路徑** `_ensure_primary_route()`。

        `action_provision` 是租戶的請求觸發的，`_ensure_primary_route()` 走
        `.sudo()`，而 `.sudo()` **不會**洗掉 context ⇒ 租戶 RPC 帶進來的
        `default_access_app_id` 會一路流到 `Route.sudo().create()`。這不是一個
        假想的呼叫端，是這道守衛存在的理由本身。

        `session_duration` 是**刻意挑的**對照欄位：同機制（也走
        `_add_missing_default_values`）、有欄位級 default、且不是錨點。它必須被
        注入成功，否則下面那條「沒被注入」可能只是 context 根本沒到——而那會讓
        本測試在守衛被拿掉之後仍然是綠的。
        """
        sh = self._smart_home()
        tainted = sh.with_context(
            default_session_duration='168h',
            **self._anchor_ctx_defaults(self.victim_app))

        route = tainted._ensure_primary_route()

        self.assertEqual(
            self._db(route.id, 'session_duration'), '168h',
            '陽性對照：`default_<field>` context 對**非 anchor 欄位**必須真的'
            '生效——不生效的話，下面每一條就算守衛被拿掉也會是綠的')
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            self.assertFalse(
                self._db(route.id, field),
                '#753 同型：`default_%s` 經平台建立路徑不得落地（實得 %r）'
                % (field, self._db(route.id, field)))

    def test_B2_direct_create_ignores_caller_supplied_defaults(self):
        """同一個載體，直接打 `Route.create()`（涵蓋未來新增的建立點）。"""
        sh = self._smart_home()
        route = self.Route.with_context(
            default_session_duration='30m',
            **self._anchor_ctx_defaults(self.victim_app),
        ).create({'smart_home_id': sh.id, 'is_primary': True})

        self.assertEqual(
            self._db(route.id, 'session_duration'), '30m',
            '陽性對照：同一個載體對非 anchor 欄位必須真的生效')
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            self.assertFalse(
                self._db(route.id, field),
                '#753 同型：`default_%s` 不得落地' % field)

    def test_B3_vals_supplied_anchor_is_pressed_too(self):
        """呼叫端**明寫在 vals 裡**的那一份同樣要被壓掉。

        這一格與 B1／B2 抓的不是同一件事：`pop` 版的守衛在這裡是**綠的**
        （它清得掉寫在 vals 裡的），紅的只有 B1／B2。反過來，一個「只堵 context
        載體」的修法在這裡會紅。兩格都要有。
        """
        sh = self._smart_home()
        vals = {'smart_home_id': sh.id, 'is_primary': True}
        vals.update({f: self.victim_app
                     for f in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS})
        route = self.Route.create(vals)
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            self.assertFalse(
                self._db(route.id, field),
                'vals 直接帶進來的 %s 必須被壓成伺服器端值' % field)

    def test_B4_legit_create_is_not_broken_by_the_guard(self):
        """陰性對照：修法不得把合法 create 一起炸掉。

        `access_app_id` 沒有欄位級 default，所以壓 `False` 是對的；但若日後新增
        一個 `required=True` ＋ 有 default 的錨點，無腦壓 False 會讓 required 在
        `super().create()` 內炸掉——而 B1～B3 全部仍是綠的（它們只斷言「攻擊者的
        值沒落地」，create 直接失敗同樣滿足）。#753 的 D 系列就是這條的先例。
        """
        sh = self._smart_home()
        route = sh._ensure_primary_route()
        self.assertTrue(route.id, '一般合法 create 不得失敗')
        self.assertTrue(route.is_primary)
        self.assertEqual(
            route.session_duration, '24h',
            '沒被注入時必須拿到欄位自己的 default')
        self.assertTrue(
            route.hostname,
            'hostname 衍生自 smart_home.subdomain，合法建立後必須算得出來')


class TestShRouteAnchorIrDefault(_ShRouteAnchor768Fixture):
    """C：`ir.default` 載體。

    ⚠ 可達性與 SA 那邊**不同**，不要照抄結論：平台建立路徑走 sudo（uid=1），
    `_get_model_defaults` 只收 `user_id = env.uid` 或 `user_id IS NULL` 的列
    ⇒ 租戶自己那一列在這條路徑上收不到，**全域列才收得到**。所以這裡用全域列打，
    並在同一個方法裡證明這個載體真的有到。
    """

    def _set_global_default(self, field_name, value):
        self.env['ir.default'].sudo().set(_ROUTE, field_name, value)
        stored = self.env['ir.default'].sudo().search([
            ('field_id.model', '=', _ROUTE),
            ('field_id.name', '=', field_name),
            ('user_id', '=', False),
        ])
        self.assertTrue(
            stored,
            '陽性對照 (a)：ir.default.set(%s) 必須真的落庫，否則本測試零鑑別力'
            % field_name)
        self.assertIn(
            json.dumps(value, ensure_ascii=False),
            stored.mapped('json_value'),
            '陽性對照 (a)：落庫的值必須是攻擊者選的那個')

    def test_C1_ir_default_cannot_seed_route_anchors(self):
        """🔴 比 context 載體更隱蔽：它與呼叫端 payload 無關。

        只堵 context 的修法會在這一格紅。
        """
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            self._set_global_default(field, self._forged_value(field))
        self._set_global_default('session_duration', '8h')

        sh = self._smart_home()
        route = sh._ensure_primary_route()

        self.assertEqual(
            self._db(route.id, 'session_duration'), '8h',
            '陽性對照 (b)：`ir.default` 對**非 anchor 欄位**必須真的生效——'
            '不生效的話，下面那條「沒被種進去」證明不了守衛做了任何事')
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            self.assertFalse(
                self._db(route.id, field),
                '#753 同型：`ir.default` 注入的 %s 不得落地（實得 %r）'
                % (field, self._db(route.id, field)))


class TestShRouteAnchorWriteGuard(_ShRouteAnchor768Fixture):
    """D：write 側。"""

    def test_D1_write_rejects_every_declared_anchor(self):
        sh = self._smart_home()
        route = sh._ensure_primary_route()
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            with self.assertRaises(
                    UserError,
                    msg='write 必須拒絕移動錨點 %s' % field) as ctx:
                route.write({field: self.victim_app})
            self.assertIn(
                field, str(ctx.exception),
                '訊息要指名是哪個欄位（實得 %r）' % str(ctx.exception))
            self.assertFalse(
                self._db(route.id, field), '被拒之後值不得殘留')

    def test_D2_ordinary_fields_are_still_writable(self):
        """陽性對照：守衛不得變成「什麼都不准寫」。

        沒有這一條，D1 的綠燈與「write 對本 model 整個壞掉」不可區分。
        """
        sh = self._smart_home()
        route = sh._ensure_primary_route()
        route.write({'session_duration': '1h', 'login_google': True})
        self.assertEqual(self._db(route.id, 'session_duration'), '1h')
        self.assertTrue(self._db(route.id, 'login_google'))

    def test_D3_forged_context_values_do_not_open_the_write_gate(self):
        """JSON 載不了物件身分：帶同名 key 但值是 JSON 表達得出的，閘不開。"""
        sh = self._smart_home()
        route = sh._ensure_primary_route()
        for forged in _FORGED_CTX_VALUES:
            with self.subTest(forged=forged):
                with self.assertRaises(UserError):
                    route.with_context(
                        **{_SM_ROUTE_IDENTITY_CTX_KEY: forged}
                    ).write({'access_app_id': self.victim_app})
        self.assertFalse(
            self._db(route.id, 'access_app_id'),
            '任何偽造值都不得讓錨點落地')

    def test_D4_forged_context_values_do_not_open_the_create_gate(self):
        sh = self._smart_home()
        for forged in _FORGED_CTX_VALUES:
            with self.subTest(forged=forged):
                route = self.Route.with_context(
                    **{_SM_ROUTE_IDENTITY_CTX_KEY: forged}
                ).create({
                    'smart_home_id': sh.id,
                    'is_primary': False,
                    'subdomain_prefix': 'forged-%s' % uuid.uuid4().hex[:6],
                    'access_app_id': self.victim_app,
                })
                self.assertFalse(
                    self._db(route.id, 'access_app_id'),
                    'context 值 %r 不得打開錨點閘' % (forged,))


class TestShRouteAnchorHatch(_ShRouteAnchor768Fixture):
    """E：陽性對照——伺服器端合法路徑仍寫得進錨點。"""

    def test_E1_sentinel_helper_puts_the_object_itself_in_context(self):
        """判準是**物件身分**（`is`），不是值相等。

        `assertIs` 而不是 `assertEqual`：一個回 `True` 或回一個等值副本的 helper
        會讓 `assertEqual` 通過，而那正是 JSON 偽造得出來的東西。
        """
        sh = self._smart_home()
        route = sh._ensure_primary_route()
        opened = _internal_sm_route_identity(route)
        self.assertIs(
            opened.env.context.get(_SM_ROUTE_IDENTITY_CTX_KEY),
            _INTERNAL_SM_ROUTE_IDENTITY_WRITE,
            'helper 必須把 sentinel **物件本身**放進 context')

    def test_E2_sentinel_still_writes_anchors(self):
        """少了這一條，B／C／D 的綠燈與「錨點乾脆被永遠鎖死」分不出來——那會把
        sync 回存與 teardown 這兩條正當路徑一起擋掉。"""
        sh = self._smart_home()
        route = sh._ensure_primary_route()
        for field in _SM_ROUTE_IDENTITY_ANCHOR_FIELDS:
            field_type = route._fields[field].type
            if field_type == 'boolean':
                value = True
            elif field_type == 'datetime':
                value = fields.Datetime.from_string('2026-01-02 03:04:05')
            else:
                value = 'server-chosen-%s' % field
            _internal_sm_route_identity(route).write(
                {field: value})
            self.assertEqual(
                self._db(route.id, field), value,
                '開閘的伺服器端寫入必須寫得進 %s' % field)

    def test_E3_sudo_alone_does_not_open_the_gate(self):
        """鑑別項：E2 跑在 su env 裡，所以必須證明 su／sudo **本身**打不開閘。

        沒有這一條，E2 的綠燈就與「守衛對 sudo 無效」不可區分。
        """
        sh = self._smart_home()
        route = sh._ensure_primary_route()
        with self.assertRaises(UserError):
            route.sudo().write({'access_app_id': self.victim_app})
        self.assertFalse(self._db(route.id, 'access_app_id'))

    def test_E4_smart_home_fixture_gate_does_not_open_the_route_gate(self):
        """🔴 兩把 sentinel 分開的全部理由。

        `tests/smart_home_fixture.py::sh_model()` 回的 handle 同時開了 SH 的建立
        閘與 SH 的身分錨點閘，而 SH 的既有 fixture 幾乎全部走它。若 route 沿用同
        一把 sentinel，那些 fixture 會**連帶把本表的守衛一起關掉**——症狀是「守衛
        看起來有測試、實際上從未被執行」。

        這一格直接拿「SH 兩把閘都開著」的 handle 去建 route 並注入錨點：必須擋。
        """
        sh_handle = sh_model(self.env)
        sh = sh_handle.create({
            'name': 'sh-gate-cross-768',
            'workspace_id': self.workspace.id,
        })
        sh.sudo().write({'subdomain': 'paas-sm-768-%s' % uuid.uuid4().hex[:8]})
        # 注意：這裡刻意**不**做 `.with_env(self.env)`——保留 SH 的開閘 context，
        # 那正是本測試要證明「開不到 route」的那把。
        route = sh._ensure_primary_route()
        self.assertFalse(
            self._db(route.id, 'access_app_id'),
            'SH 的閘不得順便打開 route 的閘（否則每個沿用 sh_model() 的 '
            'fixture 都會靜默停用本守衛）')
        with self.assertRaises(UserError):
            route.with_env(sh.env).write({'access_app_id': self.victim_app})


@tagged('post_install', '-at_install')
class TestShRouteAnchorOverTenantRpc(HttpCase):
    """F：租戶真 RPC —— 外層 ACL 把「直接建 route」整條擋掉。

    這一組釘住的是**可達性事實**，不是 model 守衛：`smart_home_route` 對
    `base.group_user` 的 ACL 是 `1,1,0,0`（`create=0` / `unlink=0`），所以 SA 那種
    「租戶自己 RPC create 一條 route 並夾帶錨點」的攻擊在 SH 上打不進來。

    ⚠ **這不代表 model 層守衛可以省**：真正可達的是「租戶 context 流進平台碼」那條
    （`action_provision` → `_ensure_primary_route()` → `.sudo().create()`，sudo 不洗
    context），由 B1 負責。兩件事分開測，才不會有人看到這一組是綠的就以為 model
    守衛沒有必要。
    """

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.login_name = 'tenant768_%s' % suffix
        self.tenant = self.env['res.users'].sudo().create({
            'name': 'tenant 768', 'login': self.login_name,
            'password': self.login_name,
            'email': '%s@test.example' % self.login_name,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.assertFalse(
            self.tenant.has_group('base.group_system'),
            '前提：攻擊者只能有 base.group_user')
        self.workspace = self.env[
            'woow_paas_platform.workspace'].with_user(self.tenant).create(
                {'name': 'W1 anchor 768'})
        self.sh = sh_model(self.env).create({
            'name': 'sh-rpc-768', 'workspace_id': self.workspace.id,
        }).with_env(self.env)
        self.sh.sudo().write(
            {'subdomain': 'paas-sm-768-%s' % uuid.uuid4().hex[:8]})
        self.route = self.sh._ensure_primary_route()

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

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def test_F1_tenant_cannot_create_a_route_over_rpc_at_all(self):
        """ACL `create=0` ⇒ SA 那條攻擊路徑在 SH 上不存在。"""
        self.authenticate(self.login_name, self.login_name)
        result = self._call_kw(_ROUTE, 'create', [{
            'smart_home_id': self.sh.id,
            'subdomain_prefix': 'rpc-768-%s' % uuid.uuid4().hex[:6],
        }], {'context': {'default_access_app_id': 'stolen-app'}})
        self.assertIn('error', result,
                      '租戶不得經 RPC 自建 route（回 %r）' % result)
        self.assertIn('AccessError', self._error_text(result),
                      '歸因：必須是 ACL 的 AccessError（回 %r）'
                      % self._error_text(result))

    def test_F2_tenant_cannot_write_the_anchor_over_rpc(self):
        """租戶對 route **有** write（要改自己的 Access 設定），所以 write 側的
        model 守衛在 RPC 上是真的會被打到的那一層。

        陽性對照在同一個方法裡：合法欄位改得動，證明這筆記錄對他本來就可寫——
        沒有它，「被擋了」可能只是因為他根本看不到這一列。
        """
        self.authenticate(self.login_name, self.login_name)
        ok = self._call_kw(
            _ROUTE, 'write', [[self.route.id], {'session_duration': '8h'}])
        self.assertNotIn('error', ok,
                         '陽性對照：租戶本來就改得動自己 route 的 Access 設定'
                         '（回 %r）' % self._error_text(ok))

        denied = self._call_kw(
            _ROUTE, 'write',
            [[self.route.id], {'access_app_id': 'stolen-app'}])
        self.assertIn('error', denied,
                      '租戶不得改寫身分錨點（回 %r）' % denied)
        text = self._error_text(denied)
        self.assertIn('UserError', text,
                      '歸因：必須是身分錨點守衛的 UserError，不是 ir.rule 的 '
                      'AccessError（回 %r）' % text)
        self.assertIn('access_app_id', text,
                      '訊息要指名是哪個欄位（回 %r）' % text)
        self.env.invalidate_all()
        self.assertFalse(self.route.access_app_id, '值不得落地')

    def test_F3_forged_context_does_not_open_the_hatch_over_rpc(self):
        """`call_kw` 把呼叫端 context 整包換進 env——這是偽造 sentinel 唯一的
        真實攻擊面，必須經真 RPC 打一次。"""
        self.authenticate(self.login_name, self.login_name)
        for forged in _FORGED_CTX_VALUES:
            with self.subTest(forged=forged):
                result = self._call_kw(
                    _ROUTE, 'write',
                    [[self.route.id], {'access_app_id': 'stolen-app'}],
                    {'context': {_SM_ROUTE_IDENTITY_CTX_KEY: forged}})
                self.assertIn(
                    'error', result,
                    '偽造值 %r 經 RPC 打開了逃生口' % (forged,))
        self.env.invalidate_all()
        self.assertFalse(self.route.access_app_id)

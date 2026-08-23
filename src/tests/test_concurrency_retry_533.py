# -*- coding: utf-8 -*-
"""#533：controller 吞掉並發類 DB 例外 ⇒ Odoo 的 `retrying()` 永遠看不到。

`psycopg2.errors.SerializationFailure`（SQLSTATE 40001）的 MRO 是
`SerializationFailure → TransactionRollbackError → OperationalError →
DatabaseError → Error → Exception`——**它是 `Exception` 子類**。而 `retrying()`
（`odoo/service/model.py`）的攔截點在 controller 的 except 鏈**外層**：controller
一旦用裸 `except Exception` 收掉並正常 return，重試機制就整個失效。本來會被
自動重試而成功的操作，變成使用者看到的失敗。

`retrying()` 收到 `OperationalError` 後做的是 **`env.cr.rollback()`——
connection-level 的整條 ROLLBACK**（`Cursor.rollback()` → `self._cnx.rollback()`），
不是 `ROLLBACK TO SAVEPOINT`。整個 transaction 結束，下一輪重跑才依 REPEATABLE
READ 開新 transaction 拿到**全新 snapshot**。所以「新 snapshot」是 retrying 給的，
controller 只要把例外放出去就夠——#525 A1 加的 `cr.savepoint()` 管的是**殘留**
那一半，兩者不互相涵蓋，也不互相妨礙（本檔不動 savepoint）。

## 本檔涵蓋三站

| 端點 | 出處 |
| --- | --- |
| `api_service_skip_trial` | #533 issue 本體 |
| `api_service_reset_admin_password` | **枚舉補到、issue 未點名** |
| `api_service_skip_trial_estimate` | **枚舉補到、issue 未點名** |

第二站特別值得記：model 層 `CloudService._reset_admin_password` 內**有兩處**
`except OperationalError: raise`，註解逐字寫著「#518：並發／連線類 DB 例外
（SerializationFailure 40001 等）必須傳出去讓 Odoo 的 `retrying()` 看見」——
**而 controller 的裸 `except Exception` 正在把它吞回去**。那兩處 raise 今天等於
沒有作用，傳播鏈斷在 controller 這一層。`test_model_layer_reraise_is_not_
cancelled_by_the_controller` 守的就是這條鏈。

三站**各自**有自己的注入斷言，不共用一格綠燈：任一站被改回去，只有那一站會紅。

## 為什麼一律走真 HTTP、不直呼 controller 方法

本檔每一條斷言的結論都取決於「例外**逸出被測碼之後**會發生什麼」（框架看不看得到、
會不會重試）——#572 已實測記載：例外逸出時，直呼與真 HTTP 的交易語意**會分歧**，
直呼寫的斷言在這種形狀下系統性失去鑑別力。所以 fixture 建好之後一律用
`self.url_open()` 打 JSON-RPC 端點。

## 注入手法：pgcode 不能是 None

直接 `SerializationFailure(...)` 建出來的例外，`pgcode` 是 `None`（且唯讀）。
`retrying()` 在重試分支會呼叫 `errorcodes.lookup(exc.pgcode)` ⇒ **合成例外會讓它
在第一輪就 `KeyError`，根本驅動不到真正的重試路徑**。
`test_concurrency_error_propagation_518.py` 的檔頭記著這個坑，也是它刻意只斷言
「回應沒有 result 鍵」、不斷言重試次數的理由。

本檔改用 Odoo 自己的手法補上 pgcode（`odoo/addons/test_http/controllers.py::
upload_file_retry` 就是這樣做的）：

    exc = SerializationFailure(...)
    exc.__setstate__({'pgcode': SERIALIZATION_FAILURE})

`test_the_injected_exception_carries_a_real_pgcode` 把這個前提釘成斷言——它不綠，
底下所有「有沒有重試」的斷言就都是在測一條走不到的路徑。

## 鑑別力

| | 修前 | 修後 |
| --- | --- | --- |
| 永遠衝突 | HTTP 200 + JSON-RPC `result`（`success: False`） | JSON-RPC `error`（例外傳到框架） |
| 只衝突一次 | `success: False`，被測方法只被呼叫 **1** 次 | `success: True`，被測方法被呼叫 **2** 次 |

第二列是本檔比 #518 那條守線強的地方：它直接證明「本來可以自動成功的操作」真的
被救回來了，而不只是「例外有沒有傳出去」。

⚠ **執行時間**：`retrying()` 每次重試前 `time.sleep(random.uniform(0, 2**tryno))`。
「永遠衝突」那幾條在**修好之後**會跑滿 5 次 ⇒ 單條最壞約 30 秒。這是框架行為，
不是測試寫壞了。

## 不在本檔涵蓋範圍

- **真並發下的 40001**：本檔注入的是 Python 層 `raise`，PG 那側的交易**沒有**進入
  aborted 狀態。因此「交易 aborted 之後 `ROLLBACK TO SAVEPOINT` 再
  `RELEASE SAVEPOINT` 會不會二次爆炸」這格**本檔量不到**——測試綠不代表那格成立，
  它根本沒被走到。要坐實需要兩條連線對同一列真的撞出 40001（#518 的先例是用 PR 內
  的外部真並發腳本補的）。
- **`retrying()` 重播整個 handler 造成的外部副作用**：skip-trial 鏈上
  （`subscription_lifecycle_mixin` → `account_move._pay_from_wallet`）全部是 DB
  操作；本 repo 對 `account.move` 沒有 `action_post`／`_post` override，鏈上兩處
  `send_mail` 都在別的函式且 `force_send=False`（進 mail queue，隨交易回捲）。
  但**相依 addon（ecpay 系列）是否掛在 `action_post` 上送外部電子發票 API，本檔
  沒有查證**——那個 repo 屬另一個 agent 的職責範圍。
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import json

from psycopg2.errorcodes import SERIALIZATION_FAILURE
from psycopg2.errors import SerializationFailure

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import HttpCase, tagged

from ..models.cloud_service import _internal_provision

MOD = 'woow_paas_platform'
_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.'
    'get_paas_operator_client'
)
# 🔴 `HttpCase.url_open` 的預設 `timeout=12` **對本檔不夠**，而且只在**修好之後**
# 才不夠——這一格是實測撞出來的，不是預先想到的：
#
# 修好之前，例外被 controller 吞掉、端點立刻回應，12 秒綽綽有餘（RED 那輪 18 條
# 零 error）。修好之後，「永遠衝突」那幾條會讓框架真的跑滿
# `MAX_TRIES_ON_CONCURRENCY_FAILURE = 5` 次，而每次重試前
# `time.sleep(random.uniform(0.0, 2 ** tryno))` ⇒ 四次等待的期望值是
# 1 + 2 + 4 + 8 = 15 秒、最壞 2 + 4 + 8 + 16 = 30 秒，**必然**超過 12 秒。
#
# 症狀是 `requests.exceptions.ReadTimeout` ⇒ 測試報 **error 而不是 fail**，
# 而「error」看起來像測試寫壞了、不像「被測行為正確但比較慢」。GREEN 第一輪
# 5 條 error 全是這個。
#
# ⚠ 不要改成「縮短重試次數」或 patch 掉 `time.sleep` 來讓它變快——那會把
# 「框架真的重試了 5 次」這個被測行為本身改掉。等它跑完才是對的。
_RETRY_SAFE_TIMEOUT = 90

_SKIP_TRIAL_GENERIC = 'An error occurred while skipping the trial period.'
_ESTIMATE_GENERIC = 'An error occurred while estimating the skip-trial charge.'
_OLD_PASSWORD = 'old-password-533'


def _serialization_failure():
    """建一個 **pgcode 正確** 的 40001，否則驅動不到 `retrying()` 的重試分支。

    見檔頭「注入手法」段。手法取自 Odoo 自己的
    `odoo/addons/test_http/controllers.py::upload_file_retry`。
    """
    exc = SerializationFailure(
        'could not serialize access due to concurrent update')
    exc.__setstate__({'pgcode': SERIALIZATION_FAILURE})
    return exc


class _ConcurrencyRetryProbeMixin:
    """三站共用的注入 helper 與 JSON-RPC 呼叫（**斷言不共用**）。"""

    def _post(self, url, params=None, timeout=_RETRY_SAFE_TIMEOUT):
        """打 JSON-RPC 端點，回傳**整包** parsed body。

        刻意不在這裡斷言 `result` 存在——「有沒有 result 鍵」正是本檔的判別式。

        ⚠ `timeout` 一定要蓋過框架重試的總時間，見 `_RETRY_SAFE_TIMEOUT` 的說明。
        """
        resp = self.url_open(
            url,
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'id': 533,
                'params': params or {},
            }),
            headers={'Content-Type': 'application/json'},
            timeout=timeout)
        self.env.invalidate_all()
        return resp.json()

    def _patch_always_failing(self, klass, name):
        """讓目標方法**每次**都拋 40001。回傳記錄呼叫次數的 list。

        list 是 Python 物件，不受 DB 回捲影響——這正是它在框架重播 handler
        之後仍讀得到「被呼叫了幾次」的原因。
        """
        calls = []

        def exploding(_self, *args, **kwargs):
            calls.append(1)
            raise _serialization_failure()

        patcher = patch.object(klass, name, exploding)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _patch_failing_once(self, klass, name):
        """第一次拋 40001、之後照原樣執行——模擬「重試一次就會成功」的衝突。"""
        original = getattr(klass, name)
        calls = []

        def flaky(_self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise _serialization_failure()
            return original(_self, *args, **kwargs)

        patcher = patch.object(klass, name, flaky)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    # ── 探針自驗：這條不綠，底下所有「有沒有重試」的斷言都在測一條走不到的路徑 ──
    #
    # 🔴 **刻意不在 mixin 裡直接寫成 `def test_*`**：Odoo 的 test loader
    # （`odoo/tests/loader.py::get_module_test_cases`）用的是
    # `test_case_class.__dict__.items()`——**只看 class 自己的 `__dict__`，不含
    # 繼承來的方法**，除非該 class 設了 `allow_inherited_tests_method = True`。
    # 寫在 mixin 裡的 `test_*` 三個子類**一條都不會被執行**，而輸出與「跑過且
    # 全綠」完全不可區分。所以這裡只放 helper，三個 class 各自宣告自己的那一條。
    def _assert_injected_exception_has_pgcode(self):
        """`__setstate__` 真的把 pgcode 設進去了嗎？

        直接建構的 `SerializationFailure` pgcode 是 `None`，而 `retrying()` 的
        重試分支會 `errorcodes.lookup(exc.pgcode)` ⇒ pgcode 是 None 就 `KeyError`，
        **重試路徑根本不會被走到**，而測試輸出與「重試成功」不可區分。
        """
        exc = _serialization_failure()
        self.assertEqual(
            exc.pgcode, SERIALIZATION_FAILURE,
            '注入的例外必須帶真正的 40001 pgcode，否則 retrying() 會在 '
            'errorcodes.lookup() 就炸掉，本檔所有重試相關的斷言都失去意義')


@tagged('post_install', '-at_install')
class TestSkipTrialConcurrencyReachesRetrying(
        _ConcurrencyRetryProbeMixin, HttpCase):
    """第一站：`api_service_skip_trial`（#533 issue 本體）。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id

        # 訂閱發票需要 company.sh_journal_id（sale journal）。bare install 不保證
        # 已收斂 ⇒ fixture 自帶前提，否則「成功路徑走不完」會讓陽性對照失效。
        # 慣例鏡射 test_skip_trial_rollback_525.setUp。
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 533', 'code': 'TS533', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(
            company.sh_journal_id,
            '本檔的陽性對照需要可用的訂閱 journal；沒有它 skip-trial 走不完')

        self.workspace = self.env['%s.workspace' % MOD].create({
            'name': 'WS 533', 'owner_id': self.admin_user.id})

        # 自建 plan／product：不綁 seed 商品的定價與上架狀態（#478/#494）。
        # taxes_id 空集合（#366）讓發票金額 == sh_plan_price。
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 533', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 500.0, 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day'})
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Product 533', 'type': 'service', 'list_price': 500.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])]})

        today = date.today()
        self.sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.partner.id,
            'sh_company_id': company.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 500.0,
            'sh_qty': 1,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
            'sh_trial_end_date': today + timedelta(days=6),
            'sh_start_date': today + timedelta(days=7),
            'sh_date_of_next_payment': today + timedelta(days=7),
            'sh_taxes_ids': [(6, 0, [])],
            'state': 'in_progress'})

        template = self.env['%s.cloud_app_template' % MOD].sudo().create({
            'name': 'App 533', 'slug': 'app-533',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-533',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False})
        self.service = _internal_provision(
            self.env['%s.cloud_service' % MOD].sudo()).create({
                'name': 'Svc 533', 'workspace_id': self.workspace.id,
                'template_id': template.id, 'subscription_id': self.sub.id})

        self.partner.sudo().add_wallet_points(
            2000.0, description='533 test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

        self.Service = type(self.env['%s.cloud_service' % MOD])
        self.url = '/api/cloud-services/services/%s/skip-trial' % self.service.id

    def _sub_in_trial(self):
        self.sub.invalidate_recordset()
        return self.sub.sh_trial

    def test_the_injected_exception_carries_a_real_pgcode(self):
        """探針自驗（見 mixin 的 helper；每個 class 各自宣告，不能靠繼承）。"""
        self._assert_injected_exception_has_pgcode()

    # ==================================================================
    #  陽性對照：不注入時端點會成功——四個探針的鑑別力證明
    # ==================================================================
    def test_successful_skip_trial_without_injection(self):
        """這條不綠，底下「注入後失敗」的斷言就不是在守什麼東西。"""
        parsed = self._post(self.url)

        self.assertIn('result', parsed, '未注入時端點應正常回應（回 %r）' % parsed)
        self.assertTrue(
            parsed['result'].get('success'),
            'fixture 必須走得完整條 skip-trial（回 %r）' % parsed['result'])
        self.assertFalse(
            self._sub_in_trial(),
            '成功路徑必須讓訂閱脫離試用——探針讀得到副作用的證明')

    # ==================================================================
    #  前提釘死：注入點確實在端點的執行路徑上
    # ==================================================================
    def test_injection_fires_inside_the_endpoint(self):
        """若 `_skip_trial` 哪天不再被這支端點呼叫，注入就完全沒發生。

        那時「例外沒被吞掉」會退化成恆真（根本沒有例外），而輸出與真綠一模一樣。
        """
        calls = self._patch_always_failing(self.Service, '_skip_trial')

        self._post(self.url)

        self.assertGreaterEqual(
            len(calls), 1,
            '注入的方法必須真的被端點呼叫到，否則本 class 其餘斷言都是恆真')

    # ==================================================================
    #  缺陷探針①：永遠衝突 ⇒ 例外必須傳到框架，不得被轉成業務失敗
    # ==================================================================
    def test_serialization_failure_is_not_swallowed(self):
        """修前：`except Exception` 收掉 → HTTP 200 + JSON-RPC `result`。

        修後：例外逸出 controller → 框架回 JSON-RPC `error`。兩者可區分。
        """
        self._patch_always_failing(self.Service, '_skip_trial')

        parsed = self._post(self.url)

        self.assertNotIn(
            'result', parsed,
            '並發例外被 controller 吞掉並轉成一般業務失敗 ⇒ retrying() 沒有機會'
            '以新 snapshot 重試，本來會自動成功的操作變成使用者看到的失敗（#533）。'
            '回應：%r' % parsed)

    # ==================================================================
    #  缺陷探針②：只衝突一次 ⇒ 重試應該把操作救回來
    # ==================================================================
    def test_a_transient_conflict_is_retried_and_the_operation_succeeds(self):
        """本檔比 #518 那條守線強的地方：直接證明操作被救回來了。

        修前：第一次拋 40001 就被吞掉 → `success: False`，`_skip_trial` 只被呼叫
        **一次**（沒有第二次機會）。
        修後：例外傳到 `retrying()` → 整交易 rollback → 重播 handler → 第二次成功
        → `success: True`，`_skip_trial` 被呼叫 **兩次**。
        """
        calls = self._patch_failing_once(self.Service, '_skip_trial')

        parsed = self._post(self.url)

        self.assertEqual(
            parsed.get('result', {}).get('success'), True,
            '一次性的並發衝突應該被 retrying() 以新 snapshot 重試並成功——'
            '這正是 #533 說的「本來可以自動重試成功的操作變成使用者看到失敗」。'
            '_skip_trial 被呼叫 %d 次，回應：%r' % (len(calls), parsed))

    def test_a_transient_conflict_triggers_a_second_attempt(self):
        """把上一條的機制面單獨釘住：真的有第二次嘗試，不是第一次就矇對。"""
        calls = self._patch_failing_once(self.Service, '_skip_trial')

        self._post(self.url)

        self.assertEqual(
            len(calls), 2,
            '一次性衝突下 _skip_trial 應被呼叫兩次（第一次拋 40001、第二次成功）；'
            '只被呼叫 %d 次代表 retrying() 從來沒有拿到那個例外' % len(calls))

    # ==================================================================
    #  不誤傷：新的 except 分支不得把業務例外或 AccessError 一起攔走
    # ==================================================================
    def test_business_error_still_returns_the_original_message(self):
        """`UserError` 的訊息本來就是寫給租戶看的，必須維持原文回前端（#520）。"""
        self._patch_always_failing_with(
            self.Service, '_skip_trial', UserError('Subscription is not in trial period.'))

        parsed = self._post(self.url)

        self.assertEqual(
            parsed.get('result', {}).get('error'),
            'Subscription is not in trial period.',
            '業務例外必須原文回前端，不得被新增的 except OperationalError 分支'
            '或泛用訊息蓋掉（回 %r）' % parsed)

    def test_access_error_still_returns_the_generic_message(self):
        """`AccessError` 帶內部技術名 ⇒ 維持泛用訊息（#520／#553／#567）。"""
        self._patch_always_failing_with(
            self.Service, '_skip_trial',
            AccessError('您無權存取 woow_paas_platform.cloud_service 的 helm_values 欄位'))

        parsed = self._post(self.url)

        self.assertEqual(
            parsed.get('result', {}).get('error'), _SKIP_TRIAL_GENERIC,
            'AccessError 必須維持泛用訊息，不得因為新增 except 分支而外洩內部'
            'model／欄位技術名（回 %r）' % parsed)

    def _patch_always_failing_with(self, klass, name, exception):
        """讓目標方法每次都拋**指定型別**的例外（給不誤傷那兩條用）。"""
        def exploding(_self, *args, **kwargs):
            raise exception

        patcher = patch.object(klass, name, exploding)
        patcher.start()
        self.addCleanup(patcher.stop)


@tagged('post_install', '-at_install')
class TestResetAdminPasswordConcurrencyReachesRetrying(
        _ConcurrencyRetryProbeMixin, HttpCase):
    """第二站：`api_service_reset_admin_password`（枚舉補到、issue 未點名）。

    這一站的特殊之處：**model 層已經做對了，被 controller 抵消。**
    `CloudService._reset_admin_password` 內兩處 `except OperationalError: raise`
    刻意把並發類例外外拋（註解引用 #518），而 controller 的裸 `except Exception`
    又把它吞回去 ⇒ 那兩處 raise 今天等於沒有作用。
    """

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')

        self.workspace = self.env['%s.workspace' % MOD].create({
            'name': 'WS 533 reset', 'owner_id': self.admin_user.id})
        self.template = self.env['%s.cloud_app_template' % MOD].sudo().create({
            'name': 'Reset App 533', 'slug': 'reset-app-533',
            'category': 'mcp',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/reset-533',
            'helm_chart_version': '0.1.5',
            'mcp_enabled': False,
            # admin_password 供給鏈的閘門（#357 C01）：controller 的 400 閘也讀它
            'has_admin_gui': True})
        self.service = _internal_provision(
            self.env['%s.cloud_service' % MOD].sudo().with_context(
                mcp_admin_password=_OLD_PASSWORD,
            )).create({
                'name': 'Reset Svc 533',
                'workspace_id': self.workspace.id,
                'template_id': self.template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-test-533',
                'helm_release_name': 'reset-533',
                'helm_values': json.dumps({'config': {'nonSensitive': {}}})})

        self.Service = type(self.env['%s.cloud_service' % MOD])
        self.url = (
            '/api/workspaces/%s/services/%s/reset-admin-password'
            % (self.workspace.id, self.service.id))

    def _ok_client(self):
        """operator 回 2xx ⇒ 執行會走過「提交點」，進到 model 的兩段記帳。"""
        client = MagicMock()
        client.reset_release_config.return_value = {
            'helm_revision': 8, 'rollout_awaited': False,
        }
        return client

    def test_the_injected_exception_carries_a_real_pgcode(self):
        """探針自驗（見 mixin 的 helper；每個 class 各自宣告，不能靠繼承）。"""
        self._assert_injected_exception_has_pgcode()

    # ==================================================================
    #  陽性對照
    # ==================================================================
    def test_successful_reset_without_injection(self):
        """不注入時端點會成功——證明 fixture 走得到提交點之後。"""
        with patch(_OPERATOR_CLIENT, return_value=self._ok_client()):
            parsed = self._post(self.url)

        self.assertIn('result', parsed, '未注入時端點應正常回應（回 %r）' % parsed)
        self.assertTrue(
            parsed['result'].get('success'),
            'fixture 必須走得完整條 reset（回 %r）' % parsed['result'])

    # ==================================================================
    #  前提釘死：注入落在「提交點之後」的那段記帳裡
    # ==================================================================
    def test_injection_fires_after_the_operator_committed(self):
        """注入點是 `_set_admin_password_secret`——它在 operator 回 2xx **之後**。

        若哪天憑證落庫被搬到 operator 呼叫之前，這條會紅並要求重新選注入點；
        沒有它，下面兩條會在「根本沒走到提交點」的情況下退化成恆真。
        """
        calls = self._patch_always_failing(
            self.Service, '_set_admin_password_secret')
        client = self._ok_client()

        with patch(_OPERATOR_CLIENT, return_value=client):
            self._post(self.url)

        self.assertGreaterEqual(
            len(calls), 1,
            '注入必須落在 operator 已回 2xx 之後的記帳段，否則本 class 其餘斷言恆真')
        self.assertTrue(
            client.reset_release_config.called,
            'operator 必須真的被呼叫過——注入點在它之後才有意義')

    # ==================================================================
    #  缺陷探針：model 層的 raise 不得被 controller 抵消
    # ==================================================================
    def test_model_layer_reraise_is_not_cancelled_by_the_controller(self):
        """`_reset_admin_password` 的 `except OperationalError: raise` 是刻意的。

        它的註解逐字寫著「必須傳出去讓 Odoo 的 `retrying()` 看見」。修前：
        controller 的裸 `except Exception` 把它吞回去 → HTTP 200 + JSON-RPC
        `result` ⇒ **那個 raise 今天等於沒有作用**。修後：例外傳到框架。
        """
        self._patch_always_failing(
            self.Service, '_set_admin_password_secret')

        with patch(_OPERATOR_CLIENT, return_value=self._ok_client()):
            parsed = self._post(self.url)

        self.assertNotIn(
            'result', parsed,
            'model 層特意外拋的並發例外被 controller 吞回去 ⇒ retrying() 看不到，'
            '那兩處 except OperationalError: raise 形同虛設。回應：%r' % parsed)

    def test_a_transient_conflict_is_retried_and_the_reset_succeeds(self):
        """一次性衝突：修前回失敗、修後被重試救回。"""
        calls = self._patch_failing_once(
            self.Service, '_set_admin_password_secret')

        with patch(_OPERATOR_CLIENT, return_value=self._ok_client()):
            parsed = self._post(self.url)

        self.assertEqual(
            parsed.get('result', {}).get('success'), True,
            '一次性的並發衝突應被 retrying() 重試並成功；_set_admin_password_secret '
            '被呼叫 %d 次，回應：%r' % (len(calls), parsed))


@tagged('post_install', '-at_install')
class TestSkipTrialEstimateConcurrencyReachesRetrying(
        _ConcurrencyRetryProbeMixin, HttpCase):
    """第三站：`api_service_skip_trial_estimate`（枚舉補到、issue 未點名）。

    這支 side-effect-free（`_skip_trial_estimate` 只讀不寫），所以**沒有殘留面**
    ——它的 except 鏈也因此刻意沒有 savepoint。但「並發類例外被裸 `except
    Exception` 吞掉 ⇒ retrying() 看不到」這個結構缺口與另外兩站完全相同：讀取
    路徑一樣會撞到 40001（REPEATABLE READ 下的讀寫衝突），一樣本可重試成功。
    """

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id

        company = self.env.company.sudo()
        self.workspace = self.env['%s.workspace' % MOD].create({
            'name': 'WS 533 estimate', 'owner_id': self.admin_user.id})
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 533 est', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 500.0, 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day'})
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Product 533 est', 'type': 'service', 'list_price': 500.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])]})

        today = date.today()
        self.sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.partner.id,
            'sh_company_id': company.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 500.0,
            'sh_qty': 1,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
            'sh_trial_end_date': today + timedelta(days=6),
            'sh_start_date': today + timedelta(days=7),
            'sh_date_of_next_payment': today + timedelta(days=7),
            'sh_taxes_ids': [(6, 0, [])],
            'state': 'in_progress'})

        template = self.env['%s.cloud_app_template' % MOD].sudo().create({
            'name': 'App 533 est', 'slug': 'app-533-est',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-533-est',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False})
        self.service = _internal_provision(
            self.env['%s.cloud_service' % MOD].sudo()).create({
                'name': 'Svc 533 est', 'workspace_id': self.workspace.id,
                'template_id': template.id, 'subscription_id': self.sub.id})

        self.partner.sudo().add_wallet_points(
            2000.0, description='533 estimate test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

        self.Service = type(self.env['%s.cloud_service' % MOD])
        self.url = (
            '/api/cloud-services/services/%s/skip-trial-estimate'
            % self.service.id)

    def test_the_injected_exception_carries_a_real_pgcode(self):
        """探針自驗（見 mixin 的 helper；每個 class 各自宣告，不能靠繼承）。"""
        self._assert_injected_exception_has_pgcode()

    def test_successful_estimate_without_injection(self):
        """陽性對照：不注入時端點會成功。"""
        parsed = self._post(self.url)

        self.assertIn('result', parsed, '未注入時端點應正常回應（回 %r）' % parsed)
        self.assertTrue(
            parsed['result'].get('success'),
            'fixture 必須走得完估算（回 %r）' % parsed['result'])

    def test_injection_fires_inside_the_endpoint(self):
        """前提釘死：`_skip_trial_estimate` 確實在這支端點的執行路徑上。"""
        calls = self._patch_always_failing(self.Service, '_skip_trial_estimate')

        self._post(self.url)

        self.assertGreaterEqual(
            len(calls), 1,
            '注入的方法必須真的被端點呼叫到，否則本 class 其餘斷言都是恆真')

    def test_serialization_failure_is_not_swallowed(self):
        """side-effect-free 不代表可以吞——重試機制對讀取路徑一樣有價值。"""
        self._patch_always_failing(self.Service, '_skip_trial_estimate')

        parsed = self._post(self.url)

        self.assertNotIn(
            'result', parsed,
            '估算端點同樣把並發例外轉成業務失敗 ⇒ retrying() 看不到。它沒有殘留面，'
            '但一次性衝突本來可以重試成功。回應：%r' % parsed)

    def test_a_transient_conflict_is_retried_and_the_estimate_succeeds(self):
        """一次性衝突：修前回失敗、修後被重試救回。"""
        calls = self._patch_failing_once(self.Service, '_skip_trial_estimate')

        parsed = self._post(self.url)

        self.assertEqual(
            parsed.get('result', {}).get('success'), True,
            '一次性的並發衝突應被 retrying() 重試並成功；_skip_trial_estimate '
            '被呼叫 %d 次，回應：%r' % (len(calls), parsed))

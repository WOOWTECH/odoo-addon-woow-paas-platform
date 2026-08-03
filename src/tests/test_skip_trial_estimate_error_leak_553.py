# -*- coding: utf-8 -*-
"""#553：`api_service_skip_trial_estimate` 回傳例外原文，外洩 Odoo 內部技術名。

端點原本是「一律原文」——連 `AccessError` / `MissingError` 都照吐：

    except Exception as e:
        _logger.error(...)
        return {'success': False, 'error': str(e)}

而 Odoo 這兩種例外的訊息帶的是 **ORM 內部識別字**（model 技術名
`woow_paas_platform.cloud_service`、記錄 id、內部 uid），不是寫給租戶看的業務規則。
修法比照 PR #523（`_create_service`）／PR #527（`api_service_skip_trial`）的例外分類。

本檔的四支測試分成兩種角色，別搞混：

* **陽性對照（修前修後都綠）**：`test_business_rule_message_reaches_the_tenant`。
  它證明「訊息真的走得到 response」這條管道是通的 —— 沒有它，下面三支
  「response 不含技術名」就可能只是因為**任何**訊息都到不了前端而假綠。
  這支刻意不 mock：用真實的前置條件失敗（服務沒有訂閱）產生 `UserError`。
* **缺陷探針（修前必紅）**：其餘三支。每支都同時斷言
  (i) 例外**確實被觸發**（`raised.call_count == 1`，或真實 `MissingError` 已產生）、
  (ii) `error` **等於**泛用字串（正面釘住走哪個分支，而不是只斷言「不含 X」——
  只寫否定式斷言的話，被測程式一改成「例外提前被別處攔掉、根本沒進 except」，
  斷言就退化成恆真，測試照樣綠。#525 A1／PR #527 實測過這個形態）。

`MissingError` 那支另外還帶自己的探針前置驗證：它先用**真的**已刪除記錄逼出一個
真的 `MissingError`，並斷言它的原文確實含 model 技術名。若哪天 Odoo 改了措辭、
訊息不再帶技術名，那條前置斷言會先紅並指出原因，而不是讓外洩測試靜靜地失去鑑別力。
"""
from unittest.mock import patch

from odoo.exceptions import AccessError, MissingError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.subscription_lifecycle_mixin import (
    SubscriptionLifecycleMixin,
)

_GENERIC_ERROR = 'An error occurred while estimating the skip-trial charge.'
_INTERNAL_MODEL = 'woow_paas_platform.cloud_service'


@tagged('post_install', '-at_install')
class TestSkipTrialEstimateErrorLeak553(TransactionCase):
    """skip-trial-estimate 的錯誤訊息不得挾帶 Odoo 內部技術名。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.CloudService = self.env['woow_paas_platform.cloud_service']

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Skip Trial Estimate Leak WS 553',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Estimate Leak App 553',
            'slug': 'estimate-leak-553',
            # helm_* 三欄 required=True（DB NOT NULL），不給會在 setUp 就整組 ERROR。
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/estimate-leak-553',
            'helm_chart_version': '0.0.0',
        })
        # 刻意不掛 subscription_id：陽性對照要的就是「沒有訂閱」這條真實的
        # 前置條件失敗（`_skip_trial_estimate` 第一道 raise UserError）。
        self.service = self.CloudService.create({
            'name': 'Estimate Leak Svc 553',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

    def _patch_paas_request(self):
        """controller 直呼所需的 `request` 替身（沿用 test_mcp_admin_password 的慣例）。"""
        from unittest.mock import MagicMock
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=self.env),
        )

    def _estimate(self):
        with self._patch_paas_request():
            return self.controller.api_service_skip_trial_estimate(
                service_id=self.service.id,
            )

    # ==================================================================
    #  陽性對照：業務規則訊息**必須**原文抵達租戶（修前修後都綠）
    # ==================================================================
    def test_business_rule_message_reaches_the_tenant(self):
        """`UserError` 是講給使用者聽的，原文回前端。

        這支同時是下面三支否定性斷言的探針驗證：它證明 `error` 欄位確實能把
        model 端的訊息原樣帶到 response。若這支紅了，「response 不含技術名」
        就不再是「分類正確」的證據，而可能只是訊息根本傳不出來。
        """
        result = self._estimate()

        self.assertFalse(result.get('success'), '沒有訂閱應失敗（回 %r）' % result)
        self.assertEqual(
            result.get('error'), 'No subscription linked to this service.',
            '業務規則例外的訊息必須原文回傳；統一成泛用字串會讓使用者與第一線'
            '支援都看不到原因（回 %r）' % result,
        )

    # ==================================================================
    #  AccessError：是 UserError 子類，但不得被當成業務規則訊息原文吐出
    # ==================================================================
    def test_access_error_does_not_leak_internal_names(self):
        """只寫 `except UserError` 會連 `AccessError` 一起原文回前端。

        權限錯誤的訊息帶的是 Odoo 內部 model／欄位技術名，不是業務規則 →
        走泛用訊息，細節只留 server log。
        """
        with patch.object(
            SubscriptionLifecycleMixin, '_skip_trial_estimate',
            side_effect=AccessError('您無權存取 %s 記錄（欄位 subscription_id）'
                                    % _INTERNAL_MODEL),
        ) as raised:
            result = self._estimate()

        # 前提：例外確實被觸發（否則下面的「不含技術名」是恆真的假綠）
        self.assertEqual(
            raised.call_count, 1,
            '注入的例外必須真的被觸發過——沒觸發的話「不含技術名」毫無鑑別力')
        self.assertFalse(result.get('success'), result)
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            'AccessError 不得被當成業務規則訊息原文回傳（回 %r）' % result)
        self.assertNotIn(
            _INTERNAL_MODEL, result.get('error') or '',
            'Odoo 內部 model 技術名不得出現在租戶前台')

    # ==================================================================
    #  MissingError：同為 UserError 子類，原文帶 model 名 + 記錄 id + uid
    # ==================================================================
    def test_missing_error_does_not_leak_orm_record_identifiers(self):
        """`MissingError`（記錄被並發刪除）的原文形如：

            Record does not exist or has been deleted.
            (Record: woow_paas_platform.cloud_service(42,), User: 2)

        model 技術名、記錄 id、內部 uid 全在裡面。它同樣是 `UserError` 子類，
        `except (ValidationError, UserError)` 會把它歸到「業務規則、原文回傳」。
        """
        # ── 探針前置驗證：先造一個**真的** MissingError，確認它真的帶技術名。
        #    這條紅掉代表 Odoo 措辭變了 → 下面的外洩斷言已失去鑑別力，要重寫，
        #    而不是讓它靜靜地變成恆真。
        ghost = self.CloudService.browse(999_999_999)
        with self.assertRaises(MissingError) as caught:
            ghost.name
        real_missing = caught.exception
        self.assertIn(
            _INTERNAL_MODEL, str(real_missing),
            'Odoo 的 MissingError 原文應含 model 技術名，這是本測試要擋的外洩面；'
            '若措辭已改（實得 %r），請重新選定探針字串' % str(real_missing))

        with patch.object(
            SubscriptionLifecycleMixin, '_skip_trial_estimate',
            side_effect=real_missing,
        ) as raised:
            result = self._estimate()

        self.assertEqual(raised.call_count, 1, '注入的例外必須真的被觸發過')
        self.assertFalse(result.get('success'), result)
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            'MissingError 屬 ORM 內部狀態，不得原文回傳（回 %r）' % result)
        self.assertNotIn(
            _INTERNAL_MODEL, result.get('error') or '',
            'model 技術名不得出現在租戶前台')

    # ==================================================================
    #  未預期例外：維持泛用訊息，內部細節只留 server log
    # ==================================================================
    def test_unexpected_error_stays_opaque(self):
        secret = 'psycopg2://odoo:s3cr3t@db.internal:5432/paas_prod'
        with patch.object(
            SubscriptionLifecycleMixin, '_skip_trial_estimate',
            side_effect=RuntimeError(secret),
        ) as raised:
            result = self._estimate()

        self.assertEqual(raised.call_count, 1, '注入的例外必須真的被觸發過')
        self.assertFalse(result.get('success'), result)
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            '未預期例外維持泛用訊息（回 %r）' % result)
        self.assertNotIn(secret, result.get('error') or '',
                         '內部細節（連線字串等）不得外洩到前端')

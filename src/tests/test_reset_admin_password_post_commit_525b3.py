# -*- coding: utf-8 -*-
"""#525 B3：reset-admin-password 在**提交點之後**失敗，被回報成「重設失敗」。

## 為什麼這處不是 rollback 問題（先讀這段，別照 #520 修法套）

`_reset_admin_password` 的寫入**刻意**排在 operator 回 2xx 之後（C11′
commit-on-secret）。operator 逾時／錯誤、state 閘、`has_admin_gui` 閘全都在寫入
**之前** `raise UserError` ⇒ 主要失敗路徑**零 DB 殘留**，而且叢集仍持舊密碼。

殘留窗口只剩一條很窄的：例外發生在 `_set_admin_password_secret()` **之後**
（`_is_halted()` / `self.write()` / `_rescale_halted_after_upgrade()`）。此時的狀態是
**DB 與叢集「一致地」換成了新密碼**，只有租戶被告知失敗、因而拿不到那把一次性明文。

⇒ 在這裡補 `cr.rollback()` 是**有害的**：回捲會讓 DB 退回舊密碼、而叢集已是新密碼，
把「一致但租戶不知情」變成「DB 與叢集不一致」，管理員將完全無法登入 admin GUI。

真正的缺陷是**「密碼已生效卻沒交付給使用者」＝不可回復的憑證遺失**（明文只顯示一次，
平台刻意不做回讀端點）。租戶收到「Reset failed」會繼續拿舊密碼重試 —— 而舊密碼此刻
已經失效。修法：把提交點之後的每一步都界定成**記帳**，記帳失敗不得翻轉「密碼已生效」
這個事實。

## 本檔四支測試的角色

* **陽性對照（修前修後都綠）**：`test_operator_failure_still_reports_failure`。
  它證明本端點**確實有能力**回 `success: False` 且**確實有能力**不落庫 ——
  沒有它，下面「必須回 success: True」的斷言可能只是因為端點恆為 True 而假綠，
  「舊密碼被保留」也可能只是因為根本沒有任何寫入路徑。
* **缺陷探針（修前必紅）**：其餘三支，注入點分別落在提交點之後的兩個位置。
  每支都先斷言**前提**：(i) 注入的例外確實被觸發、(ii) 落庫的密碼確實已經不是舊的
  （＝提交點確實被跨過）。少了 (ii)，「回應帶得到密碼」在「例外提早發生、根本沒到
  提交點」的情況下也會成立，斷言就失去指向性。
"""
from unittest.mock import MagicMock, patch

import json

from odoo.exceptions import AccessDenied, AccessError, UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.cloud_service import CloudService
from ..models.cloud_service import _internal_provision

_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.'
    'get_paas_operator_client'
)
_GENERIC_ERROR = 'Reset failed. Check operator logs for details.'
_OLD_PASSWORD = 'old-password-525b3'
_INTERNAL_MODEL = 'woow_paas_platform.cloud_service'


@tagged('post_install', '-at_install')
class TestResetAdminPasswordPostCommit525B3(TransactionCase):
    """提交點之後的失敗不得被回報成「重設失敗」，也不得回捲已生效的密碼。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Reset Post-commit WS 525B3',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Reset Post-commit App 525B3',
            'slug': 'reset-postcommit-525b3',
            'category': 'mcp',
            # helm_* 三欄 required=True（DB NOT NULL）
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/reset-postcommit',
            'helm_chart_version': '0.1.5',
            'mcp_enabled': False,
            # admin_password 供給鏈的閘門（#357 C01）：controller 的 400 閘也讀它
            'has_admin_gui': True,
        })
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password=_OLD_PASSWORD,
        )).create({
            'name': 'Reset Post-commit Svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test-525b3',
            'helm_release_name': 'reset-postcommit-525b3',
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        })
        self.assertEqual(
            self._secret().admin_password, _OLD_PASSWORD,
            'fixture 前提：服務起始就帶一把舊密碼，否則「密碼有沒有被換掉／被回捲」'
            '整組斷言都無從比較')
        # #991（PR #1300）：controller 經 `_PaaSCapability` 要求已認證的 workspace
        # 成員；superuser（`__system__`，active=False）不算，會在 workspace 閘就回
        # 「Workspace not found or access denied」、根本走不到注入點。真實會打
        # reset 的是 workspace admin（ROLE_ADMIN 閘），所以 harness 以一位 internal
        # admin 成員的身分呼叫 controller。只換 `request.env` 的身分：受測 service、
        # 注入點與斷言都不變，production 不放寬。
        self.member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': '525B3 workspace admin',
            'login': '525b3-workspace-admin',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': self.member.id, 'role': 'admin'})

    # ------------------------------------------------------------------
    #  helpers
    # ------------------------------------------------------------------
    def _secret(self):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', self.service.id)], limit=1,
        )

    def _patch_paas_request(self):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=self.env(user=self.member)),
        )

    def _reset(self):
        """走 controller 端點（＝租戶的唯一入口）。"""
        with self._patch_paas_request():
            return self.controller.api_service_reset_admin_password(
                workspace_id=self.workspace.id,
                service_id=self.service.id,
            )

    def _ok_client(self):
        client = MagicMock()
        client.reset_release_config.return_value = {
            'helm_revision': 8, 'rollout_awaited': False,
        }
        return client

    # ==================================================================
    #  陽性對照：提交點**之前**失敗 → 照舊回報失敗、照舊不落庫
    # ==================================================================
    def test_operator_failure_still_reports_failure(self):
        """operator 沒回 2xx ⇒ 叢集仍持舊密碼 ⇒ 「不寫入、回失敗」才是對的。

        這支釘住既有的正確行為（別把它一起「修」掉），同時是下面三支的探針驗證：
        證明端點回得出 `success: False`、也證明 secret 真的可能維持舊值。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        client = self._ok_client()
        client.reset_release_config.side_effect = PaaSOperatorError('boom')

        with patch(_OPERATOR_CLIENT, return_value=client):
            result = self._reset()

        self.assertFalse(
            result.get('success'),
            'operator 失敗＝叢集仍是舊密碼，必須回報失敗（回 %r）' % result)
        self.assertEqual(
            self._secret().admin_password, _OLD_PASSWORD,
            '提交點之前失敗不得落庫——落了就等於平台記了一把叢集沒有的密碼')

    # ==================================================================
    #  缺陷探針①：提交點之後的 AccessError 不得被回報成「重設失敗」
    # ==================================================================
    def test_post_commit_access_error_still_delivers_the_live_password(self):
        """`_is_halted()` 在 secret 落庫**之後**才被呼叫。

        在那裡注入例外＝模擬「新密碼已經是叢集與 DB 的權威值、但後續記帳炸了」。
        修前：`AccessError`（`UserError` 子類）被 controller 的 `except UserError`
        接走 → `success: False` → 租戶永久拿不到那把已生效的密碼。
        """
        client = self._ok_client()
        with patch(_OPERATOR_CLIENT, return_value=client), \
                patch.object(
                    CloudService, '_is_halted',
                    side_effect=AccessError(
                        '您無權存取 %s 記錄' % _INTERNAL_MODEL),
                ) as raised:
            result = self._reset()

        stored = self._secret().admin_password
        # 前提①：注入的例外真的被觸發
        self.assertEqual(raised.call_count, 1, '注入的例外必須真的被觸發過')
        # 前提②：提交點確實被跨過（否則「回應要帶密碼」的斷言沒有指向性）
        self.assertNotEqual(
            stored, _OLD_PASSWORD,
            '前提不成立：例外發生在提交點之前，本測試測不到 B3 的窄窗口')

        self.assertTrue(
            result.get('success'),
            '密碼已生效（operator 回 2xx＋secret 已落庫），記帳失敗不得翻轉成'
            '「重設失敗」——那句話是假的，而且租戶會拿舊密碼繼續重試（回 %r）'
            % result)
        self.assertEqual(
            (result.get('data') or {}).get('admin_password'), stored,
            '回應必須交付**已生效的那把**明文密碼：它只顯示這一次，平台刻意沒有'
            '回讀端點，這裡不交付就是不可回復的憑證遺失（回 %r）' % result)

    # ==================================================================
    #  缺陷探針②：已生效的密碼不得被回捲（＝不得補 cr.rollback()）
    # ==================================================================
    def test_post_commit_failure_must_not_roll_back_the_live_password(self):
        """把本處當成 #520 同構、無差別補 `cr.rollback()` 會製造更糟的狀態。

        叢集此刻已是新密碼；DB 回捲成舊密碼＝兩邊不一致，管理員登不進 admin GUI，
        而且後續 redeploy 會拿 DB 的舊值去覆寫叢集。這支把「不得回捲」釘死。
        """
        client = self._ok_client()
        with patch(_OPERATOR_CLIENT, return_value=client), \
                patch.object(
                    CloudService, '_is_halted',
                    side_effect=RuntimeError('bookkeeping exploded'),
                ) as raised:
            result = self._reset()

        self.assertEqual(raised.call_count, 1, '注入的例外必須真的被觸發過')
        stored = self._secret().admin_password
        self.assertNotEqual(
            stored, _OLD_PASSWORD,
            '已生效的新密碼不得被回捲成舊值——叢集已經是新的，回捲只會製造'
            'DB↔叢集不一致（#525 B3 的核心判斷）')
        self.assertEqual(
            (result.get('data') or {}).get('admin_password'), stored,
            '交付給租戶的明文必須等於落庫（＝叢集）那把，不相等就是 #357 病灶：'
            '租戶拿到一組永不生效的密碼（回 %r）' % result)

    # ==================================================================
    #  缺陷探針③：注入點落在 state 寫入**之後**，同樣不得翻轉成失敗
    # ==================================================================
    def test_post_commit_rescale_failure_still_delivers_the_password(self):
        """`_rescale_halted_after_upgrade` 排在 `self.write({state: upgrading})` 之後。

        它自己已有 catch-all，例外能逸出代表更深層的問題；但無論如何，「密碼已生效」
        這件事不因它失敗而改變。修前：走 controller 的 `except Exception` → 泛用
        「Reset failed」→ 同樣的憑證遺失。
        """
        client = self._ok_client()
        with patch(_OPERATOR_CLIENT, return_value=client), \
                patch.object(
                    CloudService, '_rescale_halted_after_upgrade',
                    side_effect=RuntimeError('rescale exploded'),
                ) as raised:
            result = self._reset()

        self.assertEqual(raised.call_count, 1, '注入的例外必須真的被觸發過')
        stored = self._secret().admin_password
        self.assertNotEqual(
            stored, _OLD_PASSWORD,
            '前提不成立：例外發生在提交點之前，本測試測不到 B3 的窄窗口')
        self.assertTrue(
            result.get('success'),
            '記帳失敗不得翻轉「密碼已生效」的事實（回 %r）' % result)
        self.assertEqual(
            (result.get('data') or {}).get('admin_password'), stored,
            '回應必須交付已生效的那把明文（回 %r）' % result)

    # ==================================================================
    #  缺陷探針④：回應組裝（gateway 憑證）失敗同樣不得吃掉已生效的密碼
    # ==================================================================
    def test_gateway_lookup_failure_still_delivers_the_password(self):
        """`_gateway_credentials()` 原本在 `try` **之外**。

        它一炸就不是「回失敗」而是直接往框架拋 → JSON-RPC 500 → 框架回捲整筆交易
        ⇒ 「叢集是新密碼、DB 是舊密碼、租戶什麼都沒拿到」，比回失敗更糟。
        gateway key 是附加品（取不到可再 Reset），已生效的 admin 密碼不是。
        """
        client = self._ok_client()
        with patch(_OPERATOR_CLIENT, return_value=client), \
                patch.object(
                    PaasController, '_gateway_credentials',
                    side_effect=RuntimeError('gateway lookup exploded'),
                ) as raised:
            result = self._reset()

        self.assertEqual(raised.call_count, 1, '注入的例外必須真的被觸發過')
        stored = self._secret().admin_password
        self.assertNotEqual(
            stored, _OLD_PASSWORD,
            '前提不成立：例外發生在提交點之前，本測試測不到這個窗口')
        self.assertTrue(
            result.get('success'),
            '回應組裝失敗不得翻轉「密碼已生效」的事實（回 %r）' % result)
        self.assertEqual(
            (result.get('data') or {}).get('admin_password'), stored,
            '回應必須交付已生效的那把明文（回 %r）' % result)

    # ==================================================================
    #  例外分類：本端點的 `except UserError` 同樣會原文吐出 AccessError
    # ==================================================================
    def test_pre_commit_access_error_does_not_leak_internal_names(self):
        """與 #553 同一個外洩面：`AccessError` 是 `UserError` 子類。

        注入點選在提交點**之前**（`_assemble_deploy_values`），確保這支測的是
        「失敗回應的訊息內容」，與上面三支的「成功語意」不互相干擾。
        """
        client = self._ok_client()
        with patch(_OPERATOR_CLIENT, return_value=client), \
                patch.object(
                    CloudService, '_assemble_deploy_values',
                    side_effect=AccessError(
                        '您無權存取 %s 的 helm_values 欄位' % _INTERNAL_MODEL),
                ) as raised:
            result = self._reset()

        self.assertEqual(raised.call_count, 1, '注入的例外必須真的被觸發過')
        self.assertFalse(result.get('success'), result)
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            'AccessError 不得被當成業務規則訊息原文回傳（回 %r）' % result)
        self.assertNotIn(
            _INTERNAL_MODEL, result.get('error') or '',
            'Odoo 內部 model 技術名不得出現在租戶前台')
        self.assertEqual(
            self._secret().admin_password, _OLD_PASSWORD,
            '提交點之前失敗不得落庫')

    def test_pre_commit_access_denied_does_not_leak_internal_names(self):
        """`AccessDenied` 是 `UserError` 的**平行**子類，不是 `AccessError` 的子類。

        `except (AccessError, MissingError)` 攔不到它 ⇒ 它會落進
        `except (ValidationError, UserError)` 走原文。這條鎖住第四個分支。
        """
        self.assertFalse(
            issubclass(AccessDenied, AccessError),
            'AccessDenied 若變成 AccessError 子類，本測試就退化成上一條的複本，'
            '請重新評估 except tuple 是否還需要各自列名')
        self.assertTrue(issubclass(AccessDenied, UserError))

        client = self._ok_client()
        with patch(_OPERATOR_CLIENT, return_value=client), \
                patch.object(
                    CloudService, '_assemble_deploy_values',
                    side_effect=AccessDenied(
                        '拒絕存取 %s 的 helm_values（uid=2）' % _INTERNAL_MODEL),
                ) as raised:
            result = self._reset()

        self.assertEqual(raised.call_count, 1, '注入的例外必須真的被觸發過')
        self.assertFalse(result.get('success'), result)
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            'AccessDenied 是認證失敗，不是業務規則訊息（回 %r）' % result)
        self.assertNotIn(
            _INTERNAL_MODEL, result.get('error') or '',
            'Odoo 內部 model 技術名不得出現在租戶前台')
        self.assertEqual(
            self._secret().admin_password, _OLD_PASSWORD,
            '提交點之前失敗不得落庫')

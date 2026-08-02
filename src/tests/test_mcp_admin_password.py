"""Regression tests for #357's one-time MCP admin password flow."""
import json
import logging
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController


class TestMcpAdminPassword(TransactionCase):
    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'MCP password workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'MCP Odoo test',
            'slug': 'mcp-odoo',
            'category': 'mcp',
            # helm_repo_url 是 required（DB NOT NULL）：不給 → setUp 就 NotNullViolation，
            # 整個 class 每支測試 ERROR（本檔 WIP 期間 3/3 全 ERROR＝從未真跑）。
            # OCI chart 慣例是空字串，對齊真實 seed。
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/mcp-server',
            'helm_chart_version': '0.1.5',
            # #357 C01：對齊真實 seed 形態——MCP 內建於 body（mcp_enabled=False），
            # admin_password 供給鏈的閘門是 has_admin_gui。
            'mcp_enabled': False,
            'has_admin_gui': True,
        })
        # controller._create_service 要求 plan_id；上面兩個 template 都沒掛
        # subscription_plan_ids，故 controller 不做歸屬檢查，借現成 seed plan 即可。
        self.plan = self.env.ref('woow_paas_platform.plan_mcp_odoo_monthly')
        self.service = self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='old-password',
        ).create({
            'name': 'MCP Odoo',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'mcp-odoo-test',
            'helm_values': json.dumps({
                'config': {
                    'nonSensitive': {
                        'connection': {'url': 'https://tenant.example'},
                        'tools': {'enabled': ['search']},
                    },
                },
            }),
        })

    def test_service_detail_masks_all_config_sensitive_leaves(self):
        values = {
            'config': {
                'sensitive': {
                    'admin_password': 'must-not-leak',
                    'nested': {'token': 'must-not-leak-either'},
                },
            },
        }
        sanitized = PaasController._sanitize_helm_values(values, self.template)
        self.assertNotIn('must-not-leak', json.dumps(sanitized))
        self.assertNotIn('must-not-leak-either', json.dumps(sanitized))

    def test_service_detail_api_masks_legacy_sensitive_values(self):
        self.service.write({
            'helm_values': json.dumps({
                'config': {
                    'sensitive': {
                        'admin_password': 'must-not-leak',
                    },
                    'nonSensitive': {
                        'connection': {'url': 'https://tenant.example'},
                        'tools': {'enabled': ['search']},
                    },
                },
            }),
        })
        payload = self.controller._format_service(self.service, include_details=True)
        self.assertNotIn('must-not-leak', json.dumps(payload))
        self.assertEqual(
            payload['helm_values']['config']['nonSensitive']['connection'],
            {'url': 'https://tenant.example'},
        )

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_reset_preserves_gui_owned_connection_and_tools(self, get_client):
        client = MagicMock()
        get_client.return_value = client

        new_password = self.service._reset_admin_password()

        self.assertNotEqual(new_password, 'old-password')
        values = json.loads(self.service.helm_values)
        self.assertEqual(
            values['config']['nonSensitive']['connection'],
            {'url': 'https://tenant.example'},
        )
        self.assertEqual(
            values['config']['nonSensitive']['tools'],
            {'enabled': ['search']},
        )
        secret = self.env['woow_paas_platform.cloud_service_secret'].search([
            ('service_id', '=', self.service.id),
        ], limit=1)
        self.assertEqual(secret.admin_password, new_password)
        payload = client.reset_release_config.call_args.kwargs['values']
        # #411：reset admin password 改走 _assemble_deploy_values 全量組裝管線 —— 送出的
        # values 除保留 GUI 擁有的 connection/tools 外，一併重組平台 injector（storage_class
        # / image_pull_secrets），避免 reset_values 語意下這些注入值從 rendered manifest 消失。
        self.assertEqual(
            payload,
            {
                'config': {
                    'nonSensitive': {
                        'connection': {'url': 'https://tenant.example'},
                        'tools': {'enabled': ['search']},
                    },
                },
                'global': {'imagePullSecrets': ['paas-registry']},
                'persistence': {'storageClass': 'longhorn-delete'},
            },
        )
        # #530：供給面多了一把 gateway API key（``auth.apiServerKey``）。這條的
        # exact-equality 是刻意的——它同時鎖住「新 key 有進供給鏈」與「沒有第三個
        # 鍵偷偷長出來」。gateway key 由平台 per-service 生成，故從 secret 讀。
        self.assertEqual(
            client.reset_release_config.call_args.kwargs['sensitive_values'],
            {
                'config': {'sensitive': {'admin_password': new_password}},
                'auth': {'apiServerKey': secret.gateway_api_key},
            },
        )

    # ------------------------------------------------------------------
    # #357 C11：逾時語意 —— 落庫在後 + 冪等重試
    # ------------------------------------------------------------------

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_timeout_keeps_stored_password_and_says_status_unknown(self, get_client):
        """C11：逾時 → 不落庫、不謊稱失敗。

        client 預算（120s）與 operator 的 saga 預算是兩回事，且 operator 的
        handler 是同步的——client 斷線不會取消它。所以逾時＝「狀態不明」，
        不是「沒發生」。不落庫是為了守住不變式：DB 有的密碼＝叢集真正生效的。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorTimeoutError,
        )
        client = MagicMock()
        client.reset_release_config.side_effect = PaaSOperatorTimeoutError(
            message='Request to PaaS Operator timed out',
        )
        get_client.return_value = client
        before = self._secret_of(self.service).admin_password

        with self.assertRaises(UserError) as caught:
            self.service._reset_admin_password()

        # 「失敗」會誤導租戶關掉 modal → 叢集可能已輪換成沒人握有的密碼。
        self.assertIn('status unknown', str(caught.exception).lower())
        self.assertNotIn('reset failed', str(caught.exception).lower())
        self.assertEqual(
            self._secret_of(self.service).admin_password, before,
            'C11：逾時不得落庫——否則 DB 存的密碼可能從未在叢集生效',
        )

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_reset_is_retryable_after_timeout(self, get_client):
        """C11：逾時後 state 仍是 running → 重按當下就成立（冪等收斂）。

        逾時的 raise 發生在 write state 之前，所以 record 沒有中間狀態卡住。
        重按會生成新密碼再跑一遍 saga，叢集與 DB 最終收斂到最新那次。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorTimeoutError,
        )
        client = MagicMock()
        client.reset_release_config.side_effect = PaaSOperatorTimeoutError(
            message='Request to PaaS Operator timed out',
        )
        get_client.return_value = client
        with self.assertRaises(UserError):
            self.service._reset_admin_password()

        self.assertEqual(
            self.service.state, 'running',
            'C11：逾時不得留下中間狀態，否則 :394 的 guard 會擋掉重試',
        )

        client.reset_release_config.side_effect = None
        retried = self.service._reset_admin_password()

        self.assertNotEqual(retried, 'old-password')
        self.assertEqual(self._secret_of(self.service).admin_password, retried)
        self.assertEqual(client.reset_release_config.call_count, 2)

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_non_timeout_failure_still_reads_as_failed(self, get_client):
        """C11 的另一半：真失敗仍要說失敗，否則「狀態不明」就沒有訊號價值。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorAPIError,
        )
        client = MagicMock()
        client.reset_release_config.side_effect = PaaSOperatorAPIError(
            message='reset-config: failed to update the chart Secret',
            status_code=500,
        )
        get_client.return_value = client
        before = self._secret_of(self.service).admin_password

        with self.assertRaises(UserError) as caught:
            self.service._reset_admin_password()

        self.assertIn('reset failed', str(caught.exception).lower())
        self.assertEqual(self._secret_of(self.service).admin_password, before)

    def test_reset_while_upgrading_explains_the_wait(self):
        """C11：成功 reset 會 stamp 'upgrading'，租戶立刻重按會撞到這裡。

        通用訊息（must be running）讀起來像壞掉，但 cron 每分鐘就會翻回
        running。訊息要講「在更新中、稍候」而不是「必須是 running」。
        """
        self.service.state = 'upgrading'

        with self.assertRaises(UserError) as caught:
            self.service._reset_admin_password()

        message = str(caught.exception).lower()
        self.assertIn('update', message)
        self.assertNotIn('must be running', message)

    # ------------------------------------------------------------------
    # #461 C11′：提交點左移（operator 不再等 rollout）後的成功語意與預算
    # ------------------------------------------------------------------

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_success_stores_password_and_stamps_upgrading(self, get_client):
        """C11′ 成功路徑：2xx ⇒ 落庫 ＋ stamp upgrading（收斂交給輪詢）。

        改成非同步後，200 的語意是「新密碼已是叢集權威值、且不再自動回退」，
        **不是**「pod 已經吃到它」。所以成功時該做的正好兩件事：把密碼落庫（因為
        它已經是真相），以及把 state stamp 成 upgrading 讓既有的 cron（1 分鐘）與
        前端（5 秒）輪詢去收斂——這條把那兩件事釘在一起，避免日後有人「順手」把
        stamp 拿掉（那會讓服務停在 running、UI 不輪詢、租戶以為已經好了）。
        """
        client = MagicMock()
        client.reset_release_config.return_value = {
            'helm_revision': 8,
            'rollout_awaited': False,   # operator 只下達 restart，不等 pod
        }
        get_client.return_value = client

        new_password = self.service._reset_admin_password()

        self.assertEqual(self._secret_of(self.service).admin_password, new_password)
        self.assertEqual(
            self.service.state, 'upgrading',
            'C11′：成功必須 stamp upgrading —— 那是「rollout 還在跑」的唯一訊號，'
            '也是前端 5s 輪詢與動作鈕互鎖的觸發條件',
        )
        self.assertFalse(self.service.error_message)

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_reset_uses_its_own_timeout_budget_not_the_generic_helm_one(self, get_client):
        """#461：reset 走專屬預算 RESET_CONFIG_TIMEOUT，不是共用的 120s。

        這是預算階梯的中間層。用真的 client（只 mock 掉 HTTP session）來驗，
        因為要證明的是「送出請求時真的帶了那個 timeout」，而不是「常數存在」。
        跨層的不等式（前端 > 平台 > operator 硬上限）由 repo-lint 的
        check_reset_budget_ladder 驗——它讀得到另外兩個 repo 位置的常數，這裡讀不到。
        """
        from odoo.addons.woow_paas_platform.services import paas_operator

        real_client = paas_operator.PaaSOperatorClient(
            base_url='http://operator.test', api_key='k',
        )
        with patch.object(real_client._session, 'request') as request:
            request.return_value = MagicMock(
                status_code=200, json=lambda: {'rollout_awaited': False},
            )
            real_client.reset_release_config(
                namespace='paas-ws-test', release_name='svc-abc',
                sensitive_values={'config': {'sensitive': {'admin_password': 'p'}}},
            )

        self.assertEqual(
            request.call_args.kwargs['timeout'], paas_operator.RESET_CONFIG_TIMEOUT,
        )
        self.assertNotEqual(
            paas_operator.RESET_CONFIG_TIMEOUT, paas_operator.HELM_OPERATION_TIMEOUT,
            'reset 借用共用 HELM_OPERATION_TIMEOUT 就是 #461 的原始形狀：'
            '那個 120s 與 operator 真正的上限無關',
        )

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_chart_version_drift_is_logged(self, get_client):
        """版號漂移要留線索：這種 reset 夾帶 chart 升版，逾時失敗後**重按不會收斂**。

        （每次都夾帶同一次升版、每次都在 helm --timeout 逾時；正解是先 redeploy。
        租戶側引導是 F-461-2，本 PR 只保證維運判讀得出來。）
        """
        client = MagicMock()
        client.reset_release_config.return_value = {'rollout_awaited': False}
        get_client.return_value = client
        # 已部署 0.1.4，template 已升到 0.1.5 ⇒ 這次 reset 會夾帶升版。
        self.service.helm_chart_version = '0.1.4'

        # ⚠️ level 必須傳 **數值** logging.INFO，不能傳字串 'INFO'（#492）：
        # odoo/netsvc.py:338 有 `logging.addLevelName(logging.RUNBOT, "INFO")`
        # 且 RUNBOT=25，這會把 stdlib 的 `logging._nameToLevel['INFO']` 由 20
        # **覆寫成 25**；unittest/_log.py:37 正是查那張表（`_nameToLevel.get`），
        # 於是 `level='INFO'` 實際 setLevel(25) → 被測的 `_logger.info()`（20）
        # 被過濾掉 → 恆紅「no logs of level INFO or higher triggered」，而且紅得
        # 像是 code 掉了 log（實際上生產行為好好的）。此坑由 repo-lint 的
        # check_assertlogs_numeric_level 把關，敘事見
        # docs/tracking/pitfalls/assertlogs-string-level-shadowed-by-odoo.md。
        with self.assertLogs(
            'odoo.addons.woow_paas_platform.models.cloud_service',
            level=logging.INFO,
        ) as logs:
            self.service._reset_admin_password()

        self.assertTrue(
            any('carries a chart upgrade 0.1.4 -> 0.1.5' in line for line in logs.output),
            'chart 版號漂移沒有留下 log ⇒ 逾時時無法分辨「基礎設施慢」與'
            '「這次 reset 其實在升版」',
        )

    # ------------------------------------------------------------------
    # #357 C01/C03：admin_password 供給鏈（create → secret → helm sensitive）
    # ------------------------------------------------------------------

    def _secret_of(self, service):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', service.id)], limit=1,
        )

    def test_create_supplies_admin_password_end_to_end(self):
        """C01 正面全鏈：has_admin_gui template 建 CS → 密碼落 secret →
        _build_mcp_sensitive_values 產出 helm 要吃的 config.sensitive.admin_password。

        修復前：create() 用 mcp_enabled 過濾（三個目標 template 皆 False）→ 空集合 →
        secret 從不建立 → 這裡的 assertTrue(secret) 紅。
        """
        secret = self._secret_of(self.service)
        self.assertTrue(secret, 'has_admin_gui template 必須在 create() 就備妥 secret')
        self.assertEqual(secret.admin_password, 'old-password')

        # 供給鏈末端：helm 實際會收到的 sensitive values。
        self.assertEqual(
            self.service._build_mcp_sensitive_values(),
            {
                'config': {'sensitive': {'admin_password': 'old-password'}},
                'auth': {'apiServerKey': secret.gateway_api_key},
            },
        )
        self.assertTrue(self.service._has_mcp_admin_secret())
        # 密碼不得外洩到 helm_values（git/API 可見面）。
        self.assertNotIn('old-password', self.service.helm_values or '')

    def test_create_supplies_gateway_api_key_end_to_end(self):
        """#530 正面全鏈：has_admin_gui template 建 CS → gateway key 落 secret →
        供給鏈末端產出 chart 要吃的 ``auth.apiServerKey``。

        鏡射上一條 admin_password 的形狀。判別器同樣是 ``has_admin_gui``（不是
        slug、也不是「有沒有 gateway」）——供給面對所有 admin GUI template 一致，
        chart 讀不讀那個鍵是 chart 的事；分岔只在「交不交付給租戶」（見
        test_gateway_credentials_*）。

        ⚠ 上游（hermes gateway）對 **< 16 字元**的 key 會**靜默不登記 api server
        平台**——健康信號直接消失、沒有任何錯誤，所以長度下限是正確性的一部分，
        不是風格。生成點與 admin_password 共用（token_urlsafe(24) ≈ 32 chars）。
        """
        secret = self._secret_of(self.service)
        self.assertTrue(
            secret.gateway_api_key,
            'has_admin_gui template 必須在 create() 就備妥 gateway API key')
        self.assertNotEqual(
            secret.gateway_api_key, secret.admin_password,
            '兩把憑證必須是不同的值——其一外洩不得讓另一個入口同時失守')
        self.assertGreaterEqual(
            len(secret.gateway_api_key), 16,
            'gateway key 低於 16 字元 → 上游靜默不登記 api server 平台（無聲失效）')
        self.assertEqual(
            self.service._build_mcp_sensitive_values()['auth'],
            {'apiServerKey': secret.gateway_api_key},
        )
        # key 不得外洩到 helm_values（git/API 可見面）。
        self.assertNotIn(secret.gateway_api_key, self.service.helm_values or '')

    def test_legacy_secret_without_gateway_key_omits_the_auth_channel(self):
        """存量服務（secret 有密碼、無 gateway key）不得送出空的 ``auth.apiServerKey``。

        #530 的存量處置是「跑一次 Reset 換發」，在那之前 redeploy 仍會走供給鏈。
        此時若送 ``auth: {apiServerKey: ''}``，chart 的三段優先序第一段拿到顯式
        空值，就可能把既有 Secret 裡那把（chart 自產、agent 正在用的）洗掉 ⇒
        gateway 當場拒絕所有請求。**整個鍵不送**才會落到第二段「查既有 Secret」。
        """
        secret = self._secret_of(self.service)
        secret.gateway_api_key = False

        sensitive = self.service._build_mcp_sensitive_values()

        self.assertEqual(
            sensitive, {'config': {'sensitive': {'admin_password': 'old-password'}}},
            '沒有 gateway key 時不得帶出 auth 子樹（空值會蓋掉 chart 既有 Secret）')

    def test_sidecar_template_without_admin_gui_gets_no_password(self):
        """C01 負面（判別器鑑別力）：mcp_enabled=True 但 has_admin_gui=False——
        即 cloud_app_n8n（plain n8n + n8n-mcp sidecar）的形態——不該被供給
        admin_password：它沒有 mcp-admin-core 的 admin GUI，chart 也不吃這條路徑。

        這支是「判別器真的從 mcp_enabled 換成 has_admin_gui」的證據：修復前
        create() 判 mcp_enabled → 會為它建 secret（錯放）→ assertFalse 紅。
        """
        sidecar_tmpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Sidecar-backed n8n',
            'slug': 'n8n-sidecar-test',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/n8n',
            'helm_chart_version': '1.0.0',
            'mcp_enabled': True,      # 外掛 sidecar
            'has_admin_gui': False,   # 但沒有 admin-core GUI
        })
        service = self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='should-not-be-stored',
        ).create({
            'name': 'Sidecar n8n',
            'workspace_id': self.workspace.id,
            'template_id': sidecar_tmpl.id,
        })
        self.assertFalse(
            self._secret_of(service),
            'mcp_enabled 是 sidecar 語意，不得觸發 admin_password 供給')
        self.assertEqual(service._build_mcp_sensitive_values(), {})
        self.assertFalse(service._has_mcp_admin_secret())
        # #530：gateway key 沿用同一個閘門 —— 沒有 admin GUI 就連 secret 記錄都
        # 不該存在，自然也不該有 gateway key（判別器只有一個，不會分岔）。
        self.assertFalse(
            self.controller._gateway_credentials(service),
            'has_admin_gui=False → 不得交付任何 gateway 憑證')

    def test_legacy_service_without_secret_yields_empty_sensitive_values(self):
        """C03（方案 A）：secret 缺席回 {}，不 raise。

        供給鏈接上前建立的 legacy 服務沒有 secret，redeploy 會走到這裡。修復前是
        raise UserError → 既有服務一 redeploy 就炸。回 {} 讓 helm 沿用現有 Secret：
        這批服務的 chart Secret 本來就是 config.secret.json="{}"（平台從沒供給過
        admin_password）→「{} → {}」零變化。
        """
        # 製造 legacy 形態：has_admin_gui=True 但 secret 不存在。
        self._secret_of(self.service).unlink()
        self.assertFalse(self._secret_of(self.service))

        self.assertEqual(
            self.service._build_mcp_sensitive_values(), {},
            'secret 缺席應回 {} 沿用現有 Secret，不得 raise 也不得亂生密碼')
        self.assertFalse(self.service._has_mcp_admin_secret())

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_reset_backfills_secret_for_legacy_service(self, get_client):
        """C03 配套：legacy 服務（secret 缺席）reset 應成功並補建 secret——這正是
        租戶取得密碼的路徑。

        本測試守的是 _set_admin_password_secret 的 create-or-write 分支（把
        :801-809 的 create 分支拿掉即紅）。

        誠實註記（勿再寫成「revert C03 的 reset guard 這裡會紅」——實跑證實不會）：
        被移除的舊 guard 原文是 `if self.template_id.mcp_enabled and not secret`，
        而本檔 fixture 對齊真實 seed 設 mcp_enabled=False → `False and ...` 短路 →
        該 guard 對三個目標 template 從不觸發、本就是死碼，移除屬清理而非行為修復。
        （它對 mcp_enabled=True 且無 secret 的 legacy 服務才是活的，但那形態的
        reset 已先被 controller 的 has_admin_gui 判別器擋下。）
        """
        get_client.return_value = MagicMock()
        self._secret_of(self.service).unlink()
        self.assertFalse(self._secret_of(self.service))

        new_password = self.service._reset_admin_password()

        secret = self._secret_of(self.service)
        self.assertTrue(secret, 'reset 應為 legacy 服務補建 secret')
        self.assertEqual(secret.admin_password, new_password)
        # #530：Reset 是存量服務取得 gateway key 的**唯一**路徑（刻意不做從叢集
        # 回讀的 backfill——那會違反「憑證權威來源在平台端」）。補建 secret 時漏掉
        # gateway key，等於這批服務永遠拿不到。
        self.assertTrue(
            secret.gateway_api_key,
            'reset 補建 secret 時必須一併鑄出 gateway API key')

    # ------------------------------------------------------------------
    # #357 C01：controller ↔ model 兩端判別器收斂（#357 根因＝兩端漂移）
    #
    # 鑑別力關鍵：fixture 的 slug 必須「不在」舊 allowlist
    # ('mcp-odoo','mcp-odoo-manage','mcp-n8n') 內，否則兩種判別器都會命中、
    # 測不出差異。下面兩支分別鎖死漂移的兩個方向。
    # ------------------------------------------------------------------

    def _patch_paas_request(self):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=self.env),
        )

    def test_controller_create_uses_has_admin_gui_not_slug_allowlist(self):
        """C01 收斂（錯擋方向）：slug 不在舊 allowlist 但 has_admin_gui=True 的
        template，controller 仍須生密碼，且該密碼必須就是落庫那組。

        這一條同時鎖死三段：controller 生成 → with_context 傳遞 → model 落庫。
        若把 controller:2278 改回 slug allowlist，這個「未來新增的 has_admin_gui
        template」拿不到密碼 → 紅。反之若 response 密碼 != secret 密碼，就是 #357
        的病灶本身（租戶拿到一組永不生效的密碼）。
        """
        future_tmpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Future MCP app',
            'slug': 'mcp-future-app',   # 刻意不在舊 slug allowlist 內
            'category': 'mcp',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/mcp-server',
            'helm_chart_version': '0.1.5',
            'mcp_enabled': False,
            'has_admin_gui': True,
        })
        with self._patch_paas_request():
            result = self.controller._create_service(
                workspace=self.workspace,
                template_id=future_tmpl.id,
                name='Future MCP',
                values={},
                plan_id=self.plan.id,
            )
        self.assertTrue(result.get('success'), result.get('error'))
        password = result['data'].get('admin_password')
        self.assertTrue(
            password, 'has_admin_gui=True → 建立 response 必須帶一次性明文密碼')

        service = self.env['woow_paas_platform.cloud_service'].browse(
            result['data']['id'])
        secret = self._secret_of(service)
        self.assertTrue(secret, 'controller 生成的密碼必須經 model 落庫')
        self.assertEqual(
            secret.admin_password, password,
            'response 的密碼必須等於落庫那組——不相等即 #357 病灶：'
            '租戶看到一組永不生效的密碼')

    def test_controller_create_skips_slug_lookalike_without_admin_gui(self):
        """C01 收斂（錯放方向）：slug 在舊 allowlist 內、但 has_admin_gui=False 的
        template 不得被供給 admin_password。

        若把 controller:2278 改回 slug allowlist，這個 template 會拿到一組密碼、
        但 model 端閘門（has_admin_gui）不會建 secret → 密碼被丟棄卻已回給租戶
        ＝#357 原病灶重現 → 紅。
        """
        lookalike = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Slug lookalike without admin GUI',
            'slug': 'mcp-odoo-manage',   # 在舊 slug allowlist 內
            'category': 'mcp',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/mcp-server',
            'helm_chart_version': '0.1.5',
            'mcp_enabled': False,
            'has_admin_gui': False,      # 但沒有 admin GUI
        })
        with self._patch_paas_request():
            result = self.controller._create_service(
                workspace=self.workspace,
                template_id=lookalike.id,
                name='Lookalike',
                values={},
                plan_id=self.plan.id,
            )
        self.assertTrue(result.get('success'), result.get('error'))
        self.assertNotIn(
            'admin_password', result['data'],
            'has_admin_gui=False → 不得回密碼（slug 像不算數）')
        service = self.env['woow_paas_platform.cloud_service'].browse(
            result['data']['id'])
        self.assertFalse(
            self._secret_of(service), 'has_admin_gui=False 不得建 secret')

    # ------------------------------------------------------------------
    # #530：gateway 憑證的換發與交付
    # ------------------------------------------------------------------

    def _make_hermes_service(self, subdomain='hermes-gw-530'):
        """建一台 hermes-agent 服務（真 seed template，才有 gateway 對外路徑）。"""
        return self.env['woow_paas_platform.cloud_service'].create({
            'name': 'Hermes gateway probe',
            'workspace_id': self.workspace.id,
            'template_id': self.env.ref(
                'woow_paas_platform.cloud_app_hermes_agent').id,
            'state': 'running',
            'subdomain': subdomain,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-hermes-530',
        })

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_reset_rotates_the_gateway_api_key_too(self, get_client):
        """Reset 一次換發兩把憑證（單一 Reset 動作＝租戶唯一的換發入口）。

        只換 admin_password 的話，存量服務永遠拿不到 gateway key（本 spec 刻意
        不做從叢集回讀的 backfill），而「Reset 會一併換掉 Gateway API Key」這句
        UI 文案也會變成謊話——租戶回 MCP 那端重填的是同一把舊 key。
        """
        get_client.return_value = MagicMock()
        before = self._secret_of(self.service).gateway_api_key
        self.assertTrue(before, '前置：fixture 服務建立時就該有 gateway key')

        self.service._reset_admin_password()

        after = self._secret_of(self.service).gateway_api_key
        self.assertTrue(after)
        self.assertNotEqual(
            after, before, 'Reset 必須同時換發 gateway API key，不能只換密碼')

    def test_gateway_credentials_delivered_with_the_full_url(self):
        """交付端：hermes-agent 一次回「key ＋ 完整 gateway 網址」。

        完整網址由**後端**組（公開根位址 + seed 的子路徑），不是丟一個根位址讓
        前端或租戶自己拼——#530 的相鄰事實是租戶把 Dashboard 區填成服務根網址、
        漏掉子路徑而連不上；gateway 區完全同一個坑。後端組出來才有東西可以跟
        seed 做跨層 lock-step（test_gateway_external_path_matches_public_url_builder）。
        """
        service = self._make_hermes_service()
        secret = self._secret_of(service)
        paas_domain = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')

        data = self.controller._gateway_credentials(service)

        self.assertTrue(secret.gateway_api_key, '前置：hermes 服務應鑄出 gateway key')
        self.assertEqual(data['gateway_api_key'], secret.gateway_api_key)
        self.assertEqual(
            data['gateway_url'],
            'https://hermes-gw-530.%s/gateway' % paas_domain)

    def test_gateway_credentials_withheld_when_the_chart_has_no_gateway(self):
        """判別力的另一半：有 admin GUI、但 chart 沒有 gateway ⇒ 不交付。

        供給面對所有 has_admin_gui template 一致（見
        test_create_supplies_gateway_api_key_end_to_end），**交付面**則綁在「這個
        chart 真的有 gateway 對外路徑」上。交了等於在一次性視窗給租戶一把打不到
        任何端點的 key 與一條不存在的網址——那比不給更糟，它看起來像產品保證。

        兩個陽性對照先立，否則這條會在錯誤的理由下變綠：

        1. key 確實鑄出來了——否則「不交付」在完全沒實作鑄造的世界也是綠的。
        2. 服務**有** subdomain——fixture 沒有 subdomain 的話，withhold 的真正原因
           會變成「算不出公開位址」，於是有人把 mcp-odoo 誤加進 gateway 子路徑
           對照表時這條照樣綠（判別器指錯了東西）。
        """
        self.service.subdomain = 'mcp-odoo-gw-probe'
        self.assertTrue(
            self._secret_of(self.service).gateway_api_key,
            '前置：mcp-odoo（has_admin_gui）同樣會鑄出 key —— 沒有這行，下面那條'
            '「不交付」在完全沒實作鑄造的世界也是綠的')
        self.assertTrue(
            self.service._public_base_url(),
            '前置：服務要算得出公開位址 —— 否則 withhold 的原因是「沒有位址」'
            '而不是「這個 chart 沒有 gateway」，判別器就指錯了東西')

        self.assertEqual(
            self.controller._gateway_credentials(self.service), {},
            'chart 沒有 gateway 對外路徑時不得交付 gateway 憑證')

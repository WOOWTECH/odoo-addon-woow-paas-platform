"""Regression tests for #357's one-time MCP admin password flow."""
import json
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
        self.assertEqual(
            client.reset_release_config.call_args.kwargs['sensitive_values'],
            {'config': {'sensitive': {'admin_password': new_password}}},
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
            {'config': {'sensitive': {'admin_password': 'old-password'}}},
        )
        self.assertTrue(self.service._has_mcp_admin_secret())
        # 密碼不得外洩到 helm_values（git/API 可見面）。
        self.assertNotIn('old-password', self.service.helm_values or '')

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

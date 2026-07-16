"""#357 C05 + C19 + C20：reset admin password 的授權守線與明文外洩面。

與 ``test_mcp_admin_password.py``（供給鏈的正／負面行為）分家的理由：本檔鎖的是
「**誰**能重置」與「明文**會不會漏出去**」，與供給鏈「密碼有沒有接上」是不同的
失效模式——前者漏了是提權，後者漏了是功能不生效。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.service.model import get_public_method
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import ROLE_USER


class _McpServiceFixture(TransactionCase):
    """共用 fixture：一個 has_admin_gui 的 running MCP service。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Reset authz workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'MCP Odoo authz',
            # 刻意不用舊 slug allowlist 內的名字：避免測試在「判別器退回 slug
            # allowlist」的世界仍然綠（見 test_mcp_admin_password.py 的 C01 收斂註解）。
            'slug': 'mcp-odoo-authz',
            'category': 'mcp',
            # helm_repo_url 是 required（DB NOT NULL）；OCI chart 慣例是空字串。
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/mcp-server',
            'helm_chart_version': '0.1.5',
            # 對齊真實 seed：MCP 內建於 body（mcp_enabled=False），供給鏈閘門是
            # has_admin_gui。
            'mcp_enabled': False,
            'has_admin_gui': True,
        })
        self.service = self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='old-password',
        ).create({
            'name': 'MCP Odoo',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'mcp-odoo-test',
        })

    def _secret_of(self, service):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', service.id)], limit=1,
        )

    def _make_member(self, login, role):
        """自建 user + workspace 成員資格。

        #357 C22：刻意不用 ``base.user_demo``——它只在載入 demo data 的 DB 存在，
        在 ``--without-demo`` 的 DB（CI 常態）會 ValueError 讓整支 ERROR 而非 skip，
        等於這條守著提權洞的斷言在那些環境從沒跑過。只依賴 base.group_user。
        """
        user = self.env['res.users'].create({
            'name': login,
            'login': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id,
            'user_id': user.id,
            'role': role,
        })
        return user

    def _patch_request_as(self, user):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=self.env(user=user)),
        )


class TestResetAdminPasswordAuthorization(_McpServiceFixture):
    """#357 C05：reset 回傳明文密碼，而授權只存在 controller → model 端必須 RPC 不可達。"""

    def test_reset_method_is_not_remotely_callable(self):
        """C05 主守線：``_reset_admin_password`` 不得可由 RPC 直呼。

        攻擊路徑（修復前為真）：workspace 成員（role=user/guest，非 admin）不打被
        ROLE_ADMIN gate 擋住的 REST 端點，改直接
        ``POST /web/dataset/call_kw {"model":"...cloud_service",
        "method":"action_reset_admin_password","args":[[<id>]]}``——cloud_service 的
        ACL 對 group_user 全開、ir.rule 只驗「是該 workspace 成員」→ 通過 →
        **RPC 回傳值即明文新密碼**，同時把真 admin 的密碼換掉（提權 + DoS）。

        這裡直接呼叫 Odoo 18 真正的 gate ``get_public_method``，而不是 mock 一個
        HTTP 請求——mock 只能證明「我 mock 的東西擋了」。該 gate 被兩條 RPC 路徑
        共用：JSON-RPC（web/controllers/dataset.py:34 的 call_kw／call_button）與
        XML-RPC（service/model.py:71 的 execute_cr）→ 鎖住它等於同時鎖住兩條。

        鑑別力：把 method 改回 ``action_reset_admin_password``（public）→ 這裡不再
        raise → 紅。
        """
        with self.assertRaises(AccessError):
            get_public_method(
                self.env['woow_paas_platform.cloud_service'],
                '_reset_admin_password',
            )

    def test_get_public_method_still_admits_public_methods(self):
        """反向鎖：證明上一條測的是「private 才被擋」，而非 gate 對任何輸入都 raise。

        沒有這條，上一條在「``get_public_method`` 恆 raise」或「model 根本沒這個
        method」的世界也會綠＝假鑑別力（本案最大教訓正是紙上綠的斷言）。
        """
        method = get_public_method(
            self.env['woow_paas_platform.cloud_service'], 'action_redeploy',
        )
        self.assertTrue(callable(method))

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_non_admin_member_is_denied_and_secret_untouched(self, get_client):
        """C05 的另一半：controller 的 ROLE_ADMIN gate 真的擋 role=user 的成員。

        除了「不得回密碼」，同時鎖「secret 不得被輪換」——否則非 admin 即使拿不到
        新密碼，也能把真 admin 的密碼洗掉（DoS）。

        ``get_paas_operator_client`` **必須** mock 成可用，否則這支測試是假鑑別力：
        沒有它時，把 gate 放寬（required_role=ROLE_USER）這個 mutation 下，成員其實
        **已通過 gate**，只是後面撞上 'PaaS Operator not configured' 才回 success=False
        → 斷言照樣綠，測到的是「operator 沒配」而不是「授權擋住」。實測坐實：
        mutation 後 probe 回 ``{'success': False, 'error': 'PaaS Operator not
        configured. Contact administrator.'}``。mock 之後「通過 gate ⟹ reset 真的
        成功」，斷言才真正落在授權這條線上。
        """
        get_client.return_value = MagicMock()
        member = self._make_member('warden-c05-member', ROLE_USER)
        before = self._secret_of(self.service).admin_password

        with self._patch_request_as(member):
            result = self.controller.api_service_reset_admin_password(
                self.workspace.id, self.service.id,
            )

        self.assertFalse(result.get('success'), '非 admin 成員必須被拒絕')
        self.assertNotIn(
            'admin_password', json.dumps(result),
            'reset 被拒時 response 不得夾帶任何密碼欄位')
        self.assertEqual(
            self._secret_of(self.service).admin_password, before,
            '被拒的 reset 不得輪換 secret——否則非 admin 可 DoS 掉真 admin 的密碼')


class TestResetAdminPasswordSanitisation(_McpServiceFixture):
    """#357 C19/C20：一次性明文的外洩面（遮罩、service detail、密碼唯一性）。"""

    def test_config_sensitive_is_masked_without_any_plaintext_prefix(self):
        """C19-3：``config.sensitive.*`` 的遮罩不得保留明文前綴。

        遮罩是「strip 失效時」的第二道防線，第二道防線自身不該洩漏明文。修復前
        ``_mask_value`` 回 ``f'{sv[:8]}...'``——原本的斷言只驗「完整值不出現」，
        對 8 字元前綴洩漏完全無感（那條斷言在洩漏前綴的世界照樣綠）。

        鑑別力：把 config.sensitive 分支改回 ``_mask_value``（8 前綴）→
        masked 變 'SuperSec...' → 這裡 assertEqual('***') 紅。
        """
        values = {
            'config': {
                'sensitive': {
                    'admin_password': 'SuperSecretPwd123456',
                    'nested': {'token': 'AnotherLongSecretValue'},
                },
            },
        }
        sanitized = PaasController._sanitize_helm_values(values, self.template)

        masked = sanitized['config']['sensitive']['admin_password']
        self.assertEqual(masked, '***', 'admin_password 必須全遮、不留前綴')
        self.assertNotIn(
            'SuperSec', masked, '遮罩後不得殘留任何明文前綴（舊實作留 8 字元）')
        # 巢狀葉子同樣全遮（chart 的 sensitive 子樹不只 admin_password）。
        self.assertEqual(sanitized['config']['sensitive']['nested']['token'], '***')

    def test_legacy_secret_subtree_keeps_its_prefix_behaviour(self):
        """C19-3 的邊界：legacy top-level ``secret`` 子樹**不得**被一起改掉。

        n8n 既有 UI 靠前綴辨識是哪一把 key，故該子樹刻意維持 8 字元前綴。這條鎖住
        「C19-3 只動 config.sensitive 分支」——若有人圖省事把兩個分支都改成全遮，
        這裡會紅。
        """
        sanitized = PaasController._sanitize_helm_values(
            {'secret': {'N8N_API_KEY': 'abcdefghijklmnop'}}, self.template,
        )
        self.assertEqual(
            sanitized['secret']['N8N_API_KEY'], 'abcdefgh...',
            'legacy secret 子樹維持 8 字元前綴（n8n UI 依賴），不隨 C19-3 改動')

    def test_service_detail_never_exposes_the_stored_password(self):
        """C20：GET service detail 不得帶出 secret 表裡的明文密碼。

        密碼只在 create／reset 的 response 回傳一次；detail 是租戶每次開頁都會打的
        端點，任何洩漏都是長期可重放的。

        鑑別力：讓 ``_format_service`` 帶上 ``_get_secret_record().admin_password``
        → 這裡紅。
        """
        payload = self.controller._format_service(self.service, include_details=True)

        # 先證明 payload 真的產出了（否則「密碼不在空 dict 裡」是 vacuous）。
        self.assertEqual(payload['id'], self.service.id)
        self.assertNotIn(
            'old-password', json.dumps(payload),
            'service detail 不得含明文 admin_password')
        self.assertNotIn('admin_password', payload)

    def test_each_service_gets_a_distinct_password(self):
        """C20：連建兩個 service → 兩組 secret 不得相等。

        鎖的是 ``_generate_admin_password`` 真的每次重新生成（而非常數／共用值）。
        刻意**不**帶 ``mcp_admin_password`` context，讓 create() 走自己的生成路徑。

        鑑別力：把 ``_generate_admin_password`` 改成回傳固定字串 → 這裡紅。
        """
        svc_a, svc_b = [
            self.env['woow_paas_platform.cloud_service'].create({
                'name': f'MCP Odoo {suffix}',
                'workspace_id': self.workspace.id,
                'template_id': self.template.id,
            })
            for suffix in ('A', 'B')
        ]

        pwd_a = self._secret_of(svc_a).admin_password
        pwd_b = self._secret_of(svc_b).admin_password
        self.assertTrue(pwd_a and pwd_b, '兩個 service 都必須自動備妥密碼')
        self.assertNotEqual(pwd_a, pwd_b, '每個 service 的 admin_password 必須獨立生成')
        # token_urlsafe(24) → ~32 chars；過短代表有人把生成器換弱了。
        self.assertGreaterEqual(len(pwd_a), 32, '密碼強度不得低於 token_urlsafe(24)')

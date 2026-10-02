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
from odoo.addons.woow_paas_platform.models.workspace_access import ROLE_ADMIN, ROLE_USER
from ..models.cloud_service import _internal_provision


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
            # 對齊真實 seed：MCP 內建於 body（mcp_enabled=False），admin_password
            # 供給鏈閘門是 has_admin_gui。
            'mcp_enabled': False,
            'has_admin_gui': True,
            # #530 review finding 3：gateway API key 有**自己的**閘門
            # （_chart_has_gateway ← seed 的 gateway.externalPath），不跟著
            # has_admin_gui 走。本 class 的四條 gateway key 遮罩／不外洩斷言都要求
            # 「這台服務真的有一把 key」，所以 fixture chart 必須宣告 gateway；少了
            # 這行，那些斷言會因為「根本沒有 key」而恆真（assertNotIn '' 家族）。
            'helm_default_values': json.dumps({'gateway': {'externalPath': '/gateway'}}),
        })
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='old-password',
        )).create({
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
        # #991（#1300）：detail 分支依呼叫者 `request.env.user.share` 決定 helm_values
        # 的 DTO，未綁 request 會 RuntimeError。以租戶裡權限最高、會按 Reset 的 internal
        # workspace admin 開詳情頁——外洩面以最有權限的租戶量；受測 service 與斷言不變。
        with self._patch_request_as(self._make_member('c20-detail-admin', ROLE_ADMIN)):
            payload = self.controller._format_service(self.service, include_details=True)

        # 先證明 payload 真的產出了（否則「密碼不在空 dict 裡」是 vacuous）。
        self.assertEqual(payload['id'], self.service.id)
        self.assertNotIn(
            'old-password', json.dumps(payload),
            'service detail 不得含明文 admin_password')
        self.assertNotIn('admin_password', payload)

    def test_gateway_api_key_is_masked_without_any_plaintext_prefix(self):
        """🔴 #530：``auth.apiServerKey`` 也必須被遮罩——它**不在** config.sensitive 底下。

        既有的遮罩是 path-aware 的：只認 top-level ``secret`` 子樹與
        ``config.sensitive`` 底下的葉節點。chart 要求的 gateway key 鍵路徑是
        ``auth.apiServerKey``，兩條都不命中 ⇒ 照抄 admin_password 的做法卻沒擴充
        這一層，這把 key 會原樣從服務詳情 API 流出，而**只掃 config.sensitive 的
        既有測試不會紅**。這條就是那個缺口的守線。

        同時做**陰性面的陽性對照**：``auth.username`` 這種同層非憑證欄位不得被
        一起遮掉（Odoo/PostgreSQL 家族的 seed 就有 ``auth.username``／
        ``auth.database``）。把整個 ``auth`` 子樹全遮會讓那些 template 的設定分頁
        顯示成一排 ``***``——遮罩要精準命中憑證那一格。
        """
        values = {
            'auth': {
                'apiServerKey': 'SuperSecretGatewayKey123456',
                'username': 'woow',
            },
        }
        sanitized = PaasController._sanitize_helm_values(values, self.template)

        # 主斷言看**結果**（整份輸出裡找不到那個值），不是「某個欄位等於某字串」。
        # 只斷言 `sanitized['auth']['apiServerKey'] == '***'` 的話，換一條洩漏路徑
        # （值被複製到別的鍵、遮罩實作改成把原值搬去 sibling）就漏掉了——斷言只看
        # 得到自己預期的失敗形狀。
        self.assertNotIn(
            'SuperSecretGatewayKey123456', json.dumps(sanitized),
            'gateway API key 的明文不得出現在遮罩後的任何位置')
        self.assertNotIn(
            'SuperSec', json.dumps(sanitized),
            '遮罩後不得殘留任何明文前綴（legacy secret 子樹那條刻意留 8 字元，'
            '這條不得比照）')
        self.assertEqual(
            sanitized['auth']['apiServerKey'], '***',
            'gateway API key 必須全遮（而不是被整個鍵刪掉——刪掉會讓設定分頁'
            '看不出這個值存在）')
        self.assertEqual(
            sanitized['auth']['username'], 'woow',
            '同層的非憑證欄位不得被連坐遮掉（整個 auth 子樹全遮是過度修法）')

    def test_service_detail_never_exposes_the_stored_gateway_api_key(self):
        """#530：GET service detail 不得帶出 secret 表裡的 gateway API key。

        與 admin_password 同一條紀律：key 只在 create／reset 的 response 回一次，
        detail 是租戶每次開頁都會打的端點，任何洩漏都是長期可重放的。本 repo 刻意
        **不新增回讀端點**——新增等於開一條新的外洩面並要自行承擔授權。

        陽性對照先立：key 真的存在才談「它不在 payload 裡」。
        """
        gateway_key = self._secret_of(self.service).gateway_api_key
        self.assertTrue(
            gateway_key,
            '前置：fixture 服務應鑄出 gateway key —— 沒有這行，下面的 assertNotIn '
            '在「key 從未被鑄造」的世界也是綠的')

        # #991（#1300）：同上一支 C20——以 internal workspace admin 的身分開詳情頁。
        with self._patch_request_as(self._make_member('530-detail-admin', ROLE_ADMIN)):
            payload = self.controller._format_service(self.service, include_details=True)

        self.assertEqual(payload['id'], self.service.id)
        self.assertNotIn(
            gateway_key, json.dumps(payload),
            'service detail 不得含明文 gateway API key')
        self.assertNotIn('gateway_api_key', payload)

    def test_gateway_api_key_is_stripped_before_helm_values_are_persisted(self):
        """#530：``auth.apiServerKey`` 不得被寫回 ``helm_values``。

        遮罩是第二道防線；第一道是「根本不落庫」。既有的 strip 同樣 path-aware
        （只 pop ``config.sensitive``），所以 gateway key 這條路徑要另外擴充。
        兩道都失效才會外洩，但只補一道就是把安全性押在另一道從沒為它設計過的
        程式碼上。

        走真正的落庫入口（``create()`` 的 vals 正規化），不是直接呼 strip helper
        ——要證明的是「這條路徑上的值不會留下」，不是「helper 可以被呼叫」。
        """
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'Gateway strip probe',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_values': json.dumps({
                'auth': {'apiServerKey': 'gw-key-must-not-persist'},
                'config': {'nonSensitive': {'tools': {'enabled': ['search']}}},
            }),
        })

        stored = json.loads(service.helm_values)
        self.assertEqual(
            stored['config']['nonSensitive']['tools'], {'enabled': ['search']},
            '前置：非敏感值必須原樣留下——整包被丟掉的話下面那條是 vacuous')
        self.assertNotIn(
            'apiServerKey', stored.get('auth', {}),
            'gateway API key 不得落進 helm_values（git／API 可見面）')
        self.assertNotIn('gw-key-must-not-persist', service.helm_values)

    def test_each_service_gets_a_distinct_gateway_api_key(self):
        """#530：連建兩個 service → 兩把 gateway key 不得相等。

        單一租戶的憑證外洩不得波及他人，所以 key 必須 per-service 生成而非常數／
        共用值。長度下限 16 是**上游的硬需求**：低於 16 時 hermes gateway 會靜默
        不登記 api server 平台——不報錯、健康信號直接消失，是無聲失效。生成點與
        admin_password 收斂在同一個 method（token_urlsafe(24) ≈ 32 chars），所以
        這裡連 32 一起斷言，換弱時兩把憑證會一起紅。
        """
        svc_a, svc_b = [
            _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
                'name': f'MCP Odoo gw {suffix}',
                'workspace_id': self.workspace.id,
                'template_id': self.template.id,
            })
            for suffix in ('A', 'B')
        ]

        key_a = self._secret_of(svc_a).gateway_api_key
        key_b = self._secret_of(svc_b).gateway_api_key
        self.assertTrue(key_a and key_b, '兩個 service 都必須自動備妥 gateway key')
        self.assertNotEqual(key_a, key_b, '每個 service 的 gateway key 必須獨立生成')
        self.assertGreaterEqual(
            len(key_a), 16,
            '低於 16 字元 → 上游靜默不登記 api server 平台（無聲失效）')
        self.assertGreaterEqual(
            len(key_a), 32, 'gateway key 強度不得低於 token_urlsafe(24)')

    def test_each_service_gets_a_distinct_password(self):
        """C20：連建兩個 service → 兩組 secret 不得相等。

        鎖的是 ``_generate_admin_password`` 真的每次重新生成（而非常數／共用值）。
        刻意**不**帶 ``mcp_admin_password`` context，讓 create() 走自己的生成路徑。

        鑑別力：把 ``_generate_admin_password`` 改成回傳固定字串 → 這裡紅。
        """
        svc_a, svc_b = [
            _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
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


class TestRedeployRpcGate(_McpServiceFixture):
    """#1185：``action_redeploy`` 與 C05 同一種攻擊形狀——public 方法＋ACL 全開＋
    owner/admin ir.rule ⇒ 租戶經 ``call_kw`` 直呼就能繞過 ``/woow`` 的
    ``_check_operation_allowed`` 觸發不可逆跳版。修法同款：實作搬進 private
    ``_redeploy``，public 入口只留 ``base.group_system`` gate。

    deny 分支必須 ``with_user`` 退出 su 才打得到：``TransactionCase`` 是 su env，
    ``env.is_system()`` 恆真（見 pattern doc ``su-env-exempt-guard-false-green``——
    這裡是它「有合法 gated 路徑」的那一邊，不是不變量 guard）。
    """

    def test_redeploy_impl_is_not_remotely_callable(self):
        """主守線：``_redeploy`` 不得可由 RPC 直呼（同 C05 的 ``get_public_method`` gate）。

        鑑別力：把實作改回 public 名稱 → 這裡不再 raise → 紅。
        """
        with self.assertRaises(AccessError):
            get_public_method(
                self.env['woow_paas_platform.cloud_service'], '_redeploy',
            )

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_workspace_admin_is_denied_before_touching_the_operator(self, get_client):
        """後台入口對 workspace admin 也擋，而且擋在任何 operator 呼叫之前。

        用 admin（ir.rule ``cloud_service_rule_owner_admin_manage`` 給 write 的
        角色；owner 已被 fixture 的 workspace 建立者佔走、一個 workspace 只能有一個）
        而不是 user：要證明的是「租戶側有寫入權的角色也不夠」，用被 ir.rule 擋掉的
        角色會為了錯的理由變綠。``get_paas_operator_client`` 必須 mock 成可用，
        否則 deny 可能只是 'PaaS Operator not configured' 的 UserError 假綠
        （同 C05 的教訓）。
        """
        client = MagicMock()
        get_client.return_value = client
        admin = self._make_member('warden-1185-admin', ROLE_ADMIN)

        with self.assertRaises(AccessError):
            self.service.with_user(admin).action_redeploy()

        client.get_release.assert_not_called()
        client.upgrade_release.assert_not_called()

    def test_system_user_still_reaches_the_implementation(self):
        """陽性對照：gate 對 system（含 su env）放行並委派給 ``_redeploy``。

        沒有這條，一個「什麼都擋」的壞 gate 會讓上面兩條為了錯的理由變綠；
        既有的 ``test_suspended_redeploy_state_482`` 等測試也依賴這條放行。
        """
        with patch.object(
            type(self.service), '_redeploy', autospec=True,
        ) as impl:
            self.service.action_redeploy()
        impl.assert_called_once()

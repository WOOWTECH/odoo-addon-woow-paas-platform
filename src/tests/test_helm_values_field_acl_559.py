"""#559：`cloud_service.helm_values` 的欄位級授權。

#535 把服務詳情 API 的憑證遮罩改成 spec-driven，但那道遮罩在 **controller 層**。
`helm_values` 欄位本身沒有 `groups=` 時，租戶可以直接打
``POST /web/dataset/call_kw`` 送 ``read(['helm_values'])`` **繞過整個 controller
遮罩層**拿到明文——鎖了前門、側門還開著。

**實測坐實**（#559，非讀碼結論）：最低權限（workspace role=guest）的
``base.group_user`` 租戶，用真 session 打 call_kw 讀得到完整明文；同一條路徑上讀
``n8n_owner_password``（有 ``groups=``）則被 ``AccessError`` 擋下——**鑑別項證明
「有 groups= 就會被擋」在這條路徑上確實生效**，所以補上 groups= 會擋住。

## 為什麼這一檔的測試必須用 ``with_user()``

加上 ``groups=`` 之後，真實的租戶服務詳情 API 立刻整條掛掉（``AccessError``），
**而當時全套測試零紅**——既有測試幾乎都以 root／superuser 身分呼叫 controller
helper，對「租戶身分」這條路徑零覆蓋。所以本檔每一條會走 controller 的斷言都
顯式 ``with_user(tenant)``；少了它，這些測試會在「租戶完全看不到自己的設定分頁」
的世界照樣全綠。
"""
import json

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision


class _TenantServiceFixture(TransactionCase):
    """一個 workspace + 一位**最低權限**（role=guest）的租戶成員 + 一台 service。"""

    PLAINTEXT = 'HELMVALUES_PLAINTEXT_DO_NOT_LEAK_559'
    N8N_PLAINTEXT = 'N8N_OWNER_PW_DO_NOT_LEAK_559'

    def setUp(self):
        super().setUp()
        self.controller = PaasController()

        # 刻意自建 user 而不用 base.user_demo：後者只在載入 demo data 的 DB 存在，
        # 在 --without-demo 的 CI DB 會 ValueError 讓整支 ERROR 而非 skip。
        self.owner = self.env['res.users'].create({
            'name': 'T559 Owner', 'login': 't559-owner-test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.tenant = self.env['res.users'].create({
            'name': 'T559 Tenant', 'login': 't559-tenant-test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'T559 workspace', 'owner_id': self.owner.id,
        })
        # 受測帳號是**純成員**、且是最低角色——洩漏面要以最低權限量。
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id,
            'user_id': self.tenant.id,
            'role': 'guest',
        })
        self.template = self.env.ref('woow_paas_platform.cloud_app_odoo')
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 't559-svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-t559',
            'helm_release_name': 't559',
        })
        self.service._apply_helm_values({
            'odooEmail': 'user@example.com',
            'odooPassword': self.PLAINTEXT,
        })
        self.service.sudo().write({'n8n_owner_password': self.N8N_PLAINTEXT})

    def _as_tenant(self):
        return self.service.with_user(self.tenant)


class TestHelmValuesFieldAcl(_TenantServiceFixture):
    """租戶不得用 ORM 直讀 helm_values，但平台讀得到。"""

    def test_tenant_cannot_read_helm_values_via_orm(self):
        """🔴 本 issue 的主守線：租戶身分 ``read(['helm_values'])`` 必須被擋。

        三格量測寫成三條斷言，缺一不可（工單的實測設計）：

        * **陽性對照**：同一個 recordset 讀 ``name`` 讀得到 ⇒ 記錄級授權（ir.rule
          workspace 成員）是通的，下面的 AccessError 不是「這筆記錄他本來就看不到」。
        * **鑑別項**：``n8n_owner_password``（本來就有 ``groups=``）被擋 ⇒
          「欄位有 groups= 就會被擋」這個機制在這條路徑上確實生效。
        * **被測項**：``helm_values`` 同樣被擋。

        少了陽性對照，「讀不到」可能只是記錄根本不可見；少了鑑別項，就無法推論
        「補上 groups= 會擋住」。
        """
        tenant_service = self._as_tenant()

        # 陽性對照
        self.assertEqual(
            tenant_service.read(['name'])[0]['name'], 't559-svc',
            '租戶對這筆 service 的記錄級存取必須是通的，否則下面兩條會因為錯的理由通過')

        # 鑑別項
        with self.assertRaises(AccessError):
            tenant_service.read(['n8n_owner_password'])

        # 被測項
        with self.assertRaises(AccessError):
            tenant_service.read(['helm_values'])

    def test_tenant_cannot_reach_the_plaintext_through_search_read(self):
        """不只 ``read``：``search_read`` 這種通用 RPC 形態也要擋。

        只擋 ``read()``（例如加一個 ``read`` override）會留下這類側門——欄位級
        ``groups=`` 是在 ``check_field_access_rights`` 生效，所以讀取形態無關。
        這條就是「機制選對了沒有」的守線。

        ⚠ 這裡**刻意不驗 ``export_data``**：實測它對本測試帳號拋的是
        ``UserError('You don't have the rights to export data')``——被**匯出權限
        群組**擋下，與欄位級 ACL 是不同機制。拿它當斷言會在「欄位 groups= 被拿掉」
        的世界照樣綠（因為仍被匯出權限擋），是零鑑別力的裝飾。
        """
        Service = self.env['woow_paas_platform.cloud_service'].with_user(self.tenant)

        with self.assertRaises(AccessError):
            Service.search_read([('id', '=', self.service.id)], ['helm_values'])

        # 陽性對照：同一形態讀非憑證欄位是通的 ⇒ 上面不是「這個 API 本來就不能用」
        rows = Service.search_read([('id', '=', self.service.id)], ['name'])
        self.assertEqual(rows[0]['name'], 't559-svc')

    def test_field_is_hidden_from_fields_get_for_tenants(self):
        """``fields_get`` 也不得吐出這個欄位——否則前端／自動化仍看得到它存在。"""
        fields = self.env['woow_paas_platform.cloud_service'].with_user(
            self.tenant).fields_get(['helm_values', 'name'])

        self.assertIn('name', fields, '陽性對照：非憑證欄位仍應列出')
        self.assertNotIn('helm_values', fields)

    def test_platform_can_still_read_helm_values(self):
        """平台身分（``base.group_system``）讀得到——否則部署管線會整條斷掉。"""
        raw = self.service.sudo().read(['helm_values'])[0]['helm_values']

        self.assertIn(self.PLAINTEXT, raw)


class TestTenantFacingPathsStillWork(_TenantServiceFixture):
    """🔴 修好資安不得讓租戶看不到自己的設定分頁。

    加 ``groups=`` 當下，真實 API 立刻 ``AccessError``，**而全套測試零紅**——
    既有測試都以 root 身分呼叫 controller helper。本 class 就是補那個盲區：
    每一條都顯式 ``with_user(tenant)``。
    """

    def test_service_detail_payload_works_for_tenant_and_is_masked(self):
        """租戶身分取服務詳情：要成功、要帶 helm_values、而且是遮罩後的。

        鑑別力：把 ``_format_service`` 的讀取改回屬性直讀（``service.helm_values``）
        ⇒ 這條以 ``AccessError`` 變紅。這正是加 groups= 當下真實 API 的症狀，
        而修復前的全套測試對它零覆蓋。
        """
        payload = self.controller._format_service(
            self._as_tenant(), include_details=True)

        self.assertIn('helm_values', payload,
                      '租戶必須仍看得到自己的設定（只是遮罩過）')
        self.assertEqual(payload['helm_values'].get('odooPassword'), '***')
        self.assertNotIn(
            self.PLAINTEXT, json.dumps(payload),
            'controller 的正常路徑不得回明文（#535 的遮罩仍須生效）')

    def test_helm_values_dict_works_under_tenant_identity(self):
        """model 的唯一讀取入口在租戶身分下必須可用（它是 controller 的依賴）。"""
        values = self._as_tenant()._helm_values_dict()

        self.assertEqual(values.get('odooPassword'), self.PLAINTEXT)

    def test_apply_helm_values_works_under_tenant_identity(self):
        """reconfigure 走的寫入路徑在租戶身分下必須可用。"""
        self._as_tenant()._apply_helm_values({'odooPassword': 'NEW_PW_559'})

        self.assertEqual(
            self.service.sudo()._helm_values_dict().get('odooPassword'),
            'NEW_PW_559')


class TestApplyHelmValuesSemantics(_TenantServiceFixture):
    """提權入口的語意：只提升欄位級權限，不順手放寬其他欄位。"""

    def test_extra_vals_are_written_with_the_callers_identity(self):
        """``extra_vals`` 不提權——ir.rule／欄位 groups 對它們照常生效。

        鑑別力：若 ``_apply_helm_values`` 改成把整包 vals 都 sudo 寫入，這條會綠
        （寫入成功），所以斷言的是「**被擋**」——租戶不得藉這個入口寫到一個
        有 groups= 的欄位。
        """
        with self.assertRaises(AccessError):
            self._as_tenant()._apply_helm_values(
                {'odooPassword': 'X'},
                extra_vals={'n8n_owner_password': 'SHOULD_NOT_BE_WRITABLE'})

        self.assertEqual(
            self.service.sudo().n8n_owner_password, self.N8N_PLAINTEXT,
            '被擋的寫入不得留下副作用')

    def test_apply_helm_values_strips_values_that_must_never_persist(self):
        """寫入路徑套用與 ``create()`` 相同的敏感值剝除。

        ``config.sensitive.*`` 與 ``auth.apiServerKey`` 不得落庫（它們另走 operator
        的分離通道）。修復前 reconfigure 的寫入**沒有**經過這層，本入口把它統一了。
        """
        self.service._apply_helm_values({
            'odooPassword': 'KEEP_ME_559',
            'config': {'sensitive': {'admin_password': 'MUST_NOT_PERSIST_559'}},
            'auth': {'apiServerKey': 'MUST_NOT_PERSIST_GATEWAY_559',
                     'username': 'woow'},
        })

        stored = self.service.sudo()._helm_values_dict()
        dumped = json.dumps(stored)

        self.assertEqual(stored['odooPassword'], 'KEEP_ME_559', '非剝除路徑要保留')
        self.assertEqual(stored['auth']['username'], 'woow',
                         '同層非憑證欄位不得被連坐剝掉')
        self.assertNotIn('MUST_NOT_PERSIST_559', dumped)
        self.assertNotIn('MUST_NOT_PERSIST_GATEWAY_559', dumped)

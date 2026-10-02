"""#462（#460 S1）service payload 的 ``template.has_admin_gui`` 合約測試。

背景：前台 Reset 密碼按鈕原本用硬編碼 slug 白名單
（``["mcp-odoo","mcp-odoo-manage","mcp-n8n"]``）決定顯不顯示，後端 reset endpoint
早已改判 ``template.has_admin_gui``（#357 C01）。兩端判準不一致 → ``has_admin_gui=True``
的 8 個模板中有 5 個（mcp-k8s／mcp-vk／mcp-opendesign／open-design／hermes-agent）
後端放行、前端卻按不到按鈕，修復對租戶不可達（#460 / F-449-4）。

本檔守的是**資料合約**：凡回傳 template 子物件的 service payload，一律帶
``has_admin_gui``，且值**直接鏡射**模板欄位——不得從 slug（或任何其他欄位）推導。
前端唯一的能力資訊入口就是這個欄位。

鑑別力設計（兩支測試刻意把 slug 與 has_admin_gui 對調成「反直覺」組合）：
  * True 案的 slug 不在舊白名單內 → 若有人拿 slug 推導，payload 會錯回 False。
  * False 案的 slug 在舊白名單內 → 若有人拿 slug 推導，payload 會錯回 True。
兩支同時綠，才能證明 payload 讀的是欄位而不是名單。
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision


class TestServicePayloadHasAdminGui462(TransactionCase):
    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'has_admin_gui payload workspace',
        })

    def _make_template(self, slug, has_admin_gui):
        """建一個最小可用的 template。

        helm_repo_url 是 required（DB NOT NULL），OCI chart 慣例給空字串——對齊真實
        seed 形態，不給會在 create 當下 NotNullViolation。
        """
        return self.env['woow_paas_platform.cloud_app_template'].create({
            'name': f'Payload contract {slug}',
            'slug': slug,
            'category': 'mcp',
            'helm_repo_url': '',
            'helm_chart_name': f'oci://example/{slug}',
            'helm_chart_version': '0.1.0',
            # mcp_enabled 與 has_admin_gui 正交，這裡固定 False 以免混淆判準
            # （#357 根因 C01：用 mcp_enabled 當閘門會同時錯放與錯擋）。
            'mcp_enabled': False,
            'has_admin_gui': has_admin_gui,
        })

    def _make_service(self, template):
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': f'svc-{template.slug}',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': f'{template.slug}-test',
            'helm_values': json.dumps({}),
        })

    def test_payload_mirrors_true_template(self):
        """has_admin_gui=True 的模板 → payload 帶 True。

        slug 刻意取 ``hermes-agent``：它**不在**舊 slug 白名單裡，正是 #460 量化出
        「後端放行、前端按不到」的那一族。
        """
        template = self._make_template('hermes-agent', True)
        service = self._make_service(template)

        payload = self.controller._format_service(service)

        self.assertIn(
            'has_admin_gui', payload['template'],
            'service payload 的 template 子物件必須帶 has_admin_gui'
            '——它是前端能力資訊的唯一入口（#460 S1）')
        self.assertTrue(
            payload['template']['has_admin_gui'],
            'has_admin_gui=True 的模板，payload 必須回 True（鏡射欄位，'
            '不得用 slug 白名單推導——hermes-agent 不在舊白名單內）')

    def test_payload_mirrors_false_template(self):
        """has_admin_gui=False 的模板 → payload 帶 False。

        slug 刻意取 ``mcp-odoo``：它**在**舊 slug 白名單裡。若 payload 還在用名單
        推導，這支會回 True 而炸——這就是「真的改讀欄位」的證據。
        """
        template = self._make_template('mcp-odoo', False)
        service = self._make_service(template)

        payload = self.controller._format_service(service)

        self.assertIn(
            'has_admin_gui', payload['template'],
            'service payload 的 template 子物件必須帶 has_admin_gui')
        self.assertFalse(
            payload['template']['has_admin_gui'],
            'has_admin_gui=False 的模板，payload 必須回 False（即使 slug 在舊'
            '白名單內）——否則前端會長出按了必失敗的按鈕')

    def test_payload_field_is_boolean(self):
        """欄位型別必須是 bool：前端做 fail-closed 判斷（undefined 視同 False），
        payload 端不得回 None／字串等會讓 truthiness 漂移的值。"""
        for slug, expected in (('hermes-agent', True), ('mcp-odoo', False)):
            with self.subTest(slug=slug):
                service = self._make_service(self._make_template(slug, expected))
                value = self.controller._format_service(service)['template']['has_admin_gui']
                self.assertIsInstance(
                    value, bool,
                    f'{slug}: has_admin_gui 必須序列化為 bool，實得 {type(value)}')
                self.assertEqual(value, expected)

    def test_detail_payload_also_carries_field(self):
        """詳情頁走 include_details=True 這條路徑（服務詳情 API）——按鈕就長在這頁，
        這條路徑漏欄位等於 fix 不可達。"""
        service = self._make_service(self._make_template('mcp-k8s', True))
        # #991（#1300）：detail 分支依呼叫者 `request.env.user.share` 決定 helm_values
        # 的 DTO，未綁 request 會 RuntimeError。真實會在詳情頁按 Reset 的是 workspace
        # admin——harness 以一位 internal admin 成員的身分呼叫；受測 service 與斷言不變。
        member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': '462 detail viewer',
            'login': 'admin-gui-462-detail-viewer',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': member.id, 'role': 'admin'})

        with patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                   new=SimpleNamespace(env=self.env(user=member))):
            payload = self.controller._format_service(service, include_details=True)

        self.assertTrue(
            payload['template'].get('has_admin_gui'),
            '服務詳情 payload（include_details=True）同樣必須帶 has_admin_gui=True')

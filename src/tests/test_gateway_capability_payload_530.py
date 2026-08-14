# -*- coding: utf-8 -*-
"""#530 gateway 能力欄位的交付判準與 payload 合約。

本檔守兩件事，兩件都是「錯了不會有人發現」的形態：

1. **交付判準從 seed 推導、不是平台側的 slug 對照表**
   （``CloudAppTemplate._gateway_external_path``）。原本平台維護一份
   ``{'hermes-agent': '/gateway'}``，與 spec 的「不得新建 slug allowlist」字面
   相反，而且路徑有兩份（chart 一份、平台一份）＝**兩邊各自看起來都對**，只有
   租戶會拿到 404。

2. **service payload 的 ``has_gateway`` 是交付判準的鏡射**
   （``service._gateway_public_url()`` 非空），前端文案的唯一資料入口。
   形狀比照 ``test_service_payload_has_admin_gui_462.py``（#460 S1）。

鑑別力設計（fixture 刻意把 slug／has_admin_gui／gateway 三者對調成「反直覺」組合）：

  * A：slug 是 ``mcp-odoo``（**不是** hermes-agent）但 seed 有 gateway → 必須 True。
    若有人把 slug 對照表搬回來，這支會回 False。
  * B：slug 是 ``hermes-agent``、``has_admin_gui=True``，但 seed **沒有** gateway
    → 必須 False。若有人拿 ``has_admin_gui`` 當交付判準（＝ #530 review 抓到的
    原缺陷：has_admin_gui=True 的模板裡只有 hermes-agent 真有 gateway，其餘的
    租戶會被告知一件永遠不會發生的事），這支會回 True。
  * C：seed 有 gateway 但服務沒有 subdomain → False（判準是**完整網址**組不組得
    出來，不是「chart 有沒有這個鍵」）。
  * D：``has_admin_gui=False`` 但 seed 有 gateway → True（兩個閘門正交：鑄造閘門
    是 has_admin_gui、交付閘門是 gateway 網址）。

A 與 B 必須同時綠，才證明判準既不是 slug 也不是 has_admin_gui。
"""
import json

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision


class TestGatewayCapabilityPayload530(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'gateway capability workspace',
        })
        # subdomain 有平台級 UNIQUE 約束：同一支測試建多台服務時共用字面量會在
        # create 當下炸 unique violation（而不是在斷言上），錯誤看起來與被測邏輯無關。
        self._subdomain_seq = 0

    def _next_subdomain(self):
        self._subdomain_seq += 1
        return 'svc-gw-530-%d' % self._subdomain_seq

    def _make_template(self, slug, has_admin_gui=True, gateway_path='/gateway',
                       raw_values=None):
        """最小可用 template；``gateway_path`` 為 None 時 seed 不含 gateway 區塊。

        helm_repo_url 是 required（DB NOT NULL），OCI chart 慣例給空字串——對齊真實
        seed 形態，不給會在 create 當下 NotNullViolation。
        """
        if raw_values is None:
            values = {'image': {'repository': 'example/app', 'tag': 'v1'}}
            if gateway_path is not None:
                values['gateway'] = {'externalPath': gateway_path}
            raw_values = json.dumps(values)
        return self.env['woow_paas_platform.cloud_app_template'].create({
            'name': f'Gateway contract {slug}',
            'slug': slug,
            'category': 'mcp',
            'helm_repo_url': '',
            'helm_chart_name': f'oci://example/{slug}',
            'helm_chart_version': '0.1.0',
            'mcp_enabled': False,
            'has_admin_gui': has_admin_gui,
            'helm_default_values': raw_values,
        })

    def _make_service(self, template, subdomain=None):
        if subdomain is None:
            subdomain = self._next_subdomain()
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': f'svc-{template.slug}',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'subdomain': subdomain,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': f'{template.slug}-test',
            'helm_values': template.helm_default_values,
        })

    # ── 交付判準的事實源：seed，不是 slug 對照表 ─────────────────────────────
    def test_external_path_read_from_seed(self):
        """``_gateway_external_path`` 回 seed 的 gateway.externalPath 原值。"""
        template = self._make_template('some-other-chart', gateway_path='/gw-530')
        self.assertEqual(template._gateway_external_path(), '/gw-530')

    def test_external_path_empty_without_gateway_block(self):
        """seed 沒有 gateway 區塊 → ''（這個 chart 沒有對外 gateway）。"""
        template = self._make_template('no-gateway', gateway_path=None)
        self.assertEqual(template._gateway_external_path(), '')

    def test_external_path_fail_closed_on_bad_shapes(self):
        """malformed／非預期形態一律 ''——寧可不交付也不交一條死鏈。

        交一條 ``https://x.woowtech.iogateway`` 比不交更糟：它看起來像產品保證，
        而租戶會拿它去 debug 一個根本不存在的端點。
        """
        cases = [
            ('malformed json', '{not json'),
            ('欄位空字串', ''),
            ('gateway 非 dict', json.dumps({'gateway': '/gateway'})),
            ('externalPath 非字串', json.dumps({'gateway': {'externalPath': 8642}})),
            ('externalPath 空字串', json.dumps({'gateway': {'externalPath': ''}})),
            ('缺前導斜線', json.dumps({'gateway': {'externalPath': 'gateway'}})),
        ]
        for index, (label, raw) in enumerate(cases):
            with self.subTest(case=label):
                template = self._make_template(f'bad-shape-{index}', raw_values=raw)
                self.assertEqual(
                    template._gateway_external_path(), '',
                    f'{label}：非預期形態必須 fail-closed 回空字串')

    def test_public_url_follows_seed_not_slug(self):
        """服務的 gateway 網址＝公開根位址 ＋ **seed 的**子路徑。

        slug 刻意取 ``mcp-odoo``：它在原本的平台側對照表裡沒有 gateway。若對照表
        被搬回來，這支會回 ''。
        """
        service = self._make_service(
            self._make_template('mcp-odoo', gateway_path='/gw-530'),
            subdomain='svc-follows-seed')
        self.assertEqual(
            service._gateway_public_url(),
            'https://svc-follows-seed.woowtech.io/gw-530',
            'gateway 網址的子路徑必須來自 seed，不得由 slug 對照表決定')

    # ── payload 合約 ────────────────────────────────────────────────────────
    def test_payload_true_for_non_hermes_slug_with_gateway(self):
        """A：slug 不是 hermes-agent，但 seed 有 gateway → payload True。"""
        service = self._make_service(self._make_template('mcp-odoo'))
        payload = self.controller._format_service(service)
        self.assertIn(
            'has_gateway', payload,
            'service payload 必須帶 has_gateway——它是前端 gateway 文案的唯一入口')
        self.assertTrue(
            payload['has_gateway'],
            'seed 有 gateway.externalPath 就必須回 True（不得用 slug 推導）')

    def test_payload_false_for_admin_gui_template_without_gateway(self):
        """B：has_admin_gui=True 但沒有 gateway → payload False。

        這支是原缺陷的雙生：前端文案原本判 ``hasAdminGui``，而 has_admin_gui=True
        的 8 個模板（mcp-odoo／mcp-odoo-manage／mcp-n8n／mcp-k8s／mcp-vk／
        mcp-opendesign／open-design／hermes-agent）中只有 hermes-agent 真的有
        gateway ⇒ 其餘的租戶被告知「Reset 會一併換發 Gateway API Key」，
        而那件事永遠不會發生。
        """
        template = self._make_template('hermes-agent', has_admin_gui=True,
                                       gateway_path=None)
        payload = self.controller._format_service(self._make_service(template))
        self.assertIn('has_gateway', payload)
        self.assertFalse(
            payload['has_gateway'],
            'has_admin_gui=True 但沒有 gateway 的模板必須回 False'
            '——用 has_admin_gui 當交付判準會誤報 7 個模板')

    def test_payload_false_without_subdomain(self):
        """C：沒有 subdomain 就組不出完整網址 → False。"""
        service = self._make_service(self._make_template('mcp-odoo'), subdomain=False)
        self.assertFalse(
            self.controller._format_service(service)['has_gateway'],
            '無 subdomain＝沒有對外根位址，交付判準必須回 False')

    def test_payload_orthogonal_to_admin_gui(self):
        """D：has_admin_gui=False 但有 gateway → True（兩個閘門正交）。"""
        template = self._make_template('open-design', has_admin_gui=False)
        payload = self.controller._format_service(self._make_service(template))
        self.assertTrue(
            payload['has_gateway'],
            'gateway 交付判準與 has_admin_gui（憑證鑄造閘門）是兩件事，不得共用')

    def test_payload_field_is_boolean(self):
        """型別必須是 bool：前端 fail-closed 比較 ``=== true``，回 None／字串會漂移。"""
        for gateway_path, expected in (('/gateway', True), (None, False)):
            with self.subTest(gateway_path=gateway_path):
                service = self._make_service(self._make_template(
                    f'bool-{expected}', gateway_path=gateway_path))
                value = self.controller._format_service(service)['has_gateway']
                self.assertIsInstance(
                    value, bool,
                    f'has_gateway 必須序列化為 bool，實得 {type(value)}')
                self.assertEqual(value, expected)

    def test_detail_payload_also_carries_field(self):
        """詳情頁走 include_details=True——文案就長在這頁，這條漏欄位等於 fix 不可達。"""
        service = self._make_service(self._make_template('mcp-k8s'))
        self.assertTrue(
            self.controller._format_service(service, include_details=True)
            .get('has_gateway'),
            '服務詳情 payload（include_details=True）同樣必須帶 has_gateway')

    # ── 前端 gate ⟺ 後端實際交付 ────────────────────────────────────────────
    def test_payload_matches_actual_credential_delivery(self):
        """跨層 lock-step：``has_gateway`` 與 ``_gateway_credentials`` 同進退。

        payload 欄位是**文案**的判準、``_gateway_credentials`` 是**實際交付**的
        判準。兩者漂移的形狀是「畫面說會換發、回應裡沒有」（或反過來），而兩邊
        各自都不會報錯。這條把它們綁在同一組 fixture 上一起驗。

        每個 case 都先鑄一次 secret，並**斷言鑄造閘門的結果**（chart 有 gateway ⟺
        secret 有 gateway key）：少了這行，「不交付」在完全沒實作鑄造的世界也是綠的
        ——四個 case 會全部因為「沒有 key」而回 {}，兩邊恰好都是 False 而假綠。
        case A／D 因此同時是本檔對 finding 3 鑄造閘門的陽性對照。

        ⚠ #672 起判準**逐 key 讀**，不是 `bool(_gateway_credentials(...))`：那個
        payload 現在也可能只帶 `dashboard_url`（dashboard 有自己的閘門），dict 非空
        於是不再等價於「gateway 有交付」。case B 的 slug 正好是 hermes-agent（有
        dashboard、無 gateway），用非空當代理會讓它讀成「gateway 交付了」——一個
        指錯東西的綠／紅。逐 key 讀同時比原版**更嚴**：兩把憑證各自被斷言，而不是
        「至少有一個東西在」。
        """
        cases = [
            ('A 非 hermes slug 有 gateway', 'mcp-odoo', True, '/gateway', None, True),
            ('B has_admin_gui 但無 gateway', 'hermes-agent', True, None, None, False),
            ('C 有 gateway 但無 subdomain', 'mcp-odoo', True, '/gateway', False, False),
            ('D 無 admin gui 但有 gateway', 'open-design', False, '/gateway', None, True),
        ]
        for label, slug, admin_gui, path, subdomain, expected in cases:
            with self.subTest(case=label):
                service = self._make_service(
                    self._make_template(slug, has_admin_gui=admin_gui,
                                        gateway_path=path),
                    subdomain=subdomain,
                )
                service._set_admin_password_secret('pw-530-lockstep')
                secret = service._get_secret_record()
                self.assertTrue(secret, f'{label}：前提不成立——secret 沒建起來')
                self.assertEqual(
                    bool(secret.gateway_api_key), path is not None,
                    f'{label}：鑄造閘門結果不符——chart 有 gateway ⟺ secret 有 key'
                    '（沒有這行，「不交付」在完全沒實作鑄造的世界也是綠的）')

                payload_flag = self.controller._format_service(service)['has_gateway']
                creds = PaasController._gateway_credentials(service)

                self.assertEqual(payload_flag, expected, f'{label}: payload 判準')
                self.assertEqual(
                    bool(creds.get('gateway_url')), expected,
                    f'{label}: 實際交付判準（_gateway_credentials 的 gateway_url）')
                self.assertEqual(
                    'gateway_api_key' in creds, expected,
                    f'{label}: 兩把 gateway 憑證必須同進退——只交網址不交 key（或反過來）'
                    '會讓租戶填得出一個永遠通不過認證的連線')

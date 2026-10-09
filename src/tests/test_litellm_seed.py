# -*- coding: utf-8 -*-
"""AI 模型閘道（LiteLLM）上架 seed 的守線斷言（2026-10-04 公開上架）。

契約來源：woow-paas-charts charts/litellm 0.2.5（0.2.5＝名稱空白／重複帳號修正；0.2.4＝304 修正；0.2.3＝多個 ChatGPT 帳號；0.1.0＝#123；0.2.1＝ChatGPT 訂閱登入頁 /woow/chatgpt 在 LiteLLM 內；0.2.2＝後台 Add Credential 子畫面）——litellm-non_root 1.103.3（uid 65534）
＋內附 PostgreSQL（平台 paas-postgres 17.11），唯一入口 ``<release>-litellm:4000``、只有一顆 PVC。
UI 帳號＝``auth.adminUser``（預設 admin，可改）、密碼＝``config.sensitive.admin_password``；master key＝平台 #530 gateway
key（``auth.apiServerKey`` → ``LITELLM_MASTER_KEY``，chart 原樣使用、不需 ``sk-`` 前綴）。

核心守線：
1. **helm_secret、帳號與密碼都可自設**（admin_username_helm_path＝``auth.adminUser``，一次性視窗顯示帳號——
   .207 正式站「沒有帳號」缺陷）；workload 是 Deployment ⇒ reset 的 rollout restart 會套到。改帳號的前提是
   chart 0.1.1：UI_USERNAME 由 Secret 帶入，pod template 不變（0.1.0 是 env 字面值，改帳號會讓 reset 的
   ``helm upgrade --atomic --timeout 15s`` 等新 pod → 逾時回退）。
2. **master key 走 gateway 通道**：seed 帶 ``gateway.externalPath=/v1`` ⇒ 平台鑄 key、一次性交付
   ``https://<sub>/v1``、Reset 一起換。缺了它 master key 會落到 chart 自產值、租戶永遠拿不到。
3. **一次性視窗的提示是 LiteLLM 的**（#1412 P3）：不是 hermes-agent 的「填進 MCP 連線設定」。
   提示是 record 的資料值 ⇒ seed 直接寫中文、⛔ 不進 .po
   （docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md，Eugene 定案通則）。
4. **兩顆 image 都雙釘、都來自 JCR**（litellm 與 PG）。
5. **規格對帳**：1 vCPU／2 GB／10 GB——template min_*、plan cs_*、唯一一顆 PVC 三處一致。
6. **租戶旋鈕只有應用層兩個**；provider key 刻意**不是**旋鈕（只在 LiteLLM 後台設、存租戶 DB）。
7. **上架即雙釘**：#1413 以隱藏卡片進 seed（隨 .206 上正式站），整合實測通過後 2026-10-04 公開上架；
   ``test_hidden_until_listed`` 從此是實際生效的守線（``is_hidden=False`` ⟹ litellm 與 PG 都要雙釘）。
"""
import json
import os
import re

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

JCR = 'jcr-prod.woowtech.io/woow-paas-docker-local/'
LITELLM_CHART = 'oci://%slitellm' % JCR
PINNED_TAG_RE = r'^\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'
# paas-postgres 的 tag 是「上游版本-建置時間」（同 woow-odoo chart 的 pin）。
PG_PINNED_TAG_RE = r'^\d+\.\d+-\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'
KEY_HINT = ('這是 LiteLLM 的 master key（等同管理員金鑰）。應用程式請一律在 LiteLLM 後台建立'
            '虛擬金鑰使用，master key 只給管理腳本。')
URL_HINT = ('OpenAI 相容 API 的 Base URL。同工作區其他服務請改用內部網址 '
            'http://<服務 release>-litellm:4000/v1。')


@tagged('post_install', '-at_install')
class TestLitellmSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_litellm')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def _optional(self):
        return {s['key']: s for s in self._tpl()._parse_helm_value_specs()['optional']}

    def _po_entries(self):
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        return list(PoFileReader(po))

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'litellm')
        self.assertEqual(tpl.name, 'AI 模型閘道（LiteLLM）')
        self.assertEqual(tpl.category, 'ai_llm')
        self.assertEqual(tpl.default_port, 4000, 'LiteLLM 本身就是唯一入口（UI 與 API 同埠）')
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertFalse(tpl.mcp_enabled)
        self.assertEqual(tpl.workload_type, 'container')

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, LITELLM_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.2.5',
                         'helm_chart_version 留空會被供給閘門靜默擋在購買前（#552）；0.1.1 起 UI_USERNAME '
                         '走 Secret（admin_username_helm_path 的前提）；0.2.1＝ChatGPT 訂閱登入頁 /woow/chatgpt；'
                         '0.2.2＝後台 Add Credential 選 ChatGPT 跳出登入方式子畫面；0.2.3＝多個 ChatGPT 帳號；'
                         '0.2.4＝舊快取後台頁面也載入 bridge；0.2.5＝名稱空白／舊憑證／重複帳號修正')

    def test_chatgpt_login_inside_litellm(self):
        """chart 0.2.1 起 ChatGPT 訂閱登入在 LiteLLM 自己的 /woow/chatgpt（用 LiteLLM 管理員登入）；
        平台服務頁不再有登入卡片。商品說明是資料值（直接寫中文、不進 .po），購買前就要講清楚入口與風險。"""
        desc = self._tpl().full_description
        for needle, why in (
                ('ChatGPT 訂閱（選用）', '商品說明要讓租戶知道有這個選用功能'),
                ('/woow/chatgpt', '入口在 LiteLLM 服務網址的 /woow/chatgpt，不在平台服務頁'),
                ('LLM Credentials → Add Credential', '0.2.2 起也能從 LiteLLM 後台的 Add Credential 連結'),
                ('瀏覽器登入或裝置代碼', '兩種登入方式（瀏覽器登入不需改 ChatGPT 設定）'),
                ('可連多個 ChatGPT 帳號', '0.2.3 起一台 LiteLLM 可連多個帳號（每個憑證一個）'),
                ('LiteLLM 管理員登入', '那一頁用 LiteLLM 管理員帳號登入'),
                ('非 OpenAI 官方', '條款風險要講在購買前'),
                ('帳號風險自負', '帳號風險由租戶承擔'),
        ):
            self.assertIn(needle, desc, why)
        self.assertNotIn('服務頁可用 ChatGPT', desc, '平台服務頁的 ChatGPT 卡片已移除，說明不可再指向它')

    def test_service_port_lockstep(self):
        self.assertEqual(self._values()['service']['port'], self._tpl().default_port)

    def test_credentials_helm_secret_and_settable(self):
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui)
        self.assertEqual(tpl.admin_password_carrier, 'helm_secret')
        self.assertTrue(tpl.admin_reset_supported)
        self.assertTrue(tpl.admin_password_settable)
        self.assertTrue(tpl.admin_credentials_settable)
        self.assertEqual(tpl.admin_username_helm_path, 'auth.adminUser',
                         'UI 登入要帳號：沒有帳號路徑時一次性視窗會寫「沒有帳號」（.207 缺陷）')
        self.assertEqual(self._values()['auth']['adminUser'], 'admin',
                         '帳號預設值要在範本裡（_admin_username 讀不到時不猜 chart 預設）')

    def test_images_from_jcr_and_double_pinned(self):
        v = self._values()
        self.assertEqual(v['image']['repository'], JCR + 'litellm')
        self.assertRegex(v['image']['tag'], PINNED_TAG_RE)
        pg = v['postgresql']['image']
        self.assertEqual(pg['repository'], JCR + 'paas-postgres')
        self.assertRegex(pg['tag'], PG_PINNED_TAG_RE,
                         'PG 也要 tag@sha256 雙釘（chart 的 imageRef 遇到 @ 就不再附加 digest）')

    def test_hidden_until_listed(self):
        """上架 ⟹ litellm 與 PG 兩顆 image 雙釘（2026-10-04 公開上架；之後若翻回隱藏，這條仍要綠）。"""
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.conceal_instances, 'hidden-only（曝光層），實例層不設')
        if not tpl.is_hidden:
            v = self._values()
            self.assertRegex(v['image']['tag'], PINNED_TAG_RE)
            self.assertRegex(v['postgresql']['image']['tag'], PG_PINNED_TAG_RE)

    def test_master_key_rides_the_gateway_channel(self):
        """seed 的 gateway.externalPath 是 #530 三個閘門（鑄造／供給／交付）的唯一事實源。"""
        tpl = self._tpl()
        self.assertEqual(tpl._gateway_external_path(), '/v1',
                         'LiteLLM 的 OpenAI 相容 API 在 /v1；缺了它 master key 不會交付給租戶')
        self.assertFalse(tpl.has_public_tunnel_address)

    def test_gateway_hints_are_litellm_specific(self):
        tpl = self._tpl()
        self.assertEqual(tpl.gateway_api_key_hint, KEY_HINT)
        self.assertEqual(tpl.gateway_url_hint, URL_HINT)
        self.assertNotIn('MCP', tpl.gateway_api_key_hint + tpl.gateway_url_hint,
                         '不是 hermes-agent 的 MCP 連線設定')

    def test_gateway_hints_are_data_values_not_in_po(self):
        """兩格提示是 record 的資料值 ⇒ seed 直接寫中文，⛔ 不進 .po（Eugene 定案通則）。"""
        fields = {'woow_paas_platform.cloud_app_template,gateway_api_key_hint',
                  'woow_paas_platform.cloud_app_template,gateway_url_hint'}
        entries = [e for e in self._po_entries()
                   if e['type'] == 'model' and e['imd_name'] == 'cloud_app_litellm'
                   and e['name'] in fields]
        self.assertEqual(entries, [], '資料值不走 .po：改 seed 的中文原文即可')

    def test_one_time_credentials_carry_the_hints(self):
        """建立／Reset 共用的 _gateway_credentials：key、/v1 網址與 LiteLLM 提示一起交付。"""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'litellm-seed.invalid')
        tpl = self._tpl()
        workspace = self.env['woow_paas_platform.workspace'].create({'name': 'litellm seed WS'})
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-litellm-seed',
            'workspace_id': workspace.id,
            'template_id': tpl.id,
            'state': 'running',
            'subdomain': 'svc-litellm-seed',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-litellm-seed',
            'helm_values': tpl.helm_default_values,
        })
        service._set_admin_password_secret('pw-litellm-seed')
        creds = PaasController._gateway_credentials(service)
        self.assertTrue(creds.get('gateway_api_key'), 'master key 要真的交付')
        self.assertEqual(creds.get('gateway_url'), 'https://svc-litellm-seed.litellm-seed.invalid/v1')
        self.assertEqual(creds.get('gateway_api_key_hint'), KEY_HINT)
        self.assertEqual(creds.get('gateway_url_hint'), URL_HINT)
        self.assertEqual(service._admin_username(), 'admin',
                         '一次性視窗的「使用者名」格（admin_username 非空才會出現）')

    def test_spec_matches_plan_and_pvc(self):
        tpl = self._tpl()
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 2.0)
        self.assertEqual(tpl.min_storage_gb, 10)
        pvc = int(self._values()['persistence']['size'].rstrip('Gi'))
        self.assertEqual(pvc, tpl.min_storage_gb, '唯一一顆 PVC＝min_storage_gb（quota 對帳）')
        plans = tpl.sudo().subscription_plan_ids
        self.assertTrue(plans)
        for plan in plans:
            self.assertEqual(plan.cs_vcpu, tpl.min_vcpu, plan.name)
            self.assertEqual(plan.cs_ram_gb, tpl.min_ram_gb, plan.name)
            self.assertEqual(plan.cs_storage_gb, pvc, plan.name)

    def test_plans_linked_and_priced(self):
        tpl = self._tpl()
        monthly = self.env.ref('woow_paas_platform.plan_litellm_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_litellm_yearly')
        self.assertEqual(set(tpl.sudo().subscription_plan_ids.ids), {monthly.id, yearly.id})
        self.assertEqual(monthly.sh_plan_price, 1500)
        self.assertEqual(yearly.sh_plan_price, 15000)
        self.assertEqual(yearly.sh_plan_price, monthly.sh_plan_price * 10, '年繳＝10 個月')
        for plan in (monthly, yearly):
            self.assertEqual(plan.service_type, 'cloud_service')
            self.assertTrue(plan.sh_trial)
            self.assertEqual(plan.sh_trial_duration, 7)
            self.assertEqual(plan.sh_trial_unit, 'day')
        for product, plan in (
            (self.env.ref('woow_paas_platform.product_litellm_monthly'), monthly),
            (self.env.ref('woow_paas_platform.product_litellm_yearly'), yearly),
        ):
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, plan.sh_plan_price)
            self.assertTrue(product.sh_product_subscribe)
            self.assertFalse(product.taxes_id, 'seed 明寫 taxes_id=[]，不吃 company 預設')

    def test_tenant_knobs_are_app_level_only(self):
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs['required'], [], '開通不需要租戶先填任何東西')
        by_key = self._optional()
        self.assertEqual(set(by_key), {'litellm.spendLogRetention', 'litellm.apiDocs'},
                         'provider key 只在 LiteLLM 後台設（PLAN Q9），不是平台旋鈕')
        retention = by_key['litellm.spendLogRetention']
        self.assertEqual(retention['type'], 'select')
        options = {o['value'] if isinstance(o, dict) else o for o in retention['options']}
        self.assertEqual(options, {'7d', '30d', '90d', '180d', '365d'})
        self.assertEqual(retention['default'], '90d', '與 chart 預設一致')
        self.assertEqual(by_key['litellm.apiDocs']['type'], 'boolean')
        self.assertIs(by_key['litellm.apiDocs']['default'], True)
        for key in by_key:
            self.assertFalse(re.match(r'^(resources|persistence|auth|config)\b', key),
                             '方案維度與憑證都不是租戶旋鈕')

    def test_whitelist_filters_credentials_and_resources(self):
        """specs 的 key 同時是白名單：master key／salt key／方案維度送進來要被擋掉。"""
        allowed, rejected = self._tpl()._filter_allowed_helm_values({
            'litellm.apiDocs': False,
            'auth.saltKey': 'sk-evil',
            'auth.apiServerKey': 'sk-evil',
            'auth.adminUser': 'someone',
            'resources.limits.memory': '64Gi',
        })
        self.assertEqual(allowed, {'litellm.apiDocs': False})
        self.assertEqual(sorted(rejected), [
            'auth.adminUser', 'auth.apiServerKey', 'auth.saltKey', 'resources.limits.memory'],
            '帳號只能經「管理員帳密」改（會驗格式、一起換密碼），不是設定表單的旋鈕')

    def test_description_states_the_api_caveats(self):
        desc = self._tpl().full_description
        for needle, why in (
                ('沒有任何模型', '預設不內建模型（PLAN Q9）'),
                ('虛擬金鑰', '應用程式用虛擬金鑰、master key 只給管理腳本'),
                ('100 MB', '經公開網址單次請求上限'),
                ('串流', '長回應要串流，否則被入口中斷'),
                ('-litellm:4000/v1', '同工作區的內部網址'),
                ('Access 保護', '不要開 CF Access（會擋 API 用戶端）'),
                ('master key 會一起換新', 'Reset 一起換（PLAN Q8）'),
                ('約中斷 2 分鐘', '重設帳密／master key 會重啟服務（整合實測斷線約 2 分 15 秒）'),
                ('帳號預設 admin', '帳號可改（admin_username_helm_path＝auth.adminUser）'),
        ):
            self.assertIn(needle, desc, why)

    def test_no_public_url_injection(self):
        """chart 的 litellm.publicUrl 選配：UI redirect 由請求推導，平台不注入（不加 injector）。"""
        CloudService = self.env['woow_paas_platform.cloud_service']
        self.assertEqual(
            CloudService._inject_allowed_origins({}, 'litellm', 'https://paas-llm.example'), {})

# -*- coding: utf-8 -*-
"""MCP：AI 模型閘道（mcp-litellm）seed 的守線斷言（2026-10-05 以隱藏範本進 seed，10-07 公開上架）。

契約來源：Gitea WOOWTECH/woow-mcp-server ``apps/litellm``（#33，main 0687392f）README §PaaS seed 契約；
chart＝woow-paas-charts ``charts/mcp-server`` 0.1.7（平台共用；2026-10-08 起，#1530 非 root）。家族不變式（has_admin_gui、requiredFields
REPLACE、envPrefix 用 camelCase、規格對帳、方案綁定）由
``test_mcp_server_template_seed.TestMcpLitellmTemplateSeed`` 守；同工作區網址注入（真範本）由
``test_workspace_service_url_injection`` 守。本檔守本 app 專屬的契約：

1. **釘版**：chart mcp-server 0.1.7（OCI）；image litellm-mcp-admin 來自 JCR、雙釘，而且釘的就是 main CI
   推的那一顆（現行＝woow-mcp-server #50 合併 1d16391d、run 18319）——換 image 時這條會紅，逼下一個人覆核啟動
   契約（#708 教訓）。
2. **啟動契約**：``litellm-mcp-server``、args 只有 ``--port 8000``（launcher 硬綁 127.0.0.1，seed 不給
   host）、``mcp_server.port``＝args 的 port（core proxy 轉發目標，不一致一律 502）。
3. **env_prefix 兩處同值**：chart 的 configmap 以頂層 ``envPrefix`` 蓋掉巢狀那格；connection 鍵
   base_url／master_key 靠它變成 ``LITELLM_MCP_BASE_URL``／``LITELLM_MCP_MASTER_KEY``。
4. **出廠唯讀**：``LITELLM_MCP_READONLY="1"``，seed 不帶任何放寬 gate 的 env；平台設定表單不開任何欄位
   （解鎖在服務自己的 admin Settings 頁）。
5. **不帶秘密、不帶網址**：``config.sensitive`` 空、``nonSensitive`` 沒有 connection——網址由平台組裝時
   注入（同工作區 LiteLLM），金鑰由租戶自己貼；本範本也沒有 gateway 通道，平台不鑄／不交付任何 key。
6. **方案與上架**：月 3,000／年 24,000、試用 7 天、免稅、雙向綁定；上架只翻曝光層（is_hidden），
   實例層從未設。
7. **說明**：唯讀／解鎖／建議 viewer key／預填網址只在第一次開通／``/mcp`` 不加結尾斜線；資料值直接寫
   中文、⛔ 不進 .po（docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md）。
8. **上架即雙釘**：#1487 以隱藏卡片進 seed（隨 .210 上正式站），沙盒實測（woow-mcp-server #33）通過後
   2026-10-07 公開上架；``test_hidden_until_listed`` 從此是實際生效的守線（``is_hidden=False`` ⟹ image
   要 git-sha@sha256 雙釘）。
"""
import json
import os

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

JCR = 'jcr-prod.woowtech.io/woow-paas-docker-local/'
MCP_SERVER_CHART = 'oci://%smcp-server' % JCR
IMAGE_REPOSITORY = JCR + 'litellm-mcp-admin'
# 2026-10-12 覆核 main #50 的真發布 image：1d16391d，run 18319 job 20064。
# 在實際 digest 的離線 probe 驗 CLI、LaunchPolicy、127.0.0.1／port 8000、出廠唯讀；
# main.py／launcher.py／seed_defaults.py 三個 hash 與該 main SHA 一致。
# 1ec1bb0b..1d16391d 的 apps/litellm 及 uv.lock 無內容差異，既有啟動／唯讀檢查仍成立。
EXPECT_IMAGE_TAG = (
    '1d16391d1a62fdda2b8de6bd16491c22330b127f'
    '@sha256:1cdd351ddc781170e48e4f9b37e6d60585b81623335b051a62a043512f1a101b')
GIT_SHA_PINNED_TAG_RE = r'^[0-9a-f]{40}@sha256:[0-9a-f]{64}$'
ENV_PREFIX = 'LITELLM_MCP_'
READONLY_ENV = 'LITELLM_MCP_READONLY'


@tagged('post_install', '-at_install')
class TestMcpLitellmSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_mcp_litellm')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def _mcp_server(self):
        return self._values()['config']['nonSensitive']['mcp_server']

    def _po_entries(self):
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        return list(PoFileReader(po))

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'mcp-litellm')
        self.assertEqual(tpl.name, 'MCP：AI 模型閘道（LiteLLM）')
        self.assertEqual(tpl.category, 'mcp')
        self.assertEqual(tpl.workload_type, 'container')
        self.assertEqual(tpl.default_port, 8080, 'admin GUI＋/private_<token>/mcp 同一個 8080')
        self.assertEqual(self._values()['service']['port'], tpl.default_port)
        self.assertEqual(self._values()['app'], 'litellm')

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, MCP_SERVER_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.7',
                         'helm_chart_version 留空會被供給閘門靜默擋在購買前（#552）；0.1.7＝家族現行版'
                         '（0.1.6＝#1441 egress 不再全放、0.1.7＝#1530 非 root）')

    def test_image_from_jcr_and_double_pinned(self):
        image = self._values()['image']
        self.assertEqual(image['repository'], IMAGE_REPOSITORY)
        self.assertRegex(image['tag'], GIT_SHA_PINNED_TAG_RE, 'git-sha@sha256 雙釘')

    def test_image_repin_must_recheck_launch_spec(self):
        """#708 lockstep：image 一被重釘這條就紅，逼人先覆核啟動契約再更新常數。

        同一個 monorepo commit 會推出多個 app image（git-sha 相同、digest 不同），全域替換 git-sha
        會造出 tag 新／digest 舊的組合——所以連 digest 一起逐字釘。
        """
        self.assertEqual(
            self._values()['image']['tag'], EXPECT_IMAGE_TAG,
            'image 被重釘了：請先實查新 image 的 apps/litellm（pyproject 的 console script '
            'litellm-mcp-server、launcher 的 port／綁定位址、seed_defaults 的唯讀鍵名），確認本檔'
            'test_launch_contract／test_read_only_by_default 仍成立，最後才更新 EXPECT_IMAGE_TAG。')

    def test_launch_contract(self):
        mcp = self._mcp_server()
        self.assertEqual(mcp['command'], 'litellm-mcp-server')
        self.assertEqual(mcp['args'], ['--port', '8000'],
                         'launcher 固定 Streamable HTTP、硬綁 127.0.0.1；seed 只給 port')
        self.assertNotIn('--host', mcp['args'], '子行程只綁 127.0.0.1（安全邊界），不由 seed 決定')
        self.assertEqual(mcp['port'], 8000)
        self.assertEqual(mcp['args'][mcp['args'].index('--port') + 1], str(mcp['port']),
                         'args 的 port 必須等於 proxy 讀的 mcp_server.port，否則 /mcp 一律 502')

    def test_env_prefix_in_both_places(self):
        """chart configmap：merge(dict mcp_server.env_prefix←頂層 envPrefix, nonSensitive)，頂層勝出。"""
        values = self._values()
        self.assertEqual(values['envPrefix'], ENV_PREFIX)
        self.assertEqual(self._mcp_server()['env_prefix'], ENV_PREFIX,
                         '兩處不同值時以頂層為準——寫成同值，讀 seed 的人才不會被巢狀那格誤導')
        self.assertNotIn('env_prefix', values, 'camelCase：helm 會默默忽略 env_prefix 這個鍵')

    def test_read_only_by_default(self):
        """出廠唯讀：18 支查詢工具；寫入要租戶在 admin Settings 頁自己解鎖。"""
        env = self._mcp_server()['env']
        self.assertEqual(env, {READONLY_ENV: '1'},
                         'seed 只帶唯讀開關且值為 "1"；其他 gate（停用分類／工具）留給租戶在 admin 自己調')
        self.assertIsInstance(env[READONLY_ENV], str, 'env 值要是字串 "1"（子行程讀環境變數）')
        for key in env:
            self.assertTrue(key.startswith(ENV_PREFIX), key)

    def test_no_platform_form_fields(self):
        """平台設定表單一格都不開（同家族）：唯讀開關、金鑰、網址都在服務自己的 admin 介面設定。"""
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs['required'], [], '開通不需要租戶先填任何東西')
        self.assertEqual(specs['optional'], [])

    def test_no_secret_and_no_url_in_seed(self):
        config = self._values()['config']
        self.assertEqual(config['sensitive'], {})
        self.assertNotIn(
            'connection', config['nonSensitive'],
            'base_url 由平台組裝時注入（同工作區 LiteLLM）；seed 寫死網址會蓋過注入、也會漏到別的工作區')
        dumped = json.dumps(self._values())
        for needle in ('master_key', 'sk-', 'base_url', 'litellm.woowtech.io'):
            self.assertNotIn(needle, dumped)

    def test_required_fields_only_admin_password(self):
        """REPLACE 語意：master_key／base_url 是租戶在 admin 自己填的，列進去＝每個新租戶 pod 卡 Init。"""
        config = self._values()['config']
        self.assertTrue(config['requireFields'])
        self.assertEqual(config['requiredFields'], ['admin_password'])

    def test_no_gateway_channel_so_no_key_is_minted_or_delivered(self):
        """金鑰絕不由平台供給（PLAN §10：不跨服務傳秘密）：沒有 gateway.externalPath ⇒ 不鑄、不交付。"""
        tpl = self._tpl()
        self.assertEqual(tpl._gateway_external_path(), '')
        self.assertFalse(tpl.has_public_tunnel_address)
        self.assertEqual(tpl.admin_password_carrier, 'config_json')
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'mcp-litellm-seed.invalid')
        workspace = self.env['woow_paas_platform.workspace'].create({'name': 'mcp-litellm seed WS'})
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-mcpl-seed',
            'workspace_id': workspace.id,
            'template_id': tpl.id,
            'state': 'running',
            'subdomain': 'svc-mcpl-seed',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-mcpl-seed',
            'helm_values': tpl.helm_default_values,
        })
        creds = PaasController._gateway_credentials(service)
        self.assertNotIn('gateway_api_key', creds)
        self.assertNotIn('gateway_url', creds)

    def test_hidden_until_listed(self):
        """上架 ⟹ image 雙釘（2026-10-07 公開上架；之後若翻回隱藏，這條仍要綠）。

        上架只翻曝光層 is_hidden：方案早已掛好、實例層 conceal_instances 從不設。
        """
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.conceal_instances, 'hidden-only（曝光層），實例層不設')
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertTrue(tpl.sudo().subscription_plan_ids, '沒掛 plan 會被 _create_service 擋')
        if not tpl.is_hidden:
            self.assertRegex(self._values()['image']['tag'], GIT_SHA_PINNED_TAG_RE,
                             '上架中的範本 image 一律 git-sha@sha256 雙釘（mutable tag 不可重現）')

    def test_plans_linked_and_priced(self):
        tpl = self._tpl()
        monthly = self.env.ref('woow_paas_platform.plan_mcp_litellm_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_mcp_litellm_yearly')
        self.assertEqual(set(tpl.sudo().subscription_plan_ids.ids), {monthly.id, yearly.id})
        self.assertEqual((monthly.sh_plan_price, monthly.sh_unit), (3000, 'month'))
        self.assertEqual((yearly.sh_plan_price, yearly.sh_unit), (24000, 'year'),
                         '同其他 MCP：年繳＝月繳 ×8')
        for plan in (monthly, yearly):
            self.assertEqual(plan.service_type, 'cloud_service')
            self.assertTrue(plan.sh_trial)
            self.assertEqual(plan.sh_trial_duration, 7)
            self.assertEqual(plan.sh_trial_unit, 'day')
            self.assertFalse(plan.storefront_hidden, '方案本身不藏：上架只翻範本的 is_hidden')
            self.assertEqual(plan.ac_max_access_seats, 0, 'base 方案，無 +1access 變體')
        for product, plan in (
            (self.env.ref('woow_paas_platform.product_mcp_litellm_monthly'), monthly),
            (self.env.ref('woow_paas_platform.product_mcp_litellm_yearly'), yearly),
        ):
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, plan.sh_plan_price)
            self.assertTrue(product.sh_product_subscribe)
            self.assertFalse(product.taxes_id, 'seed 明寫 taxes_id=[]，不吃 company 預設')

    def test_description_states_the_contract(self):
        tpl = self._tpl()
        self.assertIn('出廠唯讀', tpl.description)
        desc = tpl.full_description
        for needle, why in (
                ('出廠唯讀', '預設只有查詢工具'),
                ('18 支查詢工具', '唯讀模式可用的工具數'),
                ('8 支是刪除、封鎖等', '解鎖後才有的破壞性工具'),
                ('Read-only Mode', '解鎖位置（admin Settings 頁）'),
                ('proxy_admin_viewer', '建議貼唯讀管理員的金鑰'),
                ('不要貼 master key', '不建議 master key'),
                ('只在第一次開通時預填', '同工作區網址只在第一次開通帶入（seed-if-absent）'),
                ('不含 /v1', '手填網址的格式'),
                ('/mcp（結尾不要加斜線', '/mcp/ 會被子行程 307 到內部位址'),
                ('Streamable HTTP', '用戶端傳輸方式'),
        ):
            self.assertIn(needle, desc, why)

    def test_marketplace_copy_is_data_not_po(self):
        """範本名稱與說明是 record 的資料值 ⇒ seed 直接寫中文，⛔ 不進 .po（Eugene 定案通則）。"""
        entries = [e for e in self._po_entries()
                   if e['type'] == 'model' and e['imd_name'] == 'cloud_app_mcp_litellm']
        self.assertEqual(entries, [], '資料值不走 .po：改 seed 的中文原文即可')

# -*- coding: utf-8 -*-
"""MCP：Nextcloud（mcp-nextcloud）seed 的守線斷言（以隱藏範本進 seed，沙盒實測後另開 PR 上架）。

契約來源：Gitea WOOWTECH/woow-mcp-server ``apps/nextcloud``（#44，main 9a0fafa2）README §PaaS seed 契約；
chart＝woow-paas-charts ``charts/mcp-server`` 0.1.7（#1530 非 root）。家族不變式（has_admin_gui、requiredFields
REPLACE、envPrefix 用 camelCase、規格對帳、方案綁定）由
``test_mcp_server_template_seed.TestMcpNextcloudTemplateSeed`` 守；同工作區 Nextcloud 公開網址注入（真範本）由
``test_workspace_service_url_injection`` 守。本檔守本 app 專屬的契約：

1. **釘版**：chart mcp-server 0.1.7（OCI；image 以 uid／gid 1000 執行，要非 root 的 chart）；image
   nextcloud-mcp-admin 來自 JCR、雙釘，而且釘的就是 main CI 推的那一顆（現行＝#86 合併 1dcabf91；首次上架是 #44 的 9a0fafa2）——
   換 image 時這條會紅，逼下一個人覆核啟動契約（#708 教訓）。
2. **啟動契約**：``nextcloud-mcp-server``、args 只有 port 8000（launcher 硬綁 127.0.0.1，seed 不給 host；
   app 的 LaunchPolicy 固定 command／args）、``mcp_server.port``＝args 的 port（core proxy 轉發目標，不一致一律 502）。
3. **env_prefix 兩處同值**：chart 的 configmap 以頂層 ``envPrefix`` 蓋掉巢狀那格；connection 鍵
   base_url／username／app_password 靠它變成 ``NEXTCLOUD_MCP_*``。
4. **預設全開由 app 自己做**（負責人 2026-10-25）：seed 不帶 ``mcp_server.env``（LaunchPolicy 的 env_keys
   為空，帶了也被忽略）、不帶 ``tools``（app 的 seed_defaults 第一次啟動時只寫上標記、9 支全開；之前的版本會
   停用 4 支寫入／刪除工具，已開通的服務維持自己的停用清單）；平台設定表單不開任何欄位（逐支停用在服務自己的
   Tools 頁）。
5. **不帶秘密、不帶網址**：``config.sensitive`` 空、``nonSensitive`` 沒有 connection——網址由平台組裝時
   注入（同工作區 Nextcloud 的公開網址），app password 由租戶自己貼；本範本也沒有 gateway 通道，平台不鑄／
   不交付任何 key。
6. **方案與隱藏**：照抄 mcp-litellm——月 3,000／年 24,000、試用 7 天、免稅、雙向綁定；隱藏只在曝光層
   （is_hidden），實例層不設，上架時只翻 is_hidden。
7. **說明**：預設全開／Tools 頁逐支停用／專用帳號＋應用程式密碼（不是登入密碼）／暴力破解防護與停止重試／
   預填網址只在第一次開通／``/mcp`` 不加結尾斜線；資料值直接寫中文、⛔ 不進 .po
   （docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md）。
8. **上架即雙釘**：``test_hidden_until_listed``——2026-10-08 已上架（``is_hidden=False``），image
   必須 git-sha@sha256 雙釘。
"""
import json
import os

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

JCR = 'jcr-prod.woowtech.io/woow-paas-docker-local/'
MCP_SERVER_CHART = 'oci://%smcp-server' % JCR
# woow-paas-charts main 3746cd6（#1530）：runAsNonRoot、uid／gid 1000、保留 fsGroup 1000。publish-charts
# run 16234 已推上 OCI（verify 步驟 OK oci mcp-server:0.1.7）。
# 0.1.8 保留非 root 設定，首次 seed 改為 create-only，既存狀態不可覆寫。
MCP_SERVER_CHART_VERSION = '0.1.9'
IMAGE_REPOSITORY = JCR + 'nextcloud-mcp-admin'
# 2026-10-12 覆核 main #50 的真發布 image：1d16391d，run 18319 job 20066。
# 在實際 digest 的離線 probe 驗 CLI、LaunchPolicy、127.0.0.1／port 8000、出廠唯讀；
# main.py／launcher.py／seed_defaults.py 三個 hash 與該 main SHA 一致。
# 1ec1bb0b..1d16391d 的 apps/nextcloud 及 uv.lock 無內容差異，既有啟動／唯讀檢查仍成立。
# 2026-10-18 重釘 #70（5526e865）：LaunchPolicy／console script／launcher／seed_defaults
# 對照前一 pin 均未變；uv.lock 的 mcp SDK 從 1.28.1 升為 1.30.0。
# 五支真 image 的 imports／CLI help／ASGI 限定 smoke 共 111 項通過；未啟動 lifespan／
# MCP daemon、未呼叫 MCP 工具；既有 PVC 升級與 ORM 仍未驗。來源／實機窄證據見
# docs/verification/2026-10-18-mcp-5526e865-source-contract.json
# 與同目錄的 2026-10-18-mcp-5526e865-limited-image-smoke.json。
# 2026-10-24：#71 merge 5fa1ff0a（含 #72）八支 image 的 registry digest 已逐一驗證；
# LaunchPolicy 維持 nextcloud-mcp-server、port 8000。
# 2026-10-25：改釘 #84 merge fc5fa02e（預設全開＋既有服務一次性遷移）；digest 取自
# main CI run 27630 的 push 輸出，registry 讀回一致。
# 2026-10-27：#86 merge 1dcabf91 的 image digest 已從 registry 讀回；
# 新安裝範本釘版與正式資料庫一致；此產品目前沒有既有正式服務。
EXPECT_IMAGE_TAG = (
    '1dcabf91baf088f4d7a445e8d34aeb55e03cfebe@sha256:1255f405d8a1f229e37ccc32d1dfa8bffa64f2dd67ffe88c7f437eeedb68e8f1')
GIT_SHA_PINNED_TAG_RE = r'^[0-9a-f]{40}@sha256:[0-9a-f]{64}$'
ENV_PREFIX = 'NEXTCLOUD_MCP_'


@tagged('post_install', '-at_install')
class TestMcpNextcloudSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud')

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
        self.assertEqual(tpl.slug, 'mcp-nextcloud')
        self.assertEqual(tpl.name, 'MCP：Nextcloud')
        self.assertEqual(tpl.category, 'mcp')
        self.assertEqual(tpl.workload_type, 'container')
        self.assertEqual(tpl.default_port, 8080, 'admin GUI＋/private_<token>/mcp 同一個 8080')
        self.assertEqual(self._values()['service']['port'], tpl.default_port)
        self.assertEqual(self._values()['app'], 'nextcloud')

    def test_chart_oci_pin_is_the_non_root_chart(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, MCP_SERVER_CHART)
        self.assertEqual(
            tpl.helm_chart_version, MCP_SERVER_CHART_VERSION,
            'helm_chart_version 留空會被供給閘門靜默擋在購買前（#552）；image 以 uid／gid 1000 執行，'
            '要 #1530 的非 root chart（0.1.7）——退回 0.1.5 時 pod 以 root 跑、/data 擁有者與 image 不一致')

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
            'image 被重釘了：請先實查新 image 的 apps/nextcloud（pyproject 的 console script '
            'nextcloud-mcp-server、main.py 的 LaunchPolicy、launcher 的 port／綁定位址、seed_defaults '
            '的新安裝政策），確認本檔 test_launch_contract／test_all_on_is_the_apps_own_default 仍成立，'
            '最後才更新 EXPECT_IMAGE_TAG。')

    def test_launch_contract(self):
        mcp = self._mcp_server()
        self.assertEqual(mcp['command'], 'nextcloud-mcp-server',
                         'app 的 LaunchPolicy 固定這個 command；不一致子行程不會被拉起')
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

    def test_all_on_is_the_apps_own_default(self):
        """預設全開由 app 自己做：seed_defaults 第一次啟動只寫標記、9 支全開，launcher 依 Tools 頁算 gating env。

        seed 帶 ``mcp_server.env``（例 READONLY）沒有作用（LaunchPolicy 的 env_keys 為空），還會讓讀 seed 的人
        以為唯讀是平台開關；seed 帶 ``tools`` 則會和 app 的標記（nextcloud_policy_seeded）重複維護兩份，
        而且 seed 帶任何停用清單都會讓新服務不是全開。
        """
        self.assertNotIn('env', self._mcp_server())
        self.assertEqual(set(self._mcp_server()), {'command', 'args', 'port', 'env_prefix'})
        non_sensitive = self._values()['config']['nonSensitive']
        self.assertNotIn('tools', non_sensitive, '新安裝政策由 app 的 seed_defaults 決定（全開），seed 不重複維護')
        self.assertNotIn('READONLY', json.dumps(self._values()))

    def test_no_platform_form_fields(self):
        """平台設定表單一格都不開（同家族）：工具開關、帳密、網址都在服務自己的 admin 介面設定。"""
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs['required'], [], '開通不需要租戶先填任何東西')
        self.assertEqual(specs['optional'], [])

    def test_no_secret_and_no_url_in_seed(self):
        config = self._values()['config']
        self.assertEqual(config['sensitive'], {})
        self.assertEqual(set(config['nonSensitive']), {'mcp_server'})
        self.assertNotIn(
            'connection', config['nonSensitive'],
            'base_url 由平台組裝時注入（同工作區 Nextcloud 的公開網址）；seed 寫死網址會蓋過注入、'
            '也會漏到別的工作區')
        dumped = json.dumps(self._values())
        for needle in ('app_password', 'username', 'base_url', 'nextcloud.woowtech.io'):
            self.assertNotIn(needle, dumped)

    def test_required_fields_only_admin_password(self):
        """REPLACE 語意：app_password／username／base_url 是租戶在 admin 自己填的，列進去＝每個新租戶 pod 卡 Init。"""
        config = self._values()['config']
        self.assertTrue(config['requireFields'])
        self.assertEqual(config['requiredFields'], ['admin_password'])

    def test_no_gateway_channel_so_no_secret_is_minted_or_delivered(self):
        """app password 絕不由平台供給（不跨服務傳秘密）：沒有 gateway.externalPath ⇒ 不鑄、不交付。"""
        tpl = self._tpl()
        self.assertEqual(tpl._gateway_external_path(), '')
        self.assertFalse(tpl.has_public_tunnel_address)
        self.assertEqual(tpl.admin_password_carrier, 'config_json')
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'mcp-nextcloud-seed.invalid')
        workspace = self.env['woow_paas_platform.workspace'].create({'name': 'mcp-nextcloud seed WS'})
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-mcpn-seed',
            'workspace_id': workspace.id,
            'template_id': tpl.id,
            'state': 'running',
            'subdomain': 'svc-mcpn-seed',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-mcpn-seed',
            'helm_values': tpl.helm_default_values,
        })
        creds = PaasController._gateway_credentials(service)
        self.assertNotIn('gateway_api_key', creds)
        self.assertNotIn('gateway_url', creds)

    def test_hidden_until_listed(self):
        """上架 ⟹ image 雙釘。2026-10-08 沙盒實測後已上架（只翻 is_hidden）。

        隱藏只在曝光層 is_hidden：方案先掛好、實例層 conceal_instances 從不設。
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
        """照抄 mcp-litellm 的單一方案（其他 MCP 同價）。"""
        tpl = self._tpl()
        monthly = self.env.ref('woow_paas_platform.plan_mcp_nextcloud_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_mcp_nextcloud_yearly')
        self.assertEqual(set(tpl.sudo().subscription_plan_ids.ids), {monthly.id, yearly.id})
        self.assertEqual((monthly.sh_plan_price, monthly.sh_unit), (3000, 'month'))
        self.assertEqual((yearly.sh_plan_price, yearly.sh_unit), (24000, 'year'),
                         '同其他 MCP：年繳＝月繳 ×8')
        litellm_m = self.env.ref('woow_paas_platform.plan_mcp_litellm_monthly')
        for plan in (monthly, yearly):
            self.assertEqual(plan.service_type, 'cloud_service')
            self.assertTrue(plan.sh_trial)
            self.assertEqual(plan.sh_trial_duration, 7)
            self.assertEqual(plan.sh_trial_unit, 'day')
            self.assertEqual(plan.sh_no_of_billing_cycle, litellm_m.sh_no_of_billing_cycle)
            self.assertEqual(plan.sh_never_expire, litellm_m.sh_never_expire)
            self.assertFalse(plan.storefront_hidden, '方案本身不藏：上架只翻範本的 is_hidden')
            self.assertEqual(plan.ac_max_access_seats, 0, 'base 方案，無 +1access 變體')
        for product, plan in (
            (self.env.ref('woow_paas_platform.product_mcp_nextcloud_monthly'), monthly),
            (self.env.ref('woow_paas_platform.product_mcp_nextcloud_yearly'), yearly),
        ):
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, plan.sh_plan_price)
            self.assertTrue(product.sh_product_subscribe)
            self.assertFalse(product.taxes_id, 'seed 明寫 taxes_id=[]，不吃 company 預設')

    def test_description_states_the_contract(self):
        tpl = self._tpl()
        self.assertIn('預設全開', tpl.description)
        self.assertNotIn('出廠唯讀', tpl.description + tpl.full_description)
        desc = tpl.full_description
        for needle, why in (
                ('一個指定的 Nextcloud 帳號', '以單一帳號的權限工作'),
                ('預設全開', '新服務 9 支工具全開'),
                ('9 支工具都能用', '開通後可用的工具數'),
                ('5 支讀取工具', '讀取工具'),
                ('4 支寫入與刪除工具', '寫入與刪除工具也開著'),
                ('Tools 頁', '逐支停用工具的位置'),
                ('專用帳號', '建議另建帳號、只分享需要的資料夾'),
                ('個人設定 → 安全性 → 建立新的應用程式密碼', '應用程式密碼的建立路徑'),
                ('Connection 頁', '貼帳密的位置'),
                ('請勿貼該帳號的登入密碼', '不是登入密碼'),
                ('平台不代為提供', 'app password 由租戶自己貼'),
                ('暴力破解防護', 'Nextcloud 對錯誤密碼的處置'),
                ('停止嘗試', '錯誤密碼時本服務停止重試，存新密碼後恢復'),
                ('只在第一次開通時預填它的公開網址', '同工作區網址只在第一次開通帶入（seed-if-absent）'),
                ('/mcp（結尾不要加斜線', '/mcp/ 會被子行程 307 到內部位址'),
                ('Streamable HTTP', '用戶端傳輸方式'),
                ('這個網址本身就是憑證', 'path token 外洩要換新'),
        ):
            self.assertIn(needle, desc, why)

    def test_marketplace_copy_is_data_not_po(self):
        """範本名稱與說明是 record 的資料值 ⇒ seed 直接寫中文，⛔ 不進 .po（Eugene 定案通則）。"""
        entries = [e for e in self._po_entries()
                   if e['type'] == 'model' and e['imd_name'] == 'cloud_app_mcp_nextcloud']
        self.assertEqual(entries, [], '資料值不走 .po：改 seed 的中文原文即可')

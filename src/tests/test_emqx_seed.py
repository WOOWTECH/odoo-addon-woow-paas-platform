# -*- coding: utf-8 -*-
"""EMQX 上架 seed 與「平台無法重設憑證」形態的守線斷言。

契約來源：woow-paas-charts charts/emqx 0.1.7（:18083 是唯一入口——pod 內 nginx 把 /mqtt 轉給
WebSocket listener、其餘轉給 dashboard；ClusterIP-only、release 前綴命名、built_in_database
authenticator 不再種任何 MQTT 帳號（#1361）、retained 存磁碟（#1362）、ngrok sidecar 對外轉 1883；
0.1.6 起 MQTT 授權預設拒絕、每個帳號只有「<帳號>/#」（#1529）；0.1.7 起 ngrok agent 的輸出經過遮蔽
authtoken 的過濾器、ngrok.remoteAddr 不再 render 進 shell（#1613）），
以及 paas-odoo-ci mirror-image.yml 鏡像進 JCR 的 image。

核心守線：
1. **app_managed**：EMQX 的 ``dashboard.default_password`` 依官方文件「只在第一次開機生效、
   之後變更一律忽略」⇒ 平台只透過 chart 供給初始值，helm 那條**不得**宣稱可重設。
   carrier 選錯的代價不對稱（helm_secret 誤標＝helm upgrade 成功但密碼沒變＝假裝成功），
   所以這條用多個欄位一起釘：carrier / settable / reset kind。#1774 起 Reset 走 operator
   exec ``emqx ctl admins passwd``（app_admin_reset_kind=emqx_exec），不經 helm。
2. **上架前提**：image 雙釘 ``tag@sha256``（emqx 與 ngrok 兩顆都要）。2026-10-02 鏡像落地後已公開上架，
   ``test_hidden_until_mirror_lands`` 從此是實際生效的守線（``is_hidden=False`` ⟹ 兩顆都要釘）。
3. **規格對帳**：1 vCPU / 2 Gi / 3 Gi ——template min_*、plan cs_*、chart 兩顆 PVC 加總三處一致。
   #1637 起 plan 的 ``cs_storage_gb`` 是**整台總量**（data 2＋log 1）：overlay 寫進
   ``persistence.size`` 的是總量 − log（``_PLAN_STORAGE_SPLIT``），故 min_storage_gb = cs_storage_gb。
4. **可調參數白名單**：只有應用層旋鈕（ngrok 四項 + ws listener），CPU/RAM/磁碟一律不開
   ——那三個維度由訂閱方案決定，開放等於讓租戶改自己買的規格。
5. **授權（⛔ 最重要）**：EMQX 自 5.9.0 起為 BSL 1.1，明文禁止當託管 MQTT 服務提供給第三方
   ——平台正是這個用途。只能釘 5.8.x（Apache-2.0）。digest 是唯一不會說謊的識別，所以用
   **白名單**守：seed 的 emqx digest 必須是下面登記過的 Apache-2.0 版；已知的 BSL 版另列黑名單，
   讓「有人把 #1321 的 5.10.5 換回來」紅得一眼看懂。換 5.8.x 小版時：確認 image 標籤
   ``org.opencontainers.image.licenses`` 是 Apache-2.0 後，把 amd64 子 manifest digest 加進白名單。
"""
import json
import re

from odoo.tests.common import TransactionCase, tagged

EMQX_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/emqx'
NGROK_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/ngrok'
EMQX_CHART = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/emqx'
PINNED_TAG_RE = r'^\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'
# Apache-2.0 的 EMQX（amd64 子 manifest；repo 慣例釘 amd64 child）。新增前先核對 image 標籤。
APACHE_EMQX_DIGESTS = {
    # emqx/emqx:5.8.9 — labels: licenses=Apache-2.0, edition=Opensource
    'sha256:8b92a101d1a1642a191507b9133c46aad095bb64300dba1770108a2c7261b11a',
}
# 已知的 BSL 1.1 版（不得用於平台）。
BSL_EMQX_DIGESTS = {
    # emqx/emqx:5.10.5 — labels: licenses=BSL-1.1, edition=Enterprise（#1321 曾誤釘）
    'sha256:03970774f34c75c95a2ed4218c29826aa488b39a03a3280c0e1aa1e3568fbc23',
}


@tagged('post_install', '-at_install')
class TestEmqxSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_emqx')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'emqx')
        self.assertEqual(tpl.category, 'automation')
        self.assertEqual(tpl.default_port, 18083, 'dashboard 是平台唯一對外入口')
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertFalse(tpl.mcp_enabled)
        self.assertEqual(tpl.workload_type, 'container')

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, EMQX_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.11',
                         'helm_chart_version 留空會被供給閘門靜默擋在購買前（#552）；'
                         '0.1.4 起後台 WebSocket 客户端預設連頁面本身的埠（#1418）；'
                         '0.1.6 起 MQTT 授權預設拒絕、公開網址不給 Prometheus／API 文件（#1529）；'
                         '0.1.7 起 ngrok authtoken 不再出現在容器 log（#1613）；'
                         '0.1.8–0.1.11＝charts 整合包 #254（status 前綴拒絕、登入限速、tenantSecret）')

    def test_description_matches_chart_013(self):
        """#1361／#1362／#1366：說明要跟 chart 0.1.3 的實際行為一致。"""
        tpl = self._tpl()
        desc = tpl.full_description
        self.assertIn('wss://', desc, '0.1.3 起 WebSocket 走服務網址的 /mqtt（#1366）')
        self.assertIn('沒有任何 MQTT 帳號', desc, '0.1.3 起不再預建帳號（#1361）')
        self.assertIn('retained', desc, '要說明重啟後哪些東西會留下（#1362）')
        self.assertNotIn('請把埠改成 443', desc,
                         '0.1.4 起後台 WebSocket 客户端預設就連 443，不要再叫租戶手改（#1418）')
        ws = {s['key']: s for s in tpl._parse_helm_value_specs()['optional']}['emqx.listeners.ws.enabled']
        self.assertIn('/mqtt', ws['description'], 'WS 旋鈕要寫出外部怎麼連')

    def test_description_explains_default_deny_acl(self):
        """#1529：chart 0.1.6 起授權預設拒絕，每個帳號只能用「<帳號>/#」。

        不寫清楚，租戶照舊建一個帳號給 Home Assistant 就會「連得上、收不到」（訂閱被拒、發布被丟），
        比 #1465 的「連不上」更難查。說明必須講出：預設只有自己帳號開頭的主題、中控勾超級使用者、
        其他規則在後台授權 File 編，以及改過的設定不會被重啟／升級蓋掉。
        """
        desc = self._tpl().full_description
        self.assertIn('預設拒絕', desc)
        self.assertIn('sensor01/#', desc, '要用具體例子說明「帳號名稱/#」')
        self.assertIn('超級使用者', desc, '中控（HA／Node-RED／n8n）要勾超級使用者')
        self.assertIn('授權（Authorization）', desc, '要指到後台的授權頁')
        self.assertIn('不會被覆蓋', desc, 'chart 走 base.hocon 預設值：租戶改的設定要保證留著')


    def test_platform_resets_via_emqx_ctl_not_helm(self):
        """EMQX 首開之後密碼活在它自己的資料庫：helm 那條重設只會「假裝成功」，所以 carrier
        維持 app_managed；#1774 起 Reset 改由 operator exec ``emqx ctl admins passwd``
        （app_admin_reset_kind=emqx_exec），擁有者忘記密碼時在服務頁就能救回。"""
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui, '平台要供給初始 dashboard 密碼')
        self.assertEqual(tpl.admin_password_carrier, 'app_managed')
        self.assertEqual(tpl.app_admin_reset_kind, 'emqx_exec')
        self.assertTrue(tpl.admin_reset_supported, '服務頁要有 Reset Admin Password')
        self.assertFalse(tpl.admin_password_settable)
        self.assertFalse(tpl.admin_credentials_settable, '只有 Reset，不開放租戶自設')
        self.assertFalse(tpl.admin_username_helm_path)

    def test_description_explains_reset_and_single_admin_level(self):
        """#1774（INFRA-EMQX-007-1／-3）：說明要講出忘記密碼怎麼救、以及 CE 沒有唯讀角色。

        EMQX 開源版沒有 RBAC：Dashboard 使用者與 API 金鑰的 role=viewer 會被接受，但實際擁有
        完整寫入權限（實測 viewer 帳號 POST /rules 回 201）。上游行為、chart 修不了，只能明講，
        免得租戶以為建了「唯讀」帳號或金鑰。
        """
        desc = self._tpl().full_description
        self.assertNotIn('平台事後無法代為重設', desc, '#1774 起平台可以重設')
        self.assertIn('重設管理員密碼', desc, '要指到服務頁的 Reset 按鈕')
        self.assertIn('唯讀', desc)
        self.assertIn('viewer', desc)
        self.assertIn('完整管理權限', desc)

    def test_images_from_jcr_and_double_pinned(self):
        v = self._values()
        self.assertEqual(v['image']['repository'], EMQX_IMAGE)
        self.assertEqual(v['ngrok']['image']['repository'], NGROK_IMAGE)
        for ref in (v['image']['tag'], v['ngrok']['image']['tag']):
            self.assertRegex(ref, PINNED_TAG_RE,
                             'image 一律 tag@sha256 雙釘（mutable tag 不可重現）')
        for repo in (v['image']['repository'], v['ngrok']['image']['repository']):
            self.assertNotIn('docker.io', repo)
        # ⛔ 授權：5.9+ 是 BSL 1.1（禁止託管）；只能是登記過的 Apache-2.0 digest。
        digest = v['image']['tag'].split('@', 1)[1]
        self.assertNotIn(
            digest, BSL_EMQX_DIGESTS,
            '這是 BSL 1.1 授權的 EMQX（5.9+），不得當託管服務提供給租戶')
        self.assertIn(
            digest, APACHE_EMQX_DIGESTS,
            'EMQX digest 未登記為 Apache-2.0 版；核對 image 標籤 '
            'org.opencontainers.image.licenses 後再加進 APACHE_EMQX_DIGESTS')

    def test_hidden_until_mirror_lands(self):
        """鏡像與 stg e2e 完成前不上貨架；要翻 is_hidden 時 pin 必須已經在位。"""
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        if not tpl.is_hidden:
            self.assertRegex(self._values()['image']['tag'], PINNED_TAG_RE)
            self.assertRegex(self._values()['ngrok']['image']['tag'], PINNED_TAG_RE)

    def test_spec_matches_plan_and_pvcs(self):
        tpl = self._tpl()
        v = self._values()
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 2.0)
        self.assertEqual(tpl.min_storage_gb, 3)
        data = int(v['persistence']['data']['size'].rstrip('Gi'))
        log = int(v['persistence']['log']['size'].rstrip('Gi'))
        self.assertEqual(data + log, tpl.min_storage_gb,
                         'data＋log 必須等於 min_storage_gb（quota 對帳）')
        limits = v['emqx']['resources']['limits']
        self.assertEqual(limits['cpu'], str(tpl.min_vcpu))
        self.assertEqual(limits['memory'], '%dGi' % int(tpl.min_ram_gb))
        for plan in tpl.subscription_plan_ids:
            self.assertEqual(plan.cs_vcpu, tpl.min_vcpu)
            self.assertEqual(plan.cs_ram_gb, tpl.min_ram_gb)
            self.assertEqual(plan.cs_storage_gb, data + log,
                             '#1637：cs_storage_gb 是整台總量（overlay 扣掉 log 才寫進 persistence.size）')

    def test_plans_linked_and_priced(self):
        tpl = self._tpl()
        monthly = self.env.ref('woow_paas_platform.plan_emqx_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_emqx_yearly')
        self.assertEqual(set(tpl.subscription_plan_ids.ids), {monthly.id, yearly.id})
        self.assertEqual(monthly.sh_plan_price, 2500)
        self.assertEqual(yearly.sh_plan_price, 25000)
        self.assertEqual(yearly.sh_plan_price, monthly.sh_plan_price * 10, '年繳＝10 個月')
        for plan in (monthly, yearly):
            self.assertEqual(plan.service_type, 'cloud_service')
            self.assertTrue(plan.sh_trial)
            self.assertEqual(plan.sh_trial_duration, 7)
        for product, plan in (
            (self.env.ref('woow_paas_platform.product_emqx_monthly'), monthly),
            (self.env.ref('woow_paas_platform.product_emqx_yearly'), yearly),
        ):
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, plan.sh_plan_price)
            self.assertFalse(product.taxes_id, 'seed 明寫 taxes_id=[]，不吃 company 預設')

    def test_tenant_knobs_are_app_level_only(self):
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs['required'], [], '開通不需要租戶先填任何東西')
        keys = [s['key'] for s in specs['optional']]
        self.assertEqual(sorted(keys), sorted([
            'ngrok.enabled',
            'ngrok.authtoken',
            # 商用驗收 charts#222：legacy／免費 ngrok 帳號不允許 PROXY protocol（ERR_NGROK_9014），
            # chart 0.1.6 起就有的開關開放給租戶，預設維持開啟。
            'ngrok.proxyProtocol',
            'ngrok.remoteAddr',
            'emqx.listeners.ws.enabled',
        ]))
        by_key = {s['key']: s for s in specs['optional']}
        self.assertEqual(by_key['ngrok.authtoken']['type'], 'password',
                         'password 型才會被平台在所有 API 回應中遮罩')
        self.assertEqual(by_key['ngrok.enabled']['type'], 'boolean')
        self.assertEqual(by_key['emqx.listeners.ws.enabled']['type'], 'boolean')
        for key in keys:
            self.assertFalse(re.match(r'^(resources|persistence)\b', key),
                             'CPU/RAM/磁碟由訂閱方案 cs_* 決定，不得開放給租戶改')

    def test_whitelist_filters_resource_overrides(self):
        """specs 的 key 同時是白名單：方案維度送進來要被擋掉。"""
        tpl = self._tpl()
        allowed, rejected = tpl._filter_allowed_helm_values({
            'ngrok.enabled': True,
            'resources.limits.cpu': '16',
            'persistence.size': '500Gi',
        })
        self.assertEqual(allowed, {'ngrok.enabled': True})
        self.assertEqual(sorted(rejected), ['persistence.size', 'resources.limits.cpu'])

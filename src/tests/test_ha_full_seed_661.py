# -*- coding: utf-8 -*-
"""Home Assistant 完整版（ha-full）上架 seed 的守線斷言（#661）。

鏡射 test_ha_paas_seed_woowtech_image.py 的結構。本檔的核心是**跨 repo lockstep 守線**
——交接 note「ha-handoff」§「已知 gap 3」點名的正是這條：
``helm_chart_version ↔ Chart.yaml#version ↔ image.tag ↔ seed.requiredComponents``
四者必須同一次變更一起動，而**平台端原本沒有任何測試守它**。

平台端能機械檢查的那一半是 **chart 線別 ⟺ image 線別**：

- chart ``home-assistant-full`` 配 v1 的 ``woowtech-ha`` image → sentinel 3 找不到
  ``woow_cover_entity``（v2-only 錨點），每個**全新開通**卡 Init:Error。
- chart ``home-assistant``（v1）配 v2 的 ``woowtech-ha-full`` image → v1 chart 的 sentinel
  硬編 ``woow_provision/manifest.json``，而 v2 移除了該 component → 同樣每個全新開通
  卡 Init:Error；就算繞過，v1 的 http append-if-absent 對「已帶裸 ``http:``」的 v2 seed
  永不觸發 → ``use_x_forwarded_for`` 恆 false → tunnel 後 100% 請求 400 而 pod 全綠。

``test_chart_and_image_are_same_product_line`` 就是把這兩半綁死的那條線（**雙向等價**，
不是單向蘊含）。它刻意搭配 ``test_this_template_is_the_full_line`` 一起讀：等價斷言守
「兩邊同步」，錨點斷言守「兩邊不會**一起**漂走」——只有等價那條時，把 chart 與 image
同時換成不含 ``-full`` 的 v1 座標仍然全綠（等價成立、產品卻整條變成另一條線）。
兩條**刻意分開掛在不同 test method**：遮蔽其一，另一條仍紅。

其餘斷言守 chart values 契約中「錯了就靜默壞掉」的點：image.tag 必雙釘（chart 刻意移除
``default .Chart.AppVersion`` fallback）、haProxy 四閘（未開 ``useXForwardedFor`` 時
aiohttp 對帶 XFF 的請求一律 400，而 CF tunnel 必帶 XFF——症狀是 pod Running／probe 全綠
但外部 100% 400）、以及刻意**不覆寫**的鍵不得被 seed 蓋掉（尤以 ``seed.requiredComponents``
為甚：交接 note 第 86 行的範本寫的是 chart 0.1.0 的錯錨點 ``woow_panel_frontend``，
照抄等於把已修好的 bug 用 values 覆寫回去）。
"""
import json
from odoo.tests.common import TransactionCase, tagged

# woow-k3s 實查值：4 node podCIDR 皆 10.42.X.0/24 → cluster CIDR 10.42.0.0/16。
WOOW_K3S_POD_CIDR = '10.42.0.0/16'
HA_FULL_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/woowtech-ha-full'
HA_FULL_CHART = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/home-assistant-full'
# 兩條產品線的線別標記。v1 精簡線的座標恰好是 v2 座標去掉這個後綴，所以它同時是
# 「線別判準」與「最容易誤貼的錯值」——守線據此雙向比對。
FULL_LINE_MARKER = '-full'


@tagged('post_install', '-at_install')
class TestHaFullSeed661(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_ha_full')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    # ── 跨 repo lockstep 守線（本檔的核心）────────────────────────────────────
    def test_chart_and_image_are_same_product_line(self):
        """chart 名含 ``-full`` ⟺ image repo 含 ``-full``：任一半落單即紅。

        **雙向等價**，不是單向蘊含——兩個方向各對應一種真實的壞法：

        - chart full ＋ image 非 full（v1 woowtech-ha）：sentinel 3 的 v2-only 錨點
          ``woow_cover_entity`` 在 v1 image 內不存在 → 每個全新開通卡 Init:Error。
        - chart 非 full（v1 home-assistant）＋ image full：v1 chart 的 sentinel 硬編
          ``woow_provision/manifest.json``，而 v2 已移除該 component → 同樣 Init:Error。

        ⚠ 這條**單獨存在時擋不住「兩邊一起換成 v1」**（等價仍成立）。錨點由
        test_this_template_is_the_full_line 獨立守，兩條刻意不合併。
        """
        tpl = self._tpl()
        repo = self._values()['image']['repository']
        chart_is_full = FULL_LINE_MARKER in tpl.helm_chart_name
        image_is_full = FULL_LINE_MARKER in repo
        self.assertEqual(
            chart_is_full, image_is_full,
            'chart %s 與 image %s 不同產品線：完整版 chart 必須配 woowtech-ha-full '
            'image、精簡版 chart 必須配 woowtech-ha image（chart Chart.yaml 的 '
            'OPERATOR COUPLING／THREE-WAY PRODUCT SPLIT）。任一半落單，每個全新開通'
            '都會卡 Init:Error。'
            % (tpl.helm_chart_name, repo))

    def test_this_template_is_the_full_line(self):
        """本 template 是完整版線——chart 與 image 兩側各自釘住線別標記。

        與上面的等價斷言分工：等價守「兩邊同步」，本條守「兩邊不會**一起**漂走」。
        少了本條，把 chart 與 image 同時改成 v1 座標（home-assistant ＋ woowtech-ha）
        會讓等價斷言照樣通過——那正是 cloud_app_ha_paas 那筆的座標，等於本商品被
        靜默換成另一條產品線。
        """
        tpl = self._tpl()
        repo = self._values()['image']['repository']
        self.assertIn(
            FULL_LINE_MARKER, tpl.helm_chart_name,
            'ha-full 的 chart 必須是完整版線（home-assistant-full）：%s'
            % tpl.helm_chart_name)
        self.assertIn(
            FULL_LINE_MARKER, repo,
            'ha-full 的 image 必須是完整版線（woowtech-ha-full）：%s' % repo)

    def test_chart_oci_pin(self):
        """chart＝woow-paas-charts 發佈的 home-assistant-full OCI，釘在 0.1.1。

        **不可回退 0.1.0**：0.1.0 的 ``seed.requiredComponents`` 預設值是
        ``woow_panel_frontend``——那個目錄是前端建置專案（package.json／
        rollup.config.js／src/），**沒有 manifest.json 也沒有 __init__.py**，不是 HA
        integration。sentinel 3 檢查的是 ``*/manifest.json``，所以該錨點永遠過不了：
        首次真叢集安裝時每個租戶都卡 Init:CrashLoopBackOff 重啟 10 次，log 上一行還
        印著 image=28 pvc=28（sentinel 2 數 manifest，數對了也放行了——設計是對的）。
        0.1.1 改為 woow_cover_entity（第一方、v2-only、且已確認帶 manifest.json）。
        """
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, HA_FULL_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.1')
        self.assertNotEqual(
            tpl.helm_chart_version, '0.1.0',
            '0.1.0 的 seed 錨點指向 woow_panel_frontend（非 component）→ '
            '每個租戶卡 Init:CrashLoopBackOff')

    # ── image pin ────────────────────────────────────────────────────────────
    def test_image_repository_is_woowtech_ha_full(self):
        """image＝私有 JCR 的 woowtech-ha-full（≠ HA 官方 image、≠ v1 woowtech-ha）。"""
        repo = self._values()['image']['repository']
        self.assertEqual(repo, HA_FULL_IMAGE)
        self.assertNotIn(
            'ghcr.io', repo,
            '官方 image 無烤入 /config，本 chart 的 always-on seed initContainer '
            '複製來源為空、sentinel 必中 → 全新開通卡 Init:Error')

    def test_image_tag_pinned_digest(self):
        """image 雙釘 tag@sha256；不得留 latest／chart 的佔位 tag。

        chart templates 刻意移除 ``default .Chart.AppVersion`` fallback（appVersion
        2026.7.2 追的是 HA core，woowtech registry 永遠沒有該 tag），tag 留空會
        **render 失敗**而非靜默產生拉不到的 image——那是刻意設計。
        ⚠ 本線刻意**不推 ``:latest``**，所以 tag 是唯一的可變座標，一律雙釘。
        """
        tag = self._values()['image']['tag']
        self.assertNotEqual(tag, 'latest', '本線不推 latest，且平台一律 pin digest')
        self.assertNotEqual(
            tag, '00000000.0000', 'chart values 的佔位 tag 未被平台覆寫')
        self.assertIn('@sha256:', tag, 'image tag 必須雙釘 tag@sha256')
        build_tag, digest = tag.split('@', 1)
        self.assertRegex(
            build_tag, r'^\d{8}\.\d{4}$', 'build tag 應為 UTC 時間戳 YYYYMMDD.HHMM')
        self.assertEqual(len(digest), len('sha256:') + 64, digest)

    # ── 反向代理契約（設錯＝pod 全綠但外部 100% 400）──────────────────────────
    def test_ha_proxy_four_gates_explicit(self):
        """haProxy 四鍵全部顯式寫（防 chart 端預設漂移把反代閘關掉）。

        Cloudflare tunnel 一定帶 ``X-Forwarded-For``，而 aiohttp 在
        ``use_x_forwarded_for`` 未開時對這種請求**一律回 400**。症狀極具欺騙性——
        pod ``Running``、probe 全綠，但外部 100% 請求 400。
        Helm 對未知鍵靜默忽略：chart 端改名不會報錯，只會安靜地退回預設。
        """
        ha_proxy = self._values()['haProxy']
        for key in ('seedConfiguration', 'useXForwardedFor', 'fragmentManaged'):
            self.assertIn(key, ha_proxy, 'haProxy.%s 必須顯式寫在 seed' % key)
            self.assertTrue(ha_proxy[key], 'haProxy.%s 不得為 false' % key)
        self.assertIn('trustedProxies', ha_proxy)

    def test_trusted_proxies_real_pod_cidr(self):
        """trustedProxies＝woow-k3s 真實 Pod CIDR，且不得為空。

        chart 在 ``seedConfiguration`` 開啟時對空 list **fail the render**——因為空的
        trusted_proxies 正是「永久 400 每一個被代理的請求」的那個設定。
        """
        proxies = self._values()['haProxy']['trustedProxies']
        self.assertTrue(proxies, 'trustedProxies 為空 → chart render 失敗／永久 400')
        self.assertEqual(proxies, [WOOW_K3S_POD_CIDR])

    # ── 刻意不覆寫的鍵 ───────────────────────────────────────────────────────
    def test_seed_required_components_not_overridden(self):
        """🔴 ``seed.requiredComponents`` 刻意**不**寫進 helm_default_values。

        chart 0.1.1 的預設值已經是正確的 ``["woow_cover_entity"]``（已對 registry 上的
        0.1.1 核對）。覆寫只會多一份會漂的複本。

        本條真正防的是**照抄交接 note**：canvas note「ha-handoff」第 86 行的
        helm_default_values 範本寫的是 ``{"requiredComponents":["woow_panel_frontend"]}``
        ——**那正是 chart 0.1.0 的錯錨點**（不是 component、沒有 manifest.json），
        照抄等於把已在 chart 0.1.1 修好的 bug 用 values 覆寫回去，讓每個租戶重新卡
        Init:CrashLoopBackOff。該 note 第 128 行已自行標記此處待修。

        ⚠ 若日後真有理由覆寫，值必須是**帶 manifest.json 的第一方 v2-only component**
        （chart values.yaml 列出的三個條件），且與 image pin 同一次變更一起動。
        """
        values = self._values()
        self.assertNotIn(
            'seed', values,
            'seed.* 應沿用 chart 0.1.1 預設；覆寫 requiredComponents 是把 0.1.0 的'
            '錯錨點抄回來的唯一途徑')

    def test_chart_defaults_not_overridden(self):
        """其餘刻意不覆寫的鍵不得出現在 seed values。

        - nodeSelector：chart 預設 kubernetes.io/arch=amd64，image 僅 amd64；排到
          arm64 node 會 exec format error CrashLoop。helm deep-merge 用 ``{}`` 蓋不掉，
          真要清除得寫 ``kubernetes.io/arch: null``——所以這裡連鍵都不該出現。
        - imagePullSecrets：chart 預設已是 paas-registry（image 在私有 JCR），
          operator ``_ensure_image_pull_secrets`` 另有一層。
        - persistence.storageClass：由平台 ``_inject_storage_class`` 注入。
        - networkPolicy：``allowAllEgress`` 預設 true 且必須維持（HACS 要從 github.com
          下載；DNS-only egress 會讓它變成裝不了東西的整合，且沒有清楚錯誤）。
        """
        values = self._values()
        self.assertNotIn('nodeSelector', values)
        self.assertNotIn('imagePullSecrets', values)
        self.assertNotIn('networkPolicy', values)
        self.assertNotIn('storageClass', values['persistence'])

    # ── chart 固定契約 ───────────────────────────────────────────────────────
    def test_config_mount_path_fixed(self):
        """HA config 目錄固定 /config（chart 明令不可改）；PVC 10Gi。"""
        persistence = self._values()['persistence']
        self.assertEqual(persistence['mountPath'], '/config')
        self.assertEqual(persistence['size'], '10Gi')

    def test_service_ports_8123(self):
        """service.port／targetPort 固定 8123（＝default_port＝HA 前端埠）。"""
        service = self._values()['service']
        self.assertEqual(service['port'], 8123)
        self.assertEqual(service['targetPort'], 8123)
        self.assertEqual(self._tpl().default_port, 8123)

    # ── 商品定位 ─────────────────────────────────────────────────────────────
    def test_distinct_from_ha_paas_and_haos(self):
        """三條 Home Assistant 產品線的 slug／chart／image 完全不重疊。

        chart Chart.yaml 的「THREE-WAY PRODUCT SPLIT」：ha-full（容器・完整整合集）／
        ha-paas（容器・精簡・0 元自動開通）／haos（KubeVirt VM・真 HAOS）是**並行產品**，
        不是升級階梯。互抄 helm values 正是 #336 chart↔image 原子變更鐵律被咬過的形態。
        """
        full = self._tpl()
        ha_paas = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        haos = self.env.ref('woow_paas_platform.cloud_app_haos')
        slugs = [full.slug, ha_paas.slug, haos.slug]
        self.assertEqual(slugs, ['ha-full', 'ha-paas', 'haos'])
        self.assertEqual(len(set(slugs)), 3)
        charts = [t.helm_chart_name for t in (full, ha_paas, haos)]
        self.assertEqual(len(set(charts)), 3, 'chart 不得共用：%s' % charts)
        repos = [
            json.loads(t.helm_default_values)['image']['repository']
            for t in (full, ha_paas, haos)
        ]
        self.assertEqual(len(set(repos)), 3, 'image 不得共用：%s' % repos)

    def test_storefront_exposure(self):
        """上架前台販售：is_active 且不隱藏；實例層 conceal 亦未開。

        ⚠ 與 cloud_app_ha_paas 相反——那筆是 0 元自動開通、租戶入口是 App，故
        is_hidden＋conceal_instances 兩層都關（#471 的刻意對照組），勿照抄那筆。
        """
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.is_hidden)
        self.assertFalse(tpl.conceal_instances)

    def test_no_max_per_partner(self):
        """刻意**不設** max_per_partner（＝不限，0）。

        與 ha-paas 的 1 相反，這是拍板的產品決策：完整版與精簡版無關聯、可並存，
        租戶想開幾台都可以。日後要限量請走 #501 的供給閘門（ir.config_parameter，
        營運自助、不必發版），而不是回頭設這個欄位。
        """
        self.assertFalse(
            self._tpl().max_per_partner,
            'max_per_partner 應留 0（不限）——限量請走 #501 供給閘門')

    def test_admin_gui_disabled(self):
        """has_admin_gui=False：HA 管理員帳號由租戶在 onboarding 自建，平台不持有。

        開了這個旗標會讓平台憑空生一組 admin_password 塞進 config.sensitive（chart
        沒有對應鍵＝白生），並在前台長出一顆按了必失敗的「重置密碼」按鈕。
        """
        self.assertFalse(self._tpl().has_admin_gui)


@tagged('post_install', '-at_install')
class TestHaFullQuotaLockstep(TransactionCase):
    """min_* 與 chart values 的 quota lockstep——**由 seed values 現算，不硬編**。

    workspace ns quota ＝ 存活 service 的 allocated_* 加總 × 3
    （``cloud_service._compute_ns_quota``）；本 template 的 plan cs_* 皆為 0 →
    allocated_* 走 fallback 取這三個 min_*（``_allocated_vals_from``）。

    現算而非硬編的理由：改了 ``resources``／``persistence.size`` 卻沒同步 min_* 時
    本測試就會紅——硬編三個數字只會讓兩邊各自對、合起來錯。
    """

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_ha_full')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def test_min_ram_covers_limits_memory(self):
        """🔴 min_ram_gb × 3 必須蓋得住 Σlimits.memory。

        chart limits.memory=2Gi 需要 min_ram_gb=1.0 才貢獻 3Gi；填 0.5 只貢獻 1Gi →
        同 workspace 併存其他服務時 pod ``exceeded quota``，而唯一可見症狀是
        「服務永遠 CF 502」。
        """
        tpl = self._tpl()
        limits_mem = self._values()['resources']['limits']['memory']
        self.assertTrue(limits_mem.endswith('Gi'), limits_mem)
        self.assertGreaterEqual(
            tpl.min_ram_gb * 3, float(limits_mem[:-2]),
            'min_ram_gb×3 蓋不住 limits.memory %s → pod exceeded quota（症狀只是 '
            'CF 502）' % limits_mem)
        self.assertGreaterEqual(
            tpl.min_ram_gb, 1.0, 'min_ram_gb 不可低於 1.0')

    def test_min_vcpu_covers_limits_cpu(self):
        """min_vcpu × 3 必須蓋得住 Σlimits.cpu（chart limits.cpu="2" → min_vcpu=1）。"""
        tpl = self._tpl()
        limits_cpu = self._values()['resources']['limits']['cpu']
        cpu = (float(limits_cpu[:-1]) / 1000.0 if limits_cpu.endswith('m')
               else float(limits_cpu))
        self.assertGreaterEqual(tpl.min_vcpu * 3, cpu, limits_cpu)

    def test_min_storage_matches_pvc(self):
        """min_storage_gb 必須等於 chart 的 PVC 大小（10Gi）。

        ⚠ PVC 只能長大不能縮——縮小 persistence.size 會讓整個 ``helm upgrade`` 被
        API server 拒絕，所以這兩個值一旦上線就只能同向調高。
        """
        tpl = self._tpl()
        size = self._values()['persistence']['size']
        self.assertTrue(size.endswith('Gi'), size)
        self.assertEqual(tpl.min_storage_gb, int(size[:-2]))


@tagged('post_install', '-at_install')
class TestHaFullPlansSeed(TransactionCase):
    """base plan／product／M2M link 的 seed 守線（3000 / 24000）。"""

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_ha_full')

    def test_plan_prices_and_cycles(self):
        """月 3000／年 24000（年＝月×8，全平台一致）。"""
        monthly = self.env.ref('woow_paas_platform.plan_ha_full_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_ha_full_yearly')
        self.assertEqual(monthly.sh_plan_price, 3000)
        self.assertEqual(yearly.sh_plan_price, 24000)
        self.assertEqual(monthly.sh_unit, 'month')
        self.assertEqual(yearly.sh_unit, 'year')
        self.assertEqual(
            yearly.sh_plan_price, monthly.sh_plan_price * 8,
            '平台慣例：年繳＝月繳×8')
        for plan in (monthly, yearly):
            self.assertEqual(plan.service_type, 'cloud_service')

    def test_products_match_plan_prices(self):
        """product.list_price 與 plan 價格一致，且 taxes_id 空（#366 pts 免稅）。"""
        pairs = [
            ('product_ha_full_monthly', 'plan_ha_full_monthly', 3000),
            ('product_ha_full_yearly', 'plan_ha_full_yearly', 24000),
        ]
        for product_ref, plan_ref, price in pairs:
            product = self.env.ref('woow_paas_platform.%s' % product_ref)
            plan = self.env.ref('woow_paas_platform.%s' % plan_ref)
            self.assertEqual(product.list_price, price, product_ref)
            self.assertTrue(product.sh_product_subscribe, product_ref)
            self.assertEqual(product.sh_subscription_plan_id, plan, product_ref)
            self.assertFalse(product.taxes_id, '訂閱 product 應免稅（#366）')

    def test_template_plan_links(self):
        """template ↔ 兩 base plan 的 M2M 掛上了（links.xml noupdate=0 全量 reset）。

        ⚠ 比對的是**過濾掉席數變體後**的集合：#765 起 generator 會另外 (4,) append
        1–3 席的加購變體到同一個 M2M（那條由 test_access_variants_attached 與
        test_plan_variants_generated 守）。直接對整個 subscription_plan_ids 做
        assertEqual 會把「加購變體上架」誤判成 base 掛錯。
        """
        tpl = self._tpl()
        expected = (
            self.env.ref('woow_paas_platform.plan_ha_full_monthly')
            | self.env.ref('woow_paas_platform.plan_ha_full_yearly')
        )
        base = tpl.subscription_plan_ids.filtered(
            lambda p: p.ac_max_access_seats == 0)
        self.assertEqual(
            base, expected,
            'ha-full 應恰掛兩個 base plan（月／年），實得 %s'
            % base.mapped('name'))

    def test_access_variants_attached(self):
        """#765：1–3 席的加購變體掛上本 template 且 hidden。

        原本此處記「v1 不做加購變體」，已被 Eugene 2026-08-17 拍板取代。
        全量斷言在 test_plan_variants_generated；此處抽驗本家族的席數與價格。
        """
        tpl = self._tpl()
        plans = tpl.sudo().subscription_plan_ids
        variants = plans.filtered(lambda p: p.ac_max_access_seats > 0)
        self.assertEqual(
            sorted(variants.mapped('ac_max_access_seats')), [1, 1, 2, 2, 3, 3],
            '1–3 席 × 月／年兩週期，實際：%s' % variants.mapped('name'))
        expected = {
            'plan_ha_full_ac1_monthly': (3600, 1),
            'plan_ha_full_ac1_yearly': (28800, 1),
            'plan_ha_full_ac3_monthly': (4800, 3),
            'plan_ha_full_ac3_yearly': (38400, 3),
        }
        for xmlid, (price, seats) in expected.items():
            v = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertIn(v, plans, xmlid)
            self.assertEqual(v.sh_plan_price, price, xmlid)
            self.assertEqual(v.ac_max_access_seats, seats, xmlid)
            self.assertTrue(v.storefront_hidden, xmlid)

    def test_plans_do_not_override_cs_spec(self):
        """plan 刻意**不設** cs_vcpu／cs_ram_gb／cs_storage_gb。

        設了會讓 ``_allocated_vals_from`` 改吃 plan 值、蓋掉 template 的 min_*，
        而那三個是經 chart「PLATFORM QUOTA LOCKSTEP」推導的 ns quota 用量——蓋掉會讓
        pod 撞 ``exceeded quota``，唯一可見症狀是「服務永遠 CF 502」。

        ⚠ 涵蓋 base **與** #765 的席數變體：變體被填 cs_* 時 base 側恆綠，
        只有選了加購席的租戶才踩得到。
        """
        plans = self._tpl().sudo().subscription_plan_ids
        self.assertTrue(plans, '前置：template 應掛到 plan')
        for plan in plans:
            self.assertFalse(plan.cs_vcpu, plan.name)
            self.assertFalse(plan.cs_ram_gb, plan.name)
            self.assertFalse(plan.cs_storage_gb, plan.name)

    def test_plans_distinct_from_haos_and_ha_paas(self):
        """不與 HAOS／HA PaaS 的 plan 互掛（三者是不同商品）。

        ⚠ ha-paas 的 plan 刻意 orphan（不掛任何 template M2M，#341 Wave A 撞車收斂），
        HAOS 掛的是自己的兩筆 4500/36000。互掛會讓租戶在錯的商品下買到錯的規格。
        """
        tpl = self._tpl()
        linked = set(tpl.subscription_plan_ids.ids)
        for foreign_ref in ('plan_haos_monthly', 'plan_haos_yearly',
                            'plan_ha_paas_monthly'):
            foreign = self.env.ref(
                'woow_paas_platform.%s' % foreign_ref, raise_if_not_found=False)
            if foreign:
                self.assertNotIn(foreign.id, linked, foreign_ref)

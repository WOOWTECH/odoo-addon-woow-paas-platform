# -*- coding: utf-8 -*-
"""HA PaaS seed 切換到 woowtech-ha image ＋ chart 0.2.x 的守線斷言（#336 後續）。

鏡射 test_opendesign_seed_380.py 結構。本檔的核心是 **原子性守線**：chart 0.2.x 與
image 必須同進同退（chart NOTES.txt「OPERATOR COUPLING」）——

- 只升 chart 不換 image → HA 官方 image 沒有烤入的 /config，0.2.x always-on 的
  seed-config initContainer 複製來源為空、sentinel 必中 → 所有全新開通卡 Init:Error。
- 只換 image 不升 chart → 0.1.x 把空 PVC 掛在 /config 會遮蔽 image 內建的 9 個
  custom_components → 開出來是空殼 HA。

test_chart_and_image_move_together 就是把這兩半綁死的那條線。

其餘斷言守住 chart values 契約中「錯了就靜默壞掉」的點：image.tag 必釘（chart 刻意
移除 default .Chart.AppVersion fallback）、trustedProxies 必須是真實 Pod CIDR
（append-if-absent＝首次 provision 限定，之後 helm upgrade 不會重跑，設錯就永久 400）、
以及刻意不覆寫的鍵（nodeSelector amd64／imagePullSecrets）不得被 seed 蓋掉。
"""
import json
from odoo.tests.common import TransactionCase, tagged

# woow-k3s 實查值：4 node podCIDR 皆 10.42.X.0/24 → cluster CIDR 10.42.0.0/16。
WOOW_K3S_POD_CIDR = '10.42.0.0/16'
WOOWTECH_HA_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/woowtech-ha'


@tagged('post_install', '-at_install')
class TestHAPaaSWoowtechImageSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_ha_paas')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    # ── 原子性守線 ───────────────────────────────────────────────────────────
    def test_chart_and_image_move_together(self):
        """chart 0.2+ ⟺ woowtech-ha image：任一半落單即測試紅。

        0.2.x ＋ 官方 image → 每個全新開通卡 Init:Error（seed 複製來源為空）；
        0.1.x ＋ woowtech-ha image → 空 PVC 遮蔽烤入的 custom_components。
        """
        tpl = self._tpl()
        chart_major_minor = tuple(
            int(x) for x in tpl.helm_chart_version.split('.')[:2])
        repo = self._values()['image']['repository']
        is_woowtech = repo == WOOWTECH_HA_IMAGE
        self.assertEqual(
            chart_major_minor >= (0, 2), is_woowtech,
            'chart %s 與 image %s 不同步：chart ≥0.2 必須搭 woowtech-ha image、'
            'chart 0.1.x 必須搭官方 image（chart NOTES.txt OPERATOR COUPLING）'
            % (tpl.helm_chart_version, repo))

    def test_chart_oci_pin(self):
        """chart＝woow-paas-charts 發佈的 home-assistant OCI，釘在 0.2.2。

        0.2.2（#1441）：NetworkPolicy 同工作區全通、拿掉 egress `- {}`、排除多播；seed 與 image 不變。

        **不可回退 0.2.0**：0.2.0 的 seed initContainer 用 `cp -rn /config/. /seed-target/`
        單一指令，在 image 內的 busybox v1.37.0 上 exit 0 卻複製 0 個檔（-n 把既存目的
        目錄當成不可覆蓋目標、整個遞迴靜默跳過）→ sentinel FATAL → 每個全新開通卡
        Init:Error。0.2.1 改為逐檔 cp -rn 迴圈修正（2026-07-23 stg 真叢集實測確認）。
        """
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(
            tpl.helm_chart_name,
            'oci://jcr-prod.woowtech.io/woow-paas-docker-local/home-assistant')
        self.assertEqual(tpl.helm_chart_version, '0.2.2')

    # ── image pin ────────────────────────────────────────────────────────────
    def test_image_repository_is_woowtech_ha(self):
        """image＝私有 JCR 的 woowtech-ha（≠ HA 官方 ghcr image）。"""
        repo = self._values()['image']['repository']
        self.assertEqual(repo, WOOWTECH_HA_IMAGE)
        self.assertNotIn(
            'ghcr.io', repo, '官方 image 無烤入 /config，0.2.x 用它必 Init:Error')

    def test_image_tag_pinned_digest(self):
        """image 雙釘 tag@sha256；不得留 latest／chart 的佔位 tag。

        chart templates 刻意移除 `default .Chart.AppVersion` fallback（appVersion
        追的是 HA core、woowtech registry 永遠沒有該 tag），tag 留空會 render 失敗。
        """
        tag = self._values()['image']['tag']
        self.assertNotEqual(tag, 'latest', '不得留 latest 可變 tag')
        self.assertNotEqual(
            tag, '00000000.0000', 'chart values 的佔位 tag 未被平台覆寫')
        self.assertIn('@sha256:', tag, 'image tag 必須雙釘 tag@sha256')
        build_tag, digest = tag.split('@', 1)
        self.assertRegex(
            build_tag, r'^\d{8}\.\d{4}$', 'build tag 應為 UTC 時間戳 YYYYMMDD.HHMM')
        self.assertEqual(len(digest), len('sha256:') + 64, digest)

    # ── 反向代理契約（首次 provision 限定，設錯永久 400）────────────────────
    def test_trusted_proxies_real_pod_cidr(self):
        """trustedProxies＝woow-k3s 真實 Pod CIDR，且不得為空。

        seed 的 http 區塊是 append-if-absent＝**首次 provision 限定**；之後 helm
        upgrade 不會重跑，設錯或設空會讓 HA 在 Cloudflare tunnel 後永久回 400。
        """
        proxies = self._values()['haProxy']['trustedProxies']
        self.assertTrue(proxies, 'trustedProxies 為空 → 種下的 http 區塊永久 400')
        self.assertEqual(proxies, [WOOW_K3S_POD_CIDR])

    def test_ha_proxy_gates_explicit(self):
        """seedConfiguration／useXForwardedFor 明寫 True（防 chart 預設漂移關掉閘）。"""
        ha_proxy = self._values()['haProxy']
        self.assertTrue(ha_proxy['seedConfiguration'])
        self.assertTrue(ha_proxy['useXForwardedFor'])

    # ── chart 固定契約 ───────────────────────────────────────────────────────
    def test_config_mount_path_fixed(self):
        """HA config 目錄固定 /config（chart 明令不可改）；PVC 5Gi 與 min_storage 齊。"""
        persistence = self._values()['persistence']
        self.assertEqual(persistence['mountPath'], '/config')
        self.assertEqual(persistence['size'], '5Gi')

    def test_service_ports_8123(self):
        """service.port／targetPort 固定 8123（＝default_port＝HA 前端埠）。"""
        service = self._values()['service']
        self.assertEqual(service['port'], 8123)
        self.assertEqual(service['targetPort'], 8123)
        self.assertEqual(self._tpl().default_port, 8123)

    def test_chart_defaults_not_overridden(self):
        """刻意不覆寫的鍵不得出現在 seed values。

        - nodeSelector：chart 預設 kubernetes.io/arch=amd64，image 僅 amd64；排到
          arm64 node 會 exec format error CrashLoop。helm deep-merge 用 {} 蓋不掉，
          真要清除得寫 `kubernetes.io/arch: null`——所以這裡連鍵都不該出現。
        - imagePullSecrets：chart 預設已是 paas-registry（image 在私有 JCR），
          operator releases.py 另有 setdefault 一層。
        - persistence.storageClass：由平台 _inject_storage_class 注入。
        """
        values = self._values()
        self.assertNotIn('nodeSelector', values)
        self.assertNotIn('imagePullSecrets', values)
        self.assertNotIn('storageClass', values['persistence'])

    def test_resources_lockstep_min_specs(self):
        """resources 與 min_* 對齊；min_ram_gb×3（ns quota headroom）蓋得住 limits。

        workspace ns quota = sum(存活 service allocated_ram_gb)×3
        （cloud_service._compute_ns_quota）；HA plan cs_ram_gb=0 → 走 min_ram_gb
        fallback。不足會讓 pod FailedCreate(exceeded quota) → 對外永遠連不上。
        """
        tpl = self._tpl()
        resources = self._values()['resources']
        self.assertEqual(resources['requests']['cpu'], '250m')
        self.assertEqual(resources['requests']['memory'], '512Mi')
        self.assertEqual(resources['limits']['cpu'], '1')
        self.assertEqual(resources['limits']['memory'], '1Gi')
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 1.0)
        self.assertEqual(tpl.min_storage_gb, 5)
        limits_mem = resources['limits']['memory']
        self.assertTrue(limits_mem.endswith('Gi'), limits_mem)
        self.assertGreaterEqual(tpl.min_ram_gb * 3, float(limits_mem[:-2]))

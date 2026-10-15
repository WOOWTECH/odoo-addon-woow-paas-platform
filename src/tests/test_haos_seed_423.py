# -*- coding: utf-8 -*-
"""#423 W5b：Home Assistant OS（HAOS，VM 型）上架 seed 契約。

形制對齊 test_opendesign_seed_380.py / test_mcp_3type_seed_375.py（#380 金樣板），
但多守一件既有 seed 測試沒有的事：**這是平台第一個 workload_type='vm' 的 template**，
它的資源建模（min_*）填的是 **quota 用量峰值**而不是賣給租戶的規格。那組數字被改回
「直覺值」時不會報錯，只會讓所有開通靜默卡在 ImportInProgress —— 故本檔的
TestHaosQuotaModelling 全部由 seed 的 helm values **現算**、不硬編，改一邊沒改另一邊
就紅。

seed values 的鍵名已對真 chart 核對（`helm show values
oci://jcr-prod.woowtech.io/woow-paas-docker-local/haos --version 0.1.3`），不是轉抄
design 草案 —— 草案寫的是 `imagePullSecret`（單數）與 `ingress.enabled`，真 chart
是 `imagePullSecrets`（複數 list）且**沒有** ingress 區塊。

⚠ TestHaosImagePinW2Gate 現為**回歸守線**：W2 golden image 已交付（woow-paas-ci
run 1288、2026-07-30）、digest 已釘進本 seed，故該類現在綠。它守的是「tag 被改回
佔位」與「換 image 時掉了 @sha256 那半」兩種退化，不設 PENDING 白名單 —— 白名單會
腐化成永久豁免，把守衛掏空。
"""
import json

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision

MOD = 'woow_paas_platform'

# W2 交付前的 image.tag 佔位字面 —— 已成歷史（現值 `18.1-20260730.0912@sha256:6123d82a…`），
# 保留是為了讓守衛的錯誤訊息能點名「有人把真 digest 改回佔位」。形狀＝
# `{haos-os-version}-{UTC 時間戳}@sha256:…`。佔位刻意與 chart 自己的預設佔位
# （`18.1-00000000.0000`）不同字面，好讓「seed 忘了覆寫」與「seed 等 W2」可區分。
W2_PENDING_TAG = '18.1-PENDING-W2'
CHART_DEFAULT_TAG = '18.1-00000000.0000'


class _HaosSeedMixin:

    def _tpl(self):
        return self.env.ref('%s.cloud_app_haos' % MOD)

    def _values(self):
        return json.loads(self._tpl().helm_default_values)


@tagged('post_install', '-at_install')
class TestHaosTemplateSeed(_HaosSeedMixin, TransactionCase):

    def test_marketplace_fields(self):
        """上架前台販售：name/slug、category、is_active、非隱藏、port/ingress。"""
        tpl = self._tpl()
        self.assertEqual(tpl.name, 'Home Assistant OS')
        self.assertEqual(tpl.slug, 'haos')
        self.assertEqual(tpl.category, 'container')
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.is_hidden, '走 #380 金樣板上架前台販售、不隱藏')
        self.assertEqual(tpl.default_port, 8123)
        self.assertTrue(tpl.ingress_enabled, '對外走平台 subdomain + CF tunnel')

    def test_workload_type_is_vm(self):
        """🔴 本 PR 的頭號斷言：HAOS 是 VM 型，不是容器。

        這個值一改成 container 不會有任何錯誤訊息，只會讓三條路徑同時走錯：
        就緒判定改讀 pod（VM 沒有 Deployment，撈到零顆 pod → 既有 fail-open
        會把還在 CDI import 的 VM 標成 running）、啟停改 patch replicas（VM 無
        replicas 可 scale）、helm values 不再注入 runStrategy（helm upgrade 會
        把停權中的 VM 喚醒）。
        """
        self.assertEqual(self._tpl().workload_type, 'vm')

    def test_is_the_only_vm_template(self):
        """v1 只有 HAOS 一個 VM 型 template —— 其餘全部 container（零行為改變）。

        與 test_haos_workload_type.test_seeded_vm_templates_are_exactly_haos
        互為兩側：那邊從「全體 template」看，這邊從「HAOS」看。
        """
        vm_templates = self.env['%s.cloud_app_template' % MOD].sudo().search([
            ('workload_type', '=', 'vm'),
        ])
        self.assertEqual(
            vm_templates, self._tpl(),
            'v1 的 VM 型 template 應恰為 cloud_app_haos，實際：%s'
            % vm_templates.mapped('slug'))

    def test_chart_oci_pin(self):
        """chart＝woow-paas-charts 的 charts/haos 0.1.3（OCI，helm_repo_url 空）。

        ⚠ 不是 charts/home-assistant —— 那是容器化的 HA Core（cloud_app_ha_paas）。

        0.1.0 → 0.1.5：0.1.1 修 CDI registry credential、0.1.2 加 XFF 剝除代理，
        0.1.3 將 VM guest targetPort 改為 HAOS onboarding listener :80，0.1.4 修正
        XFF proxy upstream 指向 VM Service :8123，0.1.5 加大 XFF proxy 回應標頭緩衝（M4 R10）。
        """
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(
            tpl.helm_chart_name,
            'oci://jcr-prod.woowtech.io/woow-paas-docker-local/haos')
        self.assertEqual(tpl.helm_chart_version, '0.1.5')
        self.assertNotIn(
            'home-assistant', tpl.helm_chart_name,
            'HAOS（VM）與 home-assistant（容器）是不同 chart、不同產品')

    def test_image_repository(self):
        """image repository＝jcr-prod 的 haos golden image。

        ⚠ 它是 CDI DataVolume 的 registry source（OCI containerDisk），
        **不是**任何 Pod 的 image —— 本 chart 沒有容器會拉它。
        """
        repo = self._values()['image']['repository']
        self.assertEqual(
            repo, 'jcr-prod.woowtech.io/woow-paas-docker-local/haos')
        self.assertNotIn(
            'woowtech-ha', repo, 'HAOS 的 image 不是容器版 HA 的 woowtech-ha')

    def test_image_tag_is_never_silently_wrong(self):
        """image.tag 不得是 latest、不得留 chart 自己的預設佔位、不得為空。

        chart 沒有 `default .Chart.AppVersion` fallback（appVersion 18.1 追的是
        HAOS OS 版本、registry 只有時間戳 build tag），空值會 render 失敗；但
        `latest` 或 chart 預設佔位會**成功 render**再去拉一顆不存在／會漂移的
        image —— 那才是靜默的那條路。本測試在 W2 交付前後都必須綠。
        """
        tag = self._values()['image']['tag']
        self.assertTrue(tag, 'image.tag 不得為空')
        self.assertNotEqual(tag, 'latest', '平台鐵律：零 :latest')
        self.assertNotEqual(
            tag, CHART_DEFAULT_TAG,
            'seed 必須覆寫 chart 的預設佔位 %s（留著＝seed 根本沒生效）'
            % CHART_DEFAULT_TAG)

    def test_data_volume_source_is_pinnable_registry(self):
        """dataVolume.source=registry ＋ pullMethod=node。

        - `http`（另一個合法值）直接對外網抓 .qcow2.xz：**沒有 digest 釘選**、
          每租戶一次公網下載。它是 W1 PoC 的逃生口，不是產品路徑；漂移過去不會
          報錯，只會讓整條釘版治理靜默失效。
        - `pullMethod: pod` 時 CDI 自己拉，secretRef 需要 accessKeyId／secretKey
          形狀的 secret，而平台注入的 paas-registry 是 dockerconfigjson —— 設錯
          的症狀是 DataVolume 卡在 ImportInProgress，原因埋在 importer pod log。
        """
        dv = self._values()['dataVolume']
        self.assertEqual(dv['source'], 'registry')
        self.assertEqual(dv['registry']['pullMethod'], 'node')

    def test_persistence_volume_mode_is_filesystem(self):
        """🔴 D1（#423 W1 PoC 受控 A/B，唯一變因＝volumeMode）：必須 Filesystem。

        `Block` 在本叢集必掛：CDI importer 以 UID 107／runAsNonRoot 執行且 pod 層
        無 fsGroup，而 fsGroup 對 raw block volume 不適用 → device node 屬 root，
        importer 死在 `blockdev: cannot open /dev/cdi-block-volume: Permission
        denied`。實測 Block → CrashLoop、Filesystem → Succeeded。
        """
        self.assertEqual(self._values()['persistence']['volumeMode'], 'Filesystem')

    def test_sold_spec(self):
        """賣給租戶的規格：2 vCPU／4Gi RAM／30Gi 磁碟（與 min_* 不同，見 quota 類）。

        #1637（使用者 2026-10-10「按需分配，最大給到 30G」）由 64Gi 改成 30Gi：30 GiB＝32.2 GB，
        仍符合 HAOS 官方最低 32 GB。"""
        values = self._values()
        self.assertEqual(values['vm']['cores'], 2)
        self.assertEqual(values['vm']['memory'], '4Gi')
        self.assertEqual(values['persistence']['size'], '30Gi')

    def test_service_port_matches_default_port(self):
        """Service :8123 ＝ default_port；guest targetPort :80 ＝ HAOS UI listener。

        operator 的 Cloudflare expose 取「名稱排序第一個非 headless Service」＋
        http-like port —— 兩邊對不上就是 CF 502。

        ⚠ chart 0.1.2 起渲染**兩顆** ClusterIP:8123 Service（實查 registry 產物）：
        ``{release}-haos``（主，selector 指 xff-proxy 的 Deployment）與
        ``{release}-haos-vm``（selector 指 ``vm.kubevirt.io/name``，供 proxy 回打 VM）。
        兩顆都非 headless，主 Service 是 ``-vm`` 的字串前綴 → 排序永遠在前 →
        expose 仍取到主 Service、語意不變。下面那條 assertLess 就是把這個**隱性依賴**
        釘成顯性斷言。

        ⚠ 誠實標註侷限：assertLess 驗的是命名慣例下的排序關係，不是 chart 產物本身
        （Odoo 測試環境沒有 helm）。chart 若把主 Service 改成排序落在 ``-vm`` 之後的
        名字，tunnel 會直接打到 VM、繞過剝除代理，外部靜默退回 400 —— 而本測試不會紅。
        產物層的守線在 chart tests；本斷言的作用是讓要改 Service 名稱的人 grep
        ``haos-vm`` 時撞到這條紀錄。
        """
        values, tpl = self._values(), self._tpl()
        self.assertEqual(values['service']['port'], 8123)
        self.assertEqual(values['service']['targetPort'], 80)
        self.assertEqual(values['service']['port'], tpl.default_port)

        release = 'svc-c7187a7a'
        main_svc, vm_svc = f'{release}-haos', f'{release}-haos-vm'
        self.assertLess(
            main_svc, vm_svc,
            '主 Service 必須排在 -vm Service 之前，否則 operator expose 會選到直連 '
            'VM 的那顆、繞過 XFF 剝除代理 —— #479 會靜默復發（外部一律 400）。')

    def test_xff_strip_left_to_chart_default(self):
        """xffStrip 刻意不寫進 seed —— 它是 chart 0.1.2 對 #479 的修復預設。

        實查 registry 產物（``helm show values … --version 0.1.2``）：
        ``xffStrip.enabled`` 預設 **true**，並附 nginx-unprivileged 的 digest pin。

        平台沒有理由覆寫它：設成 false 會讓主 Service 直指 VM，而 HAOS 內的 HA Core
        沒有 ``trusted_proxies`` 設定、會拒絕 CF tunnel 必帶的 ``X-Forwarded-For``，
        租戶從外部一律拿到 400、連 onboarding 畫面都打不開（#423 W6.2 實測）。

        本斷言守的是「seed 不要把 chart 的修復預設覆寫掉」，包含日後有人「為了明確」
        把 xffStrip 寫進 seed 卻填錯值。
        """
        self.assertNotIn(
            'xffStrip', self._values(),
            'xffStrip 是 chart 0.1.2 的 #479 修復預設（enabled: true），seed 刻意不設；'
            '真要覆寫請先確認不會讓外部退回 400。')

    def test_network_policy_enabled(self):
        """networkPolicy.enabled=true：租戶隔離第二層（第一層＝operator baseline NP）。

        明寫防 chart 預設漂移把它關掉 —— 關掉不會有任何錯誤訊息。
        """
        self.assertTrue(self._values()['networkPolicy']['enabled'])

    def test_injected_and_absent_keys_stay_out_of_git_values(self):
        """刻意不寫進 seed 的鍵：寫了會蓋掉平台注入、或是根本不存在的死鍵。

        - persistence.storageClass：`_inject_storage_class` 每次部署注入。
        - imagePullSecrets：operator `_ensure_image_pull_secrets` 注入，chart 亦自足。
        - vm.runStrategy：`cloud_service` 依 CloudService.state 注入（防 helm
          upgrade 喚醒停權 VM）。seed 寫死任何值都會被覆寫、只會誤導後人。
        - ingress：本 chart **沒有** ingress 區塊（對外走 CF tunnel）→ 死鍵。
        - networkPolicy.apiServerCidrs：本 chart 無此鍵（VM 不打 apiserver）。
        """
        values = self._values()
        self.assertNotIn(
            'storageClass', values['persistence'],
            'storageClass 由 _inject_storage_class 注入，seed 不得寫死')
        self.assertNotIn('imagePullSecrets', values)
        self.assertNotIn(
            'runStrategy', values['vm'],
            'runStrategy 由 CloudService.state 推導注入，seed 不得寫死')
        self.assertNotIn('ingress', values)
        self.assertNotIn('apiServerCidrs', values['networkPolicy'])

    def test_storage_class_injection_path_wired(self):
        """lockstep（W5a）：`_inject_storage_class` 認得 slug 'haos' 且落在扁平
        persistence.storageClass —— chart 端此鍵 required 且不可為空。

        woow-k3s 有兩個 default StorageClass（local-path 與 longhorn），落到
        node-local 的 local-path ＝ 30 GiB 磁碟無副本、節點死即資料失聯。
        """
        Service = self.env['%s.cloud_service' % MOD]
        injected = Service._inject_storage_class(
            self._values(), self._tpl().slug, 'longhorn-delete')
        self.assertEqual(injected['persistence']['storageClass'], 'longhorn-delete')

    def test_no_admin_gui(self):
        """has_admin_gui=False：HAOS 管理者帳號由租戶自己在 onboarding 建立。

        開了會讓平台憑空生一組 admin_password 塞進 config.sensitive（chart 沒有
        對應鍵＝白生），並在前台長出一顆按了必失敗的「重置密碼」按鈕（#460 的
        能力 gate 直接鏡射本欄位）。
        """
        self.assertFalse(self._tpl().has_admin_gui)

    def test_not_mcp(self):
        """本體是 HAOS 作業系統，非 MCP server。"""
        self.assertFalse(self._tpl().mcp_enabled)

    def test_max_per_partner_one(self):
        """每帳號限一（Q5 首批限量 8 台的 per-partner 面 ＋ PVC 數量上限保護）。

        ns ResourceQuota 硬上限 `persistentvolumeclaims: 10`，而每台 HAOS 在
        import 期間短暫佔 3 顆 PVC（target/prime/scratch）→ 同 workspace 併發
        開通 3 台以上就撞上限（design §6.3）。
        """
        self.assertEqual(self._tpl().max_per_partner, 1)

    def test_description_mentions_vm_and_onboarding(self):
        """文案要說清楚「這是 VM、不是容器版 HA」與「帳號自己建、平台不持有」。"""
        tpl = self._tpl()
        text = (tpl.description or '') + (tpl.full_description or '')
        self.assertIn('虛擬機', text)
        self.assertIn('Supervisor', text)
        self.assertIn('8123', text)
        self.assertIn('管理者帳號', text)


@tagged('post_install', '-at_install')
class TestHaosImagePinW2Gate(_HaosSeedMixin, TransactionCase):
    """image.tag 雙釘鐵律的回歸守線。

    W2（woow-paas-ci 的 HAOS 18.1 golden image build）已於 run 1288 成功、digest
    已釘進本 seed，故本類現在綠。它守的是兩種退化：(a) tag 被改回佔位字面
    （W2_PENDING_TAG 或 chart 預設佔位）；(b) 換 image 時只改可讀版本、掉了
    `@sha256:` 那半 —— 後者是「以為換了 image 其實沒換」的靜默假成功。

    刻意**不**設 PENDING 白名單：#375/#380 的教訓是白名單一旦寫下就沒人回頭清，
    守衛會被靜默掏空。
    """

    def test_image_tag_double_pinned(self):
        tag = self._values()['image']['tag']
        self.assertIn(
            '@sha256:', tag,
            'image.tag 必須雙釘 `tag@sha256:…`（平台鐵律，對齊 woowtech-ha／'
            'open-design／hermes-agent）。目前值 %r。若是 %r 或 chart 預設佔位，'
            '代表 W2 已交付的真 digest 被改回佔位 —— 釘回 tag@digest 即轉綠；'
            '**不得**為了讓 CI 綠而放寬此斷言。' % (tag, W2_PENDING_TAG))


@tagged('post_install', '-at_install')
class TestHaosQuotaModelling(_HaosSeedMixin, TransactionCase):
    """🔴 min_* ＝ **quota 用量峰值**，不是賣給租戶的規格。

    全部由 seed 的 helm values 現算、不硬編：改了 vm.memory／vm.cores／
    persistence.size 而沒同步 min_*，這裡就紅。這是 #380「quota 不足 → pod
    FailedCreate → 服務對外永遠 502」血訓在 VM 型上的對應守線，而 VM 型更糟：
    症狀只是 DataVolume 停在 ImportInProgress，events 不挖就看不到 quota 拒收。

    workspace ns quota ＝ 存活 service 的 allocated_* 加總 × 3，下限
    8 vCPU / 8Gi / 100Gi（`cloud_service._compute_ns_quota`）。守線只比較本服務的
    **邊際貢獻**，不套 workspace floor；floor 只保護低用量 workspace，無法證明
    共用 workspace 每增加一個 HAOS 時仍有足額 quota。
    """

    # W1 PoC 實測（#423 D5）：virt-launcher 的 compute 容器 requests.memory
    # ＝ guest RAM ＋ KubeVirt overhead 384 MiB；另有 initContainer
    # guest-console-log 60M。
    KUBEVIRT_MEM_OVERHEAD_GI = 384 / 1024
    INIT_MEM_GI = 60 / 1024
    # CDI v1.64 的 WebhookPvcRendering 把宣告的 64Gi 渲染成 67.8 GiB
    # （加 filesystem overhead），實測比值。
    CDI_PVC_RENDER_FACTOR = 67.8 / 64
    # CDI volume populator 於 import 期間同時存在 target + prime + prime-scratch。
    CDI_IMPORT_PVC_COUNT = 3

    def _seed_numbers(self):
        values, tpl = self._values(), self._tpl()
        self.assertTrue(
            values['vm']['memory'].endswith('Gi'), values['vm']['memory'])
        self.assertTrue(
            values['persistence']['size'].endswith('Gi'),
            values['persistence']['size'])
        return {
            'cores': values['vm']['cores'],
            'mem_gi': float(values['vm']['memory'][:-2]),
            'disk_gi': float(values['persistence']['size'][:-2]),
            'min_vcpu': tpl.min_vcpu,
            'min_ram_gb': tpl.min_ram_gb,
            'min_storage_gb': tpl.min_storage_gb,
        }

    def test_min_star_values_are_the_d5_d6_measured_ones(self):
        """D5／D6 回填值：3 / 5.0 / 72（**非** 原設計推算的 3 / 6.0 / 64）；#1637 磁碟 64 → 30Gi 後
        儲存照同一比例（72／64＝1.125）改成 34（30×1.125＝33.75 → 34）。"""
        tpl = self._tpl()
        self.assertEqual(tpl.min_vcpu, 3)
        self.assertEqual(tpl.min_ram_gb, 5.0)
        self.assertEqual(tpl.min_storage_gb, 34)

    def test_min_storage_is_import_peak_not_disk_size(self):
        """🔴 min_storage_gb 填的是 CDI import 峰值，**不是** 30 GiB 磁碟大小。

        這是最容易被「看到 30 就改回去」的一條。30×3＝90 GiB 短少 5.3 GiB（64Gi 時代是 64×3＝192
        短少 11.5），
        scratch PVC 被 ErrExceededQuota 拒收，而 DataVolume 只顯示
        ImportInProgress、不再前進。
        """
        n = self._seed_numbers()
        self.assertGreater(
            n['min_storage_gb'], n['disk_gi'],
            'min_storage_gb（%s）必須大於賣給租戶的磁碟 %sGi —— 它填的是 CDI '
            'import 的三 PVC 峰值，不是磁碟大小'
            % (n['min_storage_gb'], n['disk_gi']))

    # 🔴 兩組 filler 都遠在 floor（8 vCPU／8Gi／100Gi）之上，用意是讓前後兩次
    # 量測都落在 quota 的線性區。⛔ 別把 filler 改小：floor 一旦咬住「加入本服務
    # 之前」那次，量到的差就會被灌水。這一格由
    # `test_measured_marginal_is_filler_independent` 守著——兩組不同 filler 量出
    # 同一個邊際值，就是「floor 沒有咬住任何一次」的證據。
    # RAM 取整數是必要的：production 的 `int()` 截斷作用在**加總**上，整數 filler
    # 才讓差值恰好等於本服務自己那一份的截斷值。
    _QUOTA_FILLERS = (
        {'vcpu': 40, 'ram_gb': 60.0, 'storage_gb': 400},
        {'vcpu': 100, 'ram_gb': 200.0, 'storage_gb': 1000},
    )

    def _parse_quota(self, quota):
        """把 production 回的 ``(vcpu, "NGi", "MGi")`` 轉成數字三元組。

        ⚠ 這裡耦合的是**回傳格式**（operator ``NamespaceQuotaUpdateRequest``
        的 wire contract），不是 multiplier。格式改了會在這裡大聲炸，不會靜默
        轉綠——與 #934 要根治的那種假綠方向相反。
        """
        vcpu, ram, storage = quota
        self.assertTrue(ram.endswith('Gi'), ram)
        self.assertTrue(storage.endswith('Gi'), storage)
        return int(vcpu), int(ram[:-2]), int(storage[:-2])

    def _measured_marginal(self, min_vcpu, min_ram_gb, min_storage_gb, filler):
        """🔴 用 production 的 `_compute_ns_quota` **實測**本服務的邊際貢獻。

        ⛔ 刻意不在測試裡重寫 `×3`。#931 把 floor 的重複拿掉了，卻把 multiplier
        的重複升格成兩個具名 helper：production 從 ×3 改成 ×2 時，per-service
        storage headroom 掉到 144Gi、蓋不住 203.5Gi 的 CDI import 峰值，而三支
        測試因為自己算 `72*3` 全部照樣綠（#934）。這裡改問 production 兩次——
        加進本服務之前、之後——取差。multiplier 住在哪個常數、叫什麼名字、之後
        怎麼重構，這條守線都跟得上。

        探針用**自建的**一次性 template，不用 HAOS 那張：`CloudService.create()`
        的供給閘（停售／未釘版／global cap）與 `max_per_partner` 都掛在 template
        上，借真商品當探針等於把這條守線綁在營運狀態上。
        """
        Svc = self.env['woow_paas_platform.cloud_service']
        ws = self.env['woow_paas_platform.workspace'].create(
            {'name': '#934 quota probe ws'})
        tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': '#934 quota probe template',
            'helm_repo_url': 'oci://probe.invalid/charts',
            'helm_chart_name': 'probe',
            'helm_chart_version': '0.0.0',
        })

        def _add(name, vcpu, ram_gb, storage_gb):
            return _internal_provision(Svc).create({
                'name': name,
                'workspace_id': ws.id,
                'template_id': tpl.id,
                'allocated_vcpu': vcpu,
                'allocated_ram_gb': ram_gb,
                'allocated_storage_gb': storage_gb,
            })

        _add('#934 filler', filler['vcpu'], filler['ram_gb'],
             filler['storage_gb'])
        before = self._parse_quota(Svc._compute_ns_quota(ws))
        _add('#934 probe', min_vcpu, min_ram_gb, min_storage_gb)
        after = self._parse_quota(Svc._compute_ns_quota(ws))
        return {
            'vcpu': after[0] - before[0],
            'ram_gi': after[1] - before[1],
            'storage_gi': after[2] - before[2],
        }

    def test_measured_marginal_is_filler_independent(self):
        """🔴 探針自身的守衛：兩組不同 filler 必須量出同一個邊際值。

        這一條證明的是「floor 沒有咬住任何一次量測」——floor 若在其中一次生效，
        兩組 filler 量出的差會不一樣。沒有它，`_measured_marginal` 可能安靜地
        回一個被 floor 灌水的數字，而下面兩支守線照樣綠。
        """
        n = self._seed_numbers()
        args = (n['min_vcpu'], n['min_ram_gb'], n['min_storage_gb'])
        first = self._measured_marginal(*args, self._QUOTA_FILLERS[0])
        second = self._measured_marginal(*args, self._QUOTA_FILLERS[1])
        self.assertEqual(
            first, second,
            'filler 大小改變了量到的邊際貢獻（%r vs %r）⇒ 至少有一次量測被 '
            'workspace floor 咬住，下面兩條守線讀到的是灌水後的數字'
            % (first, second))
        self.assertGreater(
            first['storage_gi'], 0,
            '邊際貢獻量到 0 ⇒ 探針沒真的把服務加進 workspace（或 '
            '_compute_ns_quota 沒看到它），此時下面的 assertGreaterEqual 等於沒跑')

    def test_marginal_quota_does_not_apply_workspace_floors(self):
        """below-floor 輸入的邊際貢獻不得被 workspace floor 抬起來。

        量的是 production 的實際行為、不是測試自己的算式：一個 1.25Gi／20Gi 的
        小服務加進已在 floor 之上的 workspace 時，只會讓 quota 增加它自己那一份，
        8Gi／100Gi 的 floor 對它完全不生效。floor 只保護低用量 workspace，證明不了
        共用 workspace 每增加一個 HAOS 時仍有足額 quota。
        """
        m = self._measured_marginal(1, 1.25, 20, self._QUOTA_FILLERS[0])
        self.assertLess(
            m['ram_gi'], 8,
            '1.25Gi RAM 的邊際貢獻量到 %sGi ⇒ 8Gi floor 被套在單一服務上'
            % m['ram_gi'])
        self.assertLess(
            m['storage_gi'], 100,
            '20Gi storage 的邊際貢獻量到 %sGi ⇒ 100Gi floor 被套在單一服務上'
            % m['storage_gi'])

    def test_storage_quota_covers_cdi_import_peak(self):
        """storage quota 的邊際貢獻必須蓋得住 import 峰值。

        峰值 ＝ 3 顆 PVC × (宣告大小 × CDI filesystem overhead)。
        實測：64Gi → 每顆 67.8 GiB → 峰值 203.5 GiB；72×3＝216 GiB 放行。#1637 改 30Gi 後峰值
        3×30×1.059＝95.3 GiB，34×3＝102 GiB 放行。

        `_compute_ns_quota` 的 100Gi floor 只在 workspace 總量低於 floor 時提供保護；
        共用 workspace 增加一個 HAOS 時只會增加它自己那一份。這裡若套 floor，
        較小的錯誤 min_storage_gb 可能在空 workspace 假綠、加入共用 workspace 才爆。

        邊際值是向 production **實測**的（`_measured_marginal`），不是在這裡重寫
        一次 quota 算式——理由見該 method 的 docstring（#934）。
        """
        n = self._seed_numbers()
        marginal_gi = self._measured_marginal(
            n['min_vcpu'], n['min_ram_gb'], n['min_storage_gb'],
            self._QUOTA_FILLERS[0])['storage_gi']
        peak_gi = (self.CDI_IMPORT_PVC_COUNT * n['disk_gi']
                   * self.CDI_PVC_RENDER_FACTOR)
        self.assertGreaterEqual(
            marginal_gi, peak_gi,
            'min_storage_gb=%s → 每個服務只替 workspace storage quota 加 %.1fGi，'
            '蓋不住 CDI import 峰值 %.1fGi（%d 顆 × %.1fGi × %.4f 渲染係數）'
            '→ scratch PVC 會被 ErrExceededQuota 拒收，DataVolume 永遠停在 '
            'ImportInProgress'
            % (n['min_storage_gb'], marginal_gi, peak_gi,
               self.CDI_IMPORT_PVC_COUNT, n['disk_gi'],
               self.CDI_PVC_RENDER_FACTOR))

    def test_ram_quota_covers_virt_launcher_total(self):
        """RAM quota 的邊際貢獻必須蓋得住 virt-launcher pod 總量。

        總量 ＝ guest RAM ＋ KubeVirt overhead（實測 384 MiB）＋ init 容器 60M，
        **不是** guest RAM 本身。chart 讓 limits==requests，故無 2× 放大。

        `_compute_ns_quota` 的 8Gi floor 只保護低用量 workspace；每新增一個 HAOS
        實際只增加它自己那一份。套 floor 會讓過小的 min_ram_gb 在空 workspace
        假綠，卻在共用 workspace 累積 quota 缺口。

        邊際值是向 production **實測**的（`_measured_marginal`），不是在這裡重寫
        一次 quota 算式——理由見該 method 的 docstring（#934）。
        """
        n = self._seed_numbers()
        marginal_gi = self._measured_marginal(
            n['min_vcpu'], n['min_ram_gb'], n['min_storage_gb'],
            self._QUOTA_FILLERS[0])['ram_gi']
        pod_total_gi = (n['mem_gi'] + self.KUBEVIRT_MEM_OVERHEAD_GI
                        + self.INIT_MEM_GI)
        self.assertGreaterEqual(
            marginal_gi, pod_total_gi,
            'min_ram_gb=%s → 每個服務只替 workspace RAM quota 加 %dGi，蓋不住 '
            'virt-launcher 總量 %.3fGi（guest %.1fGi ＋ KubeVirt overhead %.3fGi '
            '＋ init %.3fGi）'
            % (n['min_ram_gb'], marginal_gi, pod_total_gi, n['mem_gi'],
               self.KUBEVIRT_MEM_OVERHEAD_GI, self.INIT_MEM_GI))

    def test_min_vcpu_rounds_up_over_guest_cores(self):
        """min_vcpu 是 Integer，必須嚴格大於 guest cores。

        Σlimits.cpu ＝ cores ＋ initContainer 15m（實測 2.015），無條件進位
        ⇒ min_vcpu ≥ cores ＋ 1。把 cores 調大而忘了同步這裡就會紅。
        """
        n = self._seed_numbers()
        self.assertGreaterEqual(
            n['min_vcpu'], n['cores'] + 1,
            'min_vcpu（%s）須 ≥ guest cores（%s）＋1：Σlimits.cpu ＝ cores ＋ '
            'init 15m，Integer 欄位要無條件進位'
            % (n['min_vcpu'], n['cores']))

    def test_allocated_vals_fall_back_to_template_min_star(self):
        """實呼 `_allocated_vals_from`：plan cs_* 全 0 → allocated_* 取 min_*。

        這條把「plan 不覆寫規格」與「quota 建模」接起來 —— 只要有人在 plan 上
        填了 cs_*，這裡就會回別的值而紅。
        """
        tpl = self._tpl()
        Service = self.env['%s.cloud_service' % MOD]
        for xmlid in ('plan_haos_monthly', 'plan_haos_yearly'):
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            vals = Service._allocated_vals_from(tpl, plan)
            self.assertEqual(vals['allocated_vcpu'], tpl.min_vcpu, xmlid)
            self.assertEqual(vals['allocated_ram_gb'], tpl.min_ram_gb, xmlid)
            self.assertEqual(
                vals['allocated_storage_gb'], tpl.min_storage_gb, xmlid)


@tagged('post_install', '-at_install')
class TestHaosPlansSeed(TransactionCase):
    """Q5 定價拍板（Eugene 2026-07-30，#423）：月 4500／年 36000（×8）。"""

    # base plan → (price, unit)
    EXPECTED = {
        'plan_haos_monthly': (4500, 'month'),
        'plan_haos_yearly': (36000, 'year'),
    }

    def test_base_plans(self):
        for xmlid, (price, unit) in self.EXPECTED.items():
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            self.assertEqual(plan.sh_plan_price, price, xmlid)
            self.assertEqual(plan.sh_unit, unit, xmlid)
            self.assertEqual(plan.service_type, 'cloud_service', xmlid)

    def test_yearly_is_eight_times_monthly(self):
        """年繳＝月繳×8（沿用現行折扣慣例，省 33%）。"""
        monthly = self.env.ref('%s.plan_haos_monthly' % MOD)
        yearly = self.env.ref('%s.plan_haos_yearly' % MOD)
        self.assertEqual(yearly.sh_plan_price, monthly.sh_plan_price * 8)

    def test_products_taxfree_and_link(self):
        """product 綁對 plan、taxes_id 空（#366：pts 交易免稅）。"""
        for xmlid in self.EXPECTED:
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            product = self.env.ref(
                '%s.%s' % (MOD, xmlid.replace('plan_', 'product_')))
            self.assertEqual(product.sh_subscription_plan_id, plan, xmlid)
            self.assertFalse(product.taxes_id, '%s：pts 免稅、taxes_id 應為空' % xmlid)

    def test_plans_are_storefront_visible(self):
        """base plan 不隱藏、非席數變體（前台要看得到才賣得出去）。"""
        for xmlid in self.EXPECTED:
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            self.assertFalse(plan.storefront_hidden, xmlid)
            self.assertEqual(plan.ac_max_access_seats, 0, xmlid)

    def test_plans_do_not_override_cs_spec(self):
        """🔴 plan 的 cs_* 必須全為 0（走 template.min_* fallback）。

        填了會有兩個後果：(1) allocated_* 改吃 plan 值 —— 若填「賣給租戶的
        2/4/64」就直接讓 quota 掉回 192 GiB、重演 CDI import 卡死；
        (2) `_apply_plan_spec_overlay` 會把 `resources.requests/limits` 疊進
        values，那是**容器型**的鍵，本 chart 用 vm.cores／vm.memory ⇒ 死鍵。
        """
        for xmlid in self.EXPECTED:
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            self.assertEqual(plan.cs_vcpu, 0, xmlid)
            self.assertEqual(plan.cs_ram_gb, 0.0, xmlid)
            self.assertEqual(plan.cs_storage_gb, 0, xmlid)

    def test_template_m2m_base(self):
        """template ↔ 兩 base plan 的 M2M（links.xml noupdate=0 全量 reset）。"""
        tpl = self.env.ref('%s.cloud_app_haos' % MOD)
        plans = tpl.sudo().subscription_plan_ids
        base = plans.filtered(lambda p: p.ac_max_access_seats == 0)
        self.assertEqual(len(base), 2)
        for xmlid in self.EXPECTED:
            self.assertIn(self.env.ref('%s.%s' % (MOD, xmlid)), plans, xmlid)

    def test_no_access_variants_in_v1(self):
        """Q5 拍板 v1 **不做加購變體** —— template 上不得掛任何席數變體。

        HAOS 多為單機自用、席數不適用；規格升級留後續。要加變體請回 generator
        （scripts/seed/generate_plan_variants.py）＋變體 manifest 走一輪，
        不要手寫 (4,) append 進 links_variants.xml。
        """
        tpl = self.env.ref('%s.cloud_app_haos' % MOD)
        variants = tpl.sudo().subscription_plan_ids.filtered(
            lambda p: p.ac_max_access_seats > 0)
        self.assertFalse(
            variants,
            'v1 不做加購變體，實際掛上：%s' % variants.mapped('name'))

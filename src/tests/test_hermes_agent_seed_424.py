# -*- coding: utf-8 -*-
"""#424①：Hermes Agent 完整應用上架 seed ＋ 平台注入接線的守線斷言。

鏡射 test_opendesign_seed_380.py（金樣板 SOP）＋ test_ha_paas_seed_woowtech_image.py
（hidden template ＋ 原子綁定）兩者的結構。本檔守的是「錯了就靜默壞掉」的點：

- **image 雙釘**：webui 已由 Phase A（woow-paas-charts）鏡像進 jcr-prod 並補釘
  tag@digest，``_PENDING_IMAGE_KEYS`` 因此為空；``test_webui_image_pinned_digest``
  自「交付前刻意恆紅」轉為**回歸守線**（防 tag 被改回 PENDING 佔位或掉 @sha256）
  ——佔位被部署出去就是 Init:ErrImagePull（HA PaaS 2026-07-23 stg 實證的同一 bug class）。
- **quota 蓋不住全容器 limits**：#380 血訓，min_ram_gb×3 不足 → pod FailedCreate
  → 對外永遠 CF 502。這裡用**結構關係**斷言（由 seed values 現算），不硬編數字：
  一條擋太小、一條擋太大（＝把契約 v1.1 §8.8 的「÷3 上取」規則機械化）。無 DB
  拓撲下 limits 合計 8.625Gi（agent 6 + webui 2 + console 0.5 + authProxy 0.125）
  → min_ram_gb=3.0，headroom 只剩 0.375Gi，**加任何容器都會撞紅**（刻意）。
- **DB 開關 lock-step**：Step 0 若翻案要開內建 pg/redis，storageClass path_map ／
  min_ram_gb ／ min_storage_gb 三處必須同 PR 一起動，否則 PVC 拿不到 StorageClass
  或 quota 不足。三條 lock-step 測試把它們綁死。
- **quota 蓋不住全容器 limits.cpu**（#456，2026-07-28 stg 實證）：RAM 早有守線、
  CPU 沒有，於是 `min_vcpu=1` 的邊際貢獻（×3＝3）蓋不住 3.4，共用 workspace 的
  主 pod 永久卡 FailedCreate 而 DB／UI 照樣顯示 Running。#494 上架時修成 2，並補
  上與 RAM 對稱的兩條 CPU 守線。**四條 quota 守線一律斷言「邊際貢獻」、不套
  `max(..., floor)`**——floor 只在「workspace 裡只有這一個服務」時成立，套了就會
  讓不足的值假綠（#456 正是這樣溜到 stg 的）。RAM 側原本仍留著含 floor 的寫法，
  #494 review 抓出這個一半的修法後一併校齊；兩側各只有 `_marginal_gi` /
  `_marginal_cpu` 一個定義點，避免再次分歧。
- **quota 守線的前提是 sidecar 規格不可從 seed 覆寫**：auth-proxy 的 resources 不在
  seed values 裡，是以常數代表 chart 契約值計入總量。chart 有那個鍵、seed 走全量
  管線 ⇒ 覆寫得掉，覆寫後四條斷言全部以低估值計算而假綠。由
  ``test_auth_proxy_resources_stay_chart_contract`` 明文鎖住（#494 review finding 2）。
- **定價上架形態**（#494）：`is_hidden=False` ＋ 兩 base plan（4,500／36,000／7 天
  trial）＋ M2M；plan 刻意不設 `cs_*`（設了就繞過上面的 quota 修法）；
  `conceal_instances` 維持不設（#471 拆語意，實例層不受曝光層變更影響）。
- **主 Service 命名**：operator 的 Cloudflare expose 取「名稱排序第一個非 headless
  Service」＋http-like port 猜測，主 Service 必須是裸 release fullname ＋ 8080。
- **平台注入鍵不得被 seed 蓋掉**：storageClass／imagePullSecrets／apiServerCidrs／
  dashboardPublicUrl／config.sensitive 全由平台每次部署重算注入（#411 reset 語意）。

平台側新接線（本 PR）另有兩條端到端斷言：``_inject_dashboard_public_url``
（MCP OAuth 回呼前提）與 ``_assemble_deploy_values`` 對 hermes-agent 的全量輸出。
"""
import json

from odoo.tests.common import HttpCase, TransactionCase, tagged

PLATFORM_REGISTRY = 'jcr-prod.woowtech.io/'
HERMES_AGENT_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/hermes-agent'
HERMES_WEBUI_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/hermes-webui'
WEBUI_PENDING_TAG = 'PENDING_PHASE_A_WEBUI_DIGEST'

# 尚未釘 digest 的 image 座標白名單（顯式、可 review）。每一筆都必須另有一條專屬的
# fail-loud 測試扛紅；補釘後從這裡移除。**只允許暫時性項目**，不是長期豁免清單。
# 留下死條目（該 image 已補釘卻還列在這）＝那顆 image 從此永久跳過全域掃描守衛，
# 所以 test_every_seeded_image_is_pinned 末尾會反過來斷言本集合無死條目。
_PENDING_IMAGE_KEYS = set()

# chart 的 auth-proxy sidecar（nginx-unprivileged）resources 不在 seed values 內
# ——它是 chart 契約值。此常數＝charts/hermes-agent values.yaml 的
# `authProxy.resources.limits.memory`（128Mi，已逐字核對）。chart 若改動 sidecar
# limits，本常數必須同步，否則 quota 斷言會低估。
# （同區塊的 initResources 刻意不計入總量：pod 的有效值取
#  max(sum(一般容器), max(initContainer))，init 的 64Mi limits 遠低於容器合計，
#  不影響 quota 額度。⚠ 但「不疊加總量」≠「可以不寫」——namespace 對某資源設了
#  ResourceQuota 時，**含 initContainer 在內每個容器都必須宣告該資源**，缺一即
#  pod 直接被拒（FailedCreate，是宣告問題不是額度問題）。chart 兩邊都有寫。）
AUTH_PROXY_LIMIT_GI = 128 / 1024.0

# 同上，CPU 側：chart values 的 `authProxy.resources.limits.cpu`（200m）。2026-07-31
# 以 `helm show values oci://…/hermes-agent --version 0.1.0` 向 registry 直驗，非轉抄。
# #456 的 stg 事件訊息（exceeded quota: limits.cpu=3200m）正是主 pod 的
# agent 2000m ＋ webui 1000m ＋ 本 sidecar 200m，可交叉印證。
AUTH_PROXY_LIMIT_CPU = 0.2


def _gi(quantity):
    """把 K8s 記憶體 quantity（'6Gi' / '512Mi'）換算成 float GiB。"""
    if quantity.endswith('Gi'):
        return float(quantity[:-2])
    if quantity.endswith('Mi'):
        return float(quantity[:-2]) / 1024.0
    raise AssertionError('未預期的記憶體單位（只支援 Gi/Mi）：%s' % quantity)


def _cpu(quantity):
    """把 K8s CPU quantity（'2' / '200m'）換算成 float core。"""
    if isinstance(quantity, (int, float)):
        return float(quantity)
    if quantity.endswith('m'):
        return float(quantity[:-1]) / 1000.0
    return float(quantity)


@tagged('post_install', '-at_install')
class TestHermesAgentTemplateSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_hermes_agent')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    # ── 上架形態（已定價開賣，#494）──────────────────────────────────────────
    def test_marketplace_fields(self):
        """name/slug/category、對外埠 8080（auth-proxy）、ingress 開。"""
        tpl = self._tpl()
        self.assertEqual(tpl.name, 'Hermes Agent')
        self.assertEqual(tpl.slug, 'hermes-agent')
        self.assertEqual(tpl.category, 'ai_llm')
        self.assertEqual(tpl.default_port, 8080)
        self.assertTrue(tpl.ingress_enabled)

    def test_visible_and_deployable(self):
        """#494 定價拍板解封：is_hidden=False（generic create 放行）＋ is_active=True。

        #424① 上架時因定價未拍板而 is_hidden=True；4,500／36,000 拍板後比照 #431
        模式解封（翻 is_hidden ＋掛 plan）。hidden 是 generic create 的唯一擋線
        （controllers/paas.py `_create_service`），翻回 True 等於整個商品下架。
        """
        tpl = self._tpl()
        self.assertFalse(
            tpl.is_hidden,
            '已定價上架：is_hidden 必須為 False，否則 marketplace 買不到')
        self.assertTrue(tpl.is_active, 'template 須可部署')

    def test_conceal_instances_untouched(self):
        """🔴 #471 拆語意：本 template 刻意**不**設 conceal_instances。

        解封只該動「曝光層」（is_hidden）。conceal_instances 是「實例層」——設了
        會讓既有租戶服務的詳情頁打不開、rollback／change-plan／MCP 設定全不可達
        （#464 在本 template 實測過）。cloud_app_ha_paas 的「兩層都關」是刻意的
        對照組，勿照抄。
        """
        self.assertFalse(
            self._tpl().conceal_instances,
            '本 template 只有曝光層語意；設了 conceal_instances 會鎖死既有實例')

    def test_subscription_plans_linked(self):
        """#494：template ↔ 兩 base plan 的 M2M（links.xml noupdate=0 全量 reset）。"""
        tpl = self._tpl()
        plans = tpl.sudo().subscription_plan_ids
        for xmlid in ('plan_hermes_agent_monthly', 'plan_hermes_agent_yearly'):
            self.assertIn(
                self.env.ref('woow_paas_platform.%s' % xmlid), plans, xmlid)
        base = plans.filtered(lambda p: p.ac_max_access_seats == 0)
        self.assertEqual(
            len(base), 2, '恰兩筆 base plan（月／年）；v1 不做席數加購變體')

    def test_max_per_partner_one(self):
        """每帳號限一（agent limits 6Gi 是平台最重單容器，防同帳號吃乾 ws quota）。"""
        self.assertEqual(self._tpl().max_per_partner, 1)

    def test_distinct_from_mcp_hermes(self):
        """與既有 cloud_app_mcp_hermes 是不同產品：slug／chart／image 全不同。

        撞名是本次最容易犯的錯（#424 brief 風險 10）：那筆是 Hermes 的 MCP server
        （chart mcp-server、image hermes-mcp-admin），本筆是 Hermes Agent 完整應用。
        """
        mcp = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        tpl = self._tpl()
        self.assertNotEqual(tpl.id, mcp.id)
        self.assertNotEqual(tpl.slug, mcp.slug)
        self.assertNotEqual(tpl.helm_chart_name, mcp.helm_chart_name)
        self.assertNotEqual(
            self._values()['image']['repository'],
            json.loads(mcp.helm_default_values)['image']['repository'])

    def test_not_mcp_server(self):
        """完整應用非 MCP server：mcp_enabled=False（其 MCP 是「去接外部 MCP」）。"""
        self.assertFalse(self._tpl().mcp_enabled)

    # ── auth 單一閘（拍板 Q3）───────────────────────────────────────────────
    def test_has_admin_gui_single_gate(self):
        """has_admin_gui=True：平台生成一組 admin_password 餵 webui/dashboard/ttyd/proxy。"""
        self.assertTrue(
            self._tpl().has_admin_gui,
            '單一閘 basic-auth 憑證＝#357 admin_password 供給鏈，須開 has_admin_gui')

    def test_auth_proxy_gate_explicit(self):
        """authProxy 閘明寫（防 chart 預設漂移把租戶邊界關掉）。

        ⚠ 形狀是**扁平布林** `basicAuth: true` ＋ 獨立的 `basicAuthUsername`，
        不是 open-design 那種巢狀 `{enabled, username}`——已對 charts/hermes-agent
        的 values.yaml 逐字核對。寫成巢狀會在 helm render 期炸（`.enabled` 取不到
        bool 的欄位），屬 fail-loud，但一樣要等到部署才發現。
        """
        ap = self._values()['authProxy']
        self.assertTrue(ap['enabled'], '關掉 auth-proxy＝服務裸奔對外')
        self.assertIs(
            ap['basicAuth'], True,
            'authProxy.basicAuth 是扁平布林（chart 契約），不是巢狀物件')
        self.assertEqual(ap['basicAuthUsername'], 'admin')

    def test_auth_proxy_worker_processes_pinned(self):
        """nginx worker 數必須釘死（chart values.yaml 的 ★ 註解）。

        基底 image 預設 `worker_processes auto`＝每個 **host** CPU 一個 worker、
        且不看 cgroup：排到 256 vCPU 的 node 就 fork 256 個 worker 撐爆 128Mi
        limit → OOMKilled(137) CrashLoop。chart 預設已是 2，seed 重申防漂移。
        """
        self.assertEqual(self._values()['authProxy']['workerProcesses'], 2)

    def test_dashboard_gates_explicit(self):
        """dashboard 的兩個「設錯就靜默失去防護／靜默不對外」鍵明寫。

        - bindHost 非 loopback ⇒ 上游 fail-closed auth gate **保持啟用**（改成
          127.0.0.1 就只剩 nginx 一層）。
        - externalPath 空字串 ⇒ dashboard 根本不對外路由，但平台照樣把
          dashboardPublicUrl 當 OAuth redirect_uri 發出去 → 靜默死鏈。
        """
        dashboard = self._values()['dashboard']
        self.assertEqual(
            dashboard['bindHost'], '0.0.0.0',
            'loopback 綁定會關掉 dashboard 自身的 fail-closed 登入閘')
        self.assertTrue(dashboard['externalPath'], 'externalPath 為空＝dashboard 不對外')
        self.assertEqual(dashboard['username'], 'admin')

    def test_dashboard_external_path_matches_injector(self):
        """跨層 lock-step：seed 的 dashboard.externalPath ＝平台注入 URL 的尾綴。

        chart 用 externalPath 決定 auth-proxy 把 /dashboard/ 轉去哪、以及 ttyd 式的
        資產前綴；平台則把 `{public}/dashboard` 當 OAuth redirect_uri 的權威值送進
        `config.dashboardPublicUrl`。兩者任一單獨改動 → 授權回呼打到不存在的路徑，
        而且**兩邊各自看起來都正常**。這條把它們綁死。
        """
        external_path = self._values()['dashboard']['externalPath']
        injected = self.env['woow_paas_platform.cloud_service']._inject_dashboard_public_url(
            {}, 'hermes-agent', 'https://svc-abc.woowtech.io',
        )['config']['dashboardPublicUrl']
        self.assertEqual(
            injected, 'https://svc-abc.woowtech.io' + external_path,
            '平台注入的 dashboardPublicUrl 尾綴與 seed 的 dashboard.externalPath 不一致')

    def test_no_sensitive_values_in_git(self):
        """admin_password／LLM key 不得出現在 git 的 helm_default_values。

        先做陽性對照再談「不含憑證」：``assertNotIn(secret, '')`` 恆真，而空字串
        是拿得到的——實測 Odoo 18 的 Text 欄位寫入 ``''`` 會原樣存進 DB（不是
        NULL）、讀回也是 ``''``，連 ``''.lower()`` 都不會炸。沒有這行的話，seed
        這欄被清空 ⇒ 這條「憑證不得進 git」的唯一守線靜默失效。
        """
        raw = self._tpl().helm_default_values
        self.assertIn(
            'authProxy', raw,
            'helm_default_values 裡找不到預期的 authProxy 區塊 —— 探針沒讀到真正的'
            ' seed values（欄位被清空／改名），下面三條憑證斷言等於沒跑')
        self.assertNotIn('admin_password', raw)
        self.assertNotIn('minimax', raw.lower())
        self.assertNotIn('openrouter', raw.lower())

    def test_llm_keys_are_optional_specs(self):
        """LLM key 走 helm_value_specs optional password（拍板 Q2：租戶自帶）。"""
        specs = json.loads(self._tpl().helm_value_specs)
        self.assertEqual(specs['required'], [], '本輪無必填欄（key 皆選填）')
        by_key = {o['key']: o for o in specs['optional']}
        self.assertIn('auth.minimaxApiKey', by_key)
        self.assertIn('auth.openrouterApiKey', by_key)
        for key, spec in by_key.items():
            self.assertEqual(
                spec['type'], 'password',
                '%s 是憑證，type 必須 password（前端遮蔽 + 不回顯）' % key)

    # ── chart / image pin ───────────────────────────────────────────────────
    def test_chart_oci_pin(self):
        """chart＝woow-paas-charts 發佈的獨立 hermes-agent OCI chart 0.1.0。"""
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(
            tpl.helm_chart_name,
            'oci://jcr-prod.woowtech.io/woow-paas-docker-local/hermes-agent')
        self.assertEqual(tpl.helm_chart_version, '0.1.0')

    def test_agent_image_pinned_digest(self):
        """agent image＝#424② 交付的衍生 image（含 MCP OAuth build-time patch），雙釘。

        用上游原版 image 會沒有 OAuth patch → MCP 授權跳窗流程壞掉（#424② 全部白做）。
        """
        image = self._values()['image']
        self.assertEqual(image['repository'], HERMES_AGENT_IMAGE)
        tag = image['tag']
        self.assertNotEqual(tag, 'latest', '不得留 latest 可變 tag')
        self.assertIn('@sha256:', tag, 'image 必須雙釘 tag@sha256')
        build_tag, digest = tag.split('@', 1)
        self.assertRegex(
            build_tag, r'^\d{8}\.\d{4}$', 'build tag 應為 UTC 時間戳 YYYYMMDD.HHMM')
        self.assertEqual(len(digest), len('sha256:') + 64, digest)

    def test_webui_image_pinned_digest(self):
        """webui image 必須雙釘 tag@sha256（鏡像進 jcr-prod 後已補釘）。

        歷史：本條在鏡像交付前**刻意恆紅、不 skip** —— 留 PENDING 佔位部署出去就是
        Init:ErrImagePull（HA PaaS 2026-07-23 stg 實證的同一 bug class）。
        WEBUI_PENDING_TAG 斷言留著，防止有人回退成佔位。

        釘的是 multi-arch OCI index 底下的 **amd64 子 manifest** digest，與
        open-design-console 現行 seed 同款（agent image 本身即單一 amd64 manifest，
        整個 pod 本來就 amd64-only）。
        """
        image = self._values()['webui']['image']
        self.assertEqual(image['repository'], HERMES_WEBUI_IMAGE)
        tag = image['tag']
        self.assertNotEqual(
            tag, WEBUI_PENDING_TAG,
            'Phase A 的 webui 鏡像 tag@digest 尚未補釘（PR 合併前必補真值）')
        self.assertNotEqual(tag, 'latest', '不得留 latest 可變 tag')
        self.assertIn('@sha256:', tag, 'webui image 必須雙釘 tag@sha256')

    def test_console_image_shared_with_opendesign(self):
        """console 直接重用 open-design 的 baked image，且與該 seed **同一 tag@digest**。

        console image 的內容與應用無關（ttyd + kubectl + flask，app code 由 chart
        ConfigMap 熱注入），刻意不 build 第二顆。兩筆 seed 釘同一 digest 是結構
        invariant：任一邊單獨升版就代表有人忘了另一邊。
        """
        image = self._values()['console']['image']
        od = json.loads(
            self.env.ref('woow_paas_platform.cloud_app_opendesign')
            .helm_default_values)
        self.assertEqual(
            image, od['console']['image'],
            'console image 應與 cloud_app_opendesign 完全一致（含 tag@digest）')
        # 契約 v1.1 §8.8：三個 image tag 一律 fail-loud 必釘（不因「沿用別人的值」豁免）
        self.assertNotEqual(image['tag'], 'latest', 'console 不得留 latest')
        self.assertIn('@sha256:', image['tag'], 'console image 必須雙釘 tag@sha256')

    def test_every_seeded_image_is_pinned(self):
        """掃過 seed values 內**每一個** image 座標，依 registry 分類要求釘法。

        逐一列舉的測試只守得住「今天有的那幾顆」；chart 日後多長一個容器而 seed 跟著
        加一個 image 區塊時，逐一列舉不會紅。這條是零丟棄式守衛。

        兩級釘法（依 chart values.yaml 的分類註解）：

        - **平台產品 image**（jcr-prod）：一律雙釘 ``tag@sha256``。
        - **基礎設施 sidecar**（非 jcr-prod，如 nginx-unprivileged）：比照 open-design
          只釘 tag，但仍不得為空或 ``latest``。

        `_PENDING_IMAGE_KEYS` 是**顯式可 review 的例外清單**（而非讓斷言默默放寬）：
        目前只有 webui 一顆待鏡像，它的紅由 ``test_webui_image_pinned_digest``
        專門承擔，避免同一個成因重複兩條紅。
        """
        found = []

        def walk(node, path):
            if not isinstance(node, dict):
                return
            for key, val in node.items():
                sub = path + (key,)
                if key == 'image' and isinstance(val, dict) and 'repository' in val:
                    found.append((sub, val))
                else:
                    walk(val, sub)

        values = self._values()
        # 頂層 image 沒有 'image' 這層 key，單獨收
        if 'repository' in values.get('image', {}):
            found.append((('image',), values['image']))
        walk({k: v for k, v in values.items() if k != 'image'}, ())

        self.assertTrue(found, '前置：seed 應解析出 image 座標（防走訪器壞掉變空集合）')
        # 白名單自清：條目一旦補釘就必須從 _PENDING_IMAGE_KEYS 移除，否則那顆 image
        # 從此永久跳過本掃描（守衛被自己的例外清單掏空，而且沒有任何測試會紅）。
        stale = {
            '.'.join(p) for p, img in found
            if p in _PENDING_IMAGE_KEYS and '@sha256:' in (img.get('tag') or '')
        }
        self.assertFalse(
            stale,
            '%s 已補釘卻仍留在 _PENDING_IMAGE_KEYS（死條目會讓它永久跳過掃描）' % stale)
        unknown = _PENDING_IMAGE_KEYS - {p for p, _ in found}
        self.assertFalse(
            unknown, '_PENDING_IMAGE_KEYS 指向 seed 裡不存在的路徑：%s' % unknown)
        for path, image in found:
            if path in _PENDING_IMAGE_KEYS:
                continue
            label = '.'.join(path)
            tag = image.get('tag') or ''
            self.assertTrue(tag, '%s 的 tag 為空（chart 會 render 失敗）' % label)
            self.assertNotEqual(tag, 'latest', '%s 不得用可變的 latest' % label)
            if image['repository'].startswith(PLATFORM_REGISTRY):
                self.assertIn(
                    '@sha256:', tag,
                    '%s 是平台產品 image，必須雙釘 tag@sha256（若確為待補，'
                    '加進 _PENDING_IMAGE_KEYS 並補一條專屬 fail-loud 測試）' % label)

    # ── expose 契約（錯了就路由到別的 Service）───────────────────────────────
    def test_main_service_naming_and_port(self):
        """主 Service＝裸 release fullname ＋ 8080（operator expose 偵測的命中條件）。

        operator 取「名稱排序第一個非 headless Service」＋http-like port 猜測清單；
        service.name 明寫空字串＝chart fullname（svc-{ref8}），排在 -console 等後綴
        Service 之前。填了固定名還會在同 ns 開兩台時撞名。
        """
        service = self._values()['service']
        self.assertEqual(service['port'], 8080)
        self.assertEqual(service['port'], self._tpl().default_port)
        self.assertEqual(
            service['name'], '',
            'service.name 必須留空＝release fullname，否則 expose 可能挑錯 Service')

    def test_console_subpath(self):
        """console 掛 /console 子路徑（與 open-design 同款，ttyd -b /console）。"""
        console = self._values()['console']
        self.assertTrue(console['enabled'])
        self.assertEqual(console['externalPath'], '/console')

    # ── 平台注入鍵不得被 seed 蓋掉 ──────────────────────────────────────────
    def test_platform_injected_keys_absent(self):
        """平台每次部署重算注入的鍵，seed 一律不得出現（#411 reset 語意）。

        - persistence.storageClass：_inject_storage_class
        - imagePullSecrets / global.imagePullSecrets：_inject_image_pull_secrets
          ＋ operator setdefault
        - networkPolicy.apiServerCidrs：_inject_apiserver_cidrs（cluster-specific）
        - config.dashboardPublicUrl：_inject_dashboard_public_url（per-service）
        - config.sensitive：#357 sensitive 供給鏈（絕不入 git）
        """
        values = self._values()
        self.assertNotIn('storageClass', values['persistence'])
        self.assertNotIn('storageClass', values['persistence']['data'])
        self.assertNotIn('imagePullSecrets', values)
        self.assertNotIn('global', values)
        self.assertNotIn('networkPolicy', values)
        self.assertNotIn('config', values)

    # ── quota / 規格（#380 血訓）────────────────────────────────────────────
    def _seed_container_limits_gi(self):
        """由 seed values 現算「本服務全容器 limits.memory」總和（GiB）。

        涵蓋 agent／webui（daemon pod）＋ console（獨立 Deployment，同 ns 同 quota）
        ＋ auth-proxy sidecar（chart 契約值，見 AUTH_PROXY_LIMIT_GI）；DB 開啟時
        另計 pg／redis。刻意用「現算」而非硬編：resources 一改，斷言自動跟上。
        """
        values = self._values()
        total = AUTH_PROXY_LIMIT_GI
        for path in (('agent',), ('webui',), ('console',),
                     ('postgresql',), ('redis',)):
            node = values.get(path[0]) or {}
            if not node or node.get('enabled') is False:
                continue
            limits = (node.get('resources') or {}).get('limits') or {}
            if limits.get('memory'):
                total += _gi(limits['memory'])
        return total

    def _seed_container_limits_cpu(self):
        """CPU 側的對稱版本：由 seed values 現算「本服務全容器 limits.cpu」總和。

        #456 的根因就在這條線上：邊際需求 3.4（agent 2 ＋ webui 1 ＋ console 0.2
        ＋ auth-proxy 0.2）> 邊際貢獻 min_vcpu×3。RAM 早有守線、CPU 沒有，所以
        「共用 workspace 才露餡」的缺口一路活到 stg。
        """
        values = self._values()
        total = AUTH_PROXY_LIMIT_CPU
        for key in ('agent', 'webui', 'console', 'postgresql', 'redis'):
            node = values.get(key) or {}
            if not node or node.get('enabled') is False:
                continue
            limits = (node.get('resources') or {}).get('limits') or {}
            if limits.get('cpu'):
                total += _cpu(limits['cpu'])
        return total

    @staticmethod
    def _marginal_gi(min_ram_gb):
        """本服務替 workspace quota 帶來的**記憶體邊際貢獻**（Gi）。

        ＝ `_compute_ns_quota` 記憶體算式的 per-service 項（**int 是截斷**），
        **刻意不套 `max(..., 8)` 的 floor**——理由同 CPU 側（見
        test_min_vcpu_covers_all_container_limits）：floor 只在「workspace 裡只有
        這一個服務」時成立，拿它當守線會讓不足的值假綠。
        """
        return int(min_ram_gb * 3)

    @staticmethod
    def _marginal_cpu(min_vcpu):
        """本服務替 workspace quota 帶來的 **CPU 邊際貢獻**（vCPU）。

        與 `_marginal_gi` 對稱，同樣不套 floor。兩側各只有這一個定義點——想改成
        「含 floor」時只會改到一處，不會出現一側修好、另一側還留著假綠的分歧
        （#494 review 抓到的正是這種一半的修法）。
        """
        return min_vcpu * 3

    def test_min_ram_covers_all_container_limits(self):
        """結構關係守線：min_ram_gb 的**邊際貢獻**須蓋住全容器 limits.memory 總和。

        quota = max(int(sum(存活 service allocated_ram_gb) × 3), 8) Gi
        （cloud_service._compute_ns_quota）。本 template 的 plan 刻意不設 cs_*
        → allocated_ram_gb 走 min_ram_gb fallback（見
        test_allocated_vals_fall_back_to_template_min_star）。不足 → pod
        FailedCreate(exceeded quota) → 服務對外永遠 CF 502（#380 端到端回歸）。

        ⚠ 與 CPU 側同樣**只看邊際、不套 floor**（8Gi）。本 template 目前的邊際
        （9Gi）本來就高於 floor，兩種寫法同結論；但只要有人把 limits.memory 調到
        合計 8Gi 以下，套 floor 的版本就會**恆綠**——連 min_ram_gb=0.5（邊際僅
        1.5Gi）都放行，共用 workspace 一樣爆。#494 review 指出的自我弱化。
        """
        tpl = self._tpl()
        marginal_gi = self._marginal_gi(tpl.min_ram_gb)
        needed_gi = self._seed_container_limits_gi()
        self.assertGreaterEqual(
            marginal_gi, needed_gi,
            'min_ram_gb=%s → 每個服務只替 workspace quota 加 %sGi，蓋不住全容器'
            ' limits.memory 合計 %.3fGi → 共用 workspace 必然 FailedCreate'
            % (tpl.min_ram_gb, marginal_gi, needed_gi))

    def test_min_ram_is_the_minimal_sufficient_step(self):
        """min_ram_gb ＝滿足上一條的**最小** 0.5 級距值（契約 v1.1 §8.8「÷3 上取」）。

        上一條只擋「太小」，這條擋「太大」：quota 是 workspace 級的資源天花板，
        灌水等於把同 workspace 其他服務的保護一起放鬆。兩條合起來把 §8.8 的定值
        規則完全機械化——limits 一改，期望值自動跟著算，不用回頭改數字。

        真要留額外 headroom 時就改這條並在訊息裡寫明理由，讓它成為一個**看得見的
        決定**，而不是一個沒人知道為什麼的浮動數字。

        ⚠ 同樣用邊際解 minimal：套 floor 的版本在 limits 合計 ≤ 8Gi 時會解出
        `0.5` 並用 assertEqual **強制把不安全值寫進 seed**——擋太大的那一側反而
        變成擋著「唯一安全的值」。
        """
        needed_gi = self._seed_container_limits_gi()
        minimal = 0.5
        while self._marginal_gi(minimal) < needed_gi:
            minimal += 0.5
        self.assertEqual(
            self._tpl().min_ram_gb, minimal,
            '全容器 limits.memory 合計 %.3fGi → 最小夠用的 min_ram_gb 是 %s'
            % (needed_gi, minimal))

    def test_min_vcpu_covers_all_container_limits(self):
        """🔴 #456 修法守線：min_vcpu 的**邊際貢獻**須蓋住全容器 limits.cpu 總和。

        ⚠ 這裡刻意用「邊際」（min_vcpu × 3）而非 `_compute_ns_quota` 的含 floor
        值——floor（8 vCPU）只在「workspace 裡只有這一個服務」時成立。#456 就是
        這樣溜過去的：#424① 的 E2E 全在空 workspace 跑，靠 floor 蓋住而全綠；一放
        到既有服務的共用 workspace，每加一個服務缺口就擴大 0.4，主 pod 永久卡
        FailedCreate(exceeded quota: limits.cpu=3200m)，而 DB／UI 照樣顯示
        Running——調度失敗不反映在服務狀態，所以只能靠這條在 seed 當下擋。

        套 floor 的寫法會讓 min_vcpu=1（＝#456 的缺陷值）假綠，等於守線失效。
        """
        tpl = self._tpl()
        marginal = self._marginal_cpu(tpl.min_vcpu)
        needed = self._seed_container_limits_cpu()
        self.assertGreaterEqual(
            marginal, needed,
            'min_vcpu=%s → 每個服務只替 workspace quota 加 %s vCPU，蓋不住本服務'
            '全容器 limits.cpu 合計 %.2f → 共用 workspace 必然 FailedCreate（#456）'
            % (tpl.min_vcpu, marginal, needed))

    def test_min_vcpu_is_the_minimal_sufficient_step(self):
        """min_vcpu ＝滿足上一條的**最小**整數（欄位是 Integer，級距 1）。

        與 RAM 側同樣的兩面夾擊：上一條擋太小、這條擋太大。quota 是 workspace 級
        天花板，灌水等於把同 workspace 其他服務的保護一起放鬆。合計 3.4 ÷ 3 上取
        ＝ 2；limits 一改，期望值自動跟著算。
        """
        needed = self._seed_container_limits_cpu()
        minimal = 1
        while self._marginal_cpu(minimal) < needed:
            minimal += 1
        self.assertEqual(
            self._tpl().min_vcpu, minimal,
            '全容器 limits.cpu 合計 %.2f → 最小夠用的 min_vcpu 是 %d'
            % (needed, minimal))

    def test_auth_proxy_resources_stay_chart_contract(self):
        """🔴 上面四條 quota 守線的**前提**：seed 不得覆寫 auth-proxy 的 resources。

        agent／webui／console 的 limits 由 `_seed_container_limits_*` 從 seed values
        現算，改了就自動反映；唯獨 auth-proxy sidecar 的 resources **不在 seed 裡**，
        是以常數（AUTH_PROXY_LIMIT_GI／AUTH_PROXY_LIMIT_CPU）代表 chart 契約值計入。

        但 chart **有** `authProxy.resources` 這個鍵，而 seed 的 helm_default_values
        走全量 values 管線 ⇒ 真的覆寫得掉。一旦有人從 seed 寫了更大的 limits，常數
        與實際脫節、四條 quota 斷言全部以**低估值**計算，卻照樣全綠——守線在最需要
        它的時候失效（#494 review finding 2）。

        `initResources` 一併鎖：pod 的有效需求取 max(Σ一般容器, max(initContainer))，
        init 調得夠大一樣會撐破 quota，而它同樣不在現算範圍內。

        要調 sidecar 規格請改 chart，並同步這裡的兩個常數。
        """
        auth_proxy = self._values().get('authProxy') or {}
        for key in ('resources', 'initResources'):
            self.assertNotIn(
                key, auth_proxy,
                'seed 覆寫了 authProxy.%s：quota 守線是以 chart 契約常數'
                '（AUTH_PROXY_LIMIT_GI／AUTH_PROXY_LIMIT_CPU）把這顆 sidecar 計入的，'
                '從 seed 覆寫會讓四條 quota 斷言以低估值計算而假綠。'
                '要調規格請改 chart 並同步那兩個常數。' % key)

    def test_allocated_vals_fall_back_to_template_min_star(self):
        """實呼 `_allocated_vals_from`：plan cs_* 全 0 → allocated_* 取 template min_*。

        把「plan 不覆寫規格」與「quota 建模」接起來——只要有人在 hermes plan 上
        填了 cs_*（#456 stg 臨時測試用的 0 元 plan 就填了 1/3.0/5），這裡立刻回
        別的值而紅，上面兩條 CPU／RAM 守線的前提也隨之失效。
        """
        tpl = self._tpl()
        Service = self.env['woow_paas_platform.cloud_service']
        for xmlid in ('plan_hermes_agent_monthly', 'plan_hermes_agent_yearly'):
            plan = self.env.ref('woow_paas_platform.%s' % xmlid)
            vals = Service._allocated_vals_from(tpl, plan)
            self.assertEqual(vals['allocated_vcpu'], tpl.min_vcpu, xmlid)
            self.assertEqual(vals['allocated_ram_gb'], tpl.min_ram_gb, xmlid)
            self.assertEqual(
                vals['allocated_storage_gb'], tpl.min_storage_gb, xmlid)

    def test_resources_present_on_every_seeded_container(self):
        """每個 seed 覆寫的容器都要 requests+limits 齊備（paas-ws quota 缺一即 FailedCreate）。"""
        values = self._values()
        for key in ('agent', 'webui', 'console'):
            resources = values[key].get('resources')
            self.assertTrue(resources, '%s 缺 resources' % key)
            for side in ('requests', 'limits'):
                self.assertIn(side, resources, '%s.resources 缺 %s' % (key, side))
                for dim in ('cpu', 'memory'):
                    self.assertIn(
                        dim, resources[side],
                        '%s.resources.%s 缺 %s' % (key, side, dim))

    def test_single_data_pvc(self):
        """單顆 5Gi data PVC（agent /opt/data ＋ webui ~/.hermes 共用同一顆 RWO）。"""
        data = self._values()['persistence']['data']
        self.assertEqual(data['size'], '5Gi')
        self.assertEqual(
            self._tpl().min_storage_gb, 5,
            'min_storage_gb 須與 seed 的 PVC 總量一致')

    # ── Step 0 分岔 lock-step（翻案時強制三處同動）──────────────────────────
    def _db_enabled(self):
        values = self._values()
        return {
            'postgresql': bool(values.get('postgresql', {}).get('enabled')),
            'redis': bool(values.get('redis', {}).get('enabled')),
        }

    def test_db_disabled_lockstep_storage_class(self):
        """DB 開關 ⟺ _inject_storage_class 的 hermes-agent path 必須同動。

        內建 pg/redis 的 PVC **不在**扁平 persistence.storageClass 的繼承鏈上；
        開了 DB 卻沒補 path，PVC 拿不到平台 StorageClass → Pending 永不 Bound。
        """
        paths = self.env['woow_paas_platform.cloud_service']._inject_storage_class(
            {}, 'hermes-agent', 'longhorn-delete')
        enabled = self._db_enabled()
        self.assertEqual(
            'postgresql' in paths, enabled['postgresql'],
            'postgresql.enabled 與 storageClass path_map 不同步'
            '（見 cloud_service._inject_storage_class 的 hermes-agent entry）')
        self.assertEqual(
            'redis' in paths, enabled['redis'],
            'redis.enabled 與 storageClass path_map 不同步')

    def test_db_disabled_lockstep_storage_size(self):
        """DB 開關 ⟺ min_storage_gb 必須含 DB PVC（否則 quota storage 低估）。"""
        values = self._values()
        expected = _gi(values['persistence']['data']['size'])
        for key in ('postgresql', 'redis'):
            node = values.get(key) or {}
            if node.get('enabled'):
                expected += _gi(node['persistence']['size'])
        self.assertEqual(
            float(self._tpl().min_storage_gb), expected,
            'min_storage_gb 應等於 seed 所有 PVC 容量總和')

    def test_description_mentions_gate_and_dashboard(self):
        """description／full_description 說明 basic-auth 單一閘與 /dashboard 對外形態。"""
        tpl = self._tpl()
        text = (tpl.description or '') + (tpl.full_description or '')
        self.assertIn('basic-auth', text)
        self.assertIn('admin', text)
        self.assertIn('/dashboard', text)
        self.assertIn('console', text)


@tagged('post_install', '-at_install')
class TestHermesAgentPlansSeed(TransactionCase):
    """#494 定價拍板（Eugene 2026-07-31）：月 4,500／年 36,000（×8）＋7 天 trial。"""

    # base plan → (price, unit)
    EXPECTED = {
        'plan_hermes_agent_monthly': (4500, 'month'),
        'plan_hermes_agent_yearly': (36000, 'year'),
    }

    def _plan(self, xmlid):
        return self.env.ref('woow_paas_platform.%s' % xmlid)

    def test_base_plans(self):
        for xmlid, (price, unit) in self.EXPECTED.items():
            plan = self._plan(xmlid)
            self.assertEqual(plan.sh_plan_price, price, xmlid)
            self.assertEqual(plan.sh_unit, unit, xmlid)
            self.assertEqual(plan.sh_duration, 1, xmlid)
            self.assertEqual(plan.service_type, 'cloud_service', xmlid)

    def test_yearly_is_eight_times_monthly(self):
        """年繳＝月繳×8（沿用現行折扣慣例，省 33%）。"""
        monthly = self._plan('plan_hermes_agent_monthly')
        yearly = self._plan('plan_hermes_agent_yearly')
        self.assertEqual(yearly.sh_plan_price, monthly.sh_plan_price * 8)

    def test_trial_seven_days(self):
        """拍板 7 天 trial（沿 Open Design 雲端形制）——兩個週期都要有。"""
        for xmlid in self.EXPECTED:
            plan = self._plan(xmlid)
            self.assertTrue(plan.sh_trial, xmlid)
            self.assertEqual(plan.sh_trial_duration, 7, xmlid)
            self.assertEqual(plan.sh_trial_unit, 'day', xmlid)

    def test_products_taxfree_and_link(self):
        """product 綁對 plan、taxes_id 空（#366：pts 交易免稅）。"""
        for xmlid, (price, _unit) in self.EXPECTED.items():
            plan = self._plan(xmlid)
            product = self.env.ref(
                'woow_paas_platform.%s' % xmlid.replace('plan_', 'product_'))
            self.assertEqual(product.sh_subscription_plan_id, plan, xmlid)
            self.assertEqual(product.list_price, price, xmlid)
            self.assertTrue(product.sh_product_subscribe, xmlid)
            self.assertFalse(product.taxes_id, '%s：pts 免稅、taxes_id 應為空' % xmlid)

    def test_plans_are_storefront_visible(self):
        """base plan 不隱藏、非席數變體（前台要看得到才賣得出去）。"""
        for xmlid in self.EXPECTED:
            plan = self._plan(xmlid)
            self.assertFalse(plan.storefront_hidden, xmlid)
            self.assertEqual(plan.ac_max_access_seats, 0, xmlid)

    def test_plans_do_not_override_cs_spec(self):
        """🔴 #456 的直接修法之一：plan 的 cs_* 必須全為 0。

        填了會讓 `_allocated_vals_from` 改吃 plan 值、蓋掉 template 的 min_*
        ——min_vcpu／min_ram_gb 是**quota 峰值模型**（要蓋住全容器 limits），
        不是賣給租戶的規格。stg 臨時測試用的 0 元 plan 就填了 1/3.0/5，於是
        `min_vcpu=2` 的修法會被整個繞過，#456 原地復發。

        另外 `_apply_plan_spec_overlay` 會把 cs_* 疊成 resources.requests/limits，
        與本 seed 已寫死的 agent／webui／console resources 打架。
        """
        for xmlid in self.EXPECTED:
            plan = self._plan(xmlid)
            self.assertEqual(plan.cs_vcpu, 0, xmlid)
            self.assertEqual(plan.cs_ram_gb, 0.0, xmlid)
            self.assertEqual(plan.cs_storage_gb, 0, xmlid)

    def test_no_access_variants_in_v1(self):
        """v1 不做席數加購變體——template 上不得掛任何變體 plan。

        要加變體請回 generator（scripts/seed/generate_plan_variants.py）＋變體
        manifest 走一輪，不要手寫 (4,) append 進 links_variants.xml。
        """
        tpl = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        variants = tpl.sudo().subscription_plan_ids.filtered(
            lambda p: p.ac_max_access_seats > 0)
        self.assertFalse(
            variants,
            'v1 不做加購變體，實際掛上：%s' % variants.mapped('name'))

    def test_distinct_from_mcp_hermes_plans(self):
        """與既有 Hermes MCP Server 三級距 plan 是不同商品，勿互掛。

        撞名風險同 template 側（#424 brief 風險 10）：那是 Hermes 的 MCP server
        （T1–T3，1500/4500/6000），本組是 Hermes Agent 完整應用。
        """
        agent_plans = self.env.ref(
            'woow_paas_platform.cloud_app_hermes_agent').sudo().subscription_plan_ids
        mcp_plans = self.env.ref(
            'woow_paas_platform.cloud_app_mcp_hermes').sudo().subscription_plan_ids
        self.assertTrue(agent_plans)
        self.assertTrue(mcp_plans)
        self.assertFalse(
            agent_plans & mcp_plans,
            '兩個 Hermes 產品共用了 plan：%s' % (agent_plans & mcp_plans).mapped('name'))


@tagged('post_install', '-at_install')
class TestHermesAgentStorefrontReachable(HttpCase):
    """端到端：#494 之後真的**買得到**（marketplace → 詳情 → plans 三個端點全通）。

    上面的欄位斷言證明「資料對」，這組證明「鏈路通」——#424① 期間這三個端點
    全是關的（is_hidden 同時擋 list／by-id 詳情／plans），解封的實質效力就在這裡。
    """

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')

    def _result(self, url, params='{}'):
        body = '{"jsonrpc":"2.0","method":"call","params":' + params + '}'
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    def test_listed_in_marketplace(self):
        r = self._result('/api/cloud/templates')
        self.assertTrue(r.get('success'), r)
        self.assertIn(
            self.tpl.id, [t['id'] for t in r['data']],
            '已定價上架的 template 必須出現在 marketplace 清單')

    def test_template_detail_readable(self):
        r = self._result('/api/cloud/templates/%s' % self.tpl.id)
        self.assertTrue(r.get('success'), '規格頁必須讀得到（回 %r）' % r)

    def test_plans_endpoint_returns_both_cycles(self):
        """月／年兩張卡，價格與 trial 是拍板值（前台真正拿到的數字）。"""
        r = self._result('/api/cloud-services/templates/%s/plans' % self.tpl.id)
        self.assertTrue(r.get('success'), r)
        by_unit = {p['unit']: p for p in r['data']}
        self.assertEqual(set(by_unit), {'month', 'year'}, r['data'])
        self.assertEqual(by_unit['month']['price'], 4500)
        self.assertEqual(by_unit['year']['price'], 36000)
        for unit, plan in by_unit.items():
            self.assertTrue(plan['has_trial'], unit)
            self.assertEqual(
                (plan['trial_duration'], plan['trial_unit']), (7, 'day'), unit)
            self.assertFalse(plan['variants'], 'v1 不做席數加購變體')

    def test_displayed_specs_fall_back_to_template_minimums(self):
        """前台卡片的規格 label ＝ template min_*（plan cs_* 全 0 的可見結果）。

        #456 修法（min_vcpu → 2）在這裡是**使用者看得到的數字**；若有人替 plan
        填了 cs_vcpu，label 會跟著變而紅——同一件事的另一個觀測面，與
        test_plans_do_not_override_cs_spec 互為佐證。
        """
        r = self._result('/api/cloud-services/templates/%s/plans' % self.tpl.id)
        expected = '%s vCPU · %s GB RAM' % (self.tpl.min_vcpu, self.tpl.min_ram_gb)
        for plan in r['data']:
            labels = [s['label'] for s in plan['specs']]
            self.assertIn(expected, labels, '%s：%r' % (plan['unit'], labels))


@tagged('post_install', '-at_install')
class TestHermesAgentValueInjection(TransactionCase):
    """平台側新接線：dashboardPublicUrl injector ＋ 全量 values 組裝端到端。"""

    def setUp(self):
        super().setUp()
        self.CloudService = self.env['woow_paas_platform.cloud_service']
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')

    # ── injector 單元 ───────────────────────────────────────────────────────
    def test_inject_dashboard_public_url(self):
        """hermes-agent：注入 config.dashboardPublicUrl＝公開根位址 + /dashboard。"""
        values = self.CloudService._inject_dashboard_public_url(
            {}, 'hermes-agent', 'https://svc-abc.woowtech.io')
        self.assertEqual(
            values['config']['dashboardPublicUrl'],
            'https://svc-abc.woowtech.io/dashboard')

    def test_inject_dashboard_public_url_strips_trailing_slash(self):
        """根位址帶尾斜線也不能產出 //dashboard（OAuth redirect_uri 逐字比對會 mismatch）。"""
        values = self.CloudService._inject_dashboard_public_url(
            {}, 'hermes-agent', 'https://svc-abc.woowtech.io/')
        self.assertEqual(
            values['config']['dashboardPublicUrl'],
            'https://svc-abc.woowtech.io/dashboard')

    def test_inject_dashboard_public_url_noop_for_other_templates(self):
        """其他 template 一律 no-op（不得把不認識的鍵塞進別人的 values）。"""
        for slug in ('open-design', 'n8n', 'mcp-hermes'):
            self.assertEqual(
                self.CloudService._inject_dashboard_public_url(
                    {}, slug, 'https://x.woowtech.io'),
                {}, slug)

    def test_inject_apiserver_cidrs_covers_hermes_agent(self):
        """console 同構需求：hermes-agent 也要進 apiServerCidrs 白名單（#404）。"""
        values = self.CloudService._inject_apiserver_cidrs(
            {}, 'hermes-agent', ['10.43.0.1/32'])
        self.assertEqual(
            values['networkPolicy']['apiServerCidrs'], ['10.43.0.1/32'])

    def test_inject_storage_class_flat_key(self):
        """hermes-agent 走扁平 persistence.storageClass（per-PVC 鍵由 chart 繼承）。"""
        values = self.CloudService._inject_storage_class(
            {}, 'hermes-agent', 'longhorn-delete')
        self.assertEqual(values['persistence']['storageClass'], 'longhorn-delete')

    # ── 全量組裝端到端 ──────────────────────────────────────────────────────
    def _make_service(self):
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Hermes WS 424',
            'owner_id': self.env.ref('base.user_admin').id,
        })
        return self.CloudService.create({
            'workspace_id': ws.id,
            'template_id': self.tpl.id,
            'name': 'hermes-agent',
            'reference_id': 'test-hermes-ref-424',
            'state': 'pending',
            'subdomain': 'hermes-test-424',
            # ⚠ 鐵訓（HA PaaS 2026-07-23 stg 實證）：helm_values 必須手工鏡射
            # template.helm_default_values——漏寫則 image pin 全丟、部署拉佔位 tag。
            'helm_values': self.tpl.helm_default_values,
        })

    def test_assemble_deploy_values_full_contract(self):
        """_assemble_deploy_values 對 hermes-agent 應同時補齊四類平台值。

        #411 reset 語意下，helm render base 是 chart 自己的 values.yaml，平台管理
        的值**每次都必須重新組齊**——少一個就是該功能靜默消失（apiServerCidrs 缺
        ＝console 連不到 apiserver；dashboardPublicUrl 缺＝MCP OAuth 回呼死鏈）。
        """
        svc = self._make_service()
        assembled = svc._assemble_deploy_values()
        values = assembled['values']
        # 1) template pin 值仍在（未被注入流程洗掉）
        self.assertEqual(values['image']['repository'], HERMES_AGENT_IMAGE)
        self.assertEqual(values['service']['port'], 8080)
        # 2) 四類平台注入
        self.assertTrue(values['persistence']['storageClass'])
        self.assertTrue(values['global']['imagePullSecrets'])
        self.assertTrue(values['networkPolicy']['apiServerCidrs'])
        paas_domain = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        self.assertEqual(
            values['config']['dashboardPublicUrl'],
            'https://hermes-test-424.%s/dashboard' % paas_domain)
        # 3) ingress expose（template.ingress_enabled ＋ subdomain）
        self.assertEqual(
            assembled['expose_config'],
            {'enabled': True, 'subdomain': 'hermes-test-424'})

    def test_assemble_deploy_values_excludes_sensitive(self):
        """組裝結果不得含 config.sensitive（敏感值另走 sensitive_values 通道）。"""
        svc = self._make_service()
        svc.helm_values = json.dumps(dict(
            json.loads(self.tpl.helm_default_values),
            config={'sensitive': {'admin_password': 'should-be-stripped'}}))
        values = svc._assemble_deploy_values()['values']
        self.assertNotIn('sensitive', values.get('config', {}))
        self.assertNotIn('should-be-stripped', json.dumps(values))

    def test_admin_password_secret_minted_on_create(self):
        """has_admin_gui=True → create 時自動鑄一組 admin_password（#357 供給鏈）。"""
        svc = self._make_service()
        secret = svc._get_secret_record()
        self.assertTrue(secret, 'has_admin_gui template 建立時應自動產生密碼 secret')
        self.assertTrue(secret.admin_password)

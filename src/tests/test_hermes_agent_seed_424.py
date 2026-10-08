# -*- coding: utf-8 -*-
"""#424①：Hermes Agent 完整應用上架 seed ＋ 平台注入接線的守線斷言。

鏡射 test_opendesign_seed_380.py（金樣板 SOP）＋ test_ha_paas_seed_woowtech_image.py
（hidden template ＋ 原子綁定）兩者的結構。本檔守的是「錯了就靜默壞掉」的點：

- **image 雙釘**：chart 0.1.3 起 hermes-agent 移除 webui container（nginx `/`
  直連 dashboard），seed 的 `webui` block 連同 `HERMES_WEBUI_IMAGE` /
  `WEBUI_PENDING_TAG` 常數與 `test_webui_image_pinned_digest` 一併刪除；agent
  與 console 兩顆仍走 `#424①` 既有雙釘流程。
- **quota 蓋不住全容器 limits**：#380 血訓，min_ram_gb×3 不足 → pod FailedCreate
  → 對外永遠 CF 502。這裡用**結構關係**斷言（由 seed values 現算），不硬編數字：
  一條擋太小、一條擋太大（＝把契約 v1.1 §8.8 的「÷3 上取」規則機械化）。0.1.3
  起無 DB 拓撲下 limits 合計 6.625Gi（agent 6 + console 0.5 + authProxy 0.125）
  → min_ram_gb=3.0（仍採舊值，刻意保守：升版時 quota 必須蓋得住回退相容）。
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
import xml.etree.ElementTree as ET

from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.tools import file_open
from ..models.cloud_service import _internal_provision

PLATFORM_REGISTRY = 'jcr-prod.woowtech.io/'
HERMES_AGENT_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/hermes-agent'
# chart 0.1.3 起 hermes-agent 移除 webui container（nginx `/` 直連 dashboard），
# seed 的 `webui` block 不再存在；HERMES_WEBUI_IMAGE／WEBUI_PENDING_TAG 與
# `test_webui_image_pinned_digest` 連同 ``_PENDING_IMAGE_KEYS`` 一併撤掉。
# 若日後 chart 把 webui 加回，連同下列全套恢復並重新走 digest 雙釘流程。

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
# #456 的 stg 事件訊息（exceeded quota: limits.cpu=3200m）0.1.3 起改為：
# agent 2000m ＋ console 200m ＋ 本 sidecar 200m ＝ 2.4 vCPU（邊際貢獻 2 蓋得住）。
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
            len(base), 2,
            '恰兩筆 base plan（月／年）；席數變體另計，見 test_access_variants_attached')

    def test_max_per_partner_unlimited(self):
        """每帳號不限台數：0＝不限，平台層不設本 template 的每帳號實例上限。

        ⚠ 只驗 DB 值是不夠的：0 同時是 `max_per_partner` 的**欄位 default**，所以
        「seed 顯式寫 0」與「seed 那一行整個被刪掉」讀起來一模一樣、都會綠。故本條
        一併驗 seed XML 本體，要求該 record 顯式帶著這個欄位——兩層合起來才分得出
        「刻意放寬」與「不小心漏寫」。
        """
        self.assertEqual(self._tpl().max_per_partner, 0)
        with file_open('woow_paas_platform/data/cloud_app_templates.xml') as f:
            rec = ET.parse(f).getroot().find(
                ".//record[@id='cloud_app_hermes_agent']")
        self.assertIsNotNone(rec, 'seed 內找不到 cloud_app_hermes_agent record')
        field = rec.find("field[@name='max_per_partner']")
        self.assertIsNotNone(
            field,
            'seed 必須「顯式」宣告 max_per_partner=0：刪掉那一行 DB 讀起來仍是 0'
            '（欄位 default），上面那條 assertEqual 察覺不到')
        self.assertEqual(
            field.get('eval'), '0',
            'seed 的 max_per_partner 必須是 0（不限）；改動此值請同步本條守線')

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
        """dashboard 的「設錯就靜默失去防護」鍵明寫（chart 0.1.3 起無 externalPath）。

        - bindHost 非 loopback ⇒ 上游 fail-closed auth gate **保持啟用**（改成
          127.0.0.1 就只剩 nginx 一層）。
        - chart 0.1.3 起 dashboard 不走 subpath：nginx `/` 直連，OAuth
          ``redirect_uri`` ＝root URL（由平台 ``_inject_dashboard_public_url`` 每
          次注入；見 test_dashboard_injector_outputs_root_url）。故 seed 不得帶
          ``dashboard.externalPath``——留著就等於告訴 chart 走舊 subpath，與平台
          注入的 root URL 對不起來。
        """
        dashboard = self._values()['dashboard']
        self.assertEqual(
            dashboard['bindHost'], '0.0.0.0',
            'loopback 綁定會關掉 dashboard 自身的 fail-closed 登入閘')
        self.assertNotIn(
            'externalPath', dashboard,
            'chart 0.1.3 起 dashboard 不走 subpath——seed 不該帶 externalPath')
        self.assertEqual(dashboard['username'], 'admin')

    def test_dashboard_external_path_matches_injector(self):
        """跨層 lock-step：seed 無 externalPath，平台注入的 URL 也無 suffix。

        chart 0.1.3 起 dashboard 走 root URL（nginx `/`），平台
        ``_inject_dashboard_public_url`` 對 hermes-agent slug 直接輸出 root URL，
        兩側都不得殘留 ``/dashboard`` suffix（任一側單獨漏改都會把 OAuth 回呼打到
        404 subpath）。與 0.1.x 時代的 `dashboard.externalPath` 鎖步合約已被新形態
        取代——本條把「兩側都不得有 suffix」機械化為字串層斷言。
        """
        dashboard = self._values()['dashboard']
        self.assertNotIn(
            'externalPath', dashboard,
            'seed 仍帶 dashboard.externalPath：會被 chart 當成 subpath 轉發 → OAuth'
            ' redirect_uri 打到 /dashboard 而非 root')
        injected = self.env['woow_paas_platform.cloud_service']._inject_dashboard_public_url(
            {}, 'hermes-agent', 'https://svc-abc.woowtech.io',
        )['config']['dashboardPublicUrl']
        self.assertEqual(
            injected, 'https://svc-abc.woowtech.io',
            '平台注入的 dashboardPublicUrl 必須是 root URL（chart 0.1.3）')
        self.assertNotIn(
            '/dashboard', injected,
            '平台注入的 URL 不該帶 /dashboard suffix（chart 0.1.3 移除 webui，'
            'OAuth redirect_uri 走 root）')

    def test_gateway_external_path_explicit(self):
        """#530：gateway 的對外子路徑明寫（缺鍵／空字串＝gateway 永遠不對外）。

        gateway API（容器內 :8642）原本沒有任何對外路徑——反向代理只有 dashboard／
        console／webui 三條，跨 namespace 直連又被租戶 NetworkPolicy 擋掉，於是
        MCP 的 Gateway 區無論填什麼都是 Disconnected。這個鍵是 chart 生出那條反向
        代理區塊的唯一開關；空字串或缺鍵會讓路徑靜默不存在，而平台照樣把
        ``{public}/gateway`` 當權威網址交給租戶 ⇒ 死鏈（形狀同 dashboard 那條）。

        值本身刻意明寫而非只驗真值：``/gateway`` 是**跨 repo 鎖定的契約值**
        （chart 端 values 的 ``gateway.externalPath`` 預設同值），兩邊任一單獨改動
        就是死鏈。釘法比照 test_console_subpath 對 ``/console`` 的做法。
        """
        gateway = self._values()['gateway']
        self.assertTrue(
            gateway['externalPath'],
            'gateway.externalPath 為空＝gateway 不對外路由（#530 的原始症狀）')
        self.assertEqual(gateway['externalPath'], '/gateway')

    def test_gateway_external_path_is_read_from_this_seed(self):
        """交付判準的事實源就是本 seed 欄位（``_gateway_external_path`` 讀得出來）。

        平台原本另存一份 ``{'hermes-agent': '/gateway'}`` 對照表，於是路徑有兩份：
        任一單獨改動 → 租戶拿到一條死鏈，而且**兩邊各自看起來都正常**（chart 那條
        路徑存在、平台那個字串也組得出來）。#530 review 後改成單一事實源，這條確認
        「讀得到、且讀到的就是上面那條跨 repo 契約值」——helper 回 '' 時（欄位被改名／
        形態變了）交付會靜默消失，不能只靠 runtime fail-closed 而沒有守線。

        derivation 本身（不是硬編）由 ``test_gateway_public_url_follows_seed_not_slug``
        （test_gateway_capability_payload_530.py）以不同路徑值反證。
        """
        self.assertEqual(
            self._tpl()._gateway_external_path(),
            self._values()['gateway']['externalPath'],
            '_gateway_external_path 沒讀到 seed 的 gateway.externalPath')

    def test_no_sensitive_values_in_git(self):
        """admin_password／LLM key／gateway API key 不得出現在 git 的 helm_default_values。

        先做陽性對照再談「不含憑證」：``assertNotIn(secret, '')`` 恆真，而空字串
        是拿得到的——實測 Odoo 18 的 Text 欄位寫入 ``''`` 會原樣存進 DB（不是
        NULL）、讀回也是 ``''``，連 ``''.lower()`` 都不會炸。沒有這行的話，seed
        這欄被清空 ⇒ 這條「憑證不得進 git」的唯一守線靜默失效。

        #530：``auth.apiServerKey``（gateway API key）由平台 per-service 生成、經
        sensitive 通道供給，**永遠不得寫進 seed**。chart 的憑證解析優先序第一段就是
        「顯式 values 值」，seed 一旦帶值就會蓋掉平台供給的 per-service key ⇒ 全體
        租戶共用同一把、且那把還躺在 git 裡。
        """
        raw = self._tpl().helm_default_values
        self.assertIn(
            'authProxy', raw,
            'helm_default_values 裡找不到預期的 authProxy 區塊 —— 探針沒讀到真正的'
            ' seed values（欄位被清空／改名），下面四條憑證斷言等於沒跑')
        self.assertNotIn('admin_password', raw)
        self.assertNotIn('minimax', raw.lower())
        self.assertNotIn('openrouter', raw.lower())
        self.assertNotIn('apiServerKey', raw)

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
        """chart＝woow-paas-charts 發佈的獨立 hermes-agent OCI chart 0.3.4。

        版本演進：0.1.0 → 0.1.1（#530，charts PR #69 新增 gateway 反向代理子路徑）→
        0.1.3（移除 webui container，nginx `/` 直連 dashboard；webui 移除細節見
        test_webui_block_removed）→ 0.2.0（#717，租戶自裝 CLI 的落點收斂：十個安裝器
        落點覆寫 env ＋ cli-landing-init initContainer）→ 0.2.1（#1377：agent pod 加
        checksum/tenant-llm-keys，租戶改金鑰即 rollout）→ 0.3.0（#1397：agent 改
        StatefulSet、console RBAC 綁 agent pod 名；依賴 operator 重設時重啟 opt-in
        StatefulSet，#1398）→ 0.3.1（charts #138：認得平台注入的 llmGateway.baseUrl，
        網址非空才開 networkPolicy.litellmEgress；見 cloud_service._LITELLM_BASE_URL_VALUE_PATHS）
        → 0.3.2（#1441 工作區＝區網）→ 0.3.3（#1537：去 NET_RAW、/console uid 10000、台北時區）
        → 0.3.4（#1556：MINIMAX／OPENROUTER 金鑰一律輸出，清除後 agent 不留舊值）。
        這條斷言是刻意鎖精確值的少數例外：跨 repo chart 版本是**平台指向哪一顆已發佈
        artifact** 的宣告，不是會自然演進的 living schema——寫成 `>=` 只會讓「chart 沒
        跟著發版」這個真正的失效模式假綠。平台端沒有 chart values 的任何副本，鍵名
        一致性只能靠兩邊各自的契約測試加 review。

        ⚠ 0.2.0 這一版**沒有**連帶的 helm_default_values 改動要斷言——chart 的
        cliLanding.* 全部自帶預設值，平台端零覆寫。所以「平台有沒有跟上這次 chart
        變更」在本檔的唯一可觀察面就是這個版號；不要因為 diff 只有一行就把它當成
        無關緊要的 bump。
        """
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(
            tpl.helm_chart_name,
            'oci://jcr-prod.woowtech.io/woow-paas-docker-local/hermes-agent')
        self.assertEqual(tpl.helm_chart_version, '0.3.4')

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

    def test_webui_block_removed(self):
        """chart 0.1.3 起 webui container 移除；seed 不得帶 `webui` block。

        nginx `/` 直連 dashboard（取代舊 `/`＝webui＋`/dashboard/` 雙入口）；
        ``webui`` block 若存留，chart 仍會 render 那顆 container，pod 起來多一條
        dead path（basic-auth 與 dashboard form-auth 形狀疊加，登入閘互踢）。
        守線＝斷言 block 本身不存在。
        """
        self.assertNotIn(
            'webui', self._values(),
            'chart 0.1.3 起 webui container 已移除，seed 不該帶 webui block')

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

        涵蓋 agent（daemon pod，chart 0.1.3 起獨自存在，webui 已合進 nginx `/`）
        ＋ console（獨立 Deployment，同 ns 同 quota）
        ＋ auth-proxy sidecar（chart 契約值，見 AUTH_PROXY_LIMIT_GI）；DB 開啟時
        另計 pg／redis。刻意用「現算」而非硬編：resources 一改，斷言自動跟上。
        """
        values = self._values()
        total = AUTH_PROXY_LIMIT_GI
        for path in (('agent',), ('console',),
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

        #456 的根因就在這條線上：0.1.x 邊際需求 3.4（agent 2 ＋ webui 1 ＋
        console 0.2 ＋ auth-proxy 0.2）> 邊際貢獻 min_vcpu×3。RAM 早有守線、CPU
        沒有，所以「共用 workspace 才露餡」的缺口一路活到 stg。0.1.3 起合計降到
        2.4（webui 移除），min_vcpu=2 的邊際貢獻 6 蓋得住。
        """
        values = self._values()
        total = AUTH_PROXY_LIMIT_CPU
        for key in ('agent', 'console', 'postgresql', 'redis'):
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
        """每個 seed 覆寫的容器都要 requests+limits 齊備（paas-ws quota 缺一即 FailedCreate）。

        chart 0.1.3 起只剩 agent（daemon pod）與 console（獨立 Deployment）兩顆
        走 seed 覆寫的容器——webui 已合併進 nginx `/`，auth-proxy sidecar 走 chart
        契約值（不在 seed）。"""
        values = self._values()
        for key in ('agent', 'console'):
            resources = values[key].get('resources')
            self.assertTrue(resources, '%s 缺 resources' % key)
            for side in ('requests', 'limits'):
                self.assertIn(side, resources, '%s.resources 缺 %s' % (key, side))
                for dim in ('cpu', 'memory'):
                    self.assertIn(
                        dim, resources[side],
                        '%s.resources.%s 缺 %s' % (key, side, dim))

    def test_single_data_pvc(self):
        """單顆 5Gi data PVC（agent /opt/data；0.1.3 起 webui ~/.hermes 不再寫入）。"""
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
        """description／full_description 說明 basic-auth 單一閘與 root URL 對外形態。

        chart 0.1.3 起 dashboard 走 root URL（nginx `/`），不再有 `/dashboard` 字串
        ——若 seed description 又把舊 subpath 寫進去，租戶會照舊字串連線拿到 404，
        而 seed 與 chart 兩側各自看起來都正常。守住的是「對外形態描述＝現行合約」。
        """
        tpl = self._tpl()
        text = (tpl.description or '') + (tpl.full_description or '')
        self.assertIn('basic-auth', text)
        self.assertIn('admin', text)
        self.assertIn('console', text)
        # chart 0.1.3 起 dashboard 不走 /dashboard subpath
        self.assertNotIn(
            '/dashboard', text,
            'description 仍寫 /dashboard subpath：與 0.1.3 root URL 合約不一致，'
            '租戶照舊字串會拿到 404')


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
        與本 seed 已寫死的 agent／console resources 打架（chart 0.1.3 起 webui 已
        從 seed 移除）。
        """
        for xmlid in self.EXPECTED:
            plan = self._plan(xmlid)
            self.assertEqual(plan.cs_vcpu, 0, xmlid)
            self.assertEqual(plan.cs_ram_gb, 0.0, xmlid)
            self.assertEqual(plan.cs_storage_gb, 0, xmlid)

    def test_access_variants_attached(self):
        """#765：固定 1 席的加購變體掛上本 template 且 hidden。

        取代原本的 test_no_access_variants_in_v1——#494 期記的「v1 不做加購變體」
        已被 Eugene 2026-08-17 拍板取代（比照同為完整應用的 Open Design，固定 1 席）。
        全量斷言在 test_plan_variants_generated；此處抽驗本家族的席數與價格。

        要改席數／價格請回 generator（scripts/seed/generate_plan_variants.py）＋
        變體 manifest 走一輪，不要手寫 (4,) append 進 links_variants.xml。
        """
        tpl = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        plans = tpl.sudo().subscription_plan_ids
        variants = plans.filtered(lambda p: p.ac_max_access_seats > 0)
        self.assertEqual(
            len(variants), 2,
            '固定 1 席 × 月／年兩週期＝恰兩筆，實際：%s' % variants.mapped('name'))
        for xmlid, price in (('plan_hermes_agent_ac1_monthly', 5100),
                             ('plan_hermes_agent_ac1_yearly', 40800)):
            v = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertIn(v, plans, xmlid)
            self.assertEqual(v.sh_plan_price, price, xmlid)
            self.assertEqual(v.ac_max_access_seats, 1, xmlid)
            self.assertTrue(v.storefront_hidden, xmlid)

    def test_variant_plans_do_not_override_cs_spec(self):
        """🔴 #456 守線延伸到變體：加購變體的 cs_* 同樣必須全為 0。

        base 那條（test_plans_do_not_override_cs_spec）只掃兩筆 base plan，
        #765 之後同一個 template 上多了席數變體——變體若被填 cs_*，
        `_allocated_vals_from` 一樣會蓋掉 template 的 min_*（quota 峰值模型），
        而選了加購席的租戶才會踩到，base 側恆綠。
        """
        tpl = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        variants = tpl.sudo().subscription_plan_ids.filtered(
            lambda p: p.ac_max_access_seats > 0)
        self.assertTrue(variants, '前置：本 template 應有席數變體')
        for plan in variants:
            self.assertFalse(plan.cs_vcpu, plan.name)
            self.assertFalse(plan.cs_ram_gb, plan.name)
            self.assertFalse(plan.cs_storage_gb, plan.name)

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
        """月／年兩張卡，價格與 trial 是拍板值（前台真正拿到的數字）。

        #765 起每張卡另帶固定 1 席的加購變體——這是 marketplace 部署頁的席數區塊
        （`PlanSelector.xml` 的 `t-if="seatBase"`）唯一的資料來源，**變體只在這個
        wire payload 出現、不會自成頂層 plan**，所以這裡是它唯一的 wire 級守線。
        原本此處斷言 variants 為空（#494 期的「v1 不做加購變體」）——刻意不把那行
        原樣抄進註解：可照抄的錯誤範本比散文警告更容易被人撿去用。
        """
        r = self._result('/api/cloud-services/templates/%s/plans' % self.tpl.id)
        self.assertTrue(r.get('success'), r)
        by_unit = {p['unit']: p for p in r['data']}
        self.assertEqual(set(by_unit), {'month', 'year'}, r['data'])
        self.assertEqual(by_unit['month']['price'], 4500)
        self.assertEqual(by_unit['year']['price'], 36000)
        expected_variant_price = {'month': 5100, 'year': 40800}
        for unit, plan in by_unit.items():
            self.assertTrue(plan['has_trial'], unit)
            self.assertEqual(
                (plan['trial_duration'], plan['trial_unit']), (7, 'day'), unit)
            self.assertEqual(
                [v['seats'] for v in plan['variants']], [1],
                '#765：固定 1 席，%s 卡實得 %s' % (unit, plan['variants']))
            self.assertEqual(
                plan['variants'][0]['price'], expected_variant_price[unit], unit)

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
        """hermes-agent：注入 config.dashboardPublicUrl＝公開根位址（chart 0.1.3 root URL）。

        0.1.x 時代 suffix 為 ``/dashboard``（chart 走 subpath，OAuth redirect_uri 帶尾綴）。
        0.1.3 移除 webui、nginx ``/`` 直連 dashboard：上游把 root URL 當 OAuth
        ``redirect_uri`` 的權威來源，suffix 變多餘且會把回呼打到 404 subpath。
        """
        values = self.CloudService._inject_dashboard_public_url(
            {}, 'hermes-agent', 'https://svc-abc.woowtech.io')
        self.assertEqual(
            values['config']['dashboardPublicUrl'],
            'https://svc-abc.woowtech.io')

    def test_inject_dashboard_public_url_strips_trailing_slash(self):
        """根位址帶尾斜線也要正規化掉（OAuth redirect_uri 逐字比對會 mismatch）。"""
        values = self.CloudService._inject_dashboard_public_url(
            {}, 'hermes-agent', 'https://svc-abc.woowtech.io/')
        self.assertEqual(
            values['config']['dashboardPublicUrl'],
            'https://svc-abc.woowtech.io')

    def test_inject_dashboard_public_url_noop_for_other_templates(self):
        """其他 template 一律 no-op（不得把不認識的鍵塞進別人的 values）。"""
        for slug in ('open-design', 'n8n', 'mcp-hermes'):
            self.assertEqual(
                self.CloudService._inject_dashboard_public_url(
                    {}, slug, 'https://x.woowtech.io'),
                {}, slug)

    # ── gateway 公開網址（#530）─────────────────────────────────────────────
    def test_build_gateway_public_url(self):
        """公開根位址 + seed 子路徑＝交給租戶填進 MCP 的權威網址。"""
        self.assertEqual(
            self.CloudService._build_gateway_public_url(
                '/gateway', 'https://svc-abc.woowtech.io'),
            'https://svc-abc.woowtech.io/gateway')

    def test_build_gateway_public_url_strips_trailing_slash(self):
        """根位址帶尾斜線也不能產出 //gateway。

        MCP 端把這個值直接當 base URL 去接 gateway 的能力端點；``//gateway`` 在
        nginx location 比對下不等同 ``/gateway``，租戶會拿到 404 卻以為是 key 錯。
        與 dashboard 那條同一個成因，故同樣走 rstrip 正規化而非裸內插。
        """
        self.assertEqual(
            self.CloudService._build_gateway_public_url(
                '/gateway', 'https://svc-abc.woowtech.io/'),
            'https://svc-abc.woowtech.io/gateway')

    def test_build_gateway_public_url_empty_when_either_side_missing(self):
        """任一側缺就回空字串（沒有 gateway、或沒有對外根位址）。

        回了非空值＝租戶會在一次性視窗看到一條指向不存在路徑的 gateway 網址，
        比不給還糟（它看起來像產品保證）。
        """
        for external_path, base_url in (
            ('', 'https://x.woowtech.io'),   # chart 沒有 gateway
            ('/gateway', ''),                # 服務沒有 subdomain
            ('', ''),
        ):
            with self.subTest(external_path=external_path, base_url=base_url):
                self.assertEqual(
                    self.CloudService._build_gateway_public_url(
                        external_path, base_url),
                    '')

    def test_other_seeded_templates_have_no_gateway_path(self):
        """其他既有 seed template 一律沒有 gateway 對外路徑（hermes-agent 是唯一）。

        這條是 finding 3 的守線：鑄造／供給閘門改判 ``_chart_has_gateway`` 之後，
        「哪些 template 會拿到 gateway API key」由 seed 決定。若哪天有人替別的 chart
        補上 ``gateway.externalPath``，那顆 chart 就會開始收到平台供給的
        ``auth.apiServerKey``——這條讓那個決定變成**要動測試才過得去**的顯式動作，
        而不是順手改 seed 的副作用。
        """
        # #686：mcp-hermes（pilot）原本不在這張清單裡——與 has_admin_gui 漏的是
        # 同一格（「複製 hermes 生出來的衍生品」都列了，被複製的那個沒列）。
        # （2026-10-04 起 cloud_app_litellm 也刻意帶 gateway.externalPath=/v1：
        #  charts/litellm 0.1.0 把 auth.apiServerKey 當 LITELLM_MASTER_KEY，守線在
        #  test_litellm_seed.test_master_key_rides_the_gateway_channel。）
        # （cloud_app_mcp_litellm 刻意**沒有**：它連的是 LiteLLM，金鑰由租戶自己貼，平台不鑄、
        #  不交付——PLAN §10「不跨服務傳秘密」。）
        for xmlid in ('cloud_app_mcp_hermes',
                      'cloud_app_mcp_odoo', 'cloud_app_mcp_odoo_manage',
                      'cloud_app_mcp_n8n', 'cloud_app_mcp_k8s', 'cloud_app_mcp_vk',
                      'cloud_app_mcp_opendesign', 'cloud_app_opendesign',
                      'cloud_app_mcp_litellm',
                      'cloud_app_haos', 'cloud_app_ha_paas'):
            with self.subTest(template=xmlid):
                tpl = self.env.ref('woow_paas_platform.%s' % xmlid)
                self.assertEqual(
                    tpl._gateway_external_path(), '',
                    '%s 的 seed 有 gateway.externalPath：它會開始收到平台供給的 '
                    'auth.apiServerKey。若是有意的，請在本清單移除它並補上該 chart '
                    '真的吃這個鍵的證據。' % xmlid)

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
        return _internal_provision(self.CloudService).create({
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
        # #530：gateway 對外子路徑是 seed 值、走同一條全量管線送出。少了它 chart
        # 就不生那條反向代理區塊 ⇒ 平台交付的 gateway URL 變死鏈（而服務照樣 Running）。
        self.assertEqual(values['gateway']['externalPath'], '/gateway')
        # 2) 四類平台注入
        self.assertTrue(values['persistence']['storageClass'])
        self.assertTrue(values['global']['imagePullSecrets'])
        self.assertTrue(values['networkPolicy']['apiServerCidrs'])
        paas_domain = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        # chart 0.1.3：dashboard 走 root URL（nginx `/` 直連），不再有 /dashboard suffix
        self.assertEqual(
            values['config']['dashboardPublicUrl'],
            'https://hermes-test-424.%s' % paas_domain)
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

    def test_assemble_deploy_values_excludes_gateway_api_key(self):
        """🔴 #530：``auth.apiServerKey`` 也必須被剝掉——它**不在** config.sensitive 底下。

        既有的 strip 是 path-aware 的（只 pop ``config.sensitive``），而 chart 要求
        的 gateway key 鍵在 ``auth.apiServerKey``。照抄 admin_password 的做法卻沒
        擴充 strip，這把 key 就會留在 ``_assemble_deploy_values`` 的輸出、被寫回
        ``helm_values``，接著從服務詳情 API 明文流出——而只掃 config.sensitive 的
        既有測試**不會紅**。這條就是那個缺口的守線。
        """
        svc = self._make_service()
        svc.helm_values = json.dumps(dict(
            json.loads(self.tpl.helm_default_values),
            auth={'apiServerKey': 'gw-key-should-be-stripped'}))
        values = svc._assemble_deploy_values()['values']
        self.assertNotIn('apiServerKey', values.get('auth', {}))
        self.assertNotIn('gw-key-should-be-stripped', json.dumps(values))

    def test_admin_password_secret_minted_on_create(self):
        """has_admin_gui=True → create 時自動鑄一組 admin_password（#357 供給鏈）。"""
        svc = self._make_service()
        secret = svc._get_secret_record()
        self.assertTrue(secret, 'has_admin_gui template 建立時應自動產生密碼 secret')
        self.assertTrue(secret.admin_password)

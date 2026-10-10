# -*- coding: utf-8 -*-
"""#380：Open Design 完整應用（daemon + console）上架 seed 斷言。

鏡射 test_mcp_3type_seed_375.py 結構。此為「完整應用」（非 MCP server）：獨立 image
open-design、獨立 chart open-design、category=ai_llm、daemon 對外埠 7457、雙 PVC、
console.enabled。helm_default_values 已對齊支線 A charts/open-design/values.yaml
（woow-paas-charts PR #41），只驗平台 pin/覆寫值；chart 內部契約（Recreate、fsGroup、
probe /api/health、daemon loopback bind）由 chart 預設＋chart tests 承擔、seed 不重寫。

auth（C 案，issue #380 comment 定稿）：daemon 綁 loopback、nginx auth-proxy sidecar
以 HTTP basic-auth 閘（帳號 admin、密碼＝config.sensitive.admin_password）罩住 UI／API
與 console，故設 has_admin_gui=True 複用 #357 供給鏈；OD_API_TOKEN 由 chart 自產內部化、
不入 git values。

pin：daemon 與 console image 皆已雙釘 tag@sha256（支線 B 交付；digest 來源＝paas-ci
CI push＋docker manifest inspect 雙來源交叉確認）。test_image_tag_pinned_digest ／
test_console_image_pinned_digest 守住不得回退為 latest／PENDING。
"""
import json
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestOpendesignTemplateSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_opendesign')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def test_marketplace_fields(self):
        """上架前台販售：name/slug、category=ai_llm、is_active、非隱藏、port/ingress。"""
        tpl = self._tpl()
        self.assertEqual(tpl.name, 'Open Design')
        self.assertEqual(tpl.slug, 'open-design')
        self.assertEqual(tpl.category, 'ai_llm')
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.is_hidden, '完整應用上架前台販售、不隱藏')
        self.assertEqual(tpl.default_port, 7457)
        self.assertTrue(tpl.ingress_enabled)

    def test_not_mcp(self):
        """完整應用非 MCP server：mcp_enabled=False（MCP 生態由 mcp-opendesign 承擔）。"""
        self.assertFalse(self._tpl().mcp_enabled)

    def test_has_admin_gui_c_option(self):
        """C 案 auth 接線：has_admin_gui=True——nginx auth-proxy 以 basic-auth 閘保護，
        密碼＝config.sensitive.admin_password，走 #357 供給鏈（生成＋一次性顯示＋reset）。"""
        self.assertTrue(
            self._tpl().has_admin_gui,
            'C 案 basic-auth 憑證＝#357 admin_password，須開 has_admin_gui 供給鏈')

    def test_chart_oci(self):
        """chart＝woow-paas-charts 發佈的獨立 open-design chart（非共用 mcp-server）。
        0.1.4＝在 0.1.3（console entrypoint 不再誤殺孤兒孫程序，charts#53）之上
        新增 seed-home initContainer：od-home PVC 蓋掉 image 內建 home、kubelet
        不重播 /etc/skel，故全新 instance 沒有 ~/.bashrc（console 的 web terminal
        是 interactive non-login shell，~/.bashrc 是唯一有效客製落點）。0.1.4 每次
        pod 啟動逐檔「缺才補」預設 dotfiles、永不覆蓋租戶改過的檔（charts#57，
        Refs #407）。homeSeed 預設 enabled 且沿用 daemon image，seed 無需覆寫。
        0.2.0＝OpenDesign 0.24.1 image（上游預建 base ＋ Claude Code／OpenCode）；
        authProxy.basicAuth.username 開放租戶自設並於 render 時驗證；選填
        auth.openrouterApiKey（charts#89）。0.2.1＝帳號改放 chart Secret（admin_username），
        改帳號不再改動 pod 設定（charts#91；原本必定撞 reset 的 15 秒上限而回退）。
        0.3.0＝daemon 改 StatefulSet，console RBAC 全部以 resourceNames 綁 daemon pod
        （charts#114，Refs #1360：舊版 console SA 可 exec 進同工作區所有 pod）；
        htpasswd 改 Secret volume，改密碼不重啟 daemon。
        0.4.x＝R8（#1462）：開箱預設代理（OpenCode＋釘死模型）、改 LLM 金鑰自動重啟
        （checksum/api-keys）、ephemeral-storage、白牌；0.4.1＝同工作區 LiteLLM 網址。"""
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(
            tpl.helm_chart_name,
            'oci://jcr-prod.woowtech.io/woow-paas-docker-local/open-design')
        # 0.4.4＝#1538（loopback 路由經 auth-proxy、SSRF 放行同工作區 LiteLLM）；
        # 0.4.5＝#1556（ANTHROPIC／OPENROUTER 金鑰一律輸出，清除後 daemon 不留舊值）。
        self.assertEqual(tpl.helm_chart_version, '0.4.5')

    def test_image_repository(self):
        """image repository＝獨立 open-design（≠ MCP 的 opendesign-mcp-admin）。"""
        repo = self._values()['image']['repository']
        self.assertEqual(
            repo, 'jcr-prod.woowtech.io/woow-paas-docker-local/open-design')
        self.assertNotIn('mcp', repo, '完整應用 image 非 MCP image')

    def test_image_tag_pinned_digest(self):
        """daemon image 雙釘 tag@sha256、不得留 latest／PENDING（支線 B 已交付）。"""
        tag = self._values()['image']['tag']
        self.assertNotEqual(tag, 'latest', '不得留 latest 佔位')
        self.assertNotEqual(
            tag, 'PENDING_BRANCH_B_DIGEST', '支線 B digest 未補（PR 前必補真值）')
        self.assertIn('@sha256:', tag, 'image tag 必須雙釘 tag@sha256')

    def test_console_image_pinned_digest(self):
        """console image 亦雙釘 tag@sha256（不留 latest mutable；ConfigMap 注入 app）。"""
        img = self._values()['console']['image']
        self.assertEqual(
            img['repository'],
            'jcr-prod.woowtech.io/woow-paas-docker-local/open-design-console')
        self.assertNotEqual(img['tag'], 'latest', 'console 不得留 latest')
        self.assertIn('@sha256:', img['tag'], 'console image 必須雙釘 tag@sha256')

    def test_daemon_service_port_7457(self):
        """service.port 7457（＝default_port；C 案 targetPort 由 chart 轉 authProxy）。"""
        self.assertEqual(self._values()['service']['port'], 7457)

    def test_dual_pvc(self):
        """雙 PVC（values.yaml 扁平 data/home）：data 4Gi→/app/.od、home 1Gi→
        /home/opendesign（home 保 claude login／opencode 設定跨重啟）。#1637 由 8＋2 改 4＋1。"""
        p = self._values()['persistence']
        self.assertEqual(p['data']['size'], '4Gi')
        self.assertEqual(p['data']['mountPath'], '/app/.od')
        self.assertEqual(p['home']['size'], '1Gi')
        self.assertEqual(p['home']['mountPath'], '/home/opendesign')

    def test_resources(self):
        """daemon resources（平台最重服務；對齊 values.yaml 字面 cpu "1"/"4"）：
        requests 1c/2Gi、limits 4c/8Gi。"""
        r = self._values()['resources']
        self.assertEqual(r['requests']['cpu'], '1')
        self.assertEqual(r['requests']['memory'], '2Gi')
        self.assertEqual(r['limits']['cpu'], '4')
        self.assertEqual(r['limits']['memory'], '8Gi')

    def test_auth_proxy_gate(self):
        """C 案租戶邊界：authProxy.enabled＋basicAuth.enabled、帳號 admin（明寫防 chart
        預設漂移把閘關掉；密碼＝#357 注入的 config.sensitive.admin_password）。"""
        ap = self._values()['authProxy']
        self.assertTrue(ap['enabled'], 'C 案須開 auth-proxy 閘')
        self.assertTrue(ap['basicAuth']['enabled'])
        self.assertEqual(ap['basicAuth']['username'], 'admin')

    def test_console_enabled(self):
        """console（ttyd + Flask dashboard）上架（拍板 2）：console.enabled=True。"""
        self.assertTrue(self._values()['console']['enabled'])

    def test_no_od_api_token_in_git_values(self):
        """OD_API_TOKEN 由 chart 自產內部化（auth.odApiToken lookup+persist），租戶不碰，
        不得出現在 git helm_default_values（sensitive 邊界）。

        先做陽性對照再談「不含憑證」：``assertNotIn(secret, '')`` 恆真，而空字串
        是拿得到的——實測 Odoo 18 的 Text 欄位寫入 ``''`` 會原樣存進 DB（不是
        NULL）、讀回也是 ``''``，不會有 TypeError 幫忙擋。沒有這行的話，seed 這欄
        被清空 ⇒ 憑證守線靜默失效，而輸出與真綠一模一樣。
        """
        raw = self._tpl().helm_default_values
        self.assertIn(
            'authProxy', raw,
            'helm_default_values 裡找不到預期的 authProxy 區塊 —— 探針沒讀到真正的'
            ' seed values（欄位被清空／改名），下面兩條「不含憑證」斷言等於沒跑')
        self.assertNotIn('OD_API_TOKEN', raw)
        self.assertNotIn('odApiToken', raw)

    def test_lockstep_min_specs(self):
        """min_vcpu／min_storage 與 chart 一致（vcpu1／storage5＝4Gi data + 1Gi home；#1637 由 10＝8＋2 改）。
        min_ram_gb 例外：不 lockstep chart requests(2Gi)，改為 quota-sized＝3.0——讓
        cloud_service._compute_ns_quota 的 3× headroom 蓋得住 chart daemon limits.memory(8Gi)
        ＋sidecar（見 test_min_ram_covers_daemon_quota；#380 端到端回歸）。"""
        tpl = self._tpl()
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 3.0)
        self.assertEqual(tpl.min_storage_gb, 5)

    def test_min_ram_covers_daemon_quota(self):
        """回歸守線（#380）：workspace ns quota = sum(存活 service allocated_ram_gb) × 3
        （cloud_service._compute_ns_quota）；本 template 的 plan cs_ram_gb=0 → allocated_ram_gb
        走 fallback 取 min_ram_gb。故 min_ram_gb × 3 必須 ≥ daemon limits.memory，否則 daemon
        pod FailedCreate（exceeded quota）→ service endpoints 空 → 對外 CF 502 永遠連不上。"""
        tpl = self._tpl()
        limits_mem = self._values()['resources']['limits']['memory']
        self.assertTrue(limits_mem.endswith('Gi'), limits_mem)
        daemon_gi = float(limits_mem[:-2])
        self.assertGreaterEqual(
            tpl.min_ram_gb * 3, daemon_gi,
            'min_ram_gb×3（workspace quota headroom）必須 ≥ daemon limits.memory %s；'
            '否則 daemon pod 被 ns quota 擋下、服務連不上' % limits_mem)

    def test_anthropic_key_optional_spec(self):
        """helm_value_specs：ANTHROPIC_API_KEY 為選填（key 對齊 values.yaml auth.anthropicApiKey）;
        留空則走 console TUI claude login。"""
        specs = json.loads(self._tpl().helm_value_specs)
        self.assertEqual(specs['required'], [])
        keys = [o['key'] for o in specs['optional']]
        self.assertIn('auth.anthropicApiKey', keys)

    def test_description_basic_auth_and_ecosystem(self):
        """description／full_description：帳號密碼登入（預設帳號 admin、可自行修改）／
        上游 v0.24.1／與 mcp-opendesign 的 od_api_base 生態閉環（daemon 埠 7457）。"""
        tpl = self._tpl()
        text = (tpl.description or '') + (tpl.full_description or '')
        self.assertIn('basic-auth', text)
        self.assertIn('admin', text)
        self.assertIn('自行修改', text)
        self.assertIn('v0.24.1', text)
        self.assertIn('od_api_base', text)
        self.assertIn('7457', text)


@tagged('post_install', '-at_install')
class TestOpendesignPlansSeed(TransactionCase):

    # base plan → (price, unit)；單級距 月 4500／年 36000（年×8）
    EXPECTED = {
        'plan_opendesign_monthly': (4500, 'month'),
        'plan_opendesign_yearly': (36000, 'year'),
    }

    def test_base_plans(self):
        for xmlid, (price, unit) in self.EXPECTED.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price, xmlid)
            self.assertEqual(plan.sh_unit, unit, xmlid)
            self.assertEqual(plan.service_type, 'cloud_service', xmlid)

    def test_products_taxfree_and_link(self):
        """product 綁對 plan、taxes_id 空（#366：pts 交易免稅）。"""
        for xmlid in self.EXPECTED:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            product = self.env.ref(
                f'woow_paas_platform.{xmlid.replace("plan_", "product_")}')
            self.assertEqual(product.sh_subscription_plan_id, plan, xmlid)
            self.assertFalse(product.taxes_id, '%s：pts 免稅、taxes_id 應為空' % xmlid)

    def test_template_m2m_base(self):
        """template 掛 2 個 base plan（ac_max_access_seats==0）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        plans = tpl.sudo().subscription_plan_ids
        base = plans.filtered(lambda p: p.ac_max_access_seats == 0)
        self.assertEqual(len(base), 2)
        for xmlid in self.EXPECTED:
            self.assertIn(self.env.ref(f'woow_paas_platform.{xmlid}'), plans)

    def test_access_variant_attached(self):
        """+1access 變體（5100/40800）由 generator 掛上本 template 且 hidden
        （全量斷言在 test_plan_variants_generated；此處抽驗完整應用變體）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        plans = tpl.sudo().subscription_plan_ids
        for xmlid, price in (('plan_opendesign_ac1_monthly', 5100),
                             ('plan_opendesign_ac1_yearly', 40800)):
            v = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertIn(v, plans, xmlid)
            self.assertEqual(v.sh_plan_price, price, xmlid)
            self.assertEqual(v.ac_max_access_seats, 1, xmlid)
            self.assertTrue(v.storefront_hidden, xmlid)

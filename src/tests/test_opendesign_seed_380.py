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
        """mcp_enabled=False：MCP 自 chart 0.5.0 起內建（chart 的 mcp sidecar，由
        helm_default_values 的 mcp.enabled 開），不是 operator 事後 patch sidecar 的
        mcp_enabled 形態（那條寫死 n8n env、只 patch Deployment、另開 -mcp 通道）。"""
        self.assertFalse(self._tpl().mcp_enabled)

    def test_builtin_mcp_enabled(self):
        """od-mcp-merge-20261015：內建 OpenDesign MCP（HA add-on 同一套 runtime）由
        chart 0.5.0 的 mcp.enabled 開啟；必須和含 runtime 的 image 同一次變更。"""
        self.assertEqual(self._values()['mcp'], {'enabled': True})

    def test_enabled_paseo_uses_exact_service_origin(self):
        from ..models.cloud_service import CloudService
        values = {'paseo': {'enabled': True, 'origin': 'https://stale.example'}}
        origin = 'https://svc-owned.tenant.example:8443'
        result = CloudService._inject_allowed_origins(values, 'open-design', origin)
        self.assertIs(result, values)
        self.assertEqual(result['paseo'], {'enabled': True, 'origin': origin})
        self.assertEqual(result['config']['allowedOrigins'], origin)

    def test_paseo_origin_injection_preserves_legacy_and_disabled_values(self):
        from ..models.cloud_service import CloudService
        for paseo in (None, {}, {'enabled': False}, {'enabled': 'true'}):
            with self.subTest(paseo=paseo):
                values = {} if paseo is None else {'paseo': paseo.copy()}
                result = CloudService._inject_allowed_origins(
                    values, 'open-design', 'https://svc-owned.tenant.example')
                self.assertEqual(result.get('paseo'), paseo)
        values = {'paseo': {'enabled': True, 'origin': 'https://explicit.example'}}
        result = CloudService._inject_allowed_origins(values, 'hermes-agent', 'https://other.example')
        self.assertEqual(result, values)
        self.assertEqual(result['paseo']['origin'], 'https://explicit.example')

    def test_omnigent_host_off(self):
        """2026-10-15 Open Design 暫停 Omnigent：明寫 omnigentHost.enabled=false（chart 0.5.0
        預設雖已 false，helm reuse-values 缺 key 時共用套件視為開啟），且平台不再注入
        同工作區 Omnigent 網址。"""
        from ..models.cloud_service import _OMNIGENT_SERVER_URL_VALUE_PATHS
        self.assertEqual(self._values()['omnigentHost'], {'enabled': False})
        self.assertNotIn('open-design', _OMNIGENT_SERVER_URL_VALUE_PATHS)

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
        # 0.4.5＝#1556（ANTHROPIC／OPENROUTER 金鑰一律輸出，清除後 daemon 不留舊值）；
        # 0.5.0＝內建 OpenDesign MCP（mcp sidecar＋/mcp、/private_<token>/mcp、/mcp-admin/）、
        #       omnigentHost 預設關（od-mcp-merge-20261015）。
        # 0.5.1–0.5.6＝charts 整合包 #254：console／auth-proxy 加固、prune-runs、登入失敗鎖 IP、
        #       ttyd 改 loopback＋Basic 前門、log 不記查詢字串、egress-profile=web。
        self.assertEqual(tpl.helm_chart_version, '0.5.9')
        self.assertEqual(self._values()['paseo'], {'enabled': True, 'origin': ''})
        self.assertEqual(json.loads(tpl.rollback_feature_switches)['paseo.enabled'], ['image'])

    def test_image_repository(self):
        """image repository＝獨立 open-design（≠ MCP 的 opendesign-mcp-admin）。"""
        repo = self._values()['image']['repository']
        self.assertEqual(
            repo, 'jcr-prod.woowtech.io/woow-paas-docker-local/open-design')
        self.assertNotIn('mcp', repo, '完整應用 image 非 MCP image')

    def test_image_tag_pinned_digest(self):
        """daemon image 使用已驗收的正式 1751 tag@sha256、不得留 latest／PENDING。chart 0.5.0 的 mcp sidecar
        需要含 /opt/woow（內建 MCP runtime）的 image（paas-odoo-ci #332 起）；PENDING 佔位
        不得進 develop。"""
        tag = self._values()['image']['tag']
        self.assertNotEqual(tag, 'latest', '不得留 latest 佔位')
        self.assertNotIn('PENDING', tag, 'open-design image digest 未補（paas-odoo-ci 建置核可後必補真值）')
        self.assertRegex(tag, r'^[0-9]{8}\.[0-9]{4}@sha256:[0-9a-f]{64}$', 'image tag 必須雙釘 <時戳>@sha256')
        self.assertEqual(tag, '20261026.1751@sha256:2a19e82c2955a1e1d7edb866a2b9817cb2b1a65762c928a6fcb7839b37574cf7')

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
        /home/opendesign（home 保 claude login／opencode 設定跨重啟）。#1637 容量研究：維持 8＋2。"""
        p = self._values()['persistence']
        self.assertEqual(p['data']['size'], '8Gi')
        self.assertEqual(p['data']['mountPath'], '/app/.od')
        self.assertEqual(p['home']['size'], '2Gi')
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
        """min_vcpu／min_storage 與 chart 一致（vcpu1／storage10＝8Gi data + 2Gi home）。
        min_ram_gb 例外：不 lockstep chart requests(2Gi)，改為 quota-sized＝3.5（chart 0.5.0
        加 mcp sidecar 後使用者決定）——讓 cloud_service._compute_ns_quota 的 3× headroom
        蓋得住全容器 limits.memory（見 test_quota_covers_all_container_limits）。"""
        tpl = self._tpl()
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 3.5)
        self.assertEqual(tpl.min_storage_gb, 10)

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

    # chart 0.5.0（woow-paas-charts charts/open-design values.yaml）各 sidecar 的 limits.memory，
    # 平台 helm_default_values 不覆寫它們；chart 改了這三個值這裡要跟著改（chart 端
    # tests/test-mcp.sh 用更嚴的 9Gi 上限守住）。
    CHART_SIDECAR_LIMITS_MI = {'authproxy': 128, 'mcp': 320, 'console': 512}

    @staticmethod
    def _mem_mi(quantity):
        if quantity.endswith('Gi'):
            return float(quantity[:-2]) * 1024
        if quantity.endswith('Mi'):
            return float(quantity[:-2])
        raise AssertionError('unexpected memory quantity %r' % quantity)

    def test_quota_covers_all_container_limits(self):
        """守線（od-mcp-merge-20261015）：單台 Open Design 的工作區 quota（_compute_ns_quota：
        int(sum(allocated_ram_gb)×3)Gi，本範本 plan 沒設 cs_ram_gb ⇒ 取 min_ram_gb）必須蓋得住
        daemon＋authproxy＋mcp＋console 全部容器的 limits.memory，否則 pod 被 quota 擋下
        （#380：服務永遠連不上）。注意 int() 截斷：3.5×3＝10.5 → 10Gi（3.25 只會得到 9Gi）。"""
        from ..models.cloud_service import _internal_provision
        tpl = self._tpl()
        Svc = _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo())
        workspace = self.env['woow_paas_platform.workspace'].create({'name': 'OD quota guard'})
        # 真的走平台的兩段：方案 → allocated_*（_allocated_vals_from），工作區 → quota（_compute_ns_quota）。
        plan = self.env.ref('woow_paas_platform.plan_opendesign_monthly')
        allocated = Svc._allocated_vals_from(tpl, plan)
        Svc.create({
            'name': 'svc-odquota1', 'workspace_id': workspace.id, 'template_id': tpl.id,
            'helm_release_name': 'svc-odquota1', 'helm_namespace': 'paas-ws-odquota1',
            'helm_values': '{}', **allocated,
        })
        _, ram_quota, _ = Svc._compute_ns_quota(workspace)
        values = self._values()
        self.assertTrue(values['mcp']['enabled'], '前提：內建 MCP sidecar 有開')
        total = self._mem_mi(values['resources']['limits']['memory']) + sum(self.CHART_SIDECAR_LIMITS_MI.values())
        self.assertEqual(ram_quota, '10Gi', '單台 Open Design（min_ram_gb 3.5）的工作區 quota 應為 10Gi')
        self.assertGreaterEqual(self._mem_mi(ram_quota) - total, 1024,
                                '全容器 limits 之外至少要留 1Gi 餘裕（使用者 2026-10-15 決定 3.5 的目的）')
        self.assertLessEqual(
            total, self._mem_mi(ram_quota),
            '全容器 limits.memory %.0fMi 超過單台工作區 quota %s（min_ram_gb=%s）' % (
                total, ram_quota, tpl.min_ram_gb))

    def test_anthropic_key_optional_spec(self):
        """helm_value_specs：ANTHROPIC_API_KEY 為選填（key 對齊 values.yaml auth.anthropicApiKey）;
        留空則走 console TUI claude login。"""
        specs = json.loads(self._tpl().helm_value_specs)
        self.assertEqual(specs['required'], [])
        keys = [o['key'] for o in specs['optional']]
        self.assertIn('auth.anthropicApiKey', keys)

    def test_description_basic_auth_and_builtin_mcp(self):
        """description／full_description：帳號密碼登入（預設帳號 admin、可自行修改）／上游
        v0.24.1／daemon 埠 7457／內建 MCP 的三個網址（/mcp＋Bearer、Claude.ai 的
        /private_<金鑰>/mcp、/mcp-admin/）／Cloudflare Access 會擋外部 MCP 的說明／沒有 AI 金鑰時經 MCP
        指定 opencode 仍可能呼叫外部模型（使用者 2026-10-15 決定照現狀全開、只寫明）。
        不再提 mcp-opendesign 的 od_api_base（已下架），也不提 Omnigent（暫停）。"""
        tpl = self._tpl()
        text = (tpl.description or '') + (tpl.full_description or '')
        for needle in ('basic-auth', 'admin', '自行修改', 'v0.24.1', '7457', 'MCP',
                       '/mcp', 'Authorization: Bearer', '/private_', '/mcp-admin/',
                       'Cloudflare Access', '/private_*/mcp', 'opencode', '外部模型'):
            self.assertIn(needle, text, needle)
        for gone in ('od_api_base', 'mcp-opendesign', 'Omnigent', 'omnigent'):
            self.assertNotIn(gone, text, gone)
        self.assertIn('mcp', json.loads(tpl.tags))


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

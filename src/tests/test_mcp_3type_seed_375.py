# -*- coding: utf-8 -*-
"""#375：三型 MCP admin（k8s / vk / opendesign）上架 seed 斷言。

鏡射 test_mcp_manage_seed_338.py，參數化三型。三型 launch contract 各異、
勿互抄：k8s 為 kubernetes-mcp-server（原生 http、command=k8s-mcp-http），治理三
toggle 走 mcp_server.env 全名（K8S_MCP_*）、tools.disabled 真傳導；vk/opendesign
為 supergateway 橋 stdio；opendesign 治理為 advisory-only（不 seed tools.disabled、
危險工具仍暴露）。契約源＝各 app k8s-deploy.yaml 檔頭（#375 comment）。
"""
import json
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcp3TypeTemplateSeed(TransactionCase):

    # xmlid → (app label, image repo 片段)
    TEMPLATES = {
        'cloud_app_mcp_k8s': ('k8s', 'k8s-mcp-admin'),
        'cloud_app_mcp_vk': ('vk', 'vk-mcp-admin'),
        'cloud_app_mcp_opendesign': ('opendesign', 'opendesign-mcp-admin'),
    }

    def _tpl(self, xmlid):
        return self.env.ref(f'woow_paas_platform.{xmlid}')

    def _mcp_server(self, xmlid):
        return json.loads(self._tpl(xmlid).helm_default_values)[
            'config']['nonSensitive']['mcp_server']

    def test_template_seed_common(self):
        """三型共同骨架：category=mcp、is_active、MCP 內建於 body（mcp_enabled=False、
        has_admin_gui=True）、requireFields=admin_password、sensitive 空。"""
        for xmlid, (app, repo) in self.TEMPLATES.items():
            tpl = self._tpl(xmlid)
            self.assertEqual(tpl.category, 'mcp', xmlid)
            self.assertTrue(tpl.is_active, xmlid)
            self.assertFalse(tpl.mcp_enabled,
                             '%s：MCP 內建於 body、非 sidecar' % xmlid)
            self.assertTrue(tpl.has_admin_gui,
                            '%s：image 內建 admin GUI、需 admin_password' % xmlid)
            v = json.loads(tpl.helm_default_values)
            self.assertEqual(v['app'], app, xmlid)
            self.assertIn(repo, v['image']['repository'], xmlid)
            self.assertTrue(v['config']['requireFields'], xmlid)
            self.assertEqual(v['config']['requiredFields'], ['admin_password'], xmlid)
            self.assertEqual(v['config']['sensitive'], {}, xmlid)

    def test_image_tag_is_pinned_digest(self):
        """上架必雙釘 git-sha@sha256、不得留 latest 佔位。"""
        for xmlid in self.TEMPLATES:
            tag = json.loads(self._tpl(xmlid).helm_default_values)['image']['tag']
            self.assertNotEqual(tag, 'latest', '%s 不得留 latest 佔位' % xmlid)
            self.assertIn('@sha256:', tag, '%s image tag 必須雙釘 git-sha@sha256' % xmlid)

    def test_lockstep_min_specs(self):
        """#268 lockstep：min_* 與 chart persistence 一致（vcpu1/ram1.0/storage5＝5Gi）。"""
        for xmlid in self.TEMPLATES:
            tpl = self._tpl(xmlid)
            v = json.loads(tpl.helm_default_values)
            self.assertEqual(tpl.min_vcpu, 1, xmlid)
            self.assertEqual(tpl.min_ram_gb, 1.0, xmlid)
            self.assertEqual(tpl.min_storage_gb, 5, xmlid)
            self.assertEqual(v['persistence']['size'], '5Gi', xmlid)

    def test_port_consistency_all_8000(self):
        """契約紅字：三型 mcp_server.port 齊一 8000 且 == args 內 --port
        （core proxy 反代目標，兩者不一致 proxy 一律 502）。"""
        for xmlid in self.TEMPLATES:
            mcp = self._mcp_server(xmlid)
            self.assertEqual(mcp['port'], 8000, xmlid)
            args = mcp['args']
            self.assertIn('--port', args, xmlid)
            self.assertEqual(args[args.index('--port') + 1], '8000', xmlid)

    def test_launch_k8s_native_http_env_governance(self):
        """k8s：原生 http（command=k8s-mcp-http、非 supergateway），治理三 toggle
        走 mcp_server.env 全名（不加前綴）＋TOOLSETS，env_prefix=K8S_MCP_（只作用於
        connection 鍵）。tools.disabled 真傳導（與 opendesign advisory 相反）。"""
        mcp = self._mcp_server('cloud_app_mcp_k8s')
        self.assertEqual(mcp['command'], 'k8s-mcp-http')
        self.assertNotIn('supergateway', mcp['command'])
        self.assertEqual(mcp['env_prefix'], 'K8S_MCP_')
        env = mcp['env']
        self.assertEqual(env['K8S_MCP_READ_ONLY'], '1')
        self.assertEqual(env['K8S_MCP_DISABLE_DESTRUCTIVE'], '1')
        self.assertEqual(env['K8S_MCP_ALLOW_SECRETS'], '0')
        self.assertEqual(env['K8S_MCP_TOOLSETS'], 'core,config,helm')

    def test_launch_vk_supergateway_bridge(self):
        """vk：supergateway 橋 stdio→streamableHttp（vibe-kanban-mcp global 模式），
        env 空、env_prefix 空（connection 鍵大寫即注入）。"""
        mcp = self._mcp_server('cloud_app_mcp_vk')
        self.assertEqual(mcp['command'], 'supergateway')
        self.assertIn('--stdio', mcp['args'])
        self.assertIn('vibe-kanban-mcp --mode global', mcp['args'])
        self.assertIn('streamableHttp', mcp['args'])
        self.assertEqual(mcp['env'], {})
        self.assertEqual(mcp['env_prefix'], '')

    def test_launch_opendesign_supergateway_advisory(self):
        """opendesign：supergateway 橋 python -m opendesign_mcp_server，顯式釘
        streamableHttpPath=/mcp；治理 advisory-only（不 seed tools.disabled，
        per-tool 停用不生效、危險工具仍暴露）、不設 od_api_token。與 k8s 語意相反。"""
        cfg = json.loads(self._tpl('cloud_app_mcp_opendesign').helm_default_values)[
            'config']['nonSensitive']
        mcp = cfg['mcp_server']
        self.assertEqual(mcp['command'], 'supergateway')
        self.assertIn('python -m opendesign_mcp_server', mcp['args'])
        self.assertIn('--streamableHttpPath', mcp['args'])
        self.assertEqual(
            mcp['args'][mcp['args'].index('--streamableHttpPath') + 1], '/mcp')
        self.assertEqual(mcp['env'], {})
        # advisory：此架構 per-tool 停用僅顯示層，故不得 seed tools.disabled
        self.assertNotIn('tools', cfg,
                         'opendesign 治理為 advisory，不 seed tools.disabled')

    def test_description_mentions_prereq(self):
        """三型租戶側前置需求寫入 description／full_description。"""
        prereq = {
            'cloud_app_mcp_k8s': 'kubeconfig',
            'cloud_app_mcp_vk': 'vibe_backend_url',
            'cloud_app_mcp_opendesign': 'od_api_base',
        }
        for xmlid, needle in prereq.items():
            tpl = self._tpl(xmlid)
            text = (tpl.description or '') + (tpl.full_description or '')
            self.assertIn(needle, text, xmlid)


@tagged('post_install', '-at_install')
class TestMcp3TypePlansSeed(TransactionCase):

    # base plan → (price, unit)；齊一價 月 3000／年 24000
    EXPECTED = {
        'plan_mcp_k8s_monthly': (3000, 'month'),
        'plan_mcp_k8s_yearly': (24000, 'year'),
        'plan_mcp_vk_monthly': (3000, 'month'),
        'plan_mcp_vk_yearly': (24000, 'year'),
        'plan_mcp_opendesign_monthly': (3000, 'month'),
        'plan_mcp_opendesign_yearly': (24000, 'year'),
    }
    LINKS = {
        'cloud_app_mcp_k8s': ('plan_mcp_k8s_monthly', 'plan_mcp_k8s_yearly'),
        'cloud_app_mcp_vk': ('plan_mcp_vk_monthly', 'plan_mcp_vk_yearly'),
        'cloud_app_mcp_opendesign': (
            'plan_mcp_opendesign_monthly', 'plan_mcp_opendesign_yearly'),
    }

    def test_plans(self):
        for xmlid, (price, unit) in self.EXPECTED.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price, xmlid)
            self.assertEqual(plan.sh_unit, unit, xmlid)
            self.assertEqual(plan.service_type, 'cloud_service', xmlid)

    def test_products_taxfree_and_link(self):
        """product 綁對 plan、且 taxes_id 空（#366：pts 交易免稅）。"""
        for xmlid in self.EXPECTED:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            product = self.env.ref(
                f'woow_paas_platform.{xmlid.replace("plan_", "product_")}')
            self.assertEqual(product.sh_subscription_plan_id, plan, xmlid)
            self.assertFalse(product.taxes_id, '%s：pts 免稅、taxes_id 應為空' % xmlid)

    def test_template_m2m_base(self):
        """三型 template 各掛 2 個 base plan（ac_max_access_seats==0）。"""
        for tpl_xmlid, plan_xmlids in self.LINKS.items():
            tpl = self.env.ref(f'woow_paas_platform.{tpl_xmlid}')
            plans = tpl.sudo().subscription_plan_ids
            base = plans.filtered(lambda p: p.ac_max_access_seats == 0)
            self.assertEqual(len(base), 2, tpl_xmlid)
            for p_xmlid in plan_xmlids:
                self.assertIn(
                    self.env.ref(f'woow_paas_platform.{p_xmlid}'), plans, tpl_xmlid)

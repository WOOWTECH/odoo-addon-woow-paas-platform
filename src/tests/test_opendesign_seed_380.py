# -*- coding: utf-8 -*-
"""#380：Open Design 完整應用（daemon + console）上架 seed 斷言。

鏡射 test_mcp_3type_seed_375.py 結構。此為「完整應用」（非 MCP server）：獨立 image
open-design、獨立 chart open-design、category=ai_llm、daemon 對外埠 7457、雙 PVC、
console.enabled。契約源＝上游 WOOWTECH/Woow_opendesign_docker_compose_all/k8s-manifests
實證（04-deployment / 03-pvc / 05-service / 02-config）。

注意（pin）：helm image.tag 現為 PENDING_BRANCH_B_DIGEST 佔位，支線 B 交付 digest 後補
真值；test_image_tag_pinned_digest 在 pin 補上前為**預期紅**（fail-loud，PR 前必轉綠、
不 skip 以免忘補）。注意（values）：helm_default_values 鍵名為依上游 k8s-manifests 推導
的草稿，待支線 A charts/open-design 的 values 契約對齊後同步（見 PR 描述「待對齊」清單）。
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

    def test_not_mcp_and_no_admin_gui(self):
        """完整應用非 MCP server：mcp_enabled=False；OD_API_TOKEN 供給機制待拍板，
        故刻意不設 has_admin_gui（該注入路徑 config.sensitive.admin_password 為
        mcp-admin-core 專屬、語意是 GUI 登入密碼，與 daemon bearer token 不符）。"""
        tpl = self._tpl()
        self.assertFalse(tpl.mcp_enabled)
        self.assertFalse(
            tpl.has_admin_gui,
            'OD_API_TOKEN ≠ mcp admin_password，不套 has_admin_gui 供給鏈')

    def test_chart_oci(self):
        """chart＝woow-paas-charts 發佈的獨立 open-design chart（非共用 mcp-server）。"""
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(
            tpl.helm_chart_name,
            'oci://jcr-prod.woowtech.io/woow-paas-docker-local/open-design')
        self.assertEqual(tpl.helm_chart_version, '0.1.0')

    def test_image_repository(self):
        """image repository＝獨立 open-design（≠ MCP 的 opendesign-mcp-admin）。"""
        repo = self._values()['image']['repository']
        self.assertEqual(
            repo, 'jcr-prod.woowtech.io/woow-paas-docker-local/open-design')
        self.assertNotIn('mcp', repo, '完整應用 image 非 MCP image')

    def test_image_tag_pinned_digest(self):
        """上架必雙釘 git-sha@sha256（支線 B 交付後補；PENDING 佔位期為預期紅）。"""
        tag = self._values()['image']['tag']
        self.assertNotEqual(tag, 'latest', '不得留 latest 佔位')
        self.assertNotEqual(
            tag, 'PENDING_BRANCH_B_DIGEST', '支線 B digest 未補（PR 前必補真值）')
        self.assertIn('@sha256:', tag, 'image tag 必須雙釘 git-sha@sha256')

    def test_daemon_service_port_7457(self):
        """daemon 對外埠 7457（上游 05-service 實證）。"""
        self.assertEqual(self._values()['service']['port'], 7457)

    def test_dual_pvc(self):
        """雙 PVC（上游 03-pvc 實證）：data 8Gi→/app/.od、home 2Gi→/home/opendesign。
        home PVC 保 claude login／opencode 設定跨重啟。"""
        p = self._values()['persistence']
        self.assertEqual(p['data']['size'], '8Gi')
        self.assertEqual(p['data']['mountPath'], '/app/.od')
        self.assertEqual(p['home']['size'], '2Gi')
        self.assertEqual(p['home']['mountPath'], '/home/opendesign')

    def test_resources(self):
        """daemon resources（上游 04-deployment 實證）：requests 1c/2Gi、limits 4c/8Gi
        （平台目前最重的服務）。"""
        r = self._values()['resources']
        self.assertEqual(r['requests']['cpu'], '1000m')
        self.assertEqual(r['requests']['memory'], '2Gi')
        self.assertEqual(r['limits']['cpu'], '4000m')
        self.assertEqual(r['limits']['memory'], '8Gi')

    def test_probe_health_path(self):
        """readiness/liveness 皆 GET /api/health:7457（上游 04-deployment 實證）。"""
        pr = self._values()['probes']
        self.assertEqual(pr['readiness']['path'], '/api/health')
        self.assertEqual(pr['readiness']['port'], 7457)
        self.assertEqual(pr['liveness']['path'], '/api/health')
        self.assertEqual(pr['liveness']['port'], 7457)

    def test_console_enabled(self):
        """console（ttyd + Flask dashboard）上架（拍板 2）：console.enabled=True。"""
        self.assertTrue(self._values()['console']['enabled'])

    def test_recreate_and_fsgroup(self):
        """單寫者存儲＝Recreate；securityContext.fsGroup 1001（上游 04-deployment 實證）。"""
        v = self._values()
        self.assertEqual(v['updateStrategy']['type'], 'Recreate')
        self.assertEqual(v['podSecurityContext']['fsGroup'], 1001)

    def test_api_auth_not_bypassed(self):
        """API auth 紅線（拍板 4）：不沿用上游 OD_DISABLE_API_AUTH=1（那是躲在私有 CF
        Tunnel 後的設計，平台租戶 URL 公開）。"""
        cfg = self._values()['config']
        self.assertNotEqual(
            cfg.get('OD_DISABLE_API_AUTH'), '1',
            '平台租戶 URL 公開，不得旁路 daemon API auth')

    def test_lockstep_min_specs(self):
        """min_* 與 chart 一致：vcpu1／ram2.0／storage10（8Gi data + 2Gi home）。"""
        tpl = self._tpl()
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 2.0)
        self.assertEqual(tpl.min_storage_gb, 10)

    def test_ecosystem_hint_in_description(self):
        """description／full_description 提及與 mcp-opendesign 的 od_api_base 生態閉環
        （內部位址 open-design-svc:7457 形態）。"""
        tpl = self._tpl()
        text = (tpl.description or '') + (tpl.full_description or '')
        self.assertIn('od_api_base', text)
        self.assertIn('open-design-svc:7457', text)


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

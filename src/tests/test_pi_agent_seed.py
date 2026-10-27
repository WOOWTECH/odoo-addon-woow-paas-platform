# -*- coding: utf-8 -*-
"""pi Coding Agent（pi-agent）上架 seed 的守線斷言。
契約來源：woow-paas-charts charts/pi-agent 0.1.2（nginx basic-auth sidecar :8080 是唯一入口、
pi-web 綁 loopback、非 root）、paas-odoo-ci pi-agent/（image）。核心守線只有一條：
**上架（is_hidden=False）之前 image.tag 必須雙釘 tag@sha256**——隱藏期允許佔位 tag，
翻 is_hidden 的那次變更必須同時把 tag 釘死，兩件事不能分開合。
"""
import json

from odoo.tests.common import TransactionCase, tagged

PI_IMAGE = 'jcr-prod.woowtech.io/woow-paas-docker-local/pi-agent'
PI_CHART = 'oci://jcr-prod.woowtech.io/woow-paas-docker-local/pi-agent'


@tagged('post_install', '-at_install')
class TestPiAgentSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_pi_agent')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'pi-agent')
        self.assertEqual(tpl.category, 'ai_llm')
        self.assertEqual(tpl.default_port, 8080, 'auth-proxy 是唯一入口，:8080')
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, PI_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.13')

    def test_admin_gui_supply_chain(self):
        """has_admin_gui + helm_secret：平台供給 config.sensitive.admin_password 給 chart Secret，
        reset 走 helm upgrade + rollout restart（chart 沒把 Secret 落成 PVC 檔）。"""
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui)
        self.assertEqual(tpl.admin_password_carrier, 'helm_secret')
        self.assertFalse(tpl.mcp_enabled, 'MCP sidecar 機制是 n8n 專用，pi-agent 不用')

    def test_tenant_can_set_credentials(self):
        """帳號路徑＝chart 0.1.2 的 authProxy.basicAuthUsername（注意不是 open-design 的
        authProxy.basicAuth.username）。路徑寫錯時 helm upgrade 照樣成功、htpasswd 卻還是舊
        帳號——租戶改完登不進去，而平台以為已生效。"""
        tpl = self._tpl()
        self.assertEqual(tpl.admin_username_helm_path, 'authProxy.basicAuthUsername')
        self.assertEqual(
            self._values()['authProxy']['basicAuthUsername'], 'admin',
            'template 預設帳號必須就在 admin_username_helm_path 指的位置，對話框才預填得到')

    def test_image_repository(self):
        repo = self._values()['image']['repository']
        self.assertEqual(repo, PI_IMAGE)
        self.assertNotIn('ghcr.io', repo, '平台只從 jcr-prod 拉；ghcr 是上游，PaaS image 是衍生層')

    def test_image_pinned_before_listing(self):
        """隱藏期允許佔位 tag；一旦 is_hidden=False，tag 必須是 YYYYMMDD.HHMM@sha256:<64>。"""
        tpl = self._tpl()
        tag = self._values()['image']['tag']
        if tpl.is_hidden:
            self.assertTrue(tag, 'tag 不得為空（chart 對空 tag render 失敗）')
            return
        self.assertIn('@sha256:', tag, '上架前 image.tag 必須雙釘 tag@sha256')
        build_tag, digest = tag.split('@', 1)
        self.assertRegex(build_tag, r'^\d{8}\.\d{4}$')
        self.assertRegex(digest, r'^sha256:[0-9a-f]{64}\Z')
        self.assertNotEqual(build_tag, '00000000.0000', '佔位 tag 不得上架')
        self.assertEqual(
            tag,
            '20261027.0638@sha256:d9889388778bea22af443d360482b7b8032fd7965ceb02ac2c871860694bd226',
            '新開通範本必須釘住已核准並從正式 JCR 讀回的 Pi Agent 映像',
        )

    def test_resources_match_t1(self):
        """T1 = 2 vCPU / 8 Gi / 30 Gi（#1637 容量研究：4 → 8、20 → 30）：template min_*、plan cs_*、helm values 三處一致。"""
        tpl = self._tpl()
        v = self._values()
        self.assertEqual(tpl.min_vcpu, 2)
        self.assertEqual(tpl.min_ram_gb, 8.0)
        self.assertEqual(tpl.min_storage_gb, 30)
        self.assertEqual(v['piWeb']['resources']['limits']['cpu'], '2')
        self.assertEqual(v['piWeb']['resources']['limits']['memory'], '8Gi')
        self.assertEqual(v['piWeb']['nodeMaxOldSpaceMb'], 1536, '3072 會讓每個 Node 子程序各自長到 3G')
        self.assertEqual(v['startupProbe']['timeoutSeconds'], 5)
        self.assertEqual(v['persistence']['size'], '30Gi')
        for plan in tpl.sudo().subscription_plan_ids:
            self.assertEqual(plan.cs_vcpu, 2, plan.name)
            self.assertEqual(plan.cs_ram_gb, 8.0, plan.name)
            self.assertEqual(plan.cs_storage_gb, 30, plan.name)

    def test_service_port_lockstep(self):
        v = self._values()
        self.assertEqual(v['service']['port'], 8080)
        self.assertEqual(v['service']['targetPort'], 8080)
        self.assertEqual(v['authProxy']['port'], 8080)

    def test_plans_linked_and_priced(self):
        tpl = self._tpl()
        plans = tpl.sudo().subscription_plan_ids
        m = self.env.ref('woow_paas_platform.plan_pi_agent_monthly')
        y = self.env.ref('woow_paas_platform.plan_pi_agent_yearly')
        self.assertIn(m, plans); self.assertIn(y, plans)
        self.assertEqual(m.sh_plan_price, 4500)
        self.assertEqual(y.sh_plan_price, 45000)
        for xmlid in ('product_pi_agent_monthly', 'product_pi_agent_yearly'):
            p = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertTrue(p.sh_product_subscribe, xmlid)
            self.assertFalse(p.taxes_id, '%s 免稅（同其他 cloud service 產品）' % xmlid)

    def test_hidden_only_not_concealed(self):
        """未上架（is_hidden）但不 conceal：既有服務照常可見（#471 兩層語意）。"""
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.conceal_instances)

"""TS-3b: L4 reconciler 左集蒐集（collect_left_set）的 ORM walk 完整性。

左集 = Odoo 持有的 stored-id 白名單；差集（TS-3c）= operator CF 右集 − 左集 = 孤兒候選。
漏蒐任一 stored-id 欄位 → 活躍資源被誤判成孤兒（最高衝擊正確性點），故本檔逐鍵守。

設計 doc Phase B.1 / B.3。
"""
from odoo.tests import TransactionCase, tagged

from ..services.reconciler import LeftSet, collect_left_set

# #522: server-owned identity anchors (tunnel_id/tunnel_token/dns_record_id/
# access_app_id/...) are immutable for everyone; provisioning code stores the
# ids Cloudflare hands back through this in-process sentinel hatch, and these
# fixtures simulate exactly that. See models/security_access.py.
from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
)
from ..models.cloud_service import _internal_provision
from .smart_home_fixture import sh_model  # #524：建立閘 fixture helper

# #798：`cf_policy_id` 有欄位級寫入守衛（create 壓值／write 拒絕，對 su 也生效）。
# fixture 擺狀態必須經開閘 helper，否則會撞到自己要測的守衛。
from ..models.security_access import (
    _internal_policy_cf_write as _internal_policy_cf,
)


@tagged('post_install', '-at_install')
class TestCollectLeftSet(TransactionCase):
    """collect_left_set 對 SA（2 層巢狀）/ CS（MCP 重建）/ SM 三表的 stored-id 蒐集。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = cls.env['woow_paas_platform.workspace'].create({'name': 'L4 WS'})
        cls.template = cls.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'L4 Template',
            'slug': 'l4t',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'l4t',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1,
            'min_ram_gb': 1.0,
            'min_storage_gb': 1,
        })
        cls.SA = cls.env['woow_paas_platform.security_access']
        cls.Route = cls.env['woow_paas_platform.security_access_route']
        cls.Policy = cls.env['woow_paas_platform.security_access_access_policy']
        cls.CS = cls.env['woow_paas_platform.cloud_service']
        cls.SM = sh_model(cls.env)

    def _make_sa_route_policy(self, suffix, *, tunnel_id, dns_id, app_id, cf_policy_id):
        sa = self.SA.create({'name': f'SA-{suffix}', 'workspace_id': self.ws.id})
        if tunnel_id:
            _internal(sa).write({'tunnel_id': tunnel_id})
        route = self.Route.create({
            'security_access_id': sa.id,
            'subdomain_prefix': f'l4-{suffix}',
            'service_url': 'http://localhost:80',
        })
        if dns_id:
            _internal(route).dns_record_id = dns_id
        if app_id:
            _internal(route).access_app_id = app_id
        policy = self.Policy.create({
            'route_id': route.id,
            'name': f'pol-{suffix}',
            'decision': 'allow',
            'sequence': 1,
            'rule_ids': [(0, 0, {
                'group': 'include', 'rule_type': 'email_list',
                'rule_value': 'user@acme.com', 'sequence': 1,
            })],
        })
        if cf_policy_id:
            _internal_policy_cf(policy).cf_policy_id = cf_policy_id
        return sa, route, policy

    def test_sa_two_layer_walk_collects_all_stored_ids(self):
        """SA：tunnel_id + route(dns/app/subdomain_prefix) + 巢狀 policy.cf_policy_id 全進左集。

        關鍵：cf_policy_id 在第 2 層（route_ids→access_policy_ids）；只走到 route 不展開
        policy 的版本，'cf-deep-1' 不會出現 → 此斷言紅（活躍 policy 誤判孤兒）。
        """
        self._make_sa_route_policy(
            'app1', tunnel_id='tun-sa-1', dns_id='dns-1',
            app_id='app-1', cf_policy_id='cf-deep-1')

        ls = collect_left_set(self.env)

        self.assertIn('tun-sa-1', ls.tunnel_ids)
        self.assertIn('dns-1', ls.dns_record_ids)
        self.assertIn('app-1', ls.access_app_ids)
        self.assertIn('l4-app1', ls.subdomains)
        self.assertIn('cf-deep-1', ls.access_policy_ids,
                      "2 層 walk 漏走 access_policy_ids → 活躍 policy 被誤判孤兒")

    def test_policy_walk_parity_with_direct_model_search(self):
        """collector 的 cf_policy_id 集合 == 直接 search policy model（獨立 ground truth）。

        collector 走 SA→route_ids→access_policy_ids（route_id binding 鏈）；ground truth 直接
        search policy model。兩條獨立路徑須一致 —— 若 collector 誤走 security_access_id
        store=True related 或漏層即與 ground truth 分歧而紅（design B.3 警示）。
        """
        self._make_sa_route_policy('pa', tunnel_id='', dns_id='', app_id='', cf_policy_id='cf-a')
        self._make_sa_route_policy('pb', tunnel_id='', dns_id='', app_id='', cf_policy_id='cf-b')

        ls = collect_left_set(self.env)
        ground_truth = {
            v for v in self.Policy.sudo().search([]).mapped('cf_policy_id') if v
        }
        self.assertEqual(ls.access_policy_ids, ground_truth)
        self.assertIn('cf-a', ls.access_policy_ids)
        self.assertIn('cf-b', ls.access_policy_ids)

    def test_empty_stored_id_not_in_left_set(self):
        """空 stored-id（False / ''）不入左集（in-flight 半完成不誤當『持有』）。"""
        self.SA.create({'name': 'SA-empty', 'workspace_id': self.ws.id})  # 無 tunnel_id
        ls = collect_left_set(self.env)
        self.assertNotIn(False, ls.tunnel_ids)
        self.assertNotIn('', ls.tunnel_ids)
        self.assertNotIn(False, ls.access_policy_ids)

    def test_cs_subdomain_and_mcp_reconstructed(self):
        """CS：subdomain + 重建的 MCP sidecar subdomain 都進左集。

        MCP subdomain = generate_subdomain(ns, release)+'-mcp'；只收 subdomain 不重建 MCP
        的版本，'myapp-115223fa-mcp' 缺席 → 活躍 MCP DNS/ingress 被誤判孤兒。
        """
        _internal_provision(self.CS).create({
            'name': 'CS1', 'workspace_id': self.ws.id, 'template_id': self.template.id,
            'subdomain': 'paas-cs-aaa-bbb',
            'helm_release_name': 'myapp', 'helm_namespace': 'paas-ws-115223fa',
        })
        ls = collect_left_set(self.env)
        self.assertIn('paas-cs-aaa-bbb', ls.subdomains)
        self.assertIn('myapp-115223fa-mcp', ls.subdomains,
                      "CS MCP subdomain 未重建 → 活躍 MCP route 誤判孤兒")

    def test_cs_without_helm_fields_skips_mcp(self):
        """CS 缺 helm_release_name / helm_namespace（未部署）→ 不亂湊 MCP subdomain。"""
        _internal_provision(self.CS).create({
            'name': 'CS2', 'workspace_id': self.ws.id, 'template_id': self.template.id,
            'subdomain': 'paas-cs-ccc-ddd',
        })
        ls = collect_left_set(self.env)
        self.assertIn('paas-cs-ccc-ddd', ls.subdomains)
        # 不應出現任何以 '-mcp' 結尾的殘缺重建
        self.assertFalse([s for s in ls.subdomains if s.endswith('-mcp')],
                         "缺 helm 欄位的 CS 不該產出 MCP subdomain")

    def test_sm_tunnel_and_subdomain_collected(self):
        """SM：tunnel_id（dedicated 反查鍵）+ subdomain 進左集。"""
        self.SM.create({
            'name': 'SM1', 'workspace_id': self.ws.id,
            'tunnel_id': 'tun-sm-1', 'subdomain': 'paas-sm-xxx', 'state': 'active',
        })
        ls = collect_left_set(self.env)
        self.assertIn('tun-sm-1', ls.tunnel_ids)
        self.assertIn('paas-sm-xxx', ls.subdomains)

    def test_tunnel_ids_union_sa_and_sm(self):
        """tunnel_ids 同時涵蓋 SA 與 SM（兩來源聯集，不互相遮蔽）。"""
        self._make_sa_route_policy(
            'tu', tunnel_id='tun-sa-u', dns_id='', app_id='', cf_policy_id='')
        self.SM.create({
            'name': 'SM-u', 'workspace_id': self.ws.id, 'tunnel_id': 'tun-sm-u'})
        ls = collect_left_set(self.env)
        self.assertIn('tun-sa-u', ls.tunnel_ids)
        self.assertIn('tun-sm-u', ls.tunnel_ids)

    def test_returns_leftset_instance(self):
        """回傳型別契約：LeftSet（差集引擎依賴的固定 shape）。"""
        self.assertIsInstance(collect_left_set(self.env), LeftSet)

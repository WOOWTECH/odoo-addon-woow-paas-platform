# -*- coding: utf-8 -*-
"""#1637（使用者 2026-10-10，資源稽核報告的建議）：方案 overlay 的 CPU／RAM。

1. **limits＝方案、requests 另算**（使用者 2026-10-10 定案）：CPU＝limits × 25%、最低 0.1 核；
   記憶體＝max(limits × 50%, 範本正式站實測峰值)、最低 128Mi、不超過 limits。以前 requests＝limits。
2. **死鍵**（#1532 同型）：方案規格要寫進 chart **真正讀的**鍵——
   code-server ``codeServer.resources``＋``persistence.workspace.size``（pi-data 固定）、
   pi-agent ``piWeb.resources``、headscale ``headscale.resources``（以前落在 headplane）、
   omnigent ``runner.resources`` 的 limits＝方案 − 伺服器 1 vCPU／1 GB（requests 不寫）。
3. **Odoo limit_memory_***：VMS 上限交給 chart 的 tenant 預設（不再用方案記憶體÷workers）。
4. **既有服務**：Redeploy／換方案收斂到目前方案，但目前 limits 超過範本任何方案的值＝管理員
   手調，不覆寫；舊 overlay 留在頂層、chart 不讀的鍵清掉，chart 真的在讀的頂層
   ``persistence.size``（omnigent／headscale）不能動。
5. **pi1–pi5 完全不動**（使用者 2026-10-10）：pi-agent 只在開通時套方案，既有服務 overlay 是
   no-op——helm_values 原樣（pi1／pi2 的 piWeb 12 核／16G、pi3–pi5 的範本值、頂層殘值都不變）。

``_CHART_RESOURCE_PATHS`` 是「各範本釘的 chart 版本，主容器（方案那一份）的 resources 從哪個
values 鍵來」——用 ``helm template`` 對過（PR 說明附結果）：woow-odoo／emqx／nextcloud／
litellm／tailscale 讀頂層 ``resources``（後四個 deep-merge 進主容器），其餘見表。
"""
import copy
import json

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.models.cloud_service import (
    _cpu_quantity_cores, _internal_provision, _plan_cpu_request, _plan_memory_request,
    _storage_quantity_gib)

MOD = 'woow_paas_platform'

# slug → (主容器 resources 的 values 路徑, 伺服器保留（omnigent）, 是否寫 requests)
_CHART_RESOURCE_PATHS = {
    'odoo': (('resources',), (0, 0), True),
    'n8n': (('main', 'resources'), (0, 0), True),
    'nextcloud': (('resources',), (0, 0), True),
    'emqx': (('resources',), (0, 0), True),
    'litellm': (('resources',), (0, 0), True),
    'tailscale': (('resources',), (0, 0), True),
    'headscale': (('headscale', 'resources'), (0, 0), True),
    'code-server': (('codeServer', 'resources'), (0, 0), True),
    'pi-agent': (('piWeb', 'resources'), (0, 0), True),
    'omnigent': (('runner', 'resources'), (1, 1.0), False),
}


def _at(values, path):
    for key in path:
        if not isinstance(values, dict):
            return None
        values = values.get(key)
    return values


def _mem_gib(quantity):
    return _storage_quantity_gib(quantity)


@tagged('post_install', '-at_install')
class TestPlanRequestRatio1637(TransactionCase):
    """requests 的算法：比例、下限、上限，且不超過 limits。"""

    def test_cpu(self):
        for limit, request in ((1, '250m'), (2, '500m'), (3, '750m'), (4, '1'), (8, '2'),
                               (0.25, '100m')):
            with self.subTest(limit=limit):
                self.assertEqual(_plan_cpu_request(limit), request)

    def test_memory_is_half_of_the_limit_without_a_measured_peak(self):
        for limit, request in ((0.5, '256Mi'), (1, '512Mi'), (2, '1Gi'), (4, '2Gi'),
                               (8, '4Gi'), (0.125, '128Mi'), (0.1, '102Mi')):
            with self.subTest(limit=limit):
                self.assertEqual(_plan_memory_request(limit), request)
                self.assertEqual(_plan_memory_request(limit, 'nextcloud'), request)

    def test_memory_never_below_the_measured_peak(self):
        """稽核報告表 1 的 cgroup 峰值往上取整：Odoo 2G、n8n／EMQX／LiteLLM／Code Server／pi 1G。"""
        for slug, limit, request in (('odoo', 2, '2Gi'), ('odoo', 4, '2Gi'),
                                     ('n8n', 1, '1Gi'), ('n8n', 2, '1Gi'), ('n8n', 4, '2Gi'),
                                     ('emqx', 2, '1Gi'), ('litellm', 2, '1Gi'),
                                     ('code-server', 4, '2Gi'), ('pi-agent', 4, '2Gi')):
            with self.subTest(slug=slug, limit=limit):
                self.assertEqual(_plan_memory_request(limit, slug), request)


@tagged('post_install', '-at_install')
class TestPlanOverlayChartKeys1637(TransactionCase):
    """每個有 CPU／RAM 方案的範本：方案寫進 chart 真正讀的鍵，limits＝方案、requests 取比例。"""

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']

    def _templates_with_resource_plans(self):
        Tpl = self.env['woow_paas_platform.cloud_app_template'].sudo()
        return Tpl.search([('is_active', '=', True)]).filtered(
            lambda t: any(p.cs_vcpu or p.cs_ram_gb for p in t.subscription_plan_ids))

    def test_every_template_with_resource_plans_is_covered(self):
        slugs = set(self._templates_with_resource_plans().mapped('slug'))
        self.assertEqual(slugs - set(_CHART_RESOURCE_PATHS), set(),
                         '新範本的方案有 CPU／RAM：在 _CHART_RESOURCE_PATHS 補上 chart 讀哪個鍵')
        self.assertNotIn('mcp-hermes', slugs, '#1637：Hermes MCP 方案不再帶 cs_*')

    def test_plans_land_on_the_chart_keys(self):
        for tpl in self._templates_with_resource_plans():
            path, (reserve_cpu, reserve_mem), with_requests = _CHART_RESOURCE_PATHS[tpl.slug]
            base = json.loads(tpl.helm_default_values or '{}')
            for plan in tpl.subscription_plan_ids.filtered(lambda p: p.cs_vcpu or p.cs_ram_gb):
                with self.subTest(template=tpl.slug, plan=plan.name):
                    out = self.Svc._apply_plan_spec_overlay(copy.deepcopy(base), tpl, plan)
                    res = _at(out, path)
                    self.assertIsInstance(res, dict)
                    cpu = plan.cs_vcpu - reserve_cpu
                    mem = plan.cs_ram_gb - reserve_mem
                    self.assertEqual(_cpu_quantity_cores(res['limits']['cpu']), cpu)
                    self.assertEqual(_mem_gib(res['limits']['memory']), mem)
                    if with_requests:
                        self.assertEqual(res['requests']['cpu'], _plan_cpu_request(cpu))
                        self.assertEqual(res['requests']['memory'],
                                         _plan_memory_request(mem, tpl.slug))
                        self.assertLessEqual(_cpu_quantity_cores(res['requests']['cpu']), cpu)
                        self.assertLessEqual(_mem_gib(res['requests']['memory']), mem)
                    else:
                        self.assertEqual(_at(res, ('requests',)), _at(base, path + ('requests',)),
                                         'requests 不跟方案（chart 預設）')
                    if path != ('resources',):
                        self.assertNotIn('resources', out, '頂層 resources＝chart 不讀的死鍵')

    def test_requests_only_shrink_against_the_old_overlay(self):
        """以前 requests＝limits＝方案：每個方案新的 requests 都 ≤ 以前的值。"""
        for tpl in self._templates_with_resource_plans():
            path, _reserve, with_requests = _CHART_RESOURCE_PATHS[tpl.slug]
            if not with_requests:
                continue
            base = json.loads(tpl.helm_default_values or '{}')
            for plan in tpl.subscription_plan_ids.filtered(lambda p: p.cs_vcpu and p.cs_ram_gb):
                with self.subTest(template=tpl.slug, plan=plan.name):
                    res = _at(self.Svc._apply_plan_spec_overlay(
                        copy.deepcopy(base), tpl, plan), path)
                    self.assertLessEqual(
                        _cpu_quantity_cores(res['requests']['cpu']), plan.cs_vcpu)
                    self.assertLessEqual(_mem_gib(res['requests']['memory']), plan.cs_ram_gb)

    def test_dead_key_templates_now_match_what_is_sold(self):
        """code-server／pi-agent 2 vCPU／8 GB、headscale 1／1、omnigent 3／7（＋伺服器 1／1）。"""
        cases = {
            'plan_code_server_monthly': ('code-server', ('codeServer', 'resources'),
                                         {'cpu': '2', 'memory': '8Gi'},
                                         {'cpu': '500m', 'memory': '4Gi'}),
            'plan_pi_agent_monthly': ('pi-agent', ('piWeb', 'resources'),
                                      {'cpu': '2', 'memory': '8Gi'},
                                      {'cpu': '500m', 'memory': '4Gi'}),
            'plan_headscale_monthly': ('headscale', ('headscale', 'resources'),
                                       {'cpu': '1', 'memory': '1Gi'},
                                       {'cpu': '250m', 'memory': '512Mi'}),
            'plan_omnigent_monthly': ('omnigent', ('runner', 'resources'),
                                      {'cpu': '3', 'memory': '7Gi'}, None),
        }
        for xmlid, (slug, path, limits, requests) in cases.items():
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            tpl = self.env.ref('%s.cloud_app_%s' % (MOD, slug.replace('-', '_')))
            with self.subTest(plan=xmlid):
                out = self.Svc._apply_plan_spec_overlay(
                    json.loads(tpl.helm_default_values), tpl, plan)
                self.assertEqual(_at(out, path + ('limits',)), limits)
                self.assertEqual(_at(out, path + ('requests',)), requests)
                self.assertNotIn('resources', out)

    def test_code_server_storage_is_workspace_plus_pi_data(self):
        tpl = self.env.ref('%s.cloud_app_code_server' % MOD)
        plan = self.env.ref('%s.plan_code_server_monthly' % MOD)
        out = self.Svc._apply_plan_spec_overlay(json.loads(tpl.helm_default_values), tpl, plan)
        self.assertEqual(out['persistence']['workspace']['size'], '16Gi')
        self.assertEqual(out['persistence']['piData']['size'], '4Gi')
        self.assertNotIn('size', out['persistence'], '頂層 persistence.size 是 chart 不讀的死鍵')

    def test_top_level_storage_key_stays_where_the_chart_reads_it(self):
        """pi-agent／omnigent／headscale：搬的只有 resources，頂層 persistence.size 是真的 PVC。"""
        for xmlid, slug, size in (('plan_pi_agent_monthly', 'pi-agent', '30Gi'),
                                  ('plan_omnigent_monthly', 'omnigent', '5Gi'),
                                  ('plan_headscale_monthly', 'headscale', '1Gi')):
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            tpl = self.env.ref('%s.cloud_app_%s' % (MOD, slug.replace('-', '_')))
            with self.subTest(template=slug):
                out = self.Svc._apply_plan_spec_overlay(
                    json.loads(tpl.helm_default_values), tpl, plan)
                self.assertEqual(out['persistence']['size'], size)


@tagged('post_install', '-at_install')
class TestPlanOverlayExistingServices1637(TransactionCase):
    """既有服務（Redeploy／換方案的 assemble）：收斂、清死鍵、不縮 PVC、不動管理員手調。"""

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']

    def _tpl(self, slug):
        return self.env.ref('%s.cloud_app_%s' % (MOD, slug.replace('-', '_')))

    def _plan(self, xmlid):
        return self.env.ref('%s.%s' % (MOD, xmlid))

    def _old_overlay(self, values, path_top, cpu, mem, size=None):
        """#1637 以前的 overlay 留在頂層的形狀：requests＝limits＝方案。"""
        quantity = {'cpu': str(cpu), 'memory': mem}
        values['resources'] = {'requests': dict(quantity), 'limits': dict(quantity)}
        if size:
            values.setdefault('persistence', {})['size'] = size
        return values

    def test_code_server_prod_shape_converges_without_shrinking(self):
        """正式站 2 台的形狀：工作區 20＋pi-data 5、頂層殘值 25Gi／2／4Gi。"""
        tpl = self._tpl('code-server')
        legacy = json.loads(tpl.helm_default_values)
        legacy['persistence']['workspace']['size'] = '20Gi'
        legacy['persistence']['piData']['size'] = '5Gi'
        legacy['codeServer']['resources'] = {'requests': {'cpu': '500m', 'memory': '1Gi'},
                                             'limits': {'cpu': '2', 'memory': '4Gi'}}
        self._old_overlay(legacy, None, 2, '4Gi', '25Gi')
        out = self.Svc._apply_plan_spec_overlay(
            legacy, tpl, self._plan('plan_code_server_monthly'), existing=True)
        self.assertEqual(out['persistence']['workspace']['size'], '20Gi', '只擴不縮')
        self.assertEqual(out['persistence']['piData']['size'], '5Gi')
        self.assertNotIn('size', out['persistence'])
        self.assertNotIn('resources', out)
        self.assertEqual(out['codeServer']['resources'],
                         {'requests': {'cpu': '500m', 'memory': '4Gi'},
                          'limits': {'cpu': '2', 'memory': '8Gi'}},
                         '記憶體上限 4 → 8Gi（容量研究）、requests 照規則 4Gi')

    def _pi_shapes(self):
        """正式站 pi1–pi5 的 helm_values 形狀：pi1／pi2 piWeb 12 核／16G、pi1 的 PVC 100Gi；
        pi3–pi5 是範本值；全部帶著 #1637 以前 overlay 寫在頂層的死鍵。"""
        tpl = self._tpl('pi-agent')
        base = json.loads(tpl.helm_default_values)
        manual = {'requests': {'cpu': '12', 'memory': '16Gi'},
                  'limits': {'cpu': '12', 'memory': '16Gi'}}
        shapes = {}
        for name, resources, size in (('pi1', manual, '100Gi'), ('pi2', manual, '20Gi'),
                                      ('pi3', None, '20Gi')):
            v = copy.deepcopy(base)
            if resources:
                v['piWeb']['resources'] = copy.deepcopy(resources)
            v['persistence']['size'] = size
            shapes[name] = self._old_overlay(v, None, 2, '4Gi')
        return tpl, shapes

    def test_pi1_to_pi5_overlay_is_a_no_op(self):
        """使用者 2026-10-10：pi1–pi5 這次完全不動——既有 pi-agent 套任何方案，values 原樣。"""
        tpl, shapes = self._pi_shapes()
        bigger = self.env['sh.subscription.plan'].sudo().create({
            'name': '1637 pi bigger', 'sh_plan_price': 1, 'sh_duration': 1, 'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1, 'cs_vcpu': 4, 'cs_ram_gb': 8.0, 'cs_storage_gb': 30})
        for name, values in shapes.items():
            for plan in (self._plan('plan_pi_agent_monthly'), bigger):
                with self.subTest(service=name, plan=plan.name):
                    out = self.Svc._apply_plan_spec_overlay(
                        copy.deepcopy(values), tpl, plan, existing=True)
                    self.assertEqual(out, values)

    def test_pi1_to_pi5_redeploy_values_keep_12_cores(self):
        """Redeploy／reconfigure 的 assemble：piWeb 12 核／16G、PVC、頂層殘值都照存的送。"""
        tpl, shapes = self._pi_shapes()
        ws = self.env['woow_paas_platform.workspace'].create({'name': '1637 pi ws'})
        for name, values in shapes.items():
            with self.subTest(service=name):
                svc = _internal_provision(self.Svc).create({
                    'name': 'svc-1637-%s' % name, 'workspace_id': ws.id,
                    'template_id': tpl.id, 'state': 'running',
                    'helm_namespace': 'paas-ws-test-1637', 'helm_release_name': 'svc-1637%s' % name,
                    'helm_chart_version': tpl.helm_chart_version,
                    'allocated_vcpu': 2, 'allocated_ram_gb': 4.0, 'allocated_storage_gb': 20,
                })
                svc._apply_helm_values(copy.deepcopy(values))
                plan = self._plan('plan_pi_agent_monthly')
                sent = svc._assemble_deploy_values(plan=plan)['values']
                self.assertEqual(sent['piWeb']['resources'], values['piWeb']['resources'])
                self.assertEqual(sent['persistence']['size'], values['persistence']['size'])
                self.assertEqual(sent['resources'], values['resources'])
                self.assertEqual(svc._plan_values_for(plan), svc._helm_values_dict(),
                                 '換方案要存的 values 與現在相同')

    def test_new_pi_agent_follows_the_new_rule(self):
        tpl = self._tpl('pi-agent')
        out = self.Svc._apply_plan_spec_overlay(
            json.loads(tpl.helm_default_values), tpl, self._plan('plan_pi_agent_monthly'))
        self.assertEqual(out['piWeb']['resources'],
                         {'requests': {'cpu': '500m', 'memory': '4Gi'},
                          'limits': {'cpu': '2', 'memory': '8Gi'}})

    def test_hand_raised_limits_are_left_alone_elsewhere(self):
        """通用保護：其他範本的既有服務 limits 超過所有方案（管理員手調）也不覆寫。"""
        tpl = self._tpl('code-server')
        values = json.loads(tpl.helm_default_values)
        manual = {'requests': {'cpu': '8', 'memory': '16Gi'}, 'limits': {'cpu': '8', 'memory': '16Gi'}}
        values['codeServer']['resources'] = copy.deepcopy(manual)
        with mute_logger('odoo.addons.woow_paas_platform.models.cloud_service'):
            out = self.Svc._apply_plan_spec_overlay(
                values, tpl, self._plan('plan_code_server_monthly'), existing=True)
        self.assertEqual(out['codeServer']['resources'], manual)

    def test_downgrade_still_lowers_resources(self):
        """在方案範圍內的較大值（降級前的 T4）不是手調：降到 T1 照常變小。"""
        tpl = self._tpl('n8n')
        t4 = self.Svc._apply_plan_spec_overlay(
            json.loads(tpl.helm_default_values), tpl, self._plan('plan_n8n_t4_monthly'))
        out = self.Svc._apply_plan_spec_overlay(
            t4, tpl, self._plan('plan_n8n_monthly'), existing=True)
        self.assertEqual(out['main']['resources']['limits'], {'cpu': '1', 'memory': '2Gi'})
        self.assertEqual(out['main']['resources']['requests'], {'cpu': '250m', 'memory': '1Gi'})
        self.assertEqual(out['main']['persistence']['size'], '20Gi', '儲存只擴不縮')

    def test_prod_shapes_converge_to_the_new_plans(self):
        """正式站 n8n／EMQX／LiteLLM 的形狀（舊 overlay：requests＝limits＝舊方案）：Redeploy 後
        limits＝新方案（n8n T1 1→2G、LiteLLM 2→4G，容量研究）、requests 照規則。"""
        for slug, xmlid, path, old, limits, requests in (
                ('n8n', 'plan_n8n_monthly', ('main', 'resources'), ('1', '1Gi'),
                 {'cpu': '1', 'memory': '2Gi'}, {'cpu': '250m', 'memory': '1Gi'}),
                ('emqx', 'plan_emqx_monthly', ('resources',), ('1', '2Gi'),
                 {'cpu': '1', 'memory': '2Gi'}, {'cpu': '250m', 'memory': '1Gi'}),
                ('litellm', 'plan_litellm_monthly', ('resources',), ('1', '2Gi'),
                 {'cpu': '1', 'memory': '4Gi'}, {'cpu': '250m', 'memory': '2Gi'})):
            tpl, plan = self._tpl(slug), self._plan(xmlid)
            values = json.loads(tpl.helm_default_values)
            res = values
            for key in path:
                res = res.setdefault(key, {})
            quantity = {'cpu': old[0], 'memory': old[1]}
            res.update({'requests': dict(quantity), 'limits': dict(quantity)})
            with self.subTest(template=slug):
                out = self.Svc._apply_plan_spec_overlay(values, tpl, plan, existing=True)
                self.assertEqual(_at(out, path + ('limits',)), limits)
                self.assertEqual(_at(out, path + ('requests',)), requests)

    def test_headscale_legacy_top_level_resources_leave_headplane(self):
        tpl = self._tpl('headscale')
        values = self._old_overlay(json.loads(tpl.helm_default_values), None, 1, '1Gi', '1Gi')
        out = self.Svc._apply_plan_spec_overlay(
            values, tpl, self._plan('plan_headscale_monthly'), existing=True)
        self.assertNotIn('resources', out, 'headplane 回到 chart 預設')
        self.assertEqual(out['headscale']['resources']['limits'], {'cpu': '1', 'memory': '1Gi'})
        self.assertEqual(out['persistence']['size'], '1Gi', 'PVC 大小鍵不能被當殘值清掉')

    def test_omnigent_legacy_keeps_its_10g_volume(self):
        tpl = self._tpl('omnigent')
        values = self._old_overlay(json.loads(tpl.helm_default_values), None, 4, '8Gi', '10Gi')
        out = self.Svc._apply_plan_spec_overlay(
            values, tpl, self._plan('plan_omnigent_monthly'), existing=True)
        self.assertEqual(out['persistence']['size'], '10Gi', '只擴不縮，且不是殘值')
        self.assertNotIn('resources', out)
        self.assertEqual(out['runner']['resources'], {'limits': {'cpu': '3', 'memory': '7Gi'}})


@tagged('post_install', '-at_install')
class TestOdooMemoryLimits1637(TransactionCase):
    """Odoo 的 limit_memory_* 是每個 worker 的 VMS 上限：交給 chart 的 tenant 預設（2／2.5 GiB）。

    以前 overlay 寫「方案記憶體 × 0.85 ÷ workers」（T1 約 0.85 GiB），比一個新 worker 的 VSZ
    （約 1.1–1.3 GiB）還小。現在寫 None（＝chart 預設），既有服務 Redeploy 也收斂。
    """

    def test_every_odoo_plan_leaves_memory_limits_to_the_chart(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        for xmlid, workers in (('plan_odoo_monthly', 3), ('plan_odoo_t2_monthly', 4),
                               ('plan_odoo_t3_monthly', 5)):
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            with self.subTest(plan=xmlid):
                out = Svc._apply_plan_spec_overlay(json.loads(tpl.helm_default_values), tpl, plan)
                self.assertEqual(out['conf']['workers'], workers)
                self.assertIsNone(out['conf']['limitMemoryHard'])
                self.assertIsNone(out['conf']['limitMemorySoft'])
                self.assertEqual(out['conf']['maxCronThreads'], 1)
                self.assertEqual(out['conf']['dbMaxconn'], 8)

    def test_existing_service_with_the_old_formula_converges(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        values = json.loads(tpl.helm_default_values)
        values['conf'].update({'workers': 2, 'limitMemoryHard': 912680550,
                               'limitMemorySoft': 751619276})
        out = Svc._apply_plan_spec_overlay(
            values, tpl, self.env.ref('%s.plan_odoo_monthly' % MOD), existing=True)
        self.assertIsNone(out['conf']['limitMemoryHard'])
        self.assertIsNone(out['conf']['limitMemorySoft'])
        self.assertEqual(out['conf']['workers'], 3)

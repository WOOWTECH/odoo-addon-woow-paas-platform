# -*- coding: utf-8 -*-
"""#1637（使用者 2026-10-10「依性質跟使用需求按需分配，最大給到 30G」）：每台服務、每個方案的
磁碟（全部 PVC 加總）≤ 30Gi。

這裡鎖：

- **上限**：每個上架中的範本（``is_active``，含暫時下架的 ``is_hidden``）不套方案、以及套上它
  掛的每一個方案之後，chart 會建的 PVC 加總 ≤ 30Gi；所有方案的 ``cs_storage_gb`` ≤ 30。
- **賣的＝給的**：有 ``cs_storage_gb`` 的方案，PVC 加總＝方案值；沒有的，範本 ``min_storage_gb``
  ≥ PVC 加總（ns quota 以 ``min_storage_gb`` 計，HAOS 另含 CDI 匯入峰值）。
- 各範本改後的大小（表 ``EXPECTED``）。
- Odoo 拆分：資料＋PostgreSQL＋備份＝方案總量（20／25／30 ＝ 6＋4＋10／8＋4＋13／10＋4＋16，
  資料：備份＝3：5，容量研究），備份不小於資料；既有服務只擴不縮、PostgreSQL 永遠不動
  （StatefulSet volumeClaimTemplates）。
- EMQX 拆分：資料＝總量 − log。
- migration 18.0.1.0.225：noupdate=1 的方案（含 generator 產的存取席位變體）改成新值，手調過的
  不覆蓋、冪等；seed 與 migration 表同值。

``_VOLUMES`` 是「chart 從哪個 values 鍵決定每顆 PVC 的大小」——用各範本釘的 chart 版本實際
``helm template`` 對過（PR 說明附結果）。換 chart 或新增範本時要跟著補，漏了
``test_every_active_template_is_covered`` 會紅。
"""
import copy
import json
import re

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.models.cloud_service import _plan_storage_total_gib

MOD = 'woow_paas_platform'
CAP_GIB = 30

# slug → 每顆 PVC 一個 tuple：依序找第一個有值的 values 路徑；字串＝chart 預設值（values 都沒有時）。
_VOLUMES = {
    'n8n': ((('main', 'persistence', 'size'), '1Gi'),),
    'odoo': (
        (('persistence', 'size'),),
        (('postgres', 'persistence', 'size'), '8Gi'),
        (('backup', 'persistence', 'size'), ('persistence', 'size')),
    ),
    'mcp-hermes': ((('persistence', 'size'), '1Gi'),),
    'mcp-odoo': ((('persistence', 'size'), '1Gi'),),
    'mcp-odoo-manage': ((('persistence', 'size'), '1Gi'),),
    'mcp-n8n': ((('persistence', 'size'), '1Gi'),),
    'mcp-k8s': ((('persistence', 'size'), '1Gi'),),
    'mcp-vk': ((('persistence', 'size'), '1Gi'),),
    'mcp-opendesign': ((('persistence', 'size'), '1Gi'),),
    'mcp-litellm': ((('persistence', 'size'), '1Gi'),),
    'mcp-nextcloud': ((('persistence', 'size'), '1Gi'),),
    'ha-paas': ((('persistence', 'size'), '5Gi'),),
    'open-design': (
        (('persistence', 'data', 'size'), '8Gi'),
        (('persistence', 'home', 'size'), '2Gi'),
    ),
    'hermes-agent': ((('persistence', 'data', 'size'), '5Gi'),),
    'haos': ((('persistence', 'size'), '64Gi'),),
    'ha-full': ((('persistence', 'size'), '10Gi'),),
    'pi-agent': ((('persistence', 'size'), '20Gi'),),
    'omnigent': ((('persistence', 'size'), '10Gi'),),
    'code-server': (
        (('persistence', 'workspace', 'size'), '20Gi'),
        (('persistence', 'piData', 'size'), '5Gi'),
    ),
    'emqx': (
        (('persistence', 'size'), ('persistence', 'data', 'size'), '5Gi'),
        (('persistence', 'log', 'size'), '2Gi'),
    ),
    'nextcloud': (
        (('persistence', 'size'), ('persistence', 'data', 'size'), '50Gi'),
        (('persistence', 'html', 'size'), '5Gi'),
        (('postgresql', 'persistence', 'size'), '10Gi'),
        (('redis', 'persistence', 'size'), '1Gi'),
    ),
    'headscale': ((('persistence', 'size'), '1Gi'),),
    'tailscale': ((('persistence', 'size'), ('persistence', 'defaultSize'), '1Gi'),),
    'litellm': ((('persistence', 'size'), ('postgresql', 'persistence', 'size'), '10Gi'),),
}

# 改後的大小（Gi）：template＝不套方案；plans＝base 方案 xmlid → 套上後的 PVC 加總。
EXPECTED = {
    'odoo': (20, {'plan_odoo_monthly': 20, 'plan_odoo_t2_monthly': 25, 'plan_odoo_t3_monthly': 30}),
    'mcp-hermes': (1, {'plan_mcp_hermes_monthly': 1, 'plan_mcp_hermes_t2_monthly': 1,
                       'plan_mcp_hermes_t3_monthly': 1}),
    'mcp-odoo': (1, {'plan_mcp_odoo_monthly': 1}),
    'mcp-odoo-manage': (1, {}),
    'mcp-n8n': (1, {'plan_mcp_n8n_monthly': 1}),
    'mcp-k8s': (1, {}),
    'mcp-vk': (1, {}),
    'mcp-opendesign': (1, {'plan_mcp_opendesign_monthly': 1}),
    'mcp-litellm': (1, {'plan_mcp_litellm_monthly': 1}),
    'mcp-nextcloud': (1, {'plan_mcp_nextcloud_monthly': 1}),
    'haos': (30, {'plan_haos_monthly': 30}),
    'ha-full': (10, {'plan_ha_full_monthly': 10}),
    'code-server': (20, {'plan_code_server_monthly': 20}),
    'pi-agent': (30, {'plan_pi_agent_monthly': 30}),
    'n8n': (2, {'plan_n8n_monthly': 5, 'plan_n8n_t2_monthly': 10, 'plan_n8n_t3_monthly': 15,
                'plan_n8n_t4_monthly': 20}),
    'emqx': (3, {'plan_emqx_monthly': 3}),
    'litellm': (5, {'plan_litellm_monthly': 5}),
    'open-design': (10, {'plan_opendesign_monthly': 10}),
    'omnigent': (5, {'plan_omnigent_monthly': 5}),
    'hermes-agent': (5, {'plan_hermes_agent_monthly': 5}),
    'ha-paas': (5, {}),
    'headscale': (1, {'plan_headscale_monthly': 1}),
    'tailscale': (1, {'plan_tailscale_monthly': 1}),
    'nextcloud': (20, {'plan_nextcloud_monthly': 20}),
}

# 改後的 min_storage_gb：除 HAOS（CDI 匯入峰值，見範本註解）外＝沒有方案時的 PVC 加總或 T1。
EXPECTED_MIN_STORAGE = {
    'odoo': 20, 'haos': 34, 'ha-full': 10, 'code-server': 20, 'pi-agent': 30, 'n8n': 5,
    'emqx': 3, 'litellm': 5, 'open-design': 10, 'omnigent': 5, 'hermes-agent': 5, 'ha-paas': 5,
    'headscale': 1, 'tailscale': 1, 'nextcloud': 20,
    'mcp-hermes': 1, 'mcp-odoo': 1, 'mcp-odoo-manage': 1, 'mcp-n8n': 1, 'mcp-k8s': 1,
    'mcp-vk': 1, 'mcp-opendesign': 1, 'mcp-litellm': 1, 'mcp-nextcloud': 1,
}


def _at(values, path):
    for key in path:
        if not isinstance(values, dict):
            return None
        values = values.get(key)
    return values


def _gib(size):
    m = re.fullmatch(r'(\d+)(Gi|Mi)', size)
    assert m, size
    return int(m.group(1)) / (1 if m.group(2) == 'Gi' else 1024)


def _pvcs(slug, values):
    """chart 會建的每顆 PVC 大小（GiB，依 ``_VOLUMES`` 的順序）。"""
    sizes = []
    for alternatives in _VOLUMES[slug]:
        size = None
        for alt in alternatives:
            size = alt if isinstance(alt, str) else _at(values, alt)
            if size:
                break
        assert size, (slug, alternatives)
        sizes.append(_gib(size))
    return sizes


def _odoo_pvcs(values):
    data, pg, backup = _pvcs('odoo', values)
    return {'data': data, 'pg': pg, 'backup': backup}


@tagged('post_install', '-at_install')
class TestTemplateDiskCap1637(TransactionCase):
    """每個範本、每個方案：PVC 加總 ≤ 30Gi，有方案值時＝方案值。"""

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']
        self.Plan = self.env['sh.subscription.plan']

    def _templates(self):
        Tpl = self.env['woow_paas_platform.cloud_app_template'].sudo()
        return Tpl.search([('is_active', '=', True)]).filtered(
            lambda t: t.get_external_id().get(t.id, '').startswith(MOD + '.'))

    def _tpl(self, slug):
        tpl = self.env['woow_paas_platform.cloud_app_template'].sudo().search(
            [('slug', '=', slug)], limit=1)
        self.assertTrue(tpl, slug)
        return tpl

    def _overlay(self, tpl, plan):
        values = json.loads(tpl.helm_default_values or '{}')
        return self.Svc._apply_plan_spec_overlay(values, tpl, plan)

    def test_every_active_template_is_covered(self):
        slugs = set(self._templates().mapped('slug'))
        self.assertEqual(slugs - set(_VOLUMES), set(),
                         '新範本要在 _VOLUMES／EXPECTED 補上它的 PVC 怎麼算')
        self.assertEqual(slugs - set(EXPECTED), set())

    def test_every_template_and_plan_is_at_most_30gib(self):
        for tpl in self._templates():
            values = json.loads(tpl.helm_default_values or '{}')
            total = sum(_pvcs(tpl.slug, values))
            with self.subTest(template=tpl.slug):
                self.assertLessEqual(total, CAP_GIB)
                self.assertGreaterEqual(
                    tpl.min_storage_gb, total,
                    'ns quota 以 min_storage_gb 計，不能小於 PVC 加總')
            for plan in tpl.subscription_plan_ids:
                with self.subTest(template=tpl.slug, plan=plan.name):
                    out = self._overlay(tpl, plan)
                    plan_total = sum(_pvcs(tpl.slug, out))
                    self.assertLessEqual(plan_total, CAP_GIB)
                    if plan.cs_storage_gb:
                        self.assertEqual(plan_total, plan.cs_storage_gb, '賣的＝給的')
                        self.assertEqual(
                            self.Svc._allocated_vals_from(tpl, plan)['allocated_storage_gb'],
                            plan_total)

    def test_no_plan_sells_more_than_30gib(self):
        plans = self.Plan.sudo().with_context(active_test=False).search(
            [('cs_storage_gb', '>', CAP_GIB)])
        self.assertFalse(plans, plans.mapped('name'))

    def test_expected_sizes(self):
        for slug, (template_total, plan_totals) in EXPECTED.items():
            tpl = self._tpl(slug)
            with self.subTest(template=slug):
                values = json.loads(tpl.helm_default_values or '{}')
                self.assertEqual(sum(_pvcs(slug, values)), template_total)
                self.assertEqual(tpl.min_storage_gb, EXPECTED_MIN_STORAGE[slug])
            for xmlid, total in plan_totals.items():
                plan = self.env.ref('%s.%s' % (MOD, xmlid))
                with self.subTest(template=slug, plan=xmlid):
                    self.assertIn(plan, tpl.subscription_plan_ids)
                    self.assertEqual(sum(_pvcs(slug, self._overlay(tpl, plan))), total)
                    yearly = self.env.ref(
                        '%s.%s' % (MOD, xmlid.replace('_monthly', '_yearly')),
                        raise_if_not_found=False)
                    if yearly:
                        self.assertEqual(
                            sum(_pvcs(slug, self._overlay(tpl, yearly))), total, '年繳同規格')

    def test_hermes_mcp_plans_fall_back_to_the_template(self):
        """方案不再帶 cs_*：三階都是範本的 1 vCPU／1 GB／1Gi，overlay 什麼都不寫。"""
        tpl = self._tpl('mcp-hermes')
        base = json.loads(tpl.helm_default_values)
        plans = tpl.subscription_plan_ids
        self.assertEqual(len(plans), 44, '6 個 base＋38 個存取席位變體')
        for plan in plans:
            with self.subTest(plan=plan.name):
                self.assertEqual(
                    (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb, plan.cs_odoo_workers),
                    (0, 0.0, 0, 0))
                self.assertEqual(self._overlay(tpl, plan), base)
                self.assertEqual(
                    self.Svc._allocated_vals_from(tpl, plan),
                    {'allocated_vcpu': 1, 'allocated_ram_gb': 1.0, 'allocated_storage_gb': 1})

    def test_variant_plans_follow_their_base_tier(self):
        """generator 產的 +N 存取席位變體與同 tier base 同規格（odoo／n8n）。"""
        Data = self.env['ir.model.data'].sudo()
        rows = Data.search([('module', '=', MOD), ('model', '=', 'sh.subscription.plan'),
                            ('name', '=like', 'plan_%_ac%')])
        checked = 0
        for row in rows:
            m = re.fullmatch(r'(plan_(?:odoo|n8n)(?:_t\d)?)_ac\d+_(monthly|yearly)', row.name)
            if not m:
                continue
            variant = self.Plan.browse(row.res_id)
            base = self.env.ref('%s.%s_%s' % (MOD, m.group(1), m.group(2)))
            with self.subTest(variant=row.name):
                self.assertEqual(
                    (variant.cs_vcpu, variant.cs_ram_gb, variant.cs_storage_gb,
                     variant.cs_odoo_workers),
                    (base.cs_vcpu, base.cs_ram_gb, base.cs_storage_gb, base.cs_odoo_workers))
            checked += 1
        self.assertEqual(checked, 82, 'odoo 42＋n8n 40 個變體')

    def test_descriptions_match_the_new_sizes(self):
        for slug, now, before in (('haos', '30 GiB 磁碟（約 32.2 GB', '64 GB'),
                                  ('ha-full', '10 GB 磁碟', '20 GB'),
                                  ('code-server', '20 GB 磁碟', '25 GB'),
                                  ('emqx', '3 GB 磁碟', '12 GB'),
                                  ('litellm', '5 GB 磁碟', '10 GB 磁碟'),
                                  ('omnigent', '5 GB 磁碟', '10 GB 磁碟'),
                                  ('mcp-litellm', '1 GB 磁碟', '5 GB 磁碟'),
                                  ('mcp-nextcloud', '1 GB 磁碟', '5 GB 磁碟')):
            tpl = self._tpl(slug)
            with self.subTest(template=slug):
                self.assertIn(now, tpl.description)
                self.assertNotIn(before, tpl.description)
                self.assertNotIn(before, tpl.full_description or '')
        self.assertIn('30 GiB（約 32.2 GB）專屬磁碟', self._tpl('haos').full_description)
        self.assertIn('服務附 10 GB 資料磁碟', self._tpl('ha-full').full_description)
        self.assertIn('工作區 16 GB＋AI 助手資料 4 GB', self._tpl('code-server').full_description)
        self.assertIn('Supervisor', self._tpl('haos').full_description)
        self.assertIn('備份請存到外部', self._tpl('haos').description)


@tagged('post_install', '-at_install')
class TestOdooStorageSplit1637(TransactionCase):
    """Odoo：資料＋PostgreSQL＋備份＝方案總量；PostgreSQL 固定、資料：備份＝3：5。"""

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']
        self.tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        self.base = json.loads(self.tpl.helm_default_values)

    def _plan(self, xmlid):
        return self.env.ref('%s.%s' % (MOD, xmlid))

    def _test_plan(self, storage):
        return self.env['sh.subscription.plan'].sudo().create({
            'name': '1637 odoo %s' % storage, 'sh_plan_price': 1.0,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'cs_vcpu': 2, 'cs_ram_gb': 2.0, 'cs_storage_gb': storage, 'cs_odoo_workers': 2,
        })

    def _overlay(self, plan, values=None, existing=False):
        values = copy.deepcopy(self.base if values is None else values)
        return self.Svc._apply_plan_spec_overlay(values, self.tpl, plan, existing=existing)

    def test_template_is_the_t1_split(self):
        self.assertEqual(_odoo_pvcs(self.base), {'data': 6, 'pg': 4, 'backup': 10})
        self.assertEqual(self.base['postgres']['database'], 'odoo', '原本的鍵不能被蓋掉')

    def test_new_services_split_the_plan_total(self):
        for xmlid, split in (('plan_odoo_monthly', {'data': 6, 'pg': 4, 'backup': 10}),
                             ('plan_odoo_yearly', {'data': 6, 'pg': 4, 'backup': 10}),
                             ('plan_odoo_t2_monthly', {'data': 8, 'pg': 4, 'backup': 13}),
                             ('plan_odoo_t3_monthly', {'data': 10, 'pg': 4, 'backup': 16})):
            with self.subTest(plan=xmlid):
                plan = self._plan(xmlid)
                out = self._overlay(plan)
                self.assertEqual(_odoo_pvcs(out), split)
                self.assertEqual(sum(split.values()), plan.cs_storage_gb)
                self.assertEqual(out['postgres']['persistence']['size'], '4Gi')

    def test_backup_is_never_smaller_than_data(self):
        for storage in range(7, 31):
            with self.subTest(storage=storage):
                pvcs = _odoo_pvcs(self._overlay(self._test_plan(storage)))
                self.assertGreaterEqual(pvcs['backup'], pvcs['data'])
                self.assertEqual(sum(pvcs.values()), storage)

    def test_plan_too_small_or_postgres_unknown_fails_loud(self):
        with self.assertRaises(ValueError):
            self._overlay(self._test_plan(5))
        values = copy.deepcopy(self.base)
        del values['postgres']['persistence']
        with self.assertRaises(ValueError):
            self._overlay(self._plan('plan_odoo_monthly'), values)

    def test_existing_upgrade_grows_data_and_backup_but_never_postgres(self):
        created = self._overlay(self._plan('plan_odoo_monthly'))
        up = self._overlay(self._plan('plan_odoo_t3_monthly'), created, existing=True)
        self.assertEqual(_odoo_pvcs(up), {'data': 10, 'pg': 4, 'backup': 16})
        down = self._overlay(self._plan('plan_odoo_monthly'), up, existing=True)
        self.assertEqual(_odoo_pvcs(down), {'data': 10, 'pg': 4, 'backup': 16}, '只擴不縮')
        same = self._overlay(self._plan('plan_odoo_t2_monthly'), created, existing=True)
        self.assertEqual(_odoo_pvcs(same), {'data': 8, 'pg': 4, 'backup': 13})

    def test_existing_service_on_its_plan_is_a_no_op(self):
        for xmlid in ('plan_odoo_monthly', 'plan_odoo_t2_monthly', 'plan_odoo_t3_monthly'):
            with self.subTest(plan=xmlid):
                plan = self._plan(xmlid)
                created = self._overlay(plan)
                self.assertEqual(self._overlay(plan, created, existing=True), created)

    def test_legacy_service_is_left_alone(self):
        """#1637 以前開通的 Odoo：values 沒有 postgres.persistence.size（chart 預設 8Gi）、備份跟著
        資料（backup.persistence.size 空）。整台 48Gi 已大於任何新方案 ⇒ 一顆都不能動。"""
        legacy = copy.deepcopy(self.base)
        legacy['persistence'] = {'size': '20Gi'}
        del legacy['postgres']['persistence']
        del legacy['backup']
        for xmlid in ('plan_odoo_monthly', 'plan_odoo_t3_monthly'):
            with self.subTest(plan=xmlid):
                with mute_logger('odoo.addons.woow_paas_platform.models.cloud_service'):
                    out = self._overlay(self._plan(xmlid), legacy, existing=True)
                self.assertEqual(out['persistence'], {'size': '20Gi'})
                self.assertNotIn('persistence', out['postgres'])
                self.assertNotIn('backup', out)

    def test_existing_backup_falls_back_to_data_size(self):
        """備份值空＝chart 用資料的大小；只擴不縮要拿那個比，不能當成「沒有現值」。"""
        values = copy.deepcopy(self.base)
        values['persistence']['size'] = '13Gi'
        values['backup']['persistence']['size'] = ''
        out = self._overlay(self._plan('plan_odoo_monthly'), values, existing=True)
        self.assertEqual(_odoo_pvcs(out), {'data': 13, 'pg': 4, 'backup': 13})

    def test_allocated_storage_is_the_total(self):
        for xmlid, total in (('plan_odoo_monthly', 20), ('plan_odoo_t2_monthly', 25),
                             ('plan_odoo_t3_monthly', 30)):
            with self.subTest(plan=xmlid):
                plan = self._plan(xmlid)
                self.assertEqual(
                    self.Svc._allocated_vals_from(self.tpl, plan)['allocated_storage_gb'], total)
                self.assertEqual(_plan_storage_total_gib(self._overlay(plan), self.tpl), total)


@tagged('post_install', '-at_install')
class TestEmqxStorageSplit1637(TransactionCase):
    """EMQX：方案總量 3 ＝ 資料 2 ＋ log 1；既有 10＋2 的服務不縮。"""

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']
        self.tpl = self.env.ref('%s.cloud_app_emqx' % MOD)
        self.base = json.loads(self.tpl.helm_default_values)
        self.plan = self.env.ref('%s.plan_emqx_monthly' % MOD)

    def test_new_service(self):
        out = self.Svc._apply_plan_spec_overlay(copy.deepcopy(self.base), self.tpl, self.plan)
        self.assertEqual(out['persistence']['size'], '2Gi')
        self.assertEqual(_pvcs('emqx', out), [2, 1])

    def test_existing_service_never_shrinks(self):
        legacy = copy.deepcopy(self.base)
        legacy['persistence'].update({'size': '10Gi', 'data': {'size': '10Gi'},
                                      'log': {'size': '2Gi'}})
        out = self.Svc._apply_plan_spec_overlay(legacy, self.tpl, self.plan, existing=True)
        self.assertEqual(_pvcs('emqx', out), [10, 2])


@tagged('post_install', '-at_install')
class TestDiskMigration1637(TransactionCase):
    """migration 18.0.1.0.225：noupdate=1 的方案改新值（磁碟＋容量研究的 RAM／workers）；手調過的不覆蓋；冪等。"""

    OLD = {
        'plan_odoo_monthly': {'cs_ram_gb': 2.0, 'cs_odoo_workers': 2},
        'plan_odoo_ac2_yearly': {'cs_ram_gb': 2.0, 'cs_odoo_workers': 2},
        'plan_odoo_t2_monthly': {'cs_storage_gb': 30, 'cs_odoo_workers': 3},
        'plan_odoo_t3_ac4_yearly': {'cs_storage_gb': 40, 'cs_odoo_workers': 4},
        'plan_n8n_monthly': {'cs_storage_gb': 10, 'cs_ram_gb': 1.0},
        'plan_n8n_t4_ac10_monthly': {'cs_storage_gb': 40},
        'plan_mcp_hermes_t2_yearly': {'cs_vcpu': 2, 'cs_ram_gb': 2.0, 'cs_storage_gb': 30},
        'plan_mcp_hermes_t3_ac10_monthly': {'cs_vcpu': 2, 'cs_ram_gb': 4.0, 'cs_storage_gb': 40},
        'plan_code_server_yearly': {'cs_storage_gb': 25, 'cs_ram_gb': 4.0},
        'plan_pi_agent_monthly': {'cs_storage_gb': 20, 'cs_ram_gb': 4.0},
        'plan_emqx_monthly': {'cs_storage_gb': 10},
        'plan_litellm_yearly': {'cs_storage_gb': 10, 'cs_ram_gb': 2.0},
        'plan_omnigent_monthly': {'cs_storage_gb': 10},
    }

    def setUp(self):
        super().setUp()
        self.Plan = self.env['sh.subscription.plan']

    def _plan(self, xmlid):
        return self.env.ref('%s.%s' % (MOD, xmlid))

    def _seed_values(self):
        return {xmlid: {f: self._plan(xmlid)[f] for f in old}
                for xmlid, old in self.OLD.items()}

    def test_migration_moves_old_plans_to_the_seed_values(self):
        seed = self._seed_values()
        prices = {x: self._plan(x).sh_plan_price for x in self.OLD}
        for xmlid, old in self.OLD.items():
            self._plan(xmlid).write(old)
        changed = self.Plan._woow_migrate_spec_1637()
        self.assertEqual(self._seed_values(), seed, 'migration 的結果＝新裝的 seed')
        self.assertGreaterEqual(len(changed), len(self.OLD))
        for xmlid, price in prices.items():
            self.assertEqual(self._plan(xmlid).sh_plan_price, price, '價格不變')

    def test_every_rule_has_a_matching_seed_record(self):
        """表裡每條規則至少對到一筆 seed 方案，且 seed 已經是新值（新裝＝升級後）。"""
        Data = self.env['ir.model.data'].sudo()
        rows = Data.search([('module', '=', MOD), ('model', '=', 'sh.subscription.plan')])
        for pattern, rule in self.Plan._SPEC_1637:
            matched = rows.filtered(lambda r: re.fullmatch(pattern, r.name))
            with self.subTest(rule=pattern):
                self.assertTrue(matched)
                for row in matched:
                    plan = self.Plan.browse(row.res_id)
                    self.assertEqual({f: plan[f] for f in rule},
                                     {f: new for f, (_old, new) in rule.items()}, row.name)

    def test_hand_tuned_plans_are_kept_and_it_is_idempotent(self):
        self._plan('plan_n8n_t2_monthly').write({'cs_storage_gb': 25})
        self._plan('plan_mcp_hermes_monthly').write(
            {'cs_vcpu': 1, 'cs_ram_gb': 2.0, 'cs_storage_gb': 5})
        self._plan('plan_odoo_t3_monthly').write({'cs_storage_gb': 40, 'cs_odoo_workers': 4})
        with mute_logger('odoo.addons.woow_paas_platform.models.sh_subscription_plan_cs_spec'):
            self.Plan._woow_migrate_spec_1637()
            again = self.Plan._woow_migrate_spec_1637()
        self.assertEqual(self._plan('plan_n8n_t2_monthly').cs_storage_gb, 25, '手調過的不覆蓋')
        self.assertEqual(self._plan('plan_mcp_hermes_monthly').cs_storage_gb, 5,
                         '任一欄手調過就整筆不動')
        self.assertEqual(self._plan('plan_mcp_hermes_monthly').cs_vcpu, 1)
        self.assertEqual(self._plan('plan_odoo_t3_monthly').cs_storage_gb, 30)
        self.assertFalse(again, '第二次沒有東西要改')

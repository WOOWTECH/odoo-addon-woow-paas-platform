# -*- coding: utf-8 -*-
"""Omnigent BYO host：部署時注入同工作區 Omnigent 的叢集內網址。

code-server／pi-agent／open-design 的 chart 帶 omnigent-host sidecar（woow-paas-charts
``shared/omnigent-host`` 共用套件），標準 values 區塊是 ``omnigentHost.serverUrl``。
平台在 ``_assemble_deploy_values`` 裡把「同工作區、running、最早建立的 Omnigent」
的叢集內網址填進去（和 ``_inject_allowed_origins`` 同一 family）：

- 只有網址，絕不帶 token／密碼（accounts 模式由租戶自己 ``omnigent login``）
- 工作區沒有 running Omnigent ⇒ no-op（chart 預設 ""）
- 多個 ⇒ 決定性地取最早建立的，記 log
- Service 名鏡射 charts/omnigent ``omnigent.fullname``；port＝``service.port``（預設 8000）
"""
import json
import logging

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import (
    _OMNIGENT_CHART_NAME,
    _OMNIGENT_DEFAULT_SERVICE_PORT,
    _OMNIGENT_SERVER_URL_VALUE_PATHS,
    _OMNIGENT_TEMPLATE_SLUG,
    _internal_provision,
)

_NS = 'paas-ws-omni0001'


def _url(release_service, namespace=_NS, port=8000):
    return 'http://%s.%s.svc.cluster.local:%d' % (release_service, namespace, port)


@tagged('post_install', '-at_install')
class TestInjectOmnigentServerUrl(TransactionCase):
    """static injector：只對帶 omnigent-host sidecar 的 chart 寫 omnigentHost.serverUrl。"""

    URL = 'http://svc-a-omnigent.paas-ws-x.svc.cluster.local:8000'

    def setUp(self):
        super().setUp()
        self.CloudService = self.env['woow_paas_platform.cloud_service']

    def test_sets_server_url_for_host_charts(self):
        for slug in ('code-server', 'pi-agent', 'open-design'):
            out = self.CloudService._inject_omnigent_server_url({}, slug, self.URL)
            self.assertEqual(out, {'omnigentHost': {'serverUrl': self.URL}}, slug)

    def test_noop_for_other_slugs(self):
        """Omnigent 自己、hermes-agent（open-design 派生但沒有 sidecar）、n8n 都不得長出鍵。"""
        for slug in ('omnigent', 'hermes-agent', 'n8n', 'odoo'):
            out = self.CloudService._inject_omnigent_server_url(
                {'existing': 1}, slug, self.URL)
            self.assertEqual(out, {'existing': 1}, slug)

    def test_noop_for_empty_url(self):
        out = self.CloudService._inject_omnigent_server_url({}, 'code-server', '')
        self.assertEqual(out, {})

    def test_preserves_other_omnigent_host_keys(self):
        out = self.CloudService._inject_omnigent_server_url(
            {'omnigentHost': {'enabled': True, 'hostName': 'x', 'serverUrl': ''}},
            'pi-agent', self.URL)
        self.assertEqual(out['omnigentHost'], {
            'enabled': True, 'hostName': 'x', 'serverUrl': self.URL})

    def test_null_block_does_not_crash(self):
        """``omnigentHost: null`` 不能讓 setdefault 拋 TypeError 打掛整個部署。"""
        out = self.CloudService._inject_omnigent_server_url(
            {'omnigentHost': None}, 'code-server', self.URL)
        self.assertEqual(out['omnigentHost'], {'serverUrl': self.URL})


@tagged('post_install', '-at_install')
class TestWorkspaceOmnigentUrlInjection(TransactionCase):
    """model 層：_assemble_deploy_values 依工作區內的 Omnigent 決定注入什麼。"""

    def setUp(self):
        super().setUp()
        self.Service = _internal_provision(self.env['woow_paas_platform.cloud_service'])
        Template = self.env['woow_paas_platform.cloud_app_template']
        Workspace = self.env['woow_paas_platform.workspace']
        self.ws = Workspace.create({'name': 'Omnigent URL WS'})
        self.other_ws = Workspace.create({'name': 'Omnigent URL other WS'})

        def tpl(slug):
            return Template.create({
                'name': 'T %s' % slug, 'slug': slug,
                'helm_repo_url': 'https://charts.example.com',
                'helm_chart_name': slug, 'helm_chart_version': '1.0.0',
                'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            })

        self.tpl_omnigent = tpl('omnigent')
        self.tpl_cs = tpl('code-server')
        self.tpl_pi = tpl('pi-agent')
        self.tpl_od = tpl('open-design')
        self.tpl_n8n = tpl('n8n')

        self.cs = self._svc(self.tpl_cs, 'svc-cs000001', helm_values={
            'omnigentHost': {'enabled': True, 'hostName': 'code-server · x'},
        })

    # ---- helpers ------------------------------------------------------

    def _svc(self, template, release, ws=None, namespace=_NS, state='running',
             helm_values=None):
        svc = self.Service.create({
            'name': release,
            'workspace_id': (ws or self.ws).id,
            'template_id': template.id,
            'helm_release_name': release,
            'helm_namespace': namespace,
            'helm_values': json.dumps(helm_values or {}),
        })
        svc.write({'state': state})
        return svc

    def _omni(self, release='svc-om000001', **kw):
        return self._svc(self.tpl_omnigent, release, **kw)

    def _set_create_date(self, svc, ts):
        self.env.cr.execute(
            'UPDATE woow_paas_platform_cloud_service SET create_date=%s WHERE id=%s',
            (ts, svc.id))
        svc.invalidate_recordset(['create_date'])

    def _injected(self, svc):
        return (svc._assemble_deploy_values()['values']
                .get('omnigentHost', {}).get('serverUrl'))

    # ---- happy path -----------------------------------------------------

    def test_injects_in_cluster_url_for_all_host_charts(self):
        self._omni()
        pi = self._svc(self.tpl_pi, 'svc-pi000001')
        od = self._svc(self.tpl_od, 'svc-od000001')
        expected = _url('svc-om000001-omnigent')
        for svc in (self.cs, pi, od):
            self.assertEqual(self._injected(svc), expected, svc.template_id.slug)

    def test_keeps_other_omnigent_host_values_and_never_adds_secrets(self):
        """只多一個 serverUrl；網址裡沒有 userinfo（user:token@）。"""
        self._omni()
        block = self.cs._assemble_deploy_values()['values']['omnigentHost']
        self.assertEqual(block, {
            'enabled': True,
            'hostName': 'code-server · x',
            'serverUrl': _url('svc-om000001-omnigent'),
        })
        self.assertNotIn('@', block['serverUrl'])

    def test_injected_on_deploy_while_service_is_pending(self):
        """fresh install 時本筆還是 pending/deploying——看的是 Omnigent 的狀態，不是本筆的。"""
        self._omni()
        cs = self._svc(self.tpl_cs, 'svc-cs000002', state='deploying')
        self.assertEqual(self._injected(cs), _url('svc-om000001-omnigent'))

    def test_assemble_stays_readonly(self):
        omni = self._omni()
        before = (self.cs.helm_values, omni.helm_values)
        self.cs._assemble_deploy_values()
        self.assertEqual((self.cs.helm_values, omni.helm_values), before)

    # ---- no-op ----------------------------------------------------------

    def test_noop_without_omnigent(self):
        self.assertIsNone(self._injected(self.cs))

    def test_noop_when_omnigent_not_running(self):
        omni = self._omni()
        for state in ('pending', 'deploying', 'initializing', 'error',
                      'upgrading', 'suspended', 'deleting'):
            omni.write({'state': state})
            self.assertIsNone(self._injected(self.cs), state)

    def test_noop_for_omnigent_in_other_workspace(self):
        self._omni(ws=self.other_ws, namespace='paas-ws-other001')
        self.assertIsNone(self._injected(self.cs))

    def test_noop_for_omnigent_in_other_namespace(self):
        """baseline NetworkPolicy 只放行同 namespace；跨 namespace 的網址給了也連不上。"""
        self._omni(namespace='paas-ws-elsewher')
        self.assertIsNone(self._injected(self.cs))

    def test_noop_for_chart_without_sidecar(self):
        self._omni()
        n8n = self._svc(self.tpl_n8n, 'svc-n8n00001')
        self.assertNotIn('omnigentHost', n8n._assemble_deploy_values()['values'])

    def test_omnigent_itself_gets_nothing(self):
        omni = self._omni()
        self._omni(release='svc-om000002')
        self.assertNotIn('omnigentHost', omni._assemble_deploy_values()['values'])

    # ---- several Omnigents ------------------------------------------------

    def test_several_running_picks_oldest_and_logs(self):
        """create_date 優先於 id：後建（id 較大）但 create_date 較早的那個勝出。"""
        newer = self._omni(release='svc-omnewer1')
        older = self._omni(release='svc-omolder1')
        self._set_create_date(newer, '2026-09-30 12:00:00')
        self._set_create_date(older, '2026-09-01 12:00:00')
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.cloud_service',
                level=logging.INFO) as logs:
            got = self._injected(self.cs)
        self.assertEqual(got, _url('svc-omolder1-omnigent'))
        self.assertTrue(
            any('2 running Omnigent' in line and str(older.id) in line
                for line in logs.output),
            logs.output)

    def test_same_create_date_tiebreaks_on_id(self):
        first = self._omni(release='svc-omfirst1')
        second = self._omni(release='svc-omsecnd1')
        for svc in (first, second):
            self._set_create_date(svc, '2026-09-15 08:00:00')
        self.assertEqual(self._injected(self.cs), _url('svc-omfirst1-omnigent'))

    def test_falls_back_when_oldest_has_broken_values(self):
        broken = self._omni(release='svc-ombroke1')
        good = self._omni(release='svc-omgood01')
        self._set_create_date(broken, '2026-09-01 00:00:00')
        self._set_create_date(good, '2026-09-02 00:00:00')
        self.env.cr.execute(
            "UPDATE woow_paas_platform_cloud_service SET helm_values='{not json' "
            "WHERE id=%s", (broken.id,))
        broken.invalidate_recordset(['helm_values'])
        self.assertEqual(self._injected(self.cs), _url('svc-omgood01-omnigent'))

    # ---- Service name / port mirror charts/omnigent _helpers.tpl -----------

    def test_release_name_containing_chart_name_is_used_verbatim(self):
        self._omni(release='my-omnigent')
        self.assertEqual(self._injected(self.cs), _url('my-omnigent'))

    def test_fullname_override_and_name_override(self):
        omni = self._omni(helm_values={'fullnameOverride': 'og-entry'})
        self.assertEqual(self._injected(self.cs), _url('og-entry'))
        omni.write({'helm_values': json.dumps({'nameOverride': 'og'})})
        self.assertEqual(self._injected(self.cs), _url('svc-om000001-og'))

    def test_service_port_override(self):
        self._omni(helm_values={'service': {'port': 18000}})
        self.assertEqual(
            self._injected(self.cs), _url('svc-om000001-omnigent', port=18000))

    def test_invalid_port_skips_injection(self):
        self._omni(helm_values={'service': {'port': '8000; rm -rf /'}})
        self.assertIsNone(self._injected(self.cs))

    def test_long_name_truncated_like_helm(self):
        """trunc 63 後結尾是 '-' 時 trimSuffix 去掉一個。"""
        release = 'r' * 62
        self._omni(release=release)
        # '<62 r>-omnigent'[:63] == '<62 r>-' → trimSuffix → '<62 r>'
        self.assertEqual(self._injected(self.cs), _url(release))

    def test_non_dns_label_name_skips_injection(self):
        self._omni(helm_values={'fullnameOverride': 'Bad_Name'})
        self.assertIsNone(self._injected(self.cs))

    # ---- seed lockstep ----------------------------------------------------

    def test_constants_match_seed(self):
        """表裡的 slug 都是真的上架 template；Omnigent 的 slug／chart 名／port 與 seed 一致。"""
        Template = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False)
        for slug in _OMNIGENT_SERVER_URL_VALUE_PATHS:
            self.assertTrue(
                Template.search([('slug', '=', slug)], limit=1), slug)
        omni_tpl = self.env.ref('woow_paas_platform.cloud_app_omnigent')
        self.assertEqual(omni_tpl.slug, _OMNIGENT_TEMPLATE_SLUG)
        self.assertEqual(
            omni_tpl.helm_chart_name.rsplit('/', 1)[-1], _OMNIGENT_CHART_NAME)
        self.assertEqual(omni_tpl.default_port, _OMNIGENT_DEFAULT_SERVICE_PORT)

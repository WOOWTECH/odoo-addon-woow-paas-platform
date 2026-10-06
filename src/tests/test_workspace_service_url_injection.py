# -*- coding: utf-8 -*-
"""同工作區服務網址注入（PLAN §10.2）：Headscale → tailscale、LiteLLM → 四個 agent／IDE＋mcp-litellm。

#1340 的 Omnigent 網址注入泛化成 ``_workspace_provider_url(provider_slug)``＋
``_WORKSPACE_URL_INJECTORS``（Omnigent 自己的行為由 ``test_omnigent_server_url_injection``
守線、不變）。本檔釘新加的兩組：

- tailscale（chart ≥ 0.1.2）的 ``tailscale.workspaceHeadscaleUrl``＝同工作區 running 的
  Headscale：``http://<release>-headscale.<ns>.svc.cluster.local:8080``（入口 Service 的第二個
  埠 ``headscale.port``；第一個埠 3000 是 Headplane）
- pi-agent／code-server／open-design／hermes-agent 的 ``llmGateway.baseUrl``＝同工作區
  running 的 LiteLLM：``http://<release>-litellm.<ns>.svc.cluster.local:4000/v1``；
  mcp-litellm（``cloud_app_mcp_litellm``，mcp-server chart）的 ``config.nonSensitive.connection.base_url``
  同一台但**不帶** ``/v1``

共同規則：只帶網址（絕不帶 preauth key／virtual key／master key）；沒有提供者、非 running、
跨工作區／跨 namespace ⇒ 不寫鍵；多台取最早建立；helm_values 已有非空值 ⇒ 尊重；組裝唯讀；
reset 路徑與 deploy 同一條組裝，前後只差注入的那條路徑（chart 端把它放在不入 pod template
的 runtime ConfigMap，所以 15 秒的 reset 不會等新 pod）。

另含 ``_heal_tailscale_control_plane``：chart 0.1.2 之前填了 loginServer 的 VPN 節點補上
``controlPlane=custom``，設定頁才不會把它顯示成預設的「官方 Tailscale」。
"""
import json
import logging
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import (
    _LITELLM_BASE_URL_SUFFIXES,
    _LITELLM_BASE_URL_VALUE_PATHS,
    _WORKSPACE_HEADSCALE_URL_VALUE_PATHS,
    _WORKSPACE_PROVIDERS,
    _WORKSPACE_URL_INJECTORS,
    _internal_provision,
)

_NS = 'paas-ws-wire0001'
_LOGGER = 'odoo.addons.woow_paas_platform.models.cloud_service'
_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
LLM_CONSUMERS = ('pi-agent', 'code-server', 'open-design', 'hermes-agent')


def _hs_url(service, namespace=_NS, port=8080):
    return 'http://%s.%s.svc.cluster.local:%d' % (service, namespace, port)


def _ll_url(service, namespace=_NS, port=4000):
    return 'http://%s.%s.svc.cluster.local:%d' % (service, namespace, port)


def _get(values, path):
    for key in path:
        values = values.get(key) if isinstance(values, dict) else None
    return values


def _flatten(values, prefix=()):
    out = {}
    for key, value in (values or {}).items():
        if isinstance(value, dict) and value:
            out.update(_flatten(value, prefix + (key,)))
        else:
            out[prefix + (key,)] = value
    return out


@tagged('post_install', '-at_install')
class TestInjectWorkspaceServiceUrl(TransactionCase):
    """static injector：依 _WORKSPACE_URL_INJECTORS 寫對的 value path、帶對的後綴。"""

    HS = 'http://svc-a-headscale.paas-ws-x.svc.cluster.local:8080'
    LL = 'http://svc-b-litellm.paas-ws-x.svc.cluster.local:4000'

    def setUp(self):
        super().setUp()
        self.CloudService = self.env['woow_paas_platform.cloud_service']

    def _inject(self, values, slug, provider, url):
        return self.CloudService._inject_workspace_service_url(values, slug, provider, url)

    def test_headscale_url_goes_to_tailscale_only(self):
        self.assertEqual(
            self._inject({}, 'tailscale', 'headscale', self.HS),
            {'tailscale': {'workspaceHeadscaleUrl': self.HS}})
        for slug in ('headscale', 'pi-agent', 'code-server', 'litellm', 'omnigent', 'n8n'):
            self.assertEqual(
                self._inject({'x': 1}, slug, 'headscale', self.HS), {'x': 1}, slug)

    def test_litellm_url_with_v1_for_llm_gateway_consumers(self):
        for slug in LLM_CONSUMERS:
            self.assertEqual(
                self._inject({}, slug, 'litellm', self.LL),
                {'llmGateway': {'baseUrl': self.LL + '/v1'}}, slug)

    def test_litellm_url_without_v1_for_mcp_litellm(self):
        """mcp-litellm 打的是 LiteLLM 管理 API 的根（/model/info…），不是 OpenAI 相容的 /v1。"""
        out = self._inject(
            {'config': {'nonSensitive': {'mcp_server': {'port': 8000}}}},
            'mcp-litellm', 'litellm', self.LL)
        self.assertEqual(out['config']['nonSensitive'], {
            'mcp_server': {'port': 8000},
            'connection': {'base_url': self.LL},
        })

    def test_litellm_url_not_for_other_slugs(self):
        for slug in ('tailscale', 'litellm', 'headscale', 'omnigent', 'n8n', 'odoo'):
            self.assertEqual(
                self._inject({'x': 1}, slug, 'litellm', self.LL), {'x': 1}, slug)

    def test_noop_for_empty_url_or_unknown_provider(self):
        self.assertEqual(self._inject({}, 'tailscale', 'headscale', ''), {})
        self.assertEqual(self._inject({}, 'tailscale', 'nope', self.HS), {})

    def test_explicit_value_is_not_overwritten(self):
        explicit = 'https://headscale.example.com'
        out = self._inject(
            {'tailscale': {'workspaceHeadscaleUrl': explicit}}, 'tailscale', 'headscale', self.HS)
        self.assertEqual(out['tailscale']['workspaceHeadscaleUrl'], explicit)
        out = self._inject(
            {'llmGateway': {'baseUrl': 'https://llm.example.com/v1'}}, 'pi-agent', 'litellm',
            self.LL)
        self.assertEqual(out['llmGateway']['baseUrl'], 'https://llm.example.com/v1')

    def test_blank_counts_as_unset_and_null_block_does_not_crash(self):
        for blank in ('', '  ', None):
            out = self._inject(
                {'tailscale': {'workspaceHeadscaleUrl': blank, 'controlPlane': 'workspace'}},
                'tailscale', 'headscale', self.HS)
            self.assertEqual(out['tailscale'], {
                'workspaceHeadscaleUrl': self.HS, 'controlPlane': 'workspace'}, repr(blank))
        self.assertEqual(
            self._inject({'llmGateway': None}, 'hermes-agent', 'litellm', self.LL),
            {'llmGateway': {'baseUrl': self.LL + '/v1'}})
        self.assertEqual(
            self._inject({'config': {'nonSensitive': None}}, 'mcp-litellm', 'litellm', self.LL),
            {'config': {'nonSensitive': {'connection': {'base_url': self.LL}}}})

    def test_tables_are_consistent(self):
        """每個 LiteLLM 消費端都有後綴（/v1 或 ''）；每個注入器的提供者都在 _WORKSPACE_PROVIDERS。"""
        self.assertEqual(set(_LITELLM_BASE_URL_SUFFIXES), set(_LITELLM_BASE_URL_VALUE_PATHS))
        self.assertTrue(set(_LITELLM_BASE_URL_SUFFIXES.values()) <= {'/v1', ''})
        self.assertEqual(_LITELLM_BASE_URL_SUFFIXES['mcp-litellm'], '')
        for slug in LLM_CONSUMERS:
            self.assertEqual(_LITELLM_BASE_URL_SUFFIXES[slug], '/v1', slug)
        for table, provider, suffixes in _WORKSPACE_URL_INJECTORS:
            self.assertIn(provider, _WORKSPACE_PROVIDERS)
            self.assertTrue(set(suffixes) <= set(table), provider)
            self.assertNotIn(provider, table, '提供者不會注入自己')
        self.assertEqual(_WORKSPACE_HEADSCALE_URL_VALUE_PATHS,
                         {'tailscale': [('tailscale', 'workspaceHeadscaleUrl')]})


@tagged('post_install', '-at_install')
class TestWorkspaceServiceUrlInjection(TransactionCase):
    """model 層：_assemble_deploy_values 依工作區內的 Headscale／LiteLLM 決定注入什麼。"""

    def setUp(self):
        super().setUp()
        self.Service = _internal_provision(self.env['woow_paas_platform.cloud_service'])
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        Workspace = self.env['woow_paas_platform.workspace']
        self.ws = Workspace.create({'name': 'Wire URL WS'})
        self.other_ws = Workspace.create({'name': 'Wire URL other WS'})
        self.tpl = {
            slug: self._tpl(slug) for slug in (
                'headscale', 'litellm', 'omnigent', 'tailscale', 'mcp-litellm', 'n8n',
            ) + LLM_CONSUMERS
        }
        self.ts = self._svc('tailscale', 'svc-ts000001', helm_values={
            'tailscale': {'controlPlane': 'workspace', 'authKey': 'hskey-auth-tenant'},
        })

    # ---- helpers ------------------------------------------------------

    def _tpl(self, slug, **extra):
        vals = {
            'name': 'T %s' % slug, 'slug': slug,
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': slug, 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        }
        vals.update(extra)
        return self.Template.create(vals)

    def _svc(self, slug, release, ws=None, namespace=_NS, state='running', helm_values=None,
             template=None):
        svc = self.Service.create({
            'name': release,
            'workspace_id': (ws or self.ws).id,
            'template_id': (template or self.tpl[slug]).id,
            'helm_release_name': release,
            'helm_namespace': namespace,
            'helm_values': json.dumps(helm_values or {}),
        })
        svc.write({'state': state})
        return svc

    def _headscale(self, release='svc-hs000001', **kw):
        return self._svc('headscale', release, **kw)

    def _litellm(self, release='svc-ll000001', **kw):
        return self._svc('litellm', release, **kw)

    def _set_create_date(self, svc, ts):
        self.env.cr.execute(
            'UPDATE woow_paas_platform_cloud_service SET create_date=%s WHERE id=%s',
            (ts, svc.id))
        svc.invalidate_recordset(['create_date'])

    def _values(self, svc):
        return svc._assemble_deploy_values()['values']

    def _hs_injected(self, svc=None):
        return _get(self._values(svc or self.ts), ('tailscale', 'workspaceHeadscaleUrl'))

    def _ll_injected(self, svc):
        return _get(self._values(svc), ('llmGateway', 'baseUrl'))

    # ---- happy path -----------------------------------------------------

    def test_tailscale_gets_the_headscale_control_url(self):
        self._headscale()
        values = self._values(self.ts)
        self.assertEqual(values['tailscale'], {
            'controlPlane': 'workspace',
            'authKey': 'hskey-auth-tenant',
            'workspaceHeadscaleUrl': _hs_url('svc-hs000001-headscale'),
        }, '只多一個網址；租戶的 controlPlane／authKey 原樣')
        self.assertNotIn('@', values['tailscale']['workspaceHeadscaleUrl'])

    def test_injected_regardless_of_control_plane(self):
        """controlPlane 是 chart 的事：官方／自訂模式 chart 不用這個值，平台照樣帶（runtime
        ConfigMap、不重啟），之後租戶切到「同工作區 Headscale」時網址已經在了。"""
        self._headscale()
        for cp in ('official', 'custom', None):
            hv = {'tailscale': {'controlPlane': cp}} if cp else {}
            ts = self._svc('tailscale', 'svc-ts%s' % (cp or 'none')[:6].ljust(6, '0'),
                           helm_values=hv)
            self.assertEqual(self._hs_injected(ts), _hs_url('svc-hs000001-headscale'), cp)

    def test_llm_consumers_get_the_litellm_v1_url(self):
        self._litellm()
        for slug in LLM_CONSUMERS:
            svc = self._svc(slug, 'svc-%s' % slug[:8].replace('-', '0'))
            self.assertEqual(self._ll_injected(svc), _ll_url('svc-ll000001-litellm') + '/v1', slug)

    def test_mcp_litellm_gets_the_base_url_without_v1(self):
        self._litellm()
        mcp = self._svc('mcp-litellm', 'svc-mcpl0001', helm_values={
            'config': {'nonSensitive': {'mcp_server': {'port': 8000}}},
        })
        self.assertEqual(self._values(mcp)['config']['nonSensitive'], {
            'mcp_server': {'port': 8000},
            'connection': {'base_url': _ll_url('svc-ll000001-litellm')},
        })

    def test_real_mcp_litellm_template_gets_only_the_url_on_top_of_its_seed(self):
        """真範本（seed 的 cloud_app_mcp_litellm）：開通時 helm_values＝範本預設。同工作區有 running
        LiteLLM ⇒ 組裝結果只多 ``connection.base_url`` 一條（不含 /v1）；啟動契約、唯讀開關、envPrefix、
        requiredFields 原樣，任何地方都不帶金鑰。沒有 LiteLLM ⇒ 不寫 connection（租戶自己填）。"""
        real = self.env.ref('woow_paas_platform.cloud_app_mcp_litellm')
        seed = json.loads(real.helm_default_values)
        mcp = self._svc('mcp-litellm', 'svc-mcpreal1', template=real, helm_values=seed)
        before = self._values(mcp)
        self.assertNotIn('connection', before['config']['nonSensitive'])
        self._litellm()
        after = self._values(mcp)
        b, a = _flatten(before), _flatten(after)
        path = ('config', 'nonSensitive', 'connection', 'base_url')
        self.assertEqual({k for k in set(a) | set(b) if a.get(k) != b.get(k)}, {path})
        self.assertEqual(a[path], _ll_url('svc-ll000001-litellm'))
        self.assertFalse(a[path].endswith('/v1'), 'LiteLLM 管理 API 的根，不是 OpenAI 相容的 /v1')
        self.assertEqual(after['config']['nonSensitive']['mcp_server'],
                         seed['config']['nonSensitive']['mcp_server'])
        self.assertEqual(after['config']['nonSensitive']['mcp_server']['env'],
                         {'LITELLM_MCP_READONLY': '1'}, '注入不會動到出廠唯讀')
        self.assertEqual(after['envPrefix'], 'LITELLM_MCP_')
        self.assertEqual(after['config']['requiredFields'], ['admin_password'])
        self.assertFalse(after['config'].get('sensitive'))
        self.assertNotIn('master_key', json.dumps(after), '只帶網址，絕不帶金鑰')

    def test_code_server_gets_both_omnigent_and_litellm(self):
        """一個 chart 可同時是兩個注入器的消費端，互不干擾。"""
        self._svc('omnigent', 'svc-om000001')
        self._litellm()
        cs = self._svc('code-server', 'svc-cs000001')
        values = self._values(cs)
        self.assertEqual(values['omnigentHost']['serverUrl'],
                         'http://svc-om000001-omnigent.%s.svc.cluster.local:8000' % _NS)
        self.assertEqual(values['llmGateway']['baseUrl'], _ll_url('svc-ll000001-litellm') + '/v1')

    def test_injected_on_deploy_while_consumer_is_pending(self):
        self._headscale()
        ts = self._svc('tailscale', 'svc-ts000002', state='deploying')
        self.assertEqual(self._hs_injected(ts), _hs_url('svc-hs000001-headscale'))

    def test_assemble_stays_readonly(self):
        hs = self._headscale()
        ll = self._litellm()
        pi = self._svc('pi-agent', 'svc-pi000001')
        before = [s.helm_values for s in (self.ts, hs, ll, pi)]
        self._values(self.ts)
        self._values(pi)
        self.assertEqual([s.helm_values for s in (self.ts, hs, ll, pi)], before)

    # ---- no-op ----------------------------------------------------------

    def test_noop_without_provider(self):
        pi = self._svc('pi-agent', 'svc-pi000001')
        self.assertNotIn('workspaceHeadscaleUrl', self._values(self.ts)['tailscale'])
        self.assertNotIn('llmGateway', self._values(pi))

    def test_noop_when_provider_not_running(self):
        hs = self._headscale()
        ll = self._litellm()
        hermes = self._svc('hermes-agent', 'svc-he000001')
        for state in ('pending', 'deploying', 'initializing', 'error',
                      'upgrading', 'suspended', 'deleting'):
            hs.write({'state': state})
            ll.write({'state': state})
            self.assertIsNone(self._hs_injected(), state)
            self.assertIsNone(self._ll_injected(hermes), state)

    def test_noop_for_provider_in_other_workspace_or_namespace(self):
        self._headscale(ws=self.other_ws, namespace='paas-ws-other001')
        self._litellm(ws=self.other_ws, namespace='paas-ws-other001')
        self._headscale(release='svc-hs000009', namespace='paas-ws-elsewher')
        od = self._svc('open-design', 'svc-od000001')
        self.assertIsNone(self._hs_injected())
        self.assertIsNone(self._ll_injected(od))

    def test_providers_and_other_charts_get_nothing(self):
        hs = self._headscale()
        ll = self._litellm()
        self._headscale(release='svc-hs000002')
        self._litellm(release='svc-ll000002')
        n8n = self._svc('n8n', 'svc-n8n00001')
        for svc in (hs, ll, n8n):
            values = self._values(svc)
            self.assertNotIn('llmGateway', values, svc.template_id.slug)
            self.assertIsNone(_get(values, ('tailscale', 'workspaceHeadscaleUrl')),
                              svc.template_id.slug)
            self.assertIsNone(_get(values, ('config', 'nonSensitive', 'connection')),
                              svc.template_id.slug)

    # ---- several providers ----------------------------------------------

    def test_several_running_picks_oldest_and_logs(self):
        newer_hs = self._headscale(release='svc-hsnewer1')
        older_hs = self._headscale(release='svc-hsolder1')
        newer_ll = self._litellm(release='svc-llnewer1')
        older_ll = self._litellm(release='svc-llolder1')
        for svc, ts in ((newer_hs, '2026-10-04 12:00:00'), (older_hs, '2026-09-01 12:00:00'),
                        (newer_ll, '2026-10-04 12:00:00'), (older_ll, '2026-09-01 12:00:00')):
            self._set_create_date(svc, ts)
        pi = self._svc('pi-agent', 'svc-pi000001')
        with self.assertLogs(_LOGGER, level=logging.INFO) as logs:
            hs_url = self._hs_injected()
            ll_url = self._ll_injected(pi)
        self.assertEqual(hs_url, _hs_url('svc-hsolder1-headscale'))
        self.assertEqual(ll_url, _ll_url('svc-llolder1-litellm') + '/v1')
        self.assertTrue(any('2 running Headscale' in line and str(older_hs.id) in line
                            for line in logs.output), logs.output)
        self.assertTrue(any('2 running LiteLLM' in line and str(older_ll.id) in line
                            for line in logs.output), logs.output)

    def test_same_create_date_tiebreaks_on_id(self):
        first = self._headscale(release='svc-hsfirst1')
        second = self._headscale(release='svc-hssecnd1')
        for svc in (first, second):
            self._set_create_date(svc, '2026-09-15 08:00:00')
        self.assertEqual(self._hs_injected(), _hs_url('svc-hsfirst1-headscale'))

    def test_falls_back_when_oldest_has_broken_values(self):
        broken = self._litellm(release='svc-llbroke1')
        good = self._litellm(release='svc-llgood01')
        self._set_create_date(broken, '2026-09-01 00:00:00')
        self._set_create_date(good, '2026-09-02 00:00:00')
        self.env.cr.execute(
            "UPDATE woow_paas_platform_cloud_service SET helm_values='{not json' "
            "WHERE id=%s", (broken.id,))
        broken.invalidate_recordset(['helm_values'])
        cs = self._svc('code-server', 'svc-cs000001')
        self.assertEqual(self._ll_injected(cs), _ll_url('svc-llgood01-litellm') + '/v1')

    # ---- explicit values --------------------------------------------------

    def test_explicit_values_win_and_are_logged_without_the_value(self):
        self._headscale()
        self._litellm()
        explicit_hs = 'https://hs.example.com'
        explicit_ll = 'https://user:token@llm.example.com/v1'
        self.ts.write({'helm_values': json.dumps(
            {'tailscale': {'controlPlane': 'workspace', 'workspaceHeadscaleUrl': explicit_hs}})})
        pi = self._svc('pi-agent', 'svc-pi000001',
                       helm_values={'llmGateway': {'baseUrl': explicit_ll}})
        mcp = self._svc('mcp-litellm', 'svc-mcpl0001', helm_values={
            'config': {'nonSensitive': {'connection': {'base_url': 'https://llm.example.com'}}}})
        with self.assertLogs(_LOGGER, level=logging.INFO) as logs:
            self.assertEqual(self._hs_injected(), explicit_hs)
            self.assertEqual(self._ll_injected(pi), explicit_ll)
            self.assertEqual(
                _get(self._values(mcp), ('config', 'nonSensitive', 'connection', 'base_url')),
                'https://llm.example.com')
        for path, svc in (('tailscale.workspaceHeadscaleUrl', self.ts),
                          ('llmGateway.baseUrl', pi),
                          ('config.nonSensitive.connection.base_url', mcp)):
            self.assertTrue(
                any('keeps its explicit %s' % path in line and str(svc.id) in line
                    for line in logs.output), (path, logs.output))
        for value in (explicit_hs, explicit_ll, 'llm.example.com'):
            self.assertFalse(any(value in line for line in logs.output),
                             '網址不進 log（管理員可能帶了 userinfo）')

    def test_blank_explicit_value_is_replaced(self):
        self._headscale()
        self.ts.write({'helm_values': json.dumps(
            {'tailscale': {'controlPlane': 'workspace', 'workspaceHeadscaleUrl': ''}})})
        self.assertEqual(self._hs_injected(), _hs_url('svc-hs000001-headscale'))

    # ---- Service name / port mirror the provider charts' _helpers.tpl ------

    def test_headscale_port_and_fullname_mirror(self):
        hs = self._headscale(helm_values={'headscale': {'port': 18080}, 'service': {'port': 3000}})
        self.assertEqual(self._hs_injected(), _hs_url('svc-hs000001-headscale', port=18080),
                         '控制埠是 headscale.port，不是入口第一個埠 service.port（Headplane）')
        hs.write({'helm_values': json.dumps({'fullnameOverride': 'vpn-ctl'})})
        self.assertEqual(self._hs_injected(), _hs_url('vpn-ctl'))
        hs.write({'helm_values': json.dumps({'nameOverride': 'vpn'})})
        self.assertEqual(self._hs_injected(), _hs_url('svc-hs000001-vpn'))
        hs.write({'helm_values': json.dumps({'nameOverride': 'hs'})})
        self.assertEqual(self._hs_injected(), _hs_url('svc-hs000001'),
                         'chart 的 contains 規則：release 名已含 name（svc-hs… 含 hs）→ 用 release 名')
        hs.write({'helm_values': json.dumps({'headscale': {'port': '8080; reboot'}})})
        self.assertIsNone(self._hs_injected(), '非整數 port 不組網址')
        hs.write({'helm_values': json.dumps({'fullnameOverride': 'Bad_Name'})})
        self.assertIsNone(self._hs_injected(), '非 DNS label 不組網址')

    def test_release_name_containing_chart_name_is_used_verbatim(self):
        self._headscale(release='my-headscale')
        self._litellm(release='team-litellm-1')
        cs = self._svc('code-server', 'svc-cs000001')
        self.assertEqual(self._hs_injected(), _hs_url('my-headscale'))
        self.assertEqual(self._ll_injected(cs), _ll_url('team-litellm-1') + '/v1')

    def test_litellm_port_override(self):
        self._litellm(helm_values={'service': {'port': 14000}})
        cs = self._svc('code-server', 'svc-cs000001')
        self.assertEqual(self._ll_injected(cs), _ll_url('svc-ll000001-litellm', port=14000) + '/v1')

    def test_litellm_egress_mismatch_warns_but_still_injects(self):
        """hermes／OD 的 litellmEgress 只對 name=litellm 的 pod 開 4000：對不上照常注入、記 warning。"""
        ll = self._litellm(helm_values={'service': {'port': 4000, 'targetPort': 9000}})
        hermes = self._svc('hermes-agent', 'svc-he000001')
        with self.assertLogs(_LOGGER, level=logging.WARNING) as logs:
            got = self._ll_injected(hermes)
        self.assertEqual(got, _ll_url('svc-ll000001-litellm') + '/v1')
        self.assertTrue(any('targetPort 9000' in line and 'litellmEgress' in line
                            for line in logs.output), logs.output)
        ll.write({'helm_values': json.dumps({'nameOverride': 'gw'})})
        with self.assertLogs(_LOGGER, level=logging.WARNING) as logs:
            got = self._ll_injected(hermes)
        self.assertEqual(got, _ll_url('svc-ll000001-gw') + '/v1')
        self.assertTrue(any('nameOverride' in line and 'litellmEgress' in line
                            for line in logs.output), logs.output)

    def test_standard_names_log_no_warning(self):
        """正式站形態（svc-xxxxxxxx、沒有 override）：不記 warning。"""
        self._headscale()
        self._litellm()
        cs = self._svc('code-server', 'svc-cs000001')
        with self.assertNoLogs(_LOGGER, level=logging.WARNING):
            self.assertEqual(self._hs_injected(), _hs_url('svc-hs000001-headscale'))
            self.assertEqual(self._ll_injected(cs), _ll_url('svc-ll000001-litellm') + '/v1')

    # ---- reset path: the URL is runtime config (no pod restart) ------------

    def _reset_values(self, svc):
        client = MagicMock()
        client.reset_release_config.return_value = {'chart_version': '1.0.0'}
        svc.write({'state': 'running'})  # reset 完會轉 upgrading；下一次 reset 要 running
        with patch(_OPERATOR_CLIENT, return_value=client):
            svc._reset_admin_password()
        self.assertTrue(client.reset_release_config.called, '前提：reset 真的送出了')
        return client.reset_release_config.call_args.kwargs['values']

    def _resettable(self, slug, release, helm_values):
        tpl = self._tpl(
            slug, name='T %s reset' % slug, helm_repo_url='',
            helm_chart_name='oci://example/%s' % slug, mcp_enabled=False,
            has_admin_gui=True, admin_password_carrier='helm_secret',
            admin_password_settable=True,
        )
        return _internal_provision(self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='old-password-0000',
        )).create({
            'name': release, 'workspace_id': self.ws.id,
            'template_id': tpl.id, 'state': 'running',
            'helm_release_name': release, 'helm_namespace': _NS,
            'helm_values': json.dumps(helm_values),
        })

    def test_reset_after_late_provider_changes_only_the_injected_path(self):
        """VPN 節點／code-server 先開、Headscale／LiteLLM 後開，接著重設密碼。

        reset 走 ``helm upgrade --atomic --timeout 15s``，前提是 pod template 不變。注入值
        只進 chart 的 runtime ConfigMap（tailscale 0.1.2 與四個 LiteLLM 消費端 chart 的
        render-contract 測試守線），所以 reset 照常重算、把新網址帶上去是安全且想要的。
        平台這一側釘住：reset 與 deploy 同一條組裝，前後**只差注入的那條路徑**——
        controlPlane、authKey 這些會進 pod template 的租戶值一個都不動。
        """
        ts = self._resettable('tailscale', 'svc-tsreset1', {
            'tailscale': {'controlPlane': 'workspace', 'authKey': 'hskey-auth-x',
                          'serve': '8069=svc-od:80'},
        })
        cs = self._resettable('code-server', 'svc-csreset1', {'codeServer': {'x': 1}})
        before = {svc: self._reset_values(svc) for svc in (ts, cs)}
        self.assertIsNone(_get(before[ts], ('tailscale', 'workspaceHeadscaleUrl')))
        self.assertNotIn('llmGateway', before[cs])

        self._headscale()
        self._litellm()
        after = {svc: self._reset_values(svc) for svc in (ts, cs)}
        for svc, path, url in (
                (ts, ('tailscale', 'workspaceHeadscaleUrl'), _hs_url('svc-hs000001-headscale')),
                (cs, ('llmGateway', 'baseUrl'), _ll_url('svc-ll000001-litellm') + '/v1')):
            b, a = _flatten(before[svc]), _flatten(after[svc])
            changed = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
            self.assertEqual(changed, {path}, svc.template_id.slug)
            self.assertEqual(a[path], url)
            self.assertEqual(after[svc], svc._assemble_deploy_values()['values'],
                             'reset 與 deploy 同一條組裝')

    # ---- seed lockstep ----------------------------------------------------

    def test_constants_match_seed(self):
        """消費端都是真的範本（含 mcp-litellm，2026-10-05 以隱藏範本進 seed）；提供者的 chart 名、port
        與 seed 一致；tailscale 範本已釘到懂 controlPlane 的 chart；mcp-litellm 的注入路徑落在
        mcp-server chart 的 config.nonSensitive（chart 把它整棵寫進 config.base.json）。"""
        Template = self.Template.with_context(active_test=False)
        seeded = lambda slug: Template.search(  # noqa: E731
            [('slug', '=', slug), ('id', 'not in', list(t.id for t in self.tpl.values()))],
            limit=1)
        for table, _provider, _suffixes in _WORKSPACE_URL_INJECTORS:
            for slug in table:
                self.assertTrue(seeded(slug), slug)
        mcp = self.env.ref('woow_paas_platform.cloud_app_mcp_litellm')
        self.assertEqual(seeded('mcp-litellm'), mcp)
        self.assertTrue(mcp.helm_chart_name.endswith('/mcp-server'), mcp.helm_chart_name)
        mcp_seed = json.loads(mcp.helm_default_values)['config']['nonSensitive']
        self.assertIsInstance(mcp_seed, dict)
        self.assertNotIn('connection', mcp_seed, '網址交給注入；seed 寫死會蓋掉它（explicit value wins）')
        self.assertEqual(_LITELLM_BASE_URL_VALUE_PATHS['mcp-litellm'],
                         [('config', 'nonSensitive', 'connection', 'base_url')],
                         'apps/litellm 的 connection 鍵是 base_url（→ LITELLM_MCP_BASE_URL）')
        hs = self.env.ref('woow_paas_platform.cloud_app_headscale')
        self.assertEqual(hs.helm_chart_name.rsplit('/', 1)[-1],
                         _WORKSPACE_PROVIDERS['headscale']['chart'])
        hs_values = json.loads(hs.helm_default_values)
        self.assertEqual(hs_values.get('headscale', {}).get('port', 8080),
                         _WORKSPACE_PROVIDERS['headscale']['default_port'])
        self.assertEqual(_WORKSPACE_PROVIDERS['headscale']['port_path'], ('headscale', 'port'))
        ll = self.env.ref('woow_paas_platform.cloud_app_litellm')
        self.assertEqual(ll.helm_chart_name.rsplit('/', 1)[-1],
                         _WORKSPACE_PROVIDERS['litellm']['chart'])
        ll_service = json.loads(ll.helm_default_values).get('service', {})
        self.assertEqual(ll_service.get('port', 4000), _WORKSPACE_PROVIDERS['litellm']['default_port'])
        self.assertEqual(ll.default_port, _WORKSPACE_PROVIDERS['litellm']['default_port'])
        self.assertEqual(ll_service.get('targetPort', 4000), 4000,
                         'hermes／OD 的 litellmEgress 只對 LiteLLM pod 的 4000 開洞')
        ts = self.env.ref('woow_paas_platform.cloud_app_tailscale')
        specs = {s['key']: s for s in ts._parse_helm_value_specs()['optional']}
        self.assertIn('tailscale.controlPlane', specs)
        self.assertNotIn('tailscale.workspaceHeadscaleUrl', specs, '平台注入、不是租戶欄位')
        major, minor, patch_ = (int(x) for x in ts.helm_chart_version.split('.'))
        self.assertGreaterEqual((major, minor, patch_), (0, 1, 2),
                                'tailscale.workspaceHeadscaleUrl／controlPlane 要 chart ≥ 0.1.2')


@tagged('post_install', '-at_install')
class TestHealTailscaleControlPlane(TransactionCase):
    """升級期 healer：0.1.2 之前填了 loginServer 的 VPN 節點補 controlPlane=custom。"""

    def setUp(self):
        super().setUp()
        self.Service = _internal_provision(self.env['woow_paas_platform.cloud_service'])
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'Heal TS WS'})

        def tpl(slug):
            return Template.create({
                'name': 'T heal %s' % slug, 'slug': slug,
                'helm_repo_url': 'https://charts.example.com',
                'helm_chart_name': slug, 'helm_chart_version': '1.0.0',
                'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            })
        self.tpl_ts = tpl('tailscale')
        self.tpl_other = tpl('n8n')

    def _svc(self, release, helm_values, template=None):
        return self.Service.create({
            'name': release, 'workspace_id': self.ws.id,
            'template_id': (template or self.tpl_ts).id,
            'helm_release_name': release, 'helm_namespace': 'paas-ws-heal0001',
            'helm_values': json.dumps(helm_values),
        })

    def _ts(self, svc):
        return svc._helm_values_dict().get('tailscale')

    def test_backfills_custom_only_where_login_server_was_set(self):
        legacy = self._svc('svc-heal0001', {'tailscale': {
            'loginServer': 'http://svc-hs-headscale:8080', 'authKey': 'k', 'serve': '1=a:1'}})
        official = self._svc('svc-heal0002', {'tailscale': {'loginServer': '', 'hostname': 'x'}})
        chosen = self._svc('svc-heal0003', {'tailscale': {
            'controlPlane': 'official', 'loginServer': 'http://kept:8080'}})
        nothing = self._svc('svc-heal0004', {'entrance': {'basicAuthUsername': 'admin'}})
        other = self._svc('svc-heal0005', {'tailscale': {'loginServer': 'http://x:1'}},
                          template=self.tpl_other)
        broken = self._svc('svc-heal0006', {})
        self.env.cr.execute(
            "UPDATE woow_paas_platform_cloud_service SET helm_values='{not json' WHERE id=%s",
            (broken.id,))
        broken.invalidate_recordset(['helm_values'])

        healed = self.env['woow_paas_platform.cloud_service']._heal_tailscale_control_plane()

        self.assertEqual(healed, [legacy.id])
        self.assertEqual(self._ts(legacy), {
            'loginServer': 'http://svc-hs-headscale:8080', 'authKey': 'k', 'serve': '1=a:1',
            'controlPlane': 'custom'}, '其他租戶值原樣保留')
        self.assertEqual(self._ts(official), {'loginServer': '', 'hostname': 'x'})
        self.assertEqual(self._ts(chosen)['controlPlane'], 'official', '已選的不動')
        self.assertIsNone(self._ts(nothing))
        self.assertNotIn('controlPlane', self._ts(other), '只動 tailscale 範本的服務')
        self.assertEqual(
            self.env['woow_paas_platform.cloud_service']._heal_tailscale_control_plane(), [],
            '冪等：第二次沒有東西要補')

    def test_healer_is_wired_into_module_update(self):
        """data XML 的 <function> 每次 -u 都跑（同 _heal_sticky_helm_value_specs）。"""
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'data', 'cloud_app_templates.xml')
        with open(path, encoding='utf-8') as fh:
            xml = fh.read()
        self.assertIn('name="_heal_tailscale_control_plane"', xml)
        self.assertIn('model="woow_paas_platform.cloud_service"', xml)

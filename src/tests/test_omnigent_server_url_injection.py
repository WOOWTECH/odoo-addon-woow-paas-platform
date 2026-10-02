# -*- coding: utf-8 -*-
"""Omnigent BYO host：部署時注入同工作區 Omnigent 的叢集內網址。

code-server／pi-agent／open-design 的 chart 在主容器裡跑 omnigent host（woow-paas-charts
``shared/omnigent-host`` 共用套件，host 跑在同一個主容器裡），標準 values 區塊是
``omnigentHost.serverUrl``。平台在 ``_assemble_deploy_values`` 裡把「同工作區、running、
最早建立的 Omnigent」的叢集內網址填進去（和 ``_inject_allowed_origins`` 同一 family）：

- 只有網址，絕不帶 token／密碼（accounts 模式由租戶自己 ``omnigent login``）
- 執行期設定：chart 只把它放進不入 checksum 的 runtime ConfigMap，網址變動不重啟 pod，
  所以 reset 密碼（``helm upgrade --atomic --timeout 15s``）也照常重算它
- 工作區沒有 running Omnigent ⇒ no-op（chart 預設 ""）
- helm_values 已有非空的 serverUrl ⇒ 尊重、不覆寫
- 多個 ⇒ 決定性地取最早建立的，記 log
- Service 名鏡射 charts/omnigent ``omnigent.fullname``；port＝``service.port``（預設 8000）
"""
import json
import logging
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models.cloud_service import (
    _OMNIGENT_CHART_NAME,
    _OMNIGENT_DEFAULT_SERVICE_PORT,
    _OMNIGENT_SERVER_URL_VALUE_PATHS,
    _OMNIGENT_TEMPLATE_SLUG,
    _internal_provision,
)

_NS = 'paas-ws-omni0001'
_LOGGER = 'odoo.addons.woow_paas_platform.models.cloud_service'
_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)


def _url(release_service, namespace=_NS, port=8000):
    return 'http://%s.%s.svc.cluster.local:%d' % (release_service, namespace, port)


@tagged('post_install', '-at_install')
class TestInjectOmnigentServerUrl(TransactionCase):
    """static injector：只對主容器內建 omnigent host 的 chart 寫 omnigentHost.serverUrl。"""

    URL = 'http://svc-a-omnigent.paas-ws-x.svc.cluster.local:8000'

    def setUp(self):
        super().setUp()
        self.CloudService = self.env['woow_paas_platform.cloud_service']

    def test_sets_server_url_for_host_charts(self):
        for slug in ('code-server', 'pi-agent', 'open-design'):
            out = self.CloudService._inject_omnigent_server_url({}, slug, self.URL)
            self.assertEqual(out, {'omnigentHost': {'serverUrl': self.URL}}, slug)

    def test_noop_for_other_slugs(self):
        """Omnigent 自己、hermes-agent（open-design 派生但沒有內建 omnigent host）、n8n
        都不得長出鍵。"""
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

    def test_explicit_value_is_not_overwritten(self):
        """管理員明確設的網址（例如公開 https 網址）優先，平台不覆寫。"""
        explicit = 'https://omnigent.example.com'
        out = self.CloudService._inject_omnigent_server_url(
            {'omnigentHost': {'serverUrl': explicit}}, 'code-server', self.URL)
        self.assertEqual(out['omnigentHost']['serverUrl'], explicit)

    def test_empty_or_blank_value_counts_as_unset(self):
        for blank in ('', '   ', None):
            out = self.CloudService._inject_omnigent_server_url(
                {'omnigentHost': {'serverUrl': blank}}, 'open-design', self.URL)
            self.assertEqual(out['omnigentHost']['serverUrl'], self.URL, repr(blank))

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
        """各 chart 開給 Omnigent 的 egress 只涵蓋自己的 namespace，跨 ns 的網址多半連不上。"""
        self._omni(namespace='paas-ws-elsewher')
        self.assertIsNone(self._injected(self.cs))

    def test_noop_for_chart_without_omnigent_host(self):
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
        with self.assertLogs(_LOGGER, level=logging.INFO) as logs:
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

    def _assert_od_selector_warning(self, expected_url):
        """照常注入，但記「open-design 的 omnigentEgress selector 對不上」warning。"""
        with self.assertLogs(_LOGGER, level=logging.WARNING) as logs:
            got = self._injected(self.cs)
        self.assertEqual(got, expected_url)
        self.assertTrue(
            any('omnigentEgress' in line and 'pod selector' in line
                for line in logs.output), logs.output)

    def test_release_name_containing_chart_name_is_used_verbatim(self):
        """Service 名＝release 名原樣（chart ``contains`` 規則）。OD 會把
        ``my-omnigent`` 釘成 instance=my，但 pod 的 instance 是 my-omnigent ⇒ warning。"""
        self._omni(release='my-omnigent')
        self._assert_od_selector_warning(_url('my-omnigent'))

    def test_standard_name_matches_od_selector_without_warning(self):
        """正式站形態（svc-xxxxxxxx、沒有 override）：OD selector 對得上，不記 warning。"""
        self._omni()
        with self.assertNoLogs(_LOGGER, level=logging.WARNING):
            got = self._injected(self.cs)
        self.assertEqual(got, _url('svc-om000001-omnigent'))

    def test_fullname_override_and_name_override(self):
        omni = self._omni(helm_values={'fullnameOverride': 'og-entry'})
        # 不是 <X>-omnigent 形態：OD 不釘 instance，name 標籤仍是 omnigent ⇒ 對得上
        with self.assertNoLogs(_LOGGER, level=logging.WARNING):
            self.assertEqual(self._injected(self.cs), _url('og-entry'))
        omni.write({'helm_values': json.dumps({'fullnameOverride': 'other-omnigent'})})
        self._assert_od_selector_warning(_url('other-omnigent'))
        omni.write({'helm_values': json.dumps({'nameOverride': 'og'})})
        self._assert_od_selector_warning(_url('svc-om000001-og'))

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
        # 真正拿來組網址的是 helm_values 的 service.port（template 預設 merge 進來的），
        # 不是 default_port——seed 改了 service.port 而這裡沒跟上必須紅。
        seed_service = json.loads(omni_tpl.helm_default_values).get('service', {})
        self.assertEqual(
            seed_service.get('port', _OMNIGENT_DEFAULT_SERVICE_PORT),
            _OMNIGENT_DEFAULT_SERVICE_PORT)
        self.assertEqual(
            seed_service.get('targetPort', _OMNIGENT_DEFAULT_SERVICE_PORT),
            _OMNIGENT_DEFAULT_SERVICE_PORT,
            'open-design 的 omnigentEgress 只對 Omnigent pod 的 8000 開洞')

    # ---- explicit value / reachability warning ----------------------------

    def test_explicit_value_in_helm_values_wins_and_is_logged(self):
        self._omni()
        explicit = 'https://omnigent.example.com'
        self.cs.write({'helm_values': json.dumps(
            {'omnigentHost': {'serverUrl': explicit}})})
        with self.assertLogs(_LOGGER, level=logging.INFO) as logs:
            got = self._injected(self.cs)
        self.assertEqual(got, explicit)
        self.assertTrue(
            any('keeps its explicit omnigentHost.serverUrl' in line
                and str(self.cs.id) in line for line in logs.output),
            logs.output)
        self.assertFalse(any(explicit in line for line in logs.output),
                         '網址不進 log（管理員可能帶了 userinfo）')

    def test_explicit_blank_value_is_replaced(self):
        self._omni()
        self.cs.write({'helm_values': json.dumps({'omnigentHost': {'serverUrl': ''}})})
        self.assertEqual(self._injected(self.cs), _url('svc-om000001-omnigent'))

    def test_target_port_mismatch_warns_but_still_injects(self):
        self._omni(helm_values={'service': {'port': 8000, 'targetPort': 9000}})
        with self.assertLogs(_LOGGER, level=logging.WARNING) as logs:
            got = self._injected(self.cs)
        self.assertEqual(got, _url('svc-om000001-omnigent'))
        self.assertTrue(
            any('targetPort 9000' in line and 'omnigentEgress' in line
                for line in logs.output), logs.output)

    # ---- reset path: the URL is runtime config (no pod restart) ------------

    def _reset_values(self, svc):
        client = MagicMock()
        client.reset_release_config.return_value = {'chart_version': '1.0.0'}
        svc.write({'state': 'running'})  # reset 完會轉 upgrading；下一次 reset 要 running
        with patch(_OPERATOR_CLIENT, return_value=client):
            svc._reset_admin_password()
        self.assertTrue(client.reset_release_config.called, '前提：reset 真的送出了')
        return client.reset_release_config.call_args.kwargs['values']

    def test_reset_after_late_omnigent_carries_the_url(self):
        """review major（F-461-2 同型）：code-server 先開、Omnigent 後買，接著重設密碼。

        reset 走 ``helm upgrade --atomic --timeout 15s``，前提是 pod template 不變。
        網址只進 chart 的 runtime ConfigMap（不入 checksum，套件測試
        kit_assert_runtime_server_url 守線），所以 reset 照常重算、把新網址帶上去是
        **安全且想要的**：ConfigMap 就地更新，supervisor 下次讀檔就看到。這裡釘住
        平台這一側——reset 與 deploy 走同一條組裝，不另走一套。
        """
        tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'T code-server reset', 'slug': 'code-server',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example/code-server',
            'helm_chart_version': '1.0.0', 'mcp_enabled': False,
            'has_admin_gui': True, 'admin_password_carrier': 'helm_secret',
            'admin_password_settable': True,
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        cs = _internal_provision(self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='old-password-0000',
        )).create({
            'name': 'svc-csreset1', 'workspace_id': self.ws.id,
            'template_id': tpl.id, 'state': 'running',
            'helm_release_name': 'svc-csreset1', 'helm_namespace': _NS,
            'helm_values': json.dumps({'omnigentHost': {'enabled': True}}),
        })
        before = self._reset_values(cs)
        self.assertNotIn('serverUrl', before.get('omnigentHost', {}))

        self._omni()
        after = self._reset_values(cs)
        self.assertEqual(after['omnigentHost'],
                         {'enabled': True, 'serverUrl': _url('svc-om000001-omnigent')})
        self.assertEqual(after, cs._assemble_deploy_values()['values'],
                         'reset 與 deploy 同一條組裝')

    # ---- non-superuser triggers (sudo lookup) ------------------------------

    def test_tenant_members_get_the_same_url(self):
        """部署可能由租戶身分觸發（owner／admin 的「儲存設定」走未 sudo 的 service）：
        四種非系統管理員角色觸發的注入結果都和 superuser 一樣。

        這支釘的是「成員觸發的路徑通」，**不**證明 sudo 必要：今天的讀取 rule 讓
        成員讀得到同工作區每一筆服務，拿掉 sudo 它照樣綠。sudo 由下一支
        ``test_trigger_that_cannot_read_the_omnigent_still_gets_the_url`` 釘。"""
        users = {
            role: new_test_user(self.env, login='omni-url-%s' % role,
                                groups='base.group_user')
            for role in ('owner', 'admin', 'user', 'guest')
        }
        for user in users.values():
            self.assertFalse(user.has_group('base.group_system'), '前提：非系統管理員')
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Omnigent URL tenant WS', 'owner_id': users['owner'].id})
        Access = self.env['woow_paas_platform.workspace_access']
        for role in ('admin', 'user', 'guest'):
            Access.create({'workspace_id': ws.id, 'user_id': users[role].id, 'role': role})
        cs = self._svc(self.tpl_cs, 'svc-cstenant', ws=ws)
        self._omni(release='svc-omtenant', ws=ws)
        expected = _url('svc-omtenant-omnigent')

        self.assertEqual(cs._workspace_omnigent_url(), expected, 'superuser 基準')
        for role, user in users.items():
            self.assertEqual(
                cs.with_user(user)._workspace_omnigent_url(), expected, role)
        for role in ('owner', 'admin'):
            values = cs.with_user(users[role])._assemble_deploy_values()['values']
            self.assertEqual(values['omnigentHost']['serverUrl'], expected, role)

    def test_trigger_that_cannot_read_the_omnigent_still_gets_the_url(self):
        """``_workspace_omnigent_url`` 以 sudo 查詢的理由：注入結果不能由「誰觸發部署」
        決定。

        今天**不存在**這種觸發者：cloud_service 的讀取 rule 對同工作區的每一筆一樣
        （成員全看得到）、系統管理員全開、cron 是 superuser。所以這裡用一條**測試
        專用**的 global rule（遮住 Omnigent 服務）造出「讀得到本筆、讀不到同工作區
        Omnigent」的 owner，模擬日後加了依服務／依角色的可見性 rule。拿掉 sudo，這支
        會紅（沒注入網址）。"""
        owner = new_test_user(self.env, login='omni-url-hidden-owner',
                              groups='base.group_user')
        self.assertFalse(owner.has_group('base.group_system'), '前提：非系統管理員')
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Omnigent URL hidden WS', 'owner_id': owner.id})
        cs = self._svc(self.tpl_cs, 'svc-cshidden', ws=ws)
        omni = self._omni(release='svc-omhidden', ws=ws)
        expected = _url('svc-omhidden-omnigent')

        Service = self.env['woow_paas_platform.cloud_service'].with_user(owner)
        self.assertEqual(Service.search([('id', '=', omni.id)]).ids, [omni.id],
                         '前提：沒有測試 rule 時 owner 讀得到 Omnigent（今天的狀態）')
        # 沒有 groups 的 ir.rule＝global rule，和其他 rule 取 AND，系統管理員以外都受限；
        # sudo（superuser）不受 ir.rule 約束。
        self.env['ir.rule'].create({
            'name': 'test-only: hide Omnigent services from their workspace members',
            'model_id': self.env['ir.model']._get_id('woow_paas_platform.cloud_service'),
            'domain_force': "[('template_id.slug', '!=', 'omnigent')]",
            'perm_read': True, 'perm_write': True,
            'perm_create': True, 'perm_unlink': True,
        })
        self.assertFalse(Service.search([('id', '=', omni.id)]),
                         '前提：觸發者讀不到同工作區的 Omnigent')
        self.assertEqual(Service.search([('id', '=', cs.id)]).ids, [cs.id],
                         '前提：觸發者讀得到要部署的那台')

        self.assertEqual(cs.with_user(owner)._workspace_omnigent_url(), expected)
        values = cs.with_user(owner)._assemble_deploy_values()['values']
        self.assertEqual(values['omnigentHost']['serverUrl'], expected)

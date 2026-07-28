# -*- coding: utf-8 -*-
"""#449：admin_password 載體策略（config_json / helm_secret）的守線斷言。

reset saga 原本寫死 mcp-server 的檔案佈局假設（``kubectl exec`` patch
``/data/config.json``）。hermes-agent（PVC 掛 ``/opt/data``）與 open-design
（``/app/.od``）沒有那個檔——密碼由 chart 自己的 Secret 承載——於是 patch 炸、
備份未建成、補償 ``os.replace`` 跟著炸，每次密碼重設必 500。

拍板「1+3 合體」：template 帶載體欄位，平台在 reset-config request 帶策略，
operator 依策略分流。本檔守平台側的兩件事：

1. **seed 標記正確**，且是用**結構不變式**守（chart ⟺ 載體），不是硬編清單。
   照 slug 猜載體正是這個 bug 的成因。
2. **策略真的被送出去**，且供給面（sensitive_values）不因載體而異——載體只影響
   reset 怎麼做，不影響建立服務時怎麼注入。

選錯載體的後果不對稱，兩個方向都要有測試扛：``helm_secret`` 誤標 ``config_json``
＝reset 必 500（大聲）；``config_json`` 誤標 ``helm_secret`` ＝ helm upgrade 成功
但 config.json 沒動、密碼**靜默**不生效（假裝成功，更難查）。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

MCP_SERVER_CHART_SUFFIX = '/mcp-server'


class TestAdminPasswordCarrierSeed(TransactionCase):
    """seed 的載體標記與 chart 之間的結構不變式。"""

    def _templates_with_admin_gui(self):
        return self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False,
        ).search([('has_admin_gui', '=', True)])

    def test_hermes_agent_is_helm_secret(self):
        template = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        self.assertEqual(
            template.admin_password_carrier, 'helm_secret',
            'hermes-agent 的密碼在 chart Secret，PVC 掛 /opt/data 沒有 '
            '/data/config.json；標成 config_json 會讓 operator 對不存在的檔 '
            'exec patch → 每次 reset 500（#449 的原始症狀）')

    def test_open_design_is_helm_secret(self):
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        self.assertEqual(
            template.admin_password_carrier, 'helm_secret',
            'open-design 的 PVC 掛 /app/.od 與 /home/opendesign，同樣沒有 '
            '/data/config.json；basic-auth 密碼由 chart Secret 承載')

    def test_mcp_family_keeps_config_json(self):
        """mcp-server 家族必須留在既有 saga：它的密碼真的落在 PVC 上的檔。

        chart 的 seed-config 一次性把 Secret 落成 /data/config.json，之後那個檔
        同時是 admin GUI 的 runtime state store（租戶自填的 connection/tools/
        token_history 都在裡面）。改走 helm_secret 會有兩個後果：helm upgrade
        成功但檔沒動 → 密碼靜默不生效；而且少了外科式 read-modify-write 的保護。
        """
        for xmlid in (
            'cloud_app_mcp_odoo', 'cloud_app_mcp_odoo_manage', 'cloud_app_mcp_n8n',
            'cloud_app_mcp_k8s', 'cloud_app_mcp_vk', 'cloud_app_mcp_opendesign',
        ):
            template = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertEqual(
                template.admin_password_carrier, 'config_json',
                '%s 走 mcp-server chart，密碼落在 PVC 的 config.json' % xmlid)

    def test_carrier_matches_chart_family(self):
        """結構不變式：``config_json`` ⟺ chart 是 mcp-server。

        刻意不硬編「幾筆 config_json、幾筆 helm_secret」——那種計數斷言在新增
        template 時只會被 bump 過去（seed-topology-invariants 的教訓）。這裡守的
        是**判準本身**：載體由「chart 有沒有把 Secret 落成 PVC 上的設定檔」決定。
        新上架一筆 mcp-server chart 的 template 卻標 helm_secret（或反之）就會紅，
        而單純多一筆 template 不會。
        """
        for template in self._templates_with_admin_gui():
            is_mcp_server_chart = (template.helm_chart_name or '').endswith(
                MCP_SERVER_CHART_SUFFIX)
            expected = 'config_json' if is_mcp_server_chart else 'helm_secret'
            self.assertEqual(
                template.admin_password_carrier, expected,
                'template %s（chart=%s）的載體應為 %s。判準＝chart 有沒有把 '
                'Secret 落成 PVC 上的設定檔，不是 slug；新 chart 上架請先查證 '
                'chart 的 secret.yaml 與 deployment.yaml，別照抄鄰居。' % (
                    template.slug, template.helm_chart_name, expected))

    def test_every_admin_gui_template_declares_a_carrier(self):
        """required 欄位：載體不可空。空值＝operator 端分流失去判準。"""
        for template in self._templates_with_admin_gui():
            self.assertIn(
                template.admin_password_carrier, ('config_json', 'helm_secret'),
                'template %s 沒有有效載體' % template.slug)

    def test_default_is_config_json(self):
        """新建 template 預設 config_json＝既有行為，發版順序安全的支點。

        operator 先發版（其 request 欄位同樣預設 config_json）、平台後發版；
        任何一端落後都退回既有 saga，不會把 mcp 家族推進沒驗過的路徑。
        """
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Carrier default probe',
            'slug': 'carrier-default-probe',
            'category': 'mcp',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/mcp-server',
            'helm_chart_version': '0.1.5',
        })
        self.assertEqual(template.admin_password_carrier, 'config_json')


class TestAdminPasswordCarrierForwarded(TransactionCase):
    """平台把載體送進 reset-config request（operator 的分流判準來源）。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Carrier workspace',
        })

    def _make_service(self, carrier, chart, slug):
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Carrier %s' % carrier,
            'slug': slug,
            'category': 'mcp',
            # helm_repo_url 是 required（DB NOT NULL）；OCI chart 慣例是空字串。
            'helm_repo_url': '',
            'helm_chart_name': chart,
            'helm_chart_version': '0.1.5',
            'mcp_enabled': False,
            'has_admin_gui': True,
            'admin_password_carrier': carrier,
        })
        return self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='old-password',
        ).create({
            'name': 'Carrier service %s' % carrier,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-carrier-%s' % carrier.replace('_', '-'),
            'helm_values': json.dumps({
                'config': {'nonSensitive': {'connection': {'url': 'https://t.example'}}},
            }),
        })

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_config_json_template_forwards_config_json(self, get_client):
        client = MagicMock()
        get_client.return_value = client
        service = self._make_service(
            'config_json', 'oci://example/mcp-server', 'carrier-cfg')

        service._reset_admin_password()

        self.assertEqual(
            client.reset_release_config.call_args.kwargs['admin_password_carrier'],
            'config_json')

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_helm_secret_template_forwards_helm_secret(self, get_client):
        client = MagicMock()
        get_client.return_value = client
        service = self._make_service(
            'helm_secret', 'oci://example/hermes-agent', 'carrier-hs')

        service._reset_admin_password()

        self.assertEqual(
            client.reset_release_config.call_args.kwargs['admin_password_carrier'],
            'helm_secret',
            'reset-config request 沒帶 helm_secret → operator 退回 config.json '
            'saga → #449 的 500 原地復活')

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_sensitive_values_shape_is_carrier_independent(self, get_client):
        """供給面兩種載體完全相同：都是 config.sensitive.admin_password。

        兩個 chart 的 secret.yaml 都吃這個鍵（hermes-agent 的
        ``resolveSecretValue`` 明寫 explicit 來源是
        ``.Values.config.sensitive.admin_password``）。載體**只**影響 reset 怎麼
        做，不影響注入形狀——把它們混為一談會讓人誤以為 helm_secret 需要另一套
        sensitive_values 結構。
        """
        shapes = {}
        for carrier, chart, slug in (
            ('config_json', 'oci://example/mcp-server', 'carrier-shape-cfg'),
            ('helm_secret', 'oci://example/hermes-agent', 'carrier-shape-hs'),
        ):
            client = MagicMock()
            get_client.return_value = client
            service = self._make_service(carrier, chart, slug)
            new_password = service._reset_admin_password()
            sensitive = client.reset_release_config.call_args.kwargs['sensitive_values']
            self.assertEqual(
                sensitive, {'config': {'sensitive': {'admin_password': new_password}}})
            # 密碼本身當然不同，比較的是「鍵路徑集合」這個形狀。
            shapes[carrier] = _leaf_paths(sensitive)
        self.assertEqual(shapes['config_json'], shapes['helm_secret'])

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_helm_secret_path_still_uses_full_values_pipeline(self, get_client):
        """#411 的全量 values 管線對兩條路一視同仁（injector 每次重組）。

        helm_secret 路在 operator 端一樣走 ``helm upgrade``，若這裡少送 injector，
        reset 語意下 storageClass / imagePullSecrets 會從 rendered manifest 消失。
        """
        client = MagicMock()
        get_client.return_value = client
        service = self._make_service(
            'helm_secret', 'oci://example/hermes-agent', 'carrier-values-hs')

        service._reset_admin_password()

        kwargs = client.reset_release_config.call_args.kwargs
        self.assertTrue(kwargs['reset_values'])
        self.assertEqual(
            kwargs['values'], service._assemble_deploy_values()['values'])


def _leaf_paths(payload, prefix=()):
    """回傳 dict 的所有 leaf 鍵路徑集合（值不比較，只比形狀）。"""
    paths = set()
    for key, value in payload.items():
        if isinstance(value, dict):
            paths |= _leaf_paths(value, prefix + (key,))
        else:
            paths.add(prefix + (key,))
    return paths

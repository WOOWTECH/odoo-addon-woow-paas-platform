# -*- coding: utf-8 -*-
"""#1488：helm values 白名單 fail-closed。

``CloudAppTemplate._filter_allowed_helm_values`` 是租戶 values 進 helm 的唯一閘
（開通 ``_create_service``：被拒的鍵靜默丟棄；儲存設定 ``_update_service``：有被拒
的鍵就整筆拒絕）。舊版在 specs 為 null／``{}``／``{"required": [], "optional": []}``
時走「allow all（backward compatibility）」——正式站 7 個公開 mcp-*、pi-agent、
ha-full（另有隱藏的 haos、ha-paas、ha-full-apporo）全落在這條，租戶可經平台 API
覆寫 chart 的 securityContext／image／resources／serviceAccount／tolerations 等。

守線：

1. specs 沒有任何允許的鍵（null、``{}``、空陣列、``null``／非 dict、解析失敗）⇒
   租戶 values **全部拒絕**，rejected 列出每一個鍵，記一行 warning。
2. 有宣告鍵的範本行為不變：只放行宣告的鍵；比對單位是 values 的頂層鍵對 spec
   ``key`` 字串的精確比對（巢狀形狀／更深一層 dot-path 不等於宣告字串 ⇒ 拒絕；
   宣告鍵的值本身不檢查形狀）——照現有實作實測鎖住。
3. 遍歷所有 seed 範本：租戶送平台控制的鍵（securityContext／image／resources…，
   巢狀與 dot-path 兩種形狀）一律被拒；任何範本都不得把這些鍵宣告成租戶可設。
4. 端到端：mcp-odoo（seed、specs 空）開通時夾帶攻擊值 ⇒ 開通成功但值被丟棄；
   開通頁照常夾帶的 ``helm_default_values`` 也只是被丟棄（存值與送 ``{}`` 完全相同，
   正常流程不受影響）；儲存設定夾帶 ⇒ 整筆拒絕、不寫值、不升級。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

_TEMPLATE = 'woow_paas_platform.cloud_app_template'
_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_TEMPLATE_LOGGER = 'odoo.addons.woow_paas_platform.models.cloud_app_template'

# 平台刻意控制、不得讓租戶覆寫的 chart 值（mcp-server chart 0.1.5 會直接 render 進
# workload；其他自家 chart 也有同名鍵）。巢狀與扁平 dot-path 兩種形狀都送——前端送
# 的是 dot-path，直接打 API 則可以送任何形狀。
_PLATFORM_CONTROLLED_VALUES = {
    'securityContext': {'privileged': True, 'runAsUser': 0},
    'securityContext.privileged': True,
    'podSecurityContext': {'runAsUser': 0},
    'image': {'repository': 'attacker.example/evil', 'tag': 'latest'},
    'image.repository': 'attacker.example/evil',
    'resources': {'limits': {'cpu': '64', 'memory': '256Gi'}},
    'resources.limits.cpu': '64',
    'serviceAccount': {'create': True, 'automount': True},
    'serviceAccount.name': 'default',
    'tolerations': [{'operator': 'Exists'}],
    'nodeSelector': {'kubernetes.io/hostname': 'ubuntuserver-59mqj'},
    'affinity': {'nodeAffinity': {}},
    'hostNetwork': True,
    'networkPolicy': {'enabled': False},
    'networkPolicy.enabled': False,
    'extraEnv': [{'name': 'X', 'value': 'y'}],
}

# #1488 修正時 specs 為空白／null 的 seed 範本（正式站與 develop seed 一致）。
_EMPTY_SPEC_SEED_XMLIDS = (
    'cloud_app_mcp_hermes',
    'cloud_app_mcp_odoo',
    'cloud_app_mcp_odoo_manage',
    'cloud_app_mcp_n8n',
    'cloud_app_mcp_k8s',
    'cloud_app_mcp_vk',
    'cloud_app_mcp_opendesign',
    'cloud_app_pi_agent',
    'cloud_app_ha_full',
    'cloud_app_ha_full_apporo',
    'cloud_app_haos',
    'cloud_app_ha_paas',
)


def _make_template(env, slug, specs):
    vals = {
        'name': 'Whitelist 1488 %s' % slug,
        'slug': slug,
        'category': 'web',
        'helm_repo_url': '',
        'helm_chart_name': 'oci://example/whitelist-1488',
        'helm_chart_version': '0.1.0',
        'is_active': True,
    }
    if specs is not None:
        vals['helm_value_specs'] = specs
    return env[_TEMPLATE].create(vals)


@tagged('post_install', '-at_install')
class TestWhitelistFailClosedWithoutDeclaredKeys(TransactionCase):
    """specs 沒有任何允許的鍵 ⇒ 全部拒絕（舊版：全部放行）。"""

    _VALUES = {
        'securityContext': {'privileged': True},
        'image.repository': 'attacker.example/evil',
        'harmlessLookingKey': 'x',
    }

    def _assert_rejects_everything(self, tpl, label):
        # 收下 log：fail-closed 的 warning，以及解析失敗時 parser 自己的 ERROR
        # （不收的話 ERROR 會流進 Odoo 的測試 log）。
        with self.assertLogs(_TEMPLATE_LOGGER, level='WARNING'):
            filtered, rejected = tpl._filter_allowed_helm_values(dict(self._VALUES))
        self.assertEqual(filtered, {}, '%s：不得放行任何租戶值（fail-open 回歸）' % label)
        self.assertEqual(
            sorted(rejected), sorted(self._VALUES),
            '%s：rejected 必須列出每一個鍵（儲存設定的錯誤訊息靠它）' % label)

    def test_null_specs_rejects_all(self):
        """欄位沒填（DB NULL，正式站 ha-paas 的形態）。"""
        tpl = _make_template(self.env, 'wl1488-null', None)
        self.assertFalse(tpl.helm_value_specs, '前置：specs 欄位必須是空的')
        self._assert_rejects_everything(tpl, 'specs=NULL')

    def test_empty_object_specs_rejects_all(self):
        tpl = _make_template(self.env, 'wl1488-obj', '{}')
        self._assert_rejects_everything(tpl, 'specs={}')

    def test_empty_lists_specs_rejects_all(self):
        """7 個公開 mcp-*、pi-agent、ha-full 的實際形態。"""
        tpl = _make_template(
            self.env, 'wl1488-lists', json.dumps({'required': [], 'optional': []}))
        self._assert_rejects_everything(tpl, 'specs={"required": [], "optional": []}')

    def test_degenerate_specs_shapes_reject_all(self):
        """沒有可用的鍵的其他形態：null／非 dict／區段為 null／解析失敗。"""
        for i, raw in enumerate((
                'null',
                '[]',
                '["securityContext"]',          # 不是 dict：不承認裡面的字串
                '{"required": null, "optional": null}',
                '{"required": [{"label": "no key"}], "optional": [{"key": ""}]}',
                '{not json',
        )):
            with self.subTest(specs=raw):
                tpl = _make_template(self.env, 'wl1488-deg-%d' % i, raw)
                self._assert_rejects_everything(tpl, 'specs=%s' % raw)

    def test_rejection_logs_one_warning(self):
        tpl = _make_template(
            self.env, 'wl1488-log', json.dumps({'required': [], 'optional': []}))
        with self.assertLogs(_TEMPLATE_LOGGER, level='WARNING') as logs:
            tpl._filter_allowed_helm_values(dict(self._VALUES))
        hits = [line for line in logs.output if '#1488' in line]
        self.assertEqual(len(hits), 1, logs.output)
        self.assertIn(tpl.name, hits[0])

    def test_empty_values_are_still_a_noop(self):
        """沒送值不是違規：``None``／``{}`` 照舊回空、不記 warning（ConfigurationTab
        改 addon、只改其他欄位都會走到 ``values={}``）。"""
        tpl = _make_template(
            self.env, 'wl1488-noop', json.dumps({'required': [], 'optional': []}))
        for values in (None, {}):
            with self.subTest(values=values):
                self.assertEqual(tpl._filter_allowed_helm_values(values), ({}, []))


@tagged('post_install', '-at_install')
class TestWhitelistWithDeclaredKeysUnchanged(TransactionCase):
    """有宣告鍵的範本：行為與 #1488 之前相同。"""

    def setUp(self):
        super().setUp()
        self.tpl = _make_template(self.env, 'wl1488-keys', json.dumps({
            'required': [{'key': 'auth.apiKey', 'label': 'API Key', 'type': 'password'}],
            'optional': [
                {'key': 'app.flag', 'label': 'Flag', 'type': 'boolean'},
                'plain.string.key',             # 字串形態的 spec 也算宣告
                'parent',                       # 宣告一個非葉節點
            ],
        }))

    def test_only_declared_keys_pass(self):
        values = {
            'auth.apiKey': 'k',
            'app.flag': True,
            'plain.string.key': 'v',
            'securityContext': {'privileged': True},
            'image.repository': 'attacker.example/evil',
        }
        filtered, rejected = self.tpl._filter_allowed_helm_values(values)
        self.assertEqual(filtered, {
            'auth.apiKey': 'k', 'app.flag': True, 'plain.string.key': 'v'})
        self.assertEqual(sorted(rejected), ['image.repository', 'securityContext'])

    def test_all_declared_keys_means_nothing_rejected(self):
        values = {'auth.apiKey': 'k', 'app.flag': False}
        self.assertEqual(self.tpl._filter_allowed_helm_values(values), (values, []))

    def test_nested_key_matching_is_exact_on_top_level_keys(self):
        """比對單位是 values 的**頂層鍵**對宣告字串的精確比對（照現有實作實測）。"""
        cases = (
            # 巢狀形狀不等於宣告的 dot-path 字串 ⇒ 拒絕
            ({'auth': {'apiKey': 'k'}}, {}, ['auth']),
            # 比宣告更深一層的 dot-path ⇒ 拒絕
            ({'auth.apiKey.extra': 'k'}, {}, ['auth.apiKey.extra']),
            # 宣告的是父節點時，子路徑 dot-path 不等於宣告字串 ⇒ 拒絕
            ({'parent.child': 1}, {}, ['parent.child']),
            # 宣告鍵的值本身不檢查形狀：整個值原樣放行（之後在宣告路徑底下展開）
            ({'auth.apiKey': {'nested': 1}}, {'auth.apiKey': {'nested': 1}}, []),
            ({'parent': {'child': 1}}, {'parent': {'child': 1}}, []),
        )
        for values, expected_filtered, expected_rejected in cases:
            with self.subTest(values=values):
                filtered, rejected = self.tpl._filter_allowed_helm_values(values)
                self.assertEqual(filtered, expected_filtered)
                self.assertEqual(sorted(rejected), expected_rejected)


@tagged('post_install', '-at_install')
class TestSeedTemplatesRejectPlatformControlledKeys(TransactionCase):
    """守線：所有 seed 範本都不讓租戶改平台控制的鍵（不論 specs 有沒有內容）。"""

    def _templates(self):
        return self.env[_TEMPLATE].with_context(active_test=False).search([])

    def test_seed_templates_with_empty_specs_are_present(self):
        """前置（防假綠）：#1488 當時 specs 空白的 seed 範本都在、且確實落在 fail-closed
        分支。哪天其中一個補上了 specs，把它從清單移掉即可——它照樣受下一條守線約束。"""
        for xmlid in _EMPTY_SPEC_SEED_XMLIDS:
            with self.subTest(template=xmlid):
                tpl = self.env.ref('woow_paas_platform.%s' % xmlid)
                self.assertFalse(
                    tpl._tenant_value_keys(),
                    '%s 已宣告租戶鍵；請更新本清單' % xmlid)

    def test_every_template_rejects_platform_controlled_values(self):
        templates = self._templates()
        self.assertTrue(templates, '沒有任何 seed 範本，這條守線會空轉')
        for tpl in templates:
            with self.subTest(template=tpl.slug or tpl.name):
                declared = tpl._tenant_value_keys()
                leaked = declared & set(_PLATFORM_CONTROLLED_VALUES)
                self.assertFalse(
                    leaked,
                    '平台控制的鍵不得宣告成租戶可設：%s' % sorted(leaked))
                with self.assertLogs(_TEMPLATE_LOGGER, level='WARNING'):
                    filtered, rejected = tpl._filter_allowed_helm_values(
                        dict(_PLATFORM_CONTROLLED_VALUES))
                self.assertEqual(filtered, {})
                self.assertEqual(sorted(rejected), sorted(_PLATFORM_CONTROLLED_VALUES))


@tagged('post_install', '-at_install')
class TestMcpTemplateEndToEnd1488(TransactionCase):
    """mcp-odoo（真 seed，specs 空）經 controller 開通與儲存設定。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo')
        self.plan = self.env.ref('woow_paas_platform.plan_mcp_odoo_monthly')
        self.assertIn(self.plan, self.tpl.subscription_plan_ids, '前置：seed 方案掛在範本上')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Whitelist 1488 workspace',
        })
        self.member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Whitelist 1488 workspace admin',
            'login': 'whitelist-1488-workspace-admin',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id,
            'user_id': self.member.id,
            'role': 'admin',
        })

    def _create(self, name, values):
        # 呼叫者身分＝本 workspace 的 internal admin 成員（同 test_mcp_admin_password）。
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.member))):
            result = self.controller._create_service(
                workspace=self.workspace, template_id=self.tpl.id, name=name,
                values=values, plan_id=self.plan.id)
        self.assertTrue(result.get('success'), result.get('error'))
        return self.env['woow_paas_platform.cloud_service'].browse(result['data']['id'])

    def test_create_drops_platform_controlled_values(self):
        defaults = json.loads(self.tpl.helm_default_values)
        baseline = self._create('wl1488-baseline', {})._helm_values_dict()
        stored = self._create(
            'wl1488-attack', dict(_PLATFORM_CONTROLLED_VALUES))._helm_values_dict()
        self.assertEqual(stored, baseline, '開通夾帶的攻擊值必須整批丟棄')
        self.assertEqual(stored.get('image'), defaults.get('image'))
        for key in ('securityContext', 'podSecurityContext', 'serviceAccount',
                    'tolerations', 'nodeSelector', 'affinity', 'hostNetwork',
                    'networkPolicy', 'extraEnv'):
            self.assertEqual(stored.get(key), defaults.get(key), key)

    def test_create_with_frontend_defaults_echo_is_unchanged(self):
        """internal 開通頁對 specs 空的範本會把 ``helm_default_values`` 整包送回來
        （AppConfigurationPage.initializeHelmValues）。fail-closed 後它們被丟棄，部署值
        仍是範本預設——與送 ``{}`` 的結果逐位元相同。"""
        echo = json.loads(self.tpl.helm_default_values)
        self.assertTrue(echo, '前置：範本要有預設值')
        baseline = self._create('wl1488-empty', {})._helm_values_dict()
        stored = self._create('wl1488-echo', echo)._helm_values_dict()
        self.assertEqual(stored, baseline)

    def test_update_rejects_and_writes_nothing(self):
        svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'wl1488-update', 'workspace_id': self.workspace.id,
            'template_id': self.tpl.id, 'state': 'running',
            'helm_release_name': 'svc-wl1488', 'helm_namespace': 'paas-ws-wl1488',
            'helm_values': self.tpl.helm_default_values,
        })
        before = svc._helm_values_dict()
        client = MagicMock()
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            result = self.controller._update_service(
                svc, values={'securityContext': {'privileged': True},
                             'image.repository': 'attacker.example/evil'},
                version=None)
        self.assertFalse(result.get('success'), result)
        self.assertIn('Unauthorized configuration keys', result.get('error', ''))
        self.assertIn('securityContext', result.get('error', ''))
        client.upgrade_release.assert_not_called()
        svc.invalidate_recordset()
        self.assertEqual(svc._helm_values_dict(), before)
        self.assertEqual(svc.state, 'running')

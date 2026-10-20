# -*- coding: utf-8 -*-
"""ha-full 換 chart home-assistant-full 0.1.6：使用者 2026-10-07 的決定（守線）。

chart 0.1.6（woow-paas-charts #130）一次帶了五個修正，使用者只採用一部分：

- 採用（chart 預設）：/media 進資料磁碟（#1444）、egress 只放 DNS＋公網＋同工作區
  （#1436）、zeroconf/ssdp 停用＋擋多播（#1441，**暫時止血**）、httpGet 探針、
  ephemeral-storage。
- 不採用（範本明寫 false）：登入失敗封 IP（#1446，``haProxy.ipBanEnabled``）、核心設定
  搬進 .storage（#1447，``seed.unlockCoreConfig``）、平台給初始密碼（#1442，
  ``has_admin_gui`` 維持 False）。
- 資料磁碟 10G → 20G（只對新開通；既有服務不擴容，另行決定）。#1637（使用者 2026-10-10「按需分配，
  最大給到 30G」）改回 10G：正式站 8 台最多用 0.45G。10-07～10-10 之間開通的 20Gi 服務照舊（不縮）。

本檔另外守兩件「既有服務」的事——它們都不是改範本就會自動對的：

1. 既有服務 Redeploy **不會**改 PVC 大小（不擴也不縮）：服務的 ``helm_values`` 是開通當下的快照，
   Redeploy 只換 image（#1408）；``allocated_storage_gb``（工作區配額）也是開通時寫入的值。
2. 既有服務 Redeploy **會**吃到範本的 ``ipBanEnabled=false``／``unlockCoreConfig=false``：
   快照裡沒有這兩個鍵，不補的話 0.1.6 的 chart 預設 true 會生效
   （``cloud_service._inject_template_policy_keys``）。
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _TEMPLATE_POLICY_VALUE_PATHS, _internal_provision
from .operator_contract import operator_client

_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)

# 0.1.5 時代開通的 ha-full 服務存下的 helm_values 形狀（正式站既有服務即此形狀）：
# 10Gi、沒有 ipBanEnabled／unlockCoreConfig。image 用舊 tag，順便確認 #1408 照常換。
_LEGACY_SNAPSHOT = {
    'image': {
        'repository': 'jcr-prod.woowtech.io/woow-paas-docker-local/woowtech-ha-full',
        'tag': '20260807.0054@sha256:' + '0' * 64,
    },
    'persistence': {'mountPath': '/config', 'size': '10Gi'},
    'service': {'port': 8123, 'targetPort': 8123},
    'ingress': {'enabled': False, 'host': '', 'className': ''},
    'haProxy': {
        'seedConfiguration': True, 'useXForwardedFor': True,
        'trustedProxies': ['10.42.0.0/16'], 'fragmentManaged': True,
    },
    'resources': {
        'requests': {'cpu': '500m', 'memory': '1Gi'},
        'limits': {'cpu': '2', 'memory': '2Gi'},
    },
}


def _ha_full(env):
    return env.ref('woow_paas_platform.cloud_app_ha_full')


@tagged('post_install', '-at_install')
class TestHaFull016TemplateDecisions(TransactionCase):
    """範本本身：chart pin 與使用者決定的逐項對照。"""

    def _values(self):
        return json.loads(_ha_full(self.env).helm_default_values)

    def test_chart_pinned_0_1_10(self):
        # 0.1.10＝charts 整合包 #254；下面「不採用」的兩個鍵在 0.1.10 預設仍是 true，範本照舊明寫 false。
        self.assertEqual(_ha_full(self.env).helm_chart_version, '0.1.10')

    def test_ip_ban_off_1446(self):
        """#1446 不做：chart 0.1.6 預設 true，範本必須明寫 false。"""
        self.assertIs(self._values()['haProxy']['ipBanEnabled'], False)

    def test_core_config_migration_off_1447(self):
        """#1447 不做：chart 0.1.6 預設 true，範本必須明寫 false。"""
        self.assertIs(self._values()['seed']['unlockCoreConfig'], False)

    def test_no_platform_owner_password_1442(self):
        """#1442 不做：平台不給密碼 ⇒ chart 不 render owner Secret、不跑 bootstrap。"""
        tpl = _ha_full(self.env)
        self.assertFalse(tpl.has_admin_gui)
        values = self._values()
        self.assertNotIn('owner', values)
        self.assertNotIn('config', values, 'config.sensitive 只能走敏感通道，且本範本不送')

    def test_hardening_defaults_left_to_chart(self):
        """#1444／#1436／#1441／probe／ephemeral-storage 採 chart 0.1.6 預設：範本不覆寫。

        覆寫這些鍵只會多一份會漂的複本；#1441 的止血日後由全平台方案取代時才在範本
        明寫。``resources`` 只寫 cpu／memory，helm deep-merge 保留 chart 的
        ephemeral-storage。
        """
        values = self._values()
        for key in ('networkPolicy', 'probe', 'startupProbe', 'livenessProbe',
                    'readinessProbe'):
            self.assertNotIn(key, values, key)
        self.assertNotIn('media', values['persistence'])
        self.assertEqual(set(values['seed']), {'unlockCoreConfig'})
        for section in ('requests', 'limits'):
            self.assertEqual(
                set(values['resources'][section]), {'cpu', 'memory'}, section)

    def test_disk_10g_everywhere_shown(self):
        """資料磁碟 10G（#1637 改回）：PVC、min_storage_gb、卡片說明一致。"""
        tpl = _ha_full(self.env)
        self.assertEqual(self._values()['persistence']['size'], '10Gi')
        self.assertEqual(tpl.min_storage_gb, 10)
        self.assertIn('10 GB 磁碟', tpl.description)
        self.assertNotIn('20 GB', tpl.description)
        self.assertNotIn('20 GB', tpl.full_description)

    def test_full_description_matches_decisions(self):
        """客戶自己建管理者、/media 在磁碟上；不提封鎖 IP 與初始密碼。"""
        text = _ha_full(self.env).full_description
        self.assertIn('建立你自己的管理者帳號', text)
        self.assertIn('/media', text)
        self.assertIn('10 GB', text)
        for word in ('封鎖', '封 IP', '初始密碼'):
            self.assertNotIn(word, text, word)

    def test_new_service_allocation_is_10(self):
        """新開通的工作區配額用量：plan 不設 cs_storage_gb ⇒ 取 min_storage_gb＝10（#1637）。"""
        tpl = _ha_full(self.env)
        plan = self.env.ref('woow_paas_platform.plan_ha_full_monthly')
        vals = self.env['woow_paas_platform.cloud_service']._allocated_vals_from(tpl, plan)
        self.assertEqual(vals['allocated_storage_gb'], 10)


@tagged('post_install', '-at_install')
class TestHaFullExistingServiceRedeploy016(TransactionCase):
    """既有 ha-full 服務（0.1.5 時代的快照）Redeploy 到 0.1.6 的行為。"""

    def setUp(self):
        super().setUp()
        self.template = _ha_full(self.env)
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'ha-full 016 Workspace',
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create({
                'name': 'svc-ha016',
                'workspace_id': workspace.id,
                'template_id': self.template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-test',
                'helm_release_name': 'svc-ha016',
                'helm_chart_version': '0.1.5',
                'allocated_vcpu': 1,
                'allocated_ram_gb': 1.0,
                'allocated_storage_gb': 10,
                'helm_values': json.dumps(_LEGACY_SNAPSHOT),
            })

    def _redeploy(self):
        client = operator_client(
            get_release={'name': 'svc-ha016'},
            upgrade_release={'revision': 7, 'chart_version': '0.1.10'},
        )
        with patch(_CLIENT_PATH, return_value=client):
            self.service.action_redeploy()
        return client.upgrade_release.call_args.kwargs

    def _stored(self):
        self.service.invalidate_recordset()
        return json.loads(self.service.sudo().helm_values)

    def test_redeploy_keeps_existing_pvc_size(self):
        """🔴 不改 PVC：送進 helm 的 persistence.size 仍是服務自己的 10Gi。"""
        sent = self._redeploy()
        self.assertEqual(sent['version'], '0.1.10')
        self.assertTrue(sent['reset_values'])
        self.assertEqual(sent['values']['persistence']['size'], '10Gi')
        self.assertEqual(self._stored()['persistence']['size'], '10Gi')

    def test_redeploy_never_shrinks_a_20g_service(self):
        """🔴 #1637：10-07～10-10 開通的 20Gi 服務 Redeploy 仍送 20Gi（範本已改回 10Gi；PVC 縮小
        會讓整次 helm upgrade 被 API server 拒絕）。"""
        snapshot = json.loads(json.dumps(_LEGACY_SNAPSHOT))
        snapshot['persistence']['size'] = '20Gi'
        self.service._apply_helm_values(snapshot)
        self.service.sudo().write({'allocated_storage_gb': 20})
        sent = self._redeploy()
        self.assertEqual(sent['values']['persistence']['size'], '20Gi')
        self.assertEqual(self._stored()['persistence']['size'], '20Gi')
        self.service.invalidate_recordset()
        self.assertEqual(self.service.allocated_storage_gb, 20)

    def test_redeploy_keeps_workspace_quota_usage(self):
        """工作區配額用量（allocated_storage_gb）是開通時寫入的，Redeploy 不重算。"""
        self._redeploy()
        self.service.invalidate_recordset()
        self.assertEqual(self.service.allocated_storage_gb, 10)
        cpu, mem, storage = self.service._compute_ns_quota(self.service.workspace_id)
        self.assertEqual(storage, '100Gi', '單台 10G×3＝30Gi，低於 floor 100Gi')

    def test_redeploy_applies_policy_keys_from_template(self):
        """快照沒有 ipBanEnabled／unlockCoreConfig，Redeploy 必須送範本的 false。"""
        sent = self._redeploy()['values']
        self.assertIs(sent['haProxy']['ipBanEnabled'], False)
        self.assertIs(sent['seed']['unlockCoreConfig'], False)
        # 既有的 haProxy 四閘不得被覆蓋掉
        self.assertTrue(sent['haProxy']['useXForwardedFor'])
        self.assertEqual(sent['haProxy']['trustedProxies'], ['10.42.0.0/16'])

    def test_redeploy_sends_no_owner_password(self):
        """has_admin_gui=False ⇒ 不送 config.sensitive ⇒ chart 不建 owner。"""
        kwargs = self._redeploy()
        self.assertFalse(kwargs.get('sensitive_values'))
        self.assertNotIn('config', kwargs['values'])

    def test_redeploy_still_refreshes_image(self):
        """#1408 照常：image 換成範本現在釘的。"""
        sent = self._redeploy()['values']
        tpl_tag = json.loads(self.template.helm_default_values)['image']['tag']
        self.assertEqual(sent['image']['tag'], tpl_tag)


@tagged('post_install', '-at_install')
class TestTemplatePolicyKeysInjector(TransactionCase):
    """``_inject_template_policy_keys`` 的規則（不經 Redeploy）。"""

    def setUp(self):
        super().setUp()
        self.CloudService = self.env['woow_paas_platform.cloud_service']

    def test_template_wins_over_service_value(self):
        values = {'haProxy': {'ipBanEnabled': True, 'useXForwardedFor': True},
                  'seed': {'coreConfig': {'externalUrl': 'https://x'}}}
        out = self.CloudService._inject_template_policy_keys(values, _ha_full(self.env))
        self.assertIs(out['haProxy']['ipBanEnabled'], False)
        self.assertTrue(out['haProxy']['useXForwardedFor'])
        self.assertIs(out['seed']['unlockCoreConfig'], False)
        self.assertEqual(out['seed']['coreConfig'], {'externalUrl': 'https://x'})

    def test_persistence_size_is_not_a_policy_key(self):
        """PVC 大小刻意不在清單：範本調大不得變成既有服務的線上擴容。"""
        for slug, paths in _TEMPLATE_POLICY_VALUE_PATHS.items():
            self.assertNotIn(('persistence', 'size'), paths, slug)
        values = {'persistence': {'size': '10Gi'}}
        out = self.CloudService._inject_template_policy_keys(values, _ha_full(self.env))
        self.assertEqual(out['persistence']['size'], '10Gi')

    def test_noop_for_other_templates(self):
        apporo = self.env.ref('woow_paas_platform.cloud_app_ha_full_apporo')
        values = {'haProxy': {'useXForwardedFor': True}}
        out = self.CloudService._inject_template_policy_keys(dict(values), apporo)
        self.assertEqual(out, values, '封存的 APPORO 卡不在清單，不得被動到')

    def test_key_absent_in_template_is_left_alone(self):
        tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'policy no-op',
            'slug': 'policy-noop-016',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/x',
            'helm_chart_version': '0.0.1',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
            'helm_default_values': json.dumps({'haProxy': {}}),
        })
        with patch.dict(_TEMPLATE_POLICY_VALUE_PATHS,
                        {'policy-noop-016': [('haProxy', 'ipBanEnabled')]}):
            out = self.CloudService._inject_template_policy_keys(
                {'haProxy': {'ipBanEnabled': True}}, tpl)
        self.assertIs(out['haProxy']['ipBanEnabled'], True)

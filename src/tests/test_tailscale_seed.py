# -*- coding: utf-8 -*-
"""VPN 節點（相容 Tailscale）上架 seed 的守線斷言（範本先隱藏）。

契約來源：woow-paas-charts charts/tailscale 0.1.0（#125）——StatefulSet ×1、userspace
tailscaled（uid 1000）、唯一入口 Service 8080 → nginx Basic auth（帳號固定 admin）→
``tailscale web``（127.0.0.2:8088，唯讀）；``tailscale serve`` TCP 轉發只接受同 namespace 的
服務；exit node／app connector／subnet route／SSH／Funnel 一律不開（values 裡沒有這些鍵）。
image：paas-odoo-ci ``tailscale/``（#240，官方 1.102.5＋衍生自 podman 版的 entrypoint）。

核心守線：
1. **只有密碼、可自設**（同 code-server）：admin_password_settable=True、沒有
   admin_username_helm_path；htpasswd 是 Secret volume ⇒ 重設不改 pod template。
2. **定位＝工作區私有入口**（PLAN Q2／Q3）：租戶旋鈕不得出現 exit node／路由／SSH 類的鍵——
   NetworkPolicy 是 tailnet 經節點能碰到哪裡的唯一護欄，開了等於把叢集網段交給 tailnet。
3. **authKey 是 password 型、不在 config.sensitive**（平台會整棵剝掉，EMQX 0.1.1 教訓）。
4. **規格對帳**：1 vCPU／0.5 GB／1 GB——template min_*、plan cs_*、chart 唯一一顆 PVC 三處一致。
5. **上架即雙釘**：目前 image 是 PR 分支 build 的候選（暫定），#240 合併後從 main 重 build、
   核可再換；換之前保持隱藏。
"""
import json
import re

from odoo.tests.common import TransactionCase, tagged

JCR = 'jcr-prod.woowtech.io/woow-paas-docker-local/'
TAILSCALE_CHART = 'oci://%stailscale' % JCR
PINNED_TAG_RE = r'^\d{8}\.\d{4}@sha256:[0-9a-f]{64}$'
# 不得出現在租戶旋鈕裡的能力（chart 強制關閉；租戶能填就等於能把 pod 的出網範圍交給 tailnet）。
FORBIDDEN_KNOB_RE = re.compile(
    r'exit|advertise|route|connector|ssh|funnel|socks|proxy|accept', re.IGNORECASE)


@tagged('post_install', '-at_install')
class TestTailscaleSeed(TransactionCase):

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_tailscale')

    def _values(self):
        return json.loads(self._tpl().helm_default_values)

    def _optional(self):
        return {s['key']: s for s in self._tpl()._parse_helm_value_specs()['optional']}

    def test_marketplace_fields(self):
        tpl = self._tpl()
        self.assertEqual(tpl.slug, 'tailscale')
        self.assertEqual(tpl.name, 'VPN 節點（相容 Tailscale）',
                         'Tailscale 是商標：中性名稱＋「相容」字樣（PLAN Q16）')
        self.assertEqual(tpl.category, 'devops')
        self.assertEqual(tpl.default_port, 8080, 'nginx 入口（web UI）是唯一對外入口')
        self.assertTrue(tpl.ingress_enabled)
        self.assertEqual(tpl.max_per_partner, 0)
        self.assertFalse(tpl.mcp_enabled)
        self.assertEqual(tpl.workload_type, 'container')

    def test_chart_oci_pin(self):
        tpl = self._tpl()
        self.assertEqual(tpl.helm_repo_url or '', '')
        self.assertEqual(tpl.helm_chart_name, TAILSCALE_CHART)
        self.assertEqual(tpl.helm_chart_version, '0.1.0',
                         'helm_chart_version 留空會被供給閘門靜默擋在購買前（#552）')

    def test_service_port_lockstep(self):
        self.assertEqual(self._values()['service']['port'], self._tpl().default_port)

    def test_password_only_and_settable(self):
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui)
        self.assertEqual(tpl.admin_password_carrier, 'helm_secret')
        self.assertTrue(tpl.admin_reset_supported)
        self.assertTrue(tpl.admin_password_settable)
        self.assertTrue(tpl.admin_credentials_settable)
        self.assertFalse(tpl.admin_username_helm_path, '帳號固定 admin（chart 沒有帳號旋鈕）')

    def test_image_from_jcr_and_double_pinned(self):
        image = self._values()['image']
        self.assertEqual(image['repository'], JCR + 'tailscale')
        self.assertRegex(image['tag'], PINNED_TAG_RE,
                         'image 一律 tag@sha256 雙釘（mutable tag 不可重現）')

    def test_hidden_until_listed(self):
        """image 從 main 重 build 並核可、正式站驗證前不上貨架；翻 is_hidden 時 pin 必須在位。"""
        tpl = self._tpl()
        self.assertTrue(tpl.is_active)
        self.assertFalse(tpl.conceal_instances, 'hidden-only（曝光層），實例層不設')
        if not tpl.is_hidden:
            self.assertRegex(self._values()['image']['tag'], PINNED_TAG_RE)

    def test_no_tunnel_and_no_gateway(self):
        tpl = self._tpl()
        self.assertFalse(tpl.has_public_tunnel_address, '沒有 ngrok sidecar')
        self.assertEqual(tpl._gateway_external_path(), '', '沒有 #530 gateway')

    def test_spec_matches_plan_and_pvc(self):
        tpl = self._tpl()
        self.assertEqual(tpl.min_vcpu, 1)
        self.assertEqual(tpl.min_ram_gb, 0.5)
        self.assertEqual(tpl.min_storage_gb, 1)
        pvc = int(self._values()['persistence']['size'].rstrip('Gi'))
        self.assertEqual(pvc, tpl.min_storage_gb, '節點身分那顆 PVC＝min_storage_gb（quota 對帳）')
        plans = tpl.sudo().subscription_plan_ids
        self.assertTrue(plans)
        for plan in plans:
            self.assertEqual(plan.cs_vcpu, tpl.min_vcpu, plan.name)
            self.assertEqual(plan.cs_ram_gb, tpl.min_ram_gb, plan.name)
            self.assertEqual(plan.cs_storage_gb, pvc, plan.name)

    def test_half_gb_plan_overlays_as_mebibytes(self):
        """cs_ram_gb=0.5 不是整數 GB：overlay 要寫 512Mi，不能變成 0Gi。"""
        plan = self.env.ref('woow_paas_platform.plan_tailscale_monthly')
        values = self.env['woow_paas_platform.cloud_service']._apply_plan_spec_overlay(
            {}, self._tpl(), plan)
        self.assertEqual(values['resources']['limits']['memory'], '512Mi')
        self.assertEqual(values['resources']['requests']['memory'], '512Mi')
        self.assertEqual(values['persistence']['size'], '1Gi')

    def test_plans_linked_and_priced(self):
        tpl = self._tpl()
        monthly = self.env.ref('woow_paas_platform.plan_tailscale_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_tailscale_yearly')
        self.assertEqual(set(tpl.sudo().subscription_plan_ids.ids), {monthly.id, yearly.id})
        self.assertEqual(monthly.sh_plan_price, 400)
        self.assertEqual(yearly.sh_plan_price, 4000)
        self.assertEqual(yearly.sh_plan_price, monthly.sh_plan_price * 10, '年繳＝10 個月')
        for plan in (monthly, yearly):
            self.assertEqual(plan.service_type, 'cloud_service')
            self.assertTrue(plan.sh_trial)
            self.assertEqual(plan.sh_trial_duration, 7)
            self.assertEqual(plan.sh_trial_unit, 'day')
        for product, plan in (
            (self.env.ref('woow_paas_platform.product_tailscale_monthly'), monthly),
            (self.env.ref('woow_paas_platform.product_tailscale_yearly'), yearly),
        ):
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, plan.sh_plan_price)
            self.assertTrue(product.sh_product_subscribe)
            self.assertFalse(product.taxes_id, 'seed 明寫 taxes_id=[]，不吃 company 預設')

    def test_tenant_knobs_are_app_level_only(self):
        specs = self._tpl()._parse_helm_value_specs()
        self.assertEqual(specs['required'], [], '開通不需要租戶先填任何東西')
        by_key = self._optional()
        self.assertEqual(set(by_key), {
            'tailscale.loginServer',
            'tailscale.authKey',
            'tailscale.hostname',
            'tailscale.tags',
            'tailscale.serve',
            'tailscale.noLogs',
        })
        self.assertEqual(by_key['tailscale.authKey']['type'], 'password',
                         'password 型才會被平台在所有 API 回應中遮罩')
        for key in ('tailscale.loginServer', 'tailscale.hostname', 'tailscale.tags',
                    'tailscale.serve'):
            self.assertEqual(by_key[key]['type'], 'text', key)
        self.assertEqual(by_key['tailscale.noLogs']['type'], 'boolean')
        self.assertIs(by_key['tailscale.noLogs']['default'], True)
        for key in by_key:
            self.assertFalse(re.match(r'^(resources|persistence|replicaCount)\b', key),
                             'CPU/RAM/磁碟由訂閱方案決定；副本數固定 1（身分在單一 RWO PVC）')
            self.assertFalse(key.startswith('config.sensitive'),
                             'config.sensitive 會被平台整棵剝掉，租戶值到不了 chart')
            self.assertFalse(FORBIDDEN_KNOB_RE.search(key.split('.', 1)[1]),
                             '%s：exit node／路由／SSH 類能力不得開放（PLAN Q3）' % key)

    def test_whitelist_filters_forbidden_and_resource_overrides(self):
        """specs 的 key 同時是白名單：硬塞 exit node／子網路由／方案維度都要被擋掉。"""
        allowed, rejected = self._tpl()._filter_allowed_helm_values({
            'tailscale.serve': '8069=svc-1a2b3c4d:80',
            'tailscale.advertiseExitNode': True,
            'tailscale.advertiseRoutes': '10.43.0.0/16',
            'networkPolicy.enabled': False,
            'resources.limits.cpu': '16',
        })
        self.assertEqual(allowed, {'tailscale.serve': '8069=svc-1a2b3c4d:80'})
        self.assertEqual(sorted(rejected), [
            'networkPolicy.enabled', 'resources.limits.cpu',
            'tailscale.advertiseExitNode', 'tailscale.advertiseRoutes'])

    def test_description_states_scope_and_limits(self):
        desc = self._tpl().full_description
        for needle, why in (
                ('DERP', '流量多半經公共中繼，速度有限'),
                ('exit node', '明講不提供 exit node'),
                ('子網路由', '明講不提供 subnet router'),
                ('-headscale:8080', '同工作區 Headscale 的內部網址'),
                ('新機器', '換控制伺服器＝新節點身分'),
                ('清空', 'authkey 登入後清掉'),
                ('帳號固定 admin', '只有密碼可自設'),
        ):
            self.assertIn(needle, desc, why)
        auth = self._optional()['tailscale.authKey']['description']
        for needle in ('一次性', 'tag', '清空'):
            self.assertIn(needle, auth, 'authkey 建議：一次性＋帶 tag＋登入後清空')

    def test_no_public_url_injection(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        self.assertEqual(
            CloudService._inject_allowed_origins({}, 'tailscale', 'https://paas-ts.example'), {})

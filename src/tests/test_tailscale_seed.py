# -*- coding: utf-8 -*-
"""VPN 節點（相容 Tailscale）上架 seed 的守線斷言（2026-10-04 公開上架）。

契約來源：woow-paas-charts charts/tailscale 0.1.2（0.1.1＝#131、0.1.0＝#125）——StatefulSet ×1、userspace
tailscaled（uid 1000）、唯一入口 Service 8080 → nginx Basic auth（帳號＝entrance.basicAuthUsername）→
``tailscale web``（127.0.0.2:8088，唯讀）；``tailscale serve`` TCP 轉發只接受同 namespace 的
服務；exit node／app connector／subnet route／SSH／Funnel 一律不開（values 裡沒有這些鍵）。
image：paas-odoo-ci ``tailscale/``（#240，官方 1.102.5＋衍生自 podman 版的 entrypoint）。

核心守線：
1. **帳號與密碼都可自設**（同 Headscale；0.1.1 起）：admin_username_helm_path＝
   ``entrance.basicAuthUsername``（範本帶預設 admin ⇒ 一次性視窗顯示帳號，.207 正式站「沒有帳號」缺陷）；
   htpasswd 是 Secret volume ⇒ 改帳號／密碼不改 pod template。重設要靠 chart 0.1.1 的
   StatefulSet opt-in label（``paas.woowtech.io/restart-on-credential-reset``）＋operator #1398——
   0.1.0 沒有 Deployment 也沒有 label，operator 找不到可重啟的 workload ⇒ 回退、重設必失敗。
2. **定位＝工作區私有入口**（PLAN Q2／Q3）：租戶旋鈕不得出現 exit node／路由／SSH 類的鍵——
   NetworkPolicy 是 tailnet 經節點能碰到哪裡的唯一護欄，開了等於把叢集網段交給 tailnet。
3. **authKey 是 password 型、不在 config.sensitive**（平台會整棵剝掉，EMQX 0.1.1 教訓）。
4. **規格對帳**：1 vCPU／0.5 GB／1 GB——template min_*、plan cs_*、chart 唯一一顆 PVC 三處一致。
5. **上架即雙釘**：image 為 #240 合併後從 main build 並核可的 20261004.0747（#244）。2026-10-04 公開上架，
   ``test_hidden_until_listed`` 從此是實際生效的守線（``is_hidden=False`` ⟹ image 要 ``tag@sha256``）。
6. **控制平面三選一**（chart 0.1.2，PLAN §10.2）：``tailscale.controlPlane`` select（官方 Tailscale／同工作區
   Headscale／自訂網址，預設官方）。「同工作區 Headscale」的網址由平台注入 ``tailscale.workspaceHeadscaleUrl``
   （``test_workspace_service_url_injection``），那個鍵**不是**租戶欄位、白名單擋掉；只帶網址，preauth key
   仍是租戶自己貼。
"""
import json
import re

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision

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
        self.assertEqual(tpl.helm_chart_version, '0.1.2',
                         'helm_chart_version 留空會被供給閘門靜默擋在購買前（#552）；0.1.1＝'
                         'StatefulSet 帶 restart-on-credential-reset label、帳號可設定（.207 缺陷）')

    def test_service_port_lockstep(self):
        self.assertEqual(self._values()['service']['port'], self._tpl().default_port)

    def test_credentials_helm_secret_and_settable(self):
        tpl = self._tpl()
        self.assertTrue(tpl.has_admin_gui)
        self.assertEqual(tpl.admin_password_carrier, 'helm_secret')
        self.assertTrue(tpl.admin_reset_supported)
        self.assertTrue(tpl.admin_password_settable)
        self.assertTrue(tpl.admin_credentials_settable)
        self.assertEqual(tpl.admin_username_helm_path, 'entrance.basicAuthUsername',
                         '登入要帳號（nginx Basic auth）：沒有帳號路徑時一次性視窗會寫「沒有帳號」（.207 缺陷）')
        self.assertEqual(
            self._values()['entrance']['basicAuthUsername'], 'admin',
            '帳號預設值要在範本裡（_admin_username 讀不到時不猜 chart 預設）')

    def test_one_time_window_shows_the_username(self):
        """_admin_username＝一次性視窗與「管理員帳密」對話框的帳號來源；新服務要讀到 admin。"""
        tpl = self._tpl()
        workspace = self.env['woow_paas_platform.workspace'].create({'name': 'tailscale seed WS'})
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-tailscale-seed',
            'workspace_id': workspace.id,
            'template_id': tpl.id,
            'state': 'running',
            'subdomain': 'svc-tailscale-seed',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-tailscale-seed',
            'helm_values': tpl.helm_default_values,
        })
        self.assertEqual(service._admin_username(), 'admin')
        # 既有服務（建立時範本還沒有這個鍵）讀不到自己的值 ⇒ 退回範本預設，仍是 admin。
        service.helm_values = json.dumps({'service': {'port': 8080}})
        self.assertEqual(service._admin_username(), 'admin')

    def test_image_from_jcr_and_double_pinned(self):
        image = self._values()['image']
        self.assertEqual(image['repository'], JCR + 'tailscale')
        self.assertRegex(image['tag'], PINNED_TAG_RE,
                         'image 一律 tag@sha256 雙釘（mutable tag 不可重現）')

    def test_hidden_until_listed(self):
        """上架 ⟹ image 雙釘（2026-10-04 公開上架；之後若翻回隱藏，這條仍要綠）。"""
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
            'tailscale.controlPlane',
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
        cp = by_key['tailscale.controlPlane']
        self.assertEqual(cp['type'], 'select')
        self.assertEqual(cp['default'], 'official', '預設＝官方 Tailscale（＝0.1.1 行為）')
        self.assertEqual([o['value'] for o in cp['options']], ['official', 'workspace', 'custom'],
                         'chart 0.1.2 只認這三個值（其他值 chart 端不登入）')
        self.assertEqual([o['label'] for o in cp['options']],
                         ['官方 Tailscale', '同工作區 Headscale', '自訂網址'])
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
            'tailscale.workspaceHeadscaleUrl': 'http://evil.example.com',
            'tailscale.advertiseExitNode': True,
            'tailscale.advertiseRoutes': '10.43.0.0/16',
            'networkPolicy.enabled': False,
            'resources.limits.cpu': '16',
        })
        self.assertEqual(allowed, {'tailscale.serve': '8069=svc-1a2b3c4d:80'})
        self.assertEqual(sorted(rejected), [
            'networkPolicy.enabled', 'resources.limits.cpu',
            'tailscale.advertiseExitNode', 'tailscale.advertiseRoutes',
            'tailscale.workspaceHeadscaleUrl'],
            'workspaceHeadscaleUrl 只能由平台注入，租戶不能指向任意控制伺服器（自訂請選 custom）')

    def test_description_states_scope_and_limits(self):
        desc = self._tpl().full_description
        for needle, why in (
                ('DERP', '流量多半經公共中繼，速度有限'),
                ('exit node', '明講不提供 exit node'),
                ('出口節點', '中文也要講：不提供出口節點'),
                ('子網路由', '明講不提供 subnet router'),
                ('-headscale:8080', '同工作區 Headscale 的內部網址'),
                ('新機器', '換控制伺服器＝新節點身分'),
                ('清空', 'authkey 登入後清掉'),
                ('帳號預設 admin', '帳號可改（admin_username_helm_path）'),
                ('重新啟動，約數十秒', '重設帳密會重啟節點（operator #1398 重啟 opt-in StatefulSet）'),
                ('仍是同一台', '節點身分在 PVC，重設後不是新機器'),
                ('最久約 12 分鐘', '同工作區 Headscale 重啟後，靠 chart 0.1.1 的 liveness 看門狗回線'),
                ('從瀏覽器開服務網址', '入口擋 127/8（port-forward 一律 403），網頁介面只能經服務網址開'),
                ('同工作區 Headscale', '控制平面三選一（chart 0.1.2）'),
                ('只帶網址', '平台不跨服務傳 preauth key（使用者拍板：金鑰租戶自己貼）'),
                ('Headplane', 'preauth key 在 Headscale 的網頁後台建'),
                ('最早建立', '工作區有多台 Headscale 時用哪台'),
        ):
            self.assertIn(needle, desc, why)
        self.assertNotIn('不必重啟', desc, '重設帳密會重啟節點，不能再寫「不必重啟」（與管理員帳密對話框一致）')
        cp = self._optional()['tailscale.controlPlane']['description']
        for needle in ('自動帶入', '只帶網址', 'pre-auth key', '新機器'):
            self.assertIn(needle, cp, '控制平面說明：%s' % needle)
        self.assertIn('自訂網址', self._optional()['tailscale.loginServer']['description'],
                      '控制伺服器欄位只在選「自訂網址」時使用')
        auth = self._optional()['tailscale.authKey']['description']
        for needle in ('一次性', 'tag', '清空'):
            self.assertIn(needle, auth, 'authkey 建議：一次性＋帶 tag＋登入後清空')

    def test_no_public_url_injection(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        self.assertEqual(
            CloudService._inject_allowed_origins({}, 'tailscale', 'https://paas-ts.example'), {})

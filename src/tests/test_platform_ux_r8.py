"""R8 商用驗收的平台 UX 修正（#1453／#1454／#1456／#1457）的後端守線。

- #1453：「說明與支援」網址抽成單一設定值（舊的 docs.woowtech.io 不存在）＋ 服務 API
  補 ``template.documentation_url``（配置頁的「View Documentation」原本永遠不出現）。
- #1456：「最後同步於」帶 UTC offset（前端把不帶 offset 的值當當地時間）；reconfigure
  背景執行緒在 poll 早一步把服務推回 running 時仍單調回寫 helm_revision。
- #1457：/woow、OAuth、/device 頁面帶完整安全標頭；公開平台主機全路徑帶 HSTS。

前端（連線狀態三態、方案與帳期卡、訂閱列表標籤、變更方案不顯示試用）沒有 JS 測試框架，
由 PR 描述的瀏覽器實測涵蓋。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.addons.woow_paas_platform import hooks
from odoo.addons.woow_paas_platform.controllers.paas import (
    DEFAULT_HELP_URL,
    HELP_URL_PARAM,
    PaasController,
    _help_url,
)
from odoo.addons.woow_paas_platform.services.security_headers import (
    AIOT_PUBLIC_HOST,
    SECURITY_HEADERS,
    apply_hsts_header,
    apply_security_headers,
    wants_platform_hsts,
    wants_security_headers,
)
from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.tools import config

from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'


class TestHelpUrl(TransactionCase):
    """#1453：網址來源只有一個，設定錯了退回預設而不是變成可執行連結。"""

    def _set(self, raw):
        self.env['ir.config_parameter'].sudo().set_param(HELP_URL_PARAM, raw)

    def test_unset_falls_back_to_default(self):
        self._set(False)
        self.assertEqual(_help_url(self.env), DEFAULT_HELP_URL)

    def test_default_is_not_the_dead_domain(self):
        self.assertNotIn('docs.woowtech.io', DEFAULT_HELP_URL,
                         'docs.woowtech.io 是 NXDOMAIN（R8 #1453）')
        self.assertTrue(DEFAULT_HELP_URL.startswith('https://'))

    def test_configured_https_url_wins(self):
        self._set(' https://help.example.com/paas ')
        self.assertEqual(_help_url(self.env), 'https://help.example.com/paas')

    def test_non_http_values_fall_back(self):
        for raw in ('javascript:alert(1)', 'docs.example.com', 'ftp://x.example.com', 'https://'):
            with self.subTest(raw=raw):
                self._set(raw)
                self.assertEqual(_help_url(self.env), DEFAULT_HELP_URL)

    def test_registered_for_uninstall(self):
        """「模組常數＋get_param()」寫法 repo-lint 掃不到，這條補盲區。"""
        self.assertIn(HELP_URL_PARAM, hooks._CONFIG_PARAMETERS)


class TestSecurityHeaderHelpers(TransactionCase):
    """#1457：路徑／公開主機判準與「不覆蓋既有標頭」。"""

    def test_paths(self):
        for path in ('/woow', '/woow/', '/oauth2/authorize', '/oauth2/token', '/device'):
            with self.subTest(path=path):
                self.assertTrue(wants_security_headers(path))
        for path in ('/', '/web', '/web/login', '/woowfoo', '/devices', '/oauth2',
                     '/api/config', '/blog/help-center-7', '', None):
            with self.subTest(path=path):
                self.assertFalse(wants_security_headers(path))

    def test_existing_headers_are_kept(self):
        headers = {'Content-Security-Policy': "frame-ancestors 'none'"}
        apply_security_headers(headers)
        self.assertEqual(headers['Content-Security-Policy'], "frame-ancestors 'none'")
        for name in SECURITY_HEADERS:
            self.assertIn(name, headers)

    def test_hsts_does_not_lock_subdomains(self):
        """includeSubDomains／preload 是 Cloudflare zone 層的決定（#1457 建議清單）。"""
        hsts = SECURITY_HEADERS['Strict-Transport-Security']
        self.assertIn('max-age=', hsts)
        self.assertNotIn('includeSubDomains', hsts)
        self.assertNotIn('preload', hsts)

    def test_hsts_only_targets_the_public_platform_host(self):
        for host in (AIOT_PUBLIC_HOST, AIOT_PUBLIC_HOST.upper(), f'{AIOT_PUBLIC_HOST}:443',
                     f'{AIOT_PUBLIC_HOST}.'):
            with self.subTest(host=host):
                self.assertTrue(wants_platform_hsts(host))
        for host in ('www.woowtech.io', 'aiot.woowtech.io.evil.example',
                     'aiot.woowtech.io:invalid', '', None):
            with self.subTest(host=host):
                self.assertFalse(wants_platform_hsts(host))

    def test_existing_hsts_is_kept(self):
        headers = {'Strict-Transport-Security': 'max-age=63072000'}
        apply_hsts_header(headers)
        self.assertEqual(headers['Strict-Transport-Security'], 'max-age=63072000')


@tagged('post_install', '-at_install')
class TestWoowPageHelpUrlAndHeaders(HttpCase):
    """/woow 帶完整安全標頭；公開平台主機的每個路徑帶 HSTS。"""

    def setUp(self):
        super().setUp()
        self.env['res.users'].sudo().create({
            'name': 'r8_ux_user', 'login': 'r8_ux_user', 'password': 'r8_ux_user',
            'email': 'r8_ux_user@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def test_woow_page(self):
        self.env['ir.config_parameter'].sudo().set_param(
            HELP_URL_PARAM, 'https://help.example.com/r8')
        self.authenticate('r8_ux_user', 'r8_ux_user')
        res = self.url_open('/woow', timeout=30)
        self.assertEqual(res.status_code, 200, res.text[:200])
        self.assertIn('"woow_help_url": "https://help.example.com/r8"', res.text)
        for name, value in SECURITY_HEADERS.items():
            with self.subTest(header=name):
                self.assertIn(name, res.headers)
        self.assertEqual(res.headers['Strict-Transport-Security'],
                         SECURITY_HEADERS['Strict-Transport-Security'])

    def test_other_paths_untouched(self):
        """非 aiot 的預設共用主機維持既有行為（陰性對照）。"""
        res = self.url_open('/web/login', timeout=30)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('Strict-Transport-Security', res.headers)

    @staticmethod
    def _forwarded_https_headers(host):
        return {
            'Host': host,
            'X-Forwarded-Host': host,
            'X-Forwarded-Proto': 'https',
        }

    def test_aiot_host_sets_hsts_on_login_and_not_found(self):
        """ProxyFix 後的公開平台 Host 在一般與錯誤回應都得到 HSTS。"""
        with patch.dict(config.options, {'proxy_mode': True}):
            for path, status_code in (('/web/login', 200), ('/not-a-real-platform-route', 404)):
                with self.subTest(path=path):
                    res = self.url_open(path, headers=self._forwarded_https_headers(
                        AIOT_PUBLIC_HOST), timeout=30)
                    self.assertEqual(res.status_code, status_code, res.text[:200])
                    self.assertEqual(res.headers['Strict-Transport-Security'],
                                     SECURITY_HEADERS['Strict-Transport-Security'])

    def test_other_shared_host_stays_without_hsts_on_login(self):
        """共享 Odoo 的另一個 Host 不因全路徑公開平台政策被改變。"""
        with patch.dict(config.options, {'proxy_mode': True}):
            res = self.url_open('/web/login', headers=self._forwarded_https_headers(
                'www.woowtech.io'), timeout=30)
        self.assertEqual(res.status_code, 200, res.text[:200])
        self.assertNotIn('Strict-Transport-Security', res.headers)


class _ServiceFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'R8 UX Workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'R8 UX Template',
            'slug': 'r8-ux-template',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/r8-ux',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
            'documentation_url': 'https://guide.example.com/r8',
        })
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-r8-ux',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-r8-ux',
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        })
        self.service.write({'helm_chart_version': '1.0.0', 'helm_revision': 4})


class TestServicePayload(_ServiceFixture):

    def _payload(self, include_details):
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            return PaasController()._format_service(self.service, include_details=include_details)

    def test_documentation_url_in_template(self):
        """#1453：list 與 detail 兩條路都要帶（配置頁讀 detail、未來卡片可能讀 list）。"""
        for details in (False, True):
            with self.subTest(include_details=details):
                self.assertEqual(
                    self._payload(details)['template']['documentation_url'],
                    'https://guide.example.com/r8')

    def test_documentation_url_empty_string_when_unset(self):
        self.template.documentation_url = False
        self.assertEqual(self._payload(False)['template']['documentation_url'], '')

    def test_last_release_sync_at_carries_utc_offset(self):
        """#1456：不帶 offset 的值前端會當成當地時間（台北慢 8 小時，#914）。"""
        self.service.sudo().write({'last_release_sync_at': '2026-10-04 03:00:00'})
        value = self._payload(True)['last_release_sync_at']
        self.assertEqual(value, '2026-10-04T03:00:00+00:00')


class _NonClosingCursor:
    """背景執行緒的 ``odoo.registry(db).cursor()`` 導回測試 transaction（同 #552 測試）。"""

    def __init__(self, cr):
        self._cr = cr

    def __enter__(self):
        return self._cr

    def __exit__(self, *exc_info):
        return False


class _SyncThread:

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class TestReconfigureRevisionWriteBack(_ServiceFixture):
    """#1456：poll 早一步把服務推回 running 時，背景執行緒仍要回寫新的 helm_revision。"""

    def _run_update(self, client):
        fake_request = MagicMock()
        fake_request.env = self.env
        fake_registry = MagicMock()
        fake_registry.cursor.return_value = _NonClosingCursor(self.env.cr)
        client_path = 'odoo.addons.woow_paas_platform.services.paas_operator.PaaSOperatorClient'
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request), \
                patch(client_path, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread), \
                patch('odoo.registry', return_value=fake_registry):
            return PaasController()._update_service(self.service, values={}, version=None)

    def _client_racing_to(self, state, result):
        """upgrade_release 回來之前，state 已被別的寫入者（poll／刪除）改掉。"""
        client = MagicMock()

        def _upgrade(**_kwargs):
            # 先 flush 主執行緒還沒落地的 state='upgrading'，否則 invalidate_all 時才
            # flush 會蓋掉下面這筆模擬寫入。
            self.env.flush_all()
            self.env.cr.execute(
                'UPDATE woow_paas_platform_cloud_service SET state = %s WHERE id = %s',
                (state, self.service.id))
            self.env.invalidate_all()
            return result

        client.upgrade_release.side_effect = _upgrade
        return client

    def _reload(self):
        self.env.invalidate_all()
        return self.service.browse(self.service.id)

    def test_poll_won_the_race_revision_still_recorded(self):
        result = self._run_update(self._client_racing_to(
            'running', {'revision': 5, 'chart_version': '1.0.1'}))
        self.assertTrue(result.get('success'), result)
        svc = self._reload()
        self.assertEqual(svc.state, 'running', '前提：race 真的發生了')
        self.assertEqual(svc.helm_revision, 5,
                         '帳本停在升級前的 revision，要等每日對帳才追上（R8：OD 9／實際 10）')
        self.assertEqual(svc.helm_chart_version, '1.0.1')

    def test_never_moves_revision_backwards(self):
        self.service.sudo().write({'helm_revision': 9})
        self._run_update(self._client_racing_to('running', {'revision': 3}))
        self.assertEqual(self._reload().helm_revision, 9)

    def test_deleting_service_is_left_alone(self):
        self._run_update(self._client_racing_to('deleting', {'revision': 5}))
        svc = self._reload()
        self.assertEqual(svc.state, 'deleting')
        self.assertEqual(svc.helm_revision, 4)

    def test_zero_revision_never_clobbers_the_ledger(self):
        """#1414：舊 operator 讀錯欄位，upgrade 永遠回報 revision 0。

        修法前 upgrading 分支寫 ``release_info.get('revision', …)``——鍵存在 ⇒ 帳本被寫成
        0（AI-OD-004-3「更新中短暫回 0」）。讀不到正整數時不覆蓋；向 operator 讀一次
        release 也失敗就保留原值，交給 poll／每日對帳。
        """
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 0, 'chart_version': '1.0.1'}
        client.get_release.side_effect = RuntimeError('operator unreachable')
        self._run_update(client)
        svc = self._reload()
        self.assertEqual(svc.state, 'upgrading', '前提：走的是 upgrading 分支')
        self.assertEqual(svc.helm_revision, 4)
        self.assertEqual(svc.helm_chart_version, '1.0.1')

    def test_unknown_revision_is_synced_from_the_release(self):
        """#1414：upgrade 沒回報可用的 revision ⇒ 立刻讀一次 release（helm list）補上。"""
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 0}
        client.get_release.return_value = {'revision': 6, 'chart_version': '1.0.2'}
        self._run_update(client)
        svc = self._reload()
        self.assertEqual(svc.helm_revision, 6)
        self.assertEqual(client.get_release.call_args.args,
                         (self.service.helm_namespace, self.service.helm_release_name))

    def test_upgrading_path_unchanged(self):
        """陽性對照：沒有 race 時照舊走原本那條（state 仍 upgrading）。"""
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 5}
        self._run_update(client)
        svc = self._reload()
        self.assertEqual(svc.state, 'upgrading')
        self.assertEqual(svc.helm_revision, 5)

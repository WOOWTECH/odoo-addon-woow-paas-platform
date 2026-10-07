"""#1559：ngrok 連不上時，服務頁顯示原因（authtoken 錯、沒填、網域被占用、要重啟），不再只寫「尚未取得」。

operator ``/public-address`` 在沒有位址時多回 ``error_code``（封閉集合）；平台依它組服務建立者
語言的一句話，經 `_release_facts`（與 public_tunnel_url 同一個咽喉）寫進 ``public_tunnel_error``，
payload 帶給 OverviewTab。operator 給的 ``error`` 文字不採用——不把 operator 的字串直接放到
租戶畫面上，也才有多語系。
"""
import json
import os
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1559 workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': '1559 tunnel', 'slug': 'tunnel-error-1559', 'category': 'automation',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example/tunnel-error-1559',
            'helm_chart_version': '0.1.0', 'mcp_enabled': False,
            'has_public_tunnel_address': True,
        })
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-1559', 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'state': 'running',
            'helm_namespace': 'paas-ws-test', 'helm_release_name': 'svc-1559',
            'helm_values': json.dumps({}),
        })

    @staticmethod
    def _client(payload=None, raises=None):
        client = MagicMock()
        if raises is not None:
            client.get_public_address.side_effect = raises
        else:
            client.get_public_address.return_value = payload
        return client


@tagged('post_install', '-at_install')
class TestProbeReportsTheReason(_Base):

    def test_bad_token(self):
        address, error = self.service._probe_public_tunnel(self._client({
            'public_url': None, 'error_code': 'ERR_NGROK_105',
            'error': 'operator 的中文備援（平台不採用）'}))
        self.assertEqual(address, '')
        self.assertIn('authtoken is not valid', error)
        self.assertIn('ERR_NGROK_105', error)
        self.assertNotIn('operator 的中文備援', error)

    def test_address_present_has_no_reason(self):
        self.assertEqual(
            self.service._probe_public_tunnel(self._client({'public_url': 'tcp://6.tcp.ngrok.io:1'})),
            ('tcp://6.tcp.ngrok.io:1', ''))

    def test_old_operator_without_error_fields(self):
        self.assertEqual(
            self.service._probe_public_tunnel(self._client({'public_url': None})), ('', ''))

    def test_operator_down_is_not_probed(self):
        with self.assertLogs('odoo.addons.woow_paas_platform.models.cloud_service', level='WARNING'):
            got = self.service._probe_public_tunnel(self._client(raises=RuntimeError('down')))
        self.assertEqual(got, (None, None))

    def test_wrapper_unchanged(self):
        self.assertEqual(self.service._probe_public_tunnel_address(self._client({
            'public_url': None, 'error_code': 'NO_AUTHTOKEN'})), '')

    def test_every_known_code_has_a_message(self):
        for code in ('NO_AUTHTOKEN', 'SERVER_URL_MISMATCH', 'ERR_NGROK_105', 'ERR_NGROK_107',
                     'ERR_NGROK_108', 'ERR_NGROK_334', 'ERR_NGROK_4018', 'ERR_NGROK_9999'):
            with self.subTest(code=code):
                self.assertTrue(self.service._public_tunnel_error_text(code))
        self.assertIn('https://ngrok.com/docs/errors/err_ngrok_9999',
                      self.service._public_tunnel_error_text('ERR_NGROK_9999'))

    def test_unknown_shapes_are_dropped(self):
        for code in (None, '', 'whatever', 'ERR_NGROK_', 'ERR_NGROK_1 <script>', 42):
            with self.subTest(code=code):
                self.assertEqual(self.service._public_tunnel_error_text(code), '')

    def test_creator_language(self):
        self.env['res.lang']._activate_lang('zh_TW')
        self.service.create_uid.sudo().lang = 'zh_TW'
        self.assertIn('authtoken 不正確', self.service._public_tunnel_error_text('ERR_NGROK_105'))


@tagged('post_install', '-at_install')
class TestFactsAndPayload(_Base):

    def _facts(self, address, error):
        return self.service._release_facts(
            {'revision': 3, 'chart_version': '0.1.0'}, tunnel_address=address, tunnel_error=error)

    def test_reason_written_only_without_address(self):
        self.assertEqual(self._facts('', 'bad token')['public_tunnel_error'], 'bad token')
        self.assertIs(self._facts('tcp://6.tcp.ngrok.io:1', 'stale')['public_tunnel_error'], False)
        self.assertIs(self._facts('', '')['public_tunnel_error'], False)

    def test_not_probed_leaves_the_reason_alone(self):
        self.assertNotIn('public_tunnel_error', self._facts(None, None))

    def test_poll_writes_the_reason(self):
        """背景 poll（轉移狀態）一輪就把原因寫進帳本。"""
        self.service.sudo().write({'state': 'upgrading'})
        client = MagicMock()
        client.get_status.return_value = {
            'release': {'status': 'deployed', 'revision': 3, 'chart_version': '0.1.0'},
            'pods': [{'name': 'p1', 'phase': 'Running', 'ready': '1/1'}],
        }
        client.get_public_address.return_value = {'public_url': None, 'error_code': 'ERR_NGROK_105'}
        with patch.object(type(self.service), '_public_url_dns_state', return_value=None):
            self.service._poll_and_update_status(client)
        self.service.invalidate_recordset()
        self.assertIn('ERR_NGROK_105', self.service.public_tunnel_error)
        self.assertFalse(self.service.public_tunnel_url)

    def test_payload_carries_the_reason(self):
        self.service.sudo().write({'public_tunnel_error': 'bad token'})
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            payload = PaasController()._format_service(self.service, include_details=True)
        self.assertEqual(payload['public_tunnel_error'], 'bad token')


@tagged('post_install', '-at_install')
class TestTranslations(TransactionCase):

    def test_messages_have_traditional_chinese(self):
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        needles = ('no ngrok authtoken has been entered', 'the service started before it was',
                   'The ngrok authtoken is not valid', 'maximum number of agents online',
                   'already online somewhere else', 'needs a verified account',
                   'The ngrok agent could not connect')
        for needle in needles:
            with self.subTest(needle=needle):
                self.assertTrue(any(needle in src and value for src, value in code.items()),
                                'zh_TW.po 缺翻譯：%s' % needle)

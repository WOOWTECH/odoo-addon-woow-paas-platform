"""X-SEC-006-1／X-W-004-1：JSON-RPC 錯誤回應不得把 Python traceback 交給非內部使用者。

背景（商用驗收 2026-10-16）：
- 匿名 POST ``/web/dataset/call_kw``，body ``{not json`` 或不存在的 model，回 200 且
  ``error.data.debug`` 是完整 traceback（``/usr/lib/python3/dist-packages/odoo/…``、行號、
  Python 版本）。這兩條例外發生在 ``web`` 的 ``_call_kw_readonly``（``_serve_db`` 算
  readonly 旗標那一步），**在** ``ir.http._handle_error`` 之外，直接走
  ``Application.__call__`` → ``dispatcher.handle_error`` ⇒ 覆寫 ``ir.http`` 攔不到。
- portal 帳號 call_kw 被 ACL 拒絕時，48/48 筆 ``data.debug`` 帶 traceback。

修法（``services/rpc_error_redaction.py``）：包住 ``odoo.http.serialize_exception``——所有
JSON-RPC 錯誤（以及 web/stock 報表、匯出的錯誤）都經過它。非 dev 模式、而且呼叫者不是
內部使用者時，``debug`` 換成固定說明文字；``name``／``message`` 照舊給前端用。

本檔斷言：
R.1  匿名畸形 body → 無 traceback、無伺服器路徑，name／message 仍在。
R.2  匿名不存在的 model → 404 碼保留、無 traceback。
R.3  portal 的 AccessError → 中文／英文訊息保留、無 traceback。
R.4  陽性對照：內部使用者照樣拿得到 traceback（證明 R.1–R.3 不是因為回應本來就沒有 debug）。
R.5  純函式：UserError 家族與 RedirectWarning 保留 arguments（前端要用）、其他例外清掉。
"""

import json
import re

import odoo.http
from odoo.exceptions import AccessError, RedirectWarning, UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

from ..services import rpc_error_redaction as redaction

# 伺服器路徑／traceback 的指紋：只要出現任一個就算外流。
_LEAK_PATTERNS = (
    re.compile(r'Traceback \(most recent call last\)'),
    re.compile(r'File "[^"]+\.py", line \d+'),
    re.compile(r'/(?:usr|opt)/[\w./-]*odoo'),
    re.compile(r'site-packages|dist-packages'),
)


def _assert_no_leak(case, text):
    for pattern in _LEAK_PATTERNS:
        case.assertIsNone(
            pattern.search(text),
            f"回應含伺服器內部資訊（{pattern.pattern}）：{text[:400]}")


class TestRedactionPayload(TransactionCase):
    """R.5：純函式，不碰 request。"""

    def _payload(self, exc):
        try:
            raise exc
        except Exception as e:  # noqa: BLE001 — 要的就是 format_exc 的現場
            return redaction.ORIGINAL_SERIALIZE_EXCEPTION(e), e

    def test_debug_hidden_and_message_kept(self):
        payload, exc = self._payload(ValueError('bad input'))
        self.assertIn('Traceback', payload['debug'], '前提：原始 payload 帶 traceback')
        out = redaction.redact_exception_payload(payload, exc, keep_debug=False)
        self.assertEqual(out['debug'], redaction.HIDDEN_DEBUG)
        self.assertEqual(out['name'], 'builtins.ValueError')
        self.assertEqual(out['message'], 'bad input')
        self.assertEqual(out['arguments'], [])
        self.assertEqual(out['context'], {})
        self.assertIn('Traceback', payload['debug'], '不可就地改掉原 dict')

    def test_user_error_family_keeps_arguments(self):
        payload, exc = self._payload(RedirectWarning('go', 42, 'Open'))
        out = redaction.redact_exception_payload(payload, exc, keep_debug=False)
        self.assertEqual(out['debug'], redaction.HIDDEN_DEBUG)
        self.assertEqual(list(out['arguments']), list(exc.args),
                         'RedirectWarning（不是 UserError 子類）的 action／按鈕文字在 arguments，前端要用')
        self.assertEqual(list(out['arguments'])[:3], ['go', 42, 'Open'])
        payload, exc = self._payload(UserError('nope'))
        out = redaction.redact_exception_payload(payload, exc, keep_debug=False)
        self.assertEqual(list(out['arguments']), ['nope'])

    def test_keep_debug_returns_payload_unchanged(self):
        payload, exc = self._payload(AccessError('denied'))
        self.assertIs(redaction.redact_exception_payload(payload, exc, keep_debug=True), payload)

    def test_patch_installed_once(self):
        self.assertIs(odoo.http.serialize_exception, redaction.serialize_exception)
        redaction.install()
        redaction.install()
        self.assertIs(odoo.http.serialize_exception, redaction.serialize_exception,
                      'install() 要冪等，不可一層包一層')
        self.assertIsNot(redaction.ORIGINAL_SERIALIZE_EXCEPTION, redaction.serialize_exception)


@tagged('post_install', '-at_install')
class TestCallKwErrorRedaction(HttpCase):
    """R.1–R.4：真的打 /web/dataset/call_kw。"""

    def setUp(self):
        super().setUp()
        self.env['res.users'].sudo().create({
            'name': 'xsec006 portal', 'login': 'xsec006_portal', 'password': 'xsec006_portal',
            'email': 'xsec006_portal@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
        })
        self.env['res.users'].sudo().create({
            'name': 'xsec006 internal', 'login': 'xsec006_internal',
            'password': 'xsec006_internal', 'email': 'xsec006_internal@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def _post(self, path, body):
        res = self.url_open(path, data=body, timeout=30,
                            headers={'Content-Type': 'application/json'})
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res, res.json()

    @staticmethod
    def _envelope(model, method, args=None, kwargs=None, req_id='not-an-int'):
        return json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'id': req_id,
            'params': {'model': model, 'method': method,
                       'args': args or [], 'kwargs': kwargs or {}},
        })

    def test_R1_anonymous_malformed_body(self):
        with mute_logger('odoo.http'):
            res, data = self._post('/web/dataset/call_kw', '{not json')
        _assert_no_leak(self, res.text)
        error = data['error']
        self.assertEqual(error['data']['debug'], redaction.HIDDEN_DEBUG)
        self.assertEqual(error['data']['name'], 'json.decoder.JSONDecodeError')
        self.assertTrue(error['data']['message'])
        self.assertEqual(error['message'], 'Odoo Server Error')

    def test_R2_anonymous_unknown_model(self):
        with mute_logger('odoo.http'):
            res, data = self._post(
                '/web/dataset/call_kw/qa.nonexistent.model/search_read',
                self._envelope('qa.nonexistent.model', 'search_read'))
        _assert_no_leak(self, res.text)
        self.assertEqual(data['error']['code'], 404)
        self.assertEqual(data['error']['data']['debug'], redaction.HIDDEN_DEBUG)
        self.assertEqual(data['error']['data']['name'], 'werkzeug.exceptions.NotFound')

    def test_R3_portal_access_error(self):
        self.authenticate('xsec006_portal', 'xsec006_portal')
        with mute_logger('odoo.http'):
            res, data = self._post(
                '/web/dataset/call_kw/ir.config_parameter/search_read',
                self._envelope('ir.config_parameter', 'search_read', kwargs={'fields': ['key']},
                               req_id=1))
        _assert_no_leak(self, res.text)
        error = data['error']['data']
        self.assertEqual(error['name'], 'odoo.exceptions.AccessError')
        self.assertTrue(error['message'], '使用者看得懂的拒絕訊息要保留')
        self.assertEqual(error['debug'], redaction.HIDDEN_DEBUG)

    def test_R4_internal_user_keeps_traceback(self):
        """陽性對照：內部使用者（員工）照樣拿得到 traceback，Odoo 後台的錯誤視窗不受影響。"""
        self.authenticate('xsec006_internal', 'xsec006_internal')
        with mute_logger('odoo.http'):
            _res, data = self._post(
                '/web/dataset/call_kw/res.partner/woow_xsec006_no_such_method',
                self._envelope('res.partner', 'woow_xsec006_no_such_method', req_id=2))
        self.assertIn('Traceback', data['error']['data']['debug'])

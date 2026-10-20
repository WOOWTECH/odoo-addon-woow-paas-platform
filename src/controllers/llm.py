"""AI 模型分頁的 BFF：``/api/llm/*``（DESIGN §3；票 LLM-01b 的 tracer 端點、LLM-04 的條款端點）。

慣例（DESIGN §3.1）：

* 全部 ``auth='user'``、``type='json'``、POST。只接受 Odoo session——HA 元件或 device flow
  拿到的 OAuth bearer token 不是 session，進不來。
* partner 一律 ``request.env.user.partner_id``，**不接受**前端傳 partner、account、user id；
  UP 一律由 service method 從**呼叫者自己的帳戶**取（本檔不碰 UP 與密文欄位，測試守）。
* 本檔不呼叫 sudo：portal 對 LLM model 零 ACL，所有讀寫經 model 上 ``_`` 開頭的
  service method（RPC 叫不到私有方法），由它們在錨定 partner 之後才提權。
* 回應：成功 ``{'success': True, 'data': …}``；失敗 HTTP 200＋``{'success': False, 'error':
  <fallback 訊息>, 'code': <小寫 snake>, …參數}``。前端 ``llm_errors.js`` 依 code 組句。
  **每一個**失敗都帶 code：連 gate 沿用的 ``paas._account_denial()``（它的 dict 沒有 code）
  也補上 ``llm_auth_required``。碼表在 DESIGN §3.4；fallback 訊息的 zh_TW 在 ``i18n/zh_TW.po``。
* **每個 handler 都有 catch-all**（〔REV〕3）：Odoo ``type='json'`` 遇到未處理的例外會把
  ``str(exception)`` 與 traceback 放進 JSON-RPC ``error.data``，``/woow`` 的 ``rpc.js`` 會直接
  顯示給租戶。所以一律回固定訊息、traceback 經 ``llm_redact`` 才進 log。``OperationalError``
  照樣往外拋給 ``retrying()``——**只有** ``keys/create`` 例外：它在請求內建了 new-api token
  （獨立交易），重播會再建一把，所以連 ``OperationalError`` 也吞掉。
"""
from __future__ import annotations

import ipaddress
import logging
import traceback
from typing import Any

from psycopg2 import OperationalError

from odoo import _
from odoo.http import Controller, request, route

from ..models.llm_common import (
    ERR_LLM_ACCOUNT_ERROR,
    ERR_LLM_AUTH_REQUIRED,
    ERR_LLM_CLOSED,
    ERR_LLM_DISABLED,
    ERR_LLM_FREE_KEY_EXISTS,
    ERR_LLM_FREE_CAPPED_TODAY,
    ERR_LLM_FREE_KEY_DAILY_LIMIT,
    ERR_LLM_INTERNAL,
    ERR_LLM_INVALID_REQUEST,
    ERR_LLM_KEY_ALREADY_SHOWN,
    ERR_LLM_KEY_INVALID,
    ERR_LLM_KEY_LIMIT,
    ERR_LLM_NOT_ACTIVATED,
    ERR_LLM_NO_REFUND_ACK_REQUIRED,
    ERR_LLM_ORDER_NOT_FOUND,
    ERR_LLM_PENDING_REVIEW,
    ERR_LLM_POINTS_OUT_OF_RANGE,
    ERR_LLM_PROVISIONING,
    ERR_LLM_RATE_LIMITED,
    ERR_LLM_REAUTH_REQUIRED,
    ERR_LLM_RESERVE_CONFIRM_REQUIRED,
    ERR_LLM_SUSPENDED,
    ERR_LLM_TOS_OUTDATED,
    ERR_LLM_TOS_REQUIRED,
    ERR_LLM_TOS_UNAVAILABLE,
    ERR_LLM_UNAVAILABLE,
    LlmError,
    llm_visible,
)
from ..services import llm_redact
from .ha_paas import HiddenChainMixin
from .paas import _account_denial

_logger = logging.getLogger(__name__)

ACCOUNT_MODEL = 'woow_paas_platform.llm_account'
KEY_MODEL = 'woow_paas_platform.llm_api_key'
ORDER_MODEL = 'woow_paas_platform.llm_credit_order'
TERMS_MODEL = 'woow_paas_platform.llm_terms'
#: 錯誤回應可以帶的參數欄位（只放數字與固定字串，前端用來組句）。
_SAFE_PARAMS = frozenset({'retry_after', 'current_version', 'max_keys', 'field',
                          'needed_7d', 'short_by'})


def _message(code: str) -> str:
    """後端的 fallback 訊息（前端以 code 組句為準）。"""
    if code == ERR_LLM_DISABLED:
        return _('This feature is not available yet.')
    if code == ERR_LLM_NOT_ACTIVATED:
        return _('Please activate the AI models service first.')
    if code == ERR_LLM_TOS_REQUIRED:
        return _('Please accept the AI Models Terms of Service first.')
    if code == ERR_LLM_TOS_OUTDATED:
        return _('The AI Models Terms of Service have been updated. Please accept them again.')
    if code == ERR_LLM_TOS_UNAVAILABLE:
        return _('The AI Models Terms of Service cannot be shown right now. Please try again later.')
    if code == ERR_LLM_PROVISIONING:
        return _('Your AI models account is being set up. Please wait a moment.')
    if code == ERR_LLM_SUSPENDED:
        return _('The AI models service is suspended.')
    if code == ERR_LLM_CLOSED:
        return _('The AI models account is closed. Please contact support to reactivate it.')
    if code == ERR_LLM_ACCOUNT_ERROR:
        return _('Setting up your AI models account failed. Please contact support.')
    if code == ERR_LLM_REAUTH_REQUIRED:
        return _('To protect your account, please verify your identity again before continuing.')
    if code == ERR_LLM_RATE_LIMITED:
        return _('Too many requests. Please try again later.')
    if code == ERR_LLM_KEY_LIMIT:
        return _('Each account can have at most 20 keys. Please delete unused keys first.')
    if code == ERR_LLM_FREE_CAPPED_TODAY:
        return _("Today's free trial is used up. You can create a free trial key again after 00:00 Taipei time.")
    if code == ERR_LLM_FREE_KEY_DAILY_LIMIT:
        return _("You have reached today's limit for creating free trial keys. Please try again tomorrow.")
    if code == ERR_LLM_FREE_KEY_EXISTS:
        return _('Each account can have only one free trial key.')
    if code == ERR_LLM_KEY_ALREADY_SHOWN:
        return _('This key has already been shown and cannot be shown again. '
                 'Please delete it and create a new one.')
    if code == ERR_LLM_KEY_INVALID:
        return _('The key settings are invalid.')
    if code == ERR_LLM_UNAVAILABLE:
        return _('The AI models service is temporarily unavailable. Please try again later.')
    if code == ERR_LLM_AUTH_REQUIRED:
        # 與 paas._account_denial() 同一句（zh_TW.po 已有譯文「需要登入」）。
        return _('Authentication required')
    if code == ERR_LLM_PENDING_REVIEW:
        return _('A previous conversion needs review. Please wait before converting again.')
    if code == ERR_LLM_POINTS_OUT_OF_RANGE:
        return _('Each conversion must be between 50 and 10,000 whole points.')
    if code == ERR_LLM_NO_REFUND_ACK_REQUIRED:
        return _('Please confirm that converted AI credit cannot be refunded.')
    if code == ERR_LLM_RESERVE_CONFIRM_REQUIRED:
        return _('This conversion may leave too few points for upcoming renewals.')
    if code in (ERR_LLM_INVALID_REQUEST, ERR_LLM_ORDER_NOT_FOUND):
        return _('The conversion request was not found or is invalid.')
    return _('Something went wrong. Please try again later.')


def _ok(data: Any) -> dict[str, Any]:
    return {'success': True, 'data': data}


def _fail(code: str, **params: Any) -> dict[str, Any]:
    payload = {'success': False, 'error': _message(code), 'code': code}
    payload.update({k: v for k, v in params.items() if k in _SAFE_PARAMS})
    return payload


def _peer_ip() -> str:
    """直接對端的位址；**不讀** client 自帶的 ``X-Forwarded-For``（〔REV〕22）。

    Odoo 開 ``proxy_mode`` 時 werkzeug ``ProxyFix`` 會改寫 ``REMOTE_ADDR``，原值留在
    ``werkzeug.proxy_fix.orig``。``CF-Connecting-IP`` 的採用條件由 LLM-03 定案（DESIGN §2.2：直接對端
    是平台 cloudflared 時才用）；在那之前，stg／沙盒在 cloudflared 後面記到的是叢集內的 pod 位址，
    不是使用者的 IP——``tos_accepted_ip`` 與 ``tos_accept`` 事件的 ``real_ip`` 都一樣（LLM-04 驗收 3
    的「真實 IP」部分依賴 LLM-03）。
    """
    environ = request.httprequest.environ
    original = environ.get('werkzeug.proxy_fix.orig') or {}
    raw = original.get('REMOTE_ADDR') or environ.get('REMOTE_ADDR') or ''
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ''


class LlmController(HiddenChainMixin, Controller):

    def _gate(self) -> dict[str, Any] | None:
        if _account_denial(request.env):
            # 同一個判準（paas._account_denial），但回應照本分頁的慣例帶穩定的 code。
            return _fail(ERR_LLM_AUTH_REQUIRED)
        if not llm_visible(request.env, request.env.user.partner_id):
            return _fail(ERR_LLM_DISABLED)
        return None

    def _run(self, endpoint: str, handler, *, swallow_operational: bool = False):
        try:
            gate = self._gate()
            if gate:
                return gate
            return _ok(handler(request.env.user.partner_id))
        except LlmError as exc:
            return _fail(exc.code, **exc.params)
        except OperationalError:
            if not swallow_operational:
                raise
            _logger.error('[LLM] %s: database error after the external call (uid=%s): %s',
                          endpoint, request.env.uid, llm_redact.redact(traceback.format_exc()))
            return _fail(ERR_LLM_UNAVAILABLE)
        except Exception:  # noqa: BLE001 - see module docstring (catch-all)
            _logger.error('[LLM] %s failed (uid=%s): %s', endpoint, request.env.uid,
                          llm_redact.redact(traceback.format_exc()))
            return _fail(ERR_LLM_INTERNAL)

    @route('/api/llm/state', auth='user', methods=['POST'], type='json')
    def llm_state(self) -> dict[str, Any]:
        return self._run('state', lambda partner: request.env[ACCOUNT_MODEL]
                         ._llm_state_payload(partner))

    @route('/api/llm/account/activate', auth='user', methods=['POST'], type='json')
    def llm_account_activate(self, tos_version=None, **kw) -> dict[str, Any]:
        def handler(partner):
            account = request.env[ACCOUNT_MODEL]._llm_activate(
                partner, tos_version, request.env.user, _peer_ip())
            return {'state': account.state}
        return self._run('account/activate', handler)

    @route('/api/llm/account/reauth', auth='user', methods=['POST'], type='json')
    def llm_account_reauth(self, password=None, **kw) -> dict[str, Any]:
        return self._run('account/reauth', lambda partner: request.env[ACCOUNT_MODEL]
                         ._llm_reauthenticate(partner, password, request.session, _peer_ip()))

    @route('/api/llm/terms', auth='user', methods=['POST'], type='json')
    def llm_terms(self, version=None, **kw) -> dict[str, Any]:
        """published 版的條款；帶 ``version`` 時依版本號查 published／retired 版（草稿不對外）。"""
        return self._run('terms', lambda partner: request.env[TERMS_MODEL]
                         ._llm_terms_payload(version))

    @route('/api/llm/account/accept_terms', auth='user', methods=['POST'], type='json')
    def llm_account_accept_terms(self, tos_version=None, **kw) -> dict[str, Any]:
        def handler(partner):
            return request.env[ACCOUNT_MODEL]._llm_accept_terms(
                partner, tos_version, request.env.user, _peer_ip())
        return self._run('account/accept_terms', handler)

    @route('/api/llm/keys/list', auth='user', methods=['POST'], type='json')
    def llm_keys_list(self) -> dict[str, Any]:
        return self._run('keys/list', lambda partner: request.env[KEY_MODEL]
                         ._llm_keys_payload(partner))

    @route('/api/llm/keys/create', auth='user', methods=['POST'], type='json')
    def llm_keys_create(self, client_request_id=None, kind=None, **kw) -> dict[str, Any]:
        def handler(partner):
            if kind != 'free_trial':
                # 付費金鑰在 LLM-07。
                raise LlmError(ERR_LLM_KEY_INVALID, field='kind')
            return request.env[KEY_MODEL]._llm_create_free_trial_key(
                partner, client_request_id, request.env.user, _peer_ip())
        return self._run('keys/create', handler, swallow_operational=True)

    @route('/api/llm/models', auth='user', methods=['POST'], type='json')
    def llm_models(self) -> dict[str, Any]:
        return self._run('models', lambda partner: request.env['woow_paas_platform.llm_swr_cache']
                         ._llm_models_payload())

    @route('/api/llm/usage/recent', auth='user', methods=['POST'], type='json')
    def llm_usage_recent(self) -> dict[str, Any]:
        return self._run('usage/recent', lambda partner: request.env[ACCOUNT_MODEL]
                         ._llm_usage_recent(partner))

    @route('/api/llm/credit/preview', auth='user', methods=['POST'], type='json')
    def llm_credit_preview(self, points=None, **kw) -> dict[str, Any]:
        return self._run('credit/preview', lambda partner: request.env[ORDER_MODEL]
                         ._llm_credit_preview(partner, points))

    @route('/api/llm/credit/convert', auth='user', methods=['POST'], type='json')
    def llm_credit_convert(self, points=None, client_request_id=None,
                           confirm_reserve=False, accept_no_refund=False, **kw) -> dict[str, Any]:
        def handler(partner):
            return request.env[ORDER_MODEL]._llm_convert(
                partner, points, client_request_id, confirm_reserve,
                accept_no_refund, request.env.user, request.session, _peer_ip())
        return self._run('credit/convert', handler)

    @route('/api/llm/credit/orders', auth='user', methods=['POST'], type='json')
    def llm_credit_orders(self, page=1, limit=20, client_request_id=None,
                          **kw) -> dict[str, Any]:
        return self._run('credit/orders', lambda partner: request.env[ORDER_MODEL]
                         ._llm_orders_payload(partner, page=page, limit=limit,
                                              client_request_id=client_request_id))

    @route('/api/llm/credit/orders/<int:order_id>', auth='user', methods=['POST'], type='json')
    def llm_credit_order(self, order_id, **kw) -> dict[str, Any]:
        return self._run('credit/orders/detail', lambda partner: request.env[ORDER_MODEL]
                         ._llm_order_payload(partner, order_id))

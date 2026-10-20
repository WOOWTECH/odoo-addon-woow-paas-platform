"""X-SEC-006-1／X-W-004-1：JSON-RPC 錯誤回應不對非內部使用者外流 Python traceback。

Odoo 18 的 ``JsonRPCDispatcher.handle_error`` 把 ``odoo.http.serialize_exception(exc)`` 原樣
放進 ``error.data``，其中 ``debug`` 是 ``traceback.format_exc()``——伺服器檔案路徑、行號、
Python 版本、model registry 查找路徑全部給呼叫者。匿名的兩條路徑（畸形 body、不存在的
model）發生在 ``web`` 的 ``_call_kw_readonly``，也就是 ``_serve_db`` 算 readonly 旗標那一步，
**不經過** ``ir.http._handle_error``（直接由 ``Application.__call__`` 呼叫
``request.dispatcher.handle_error``）⇒ 覆寫 ``ir.http`` 攔不到，只能在序列化這一層兜底。

做法：包住 ``odoo.http.serialize_exception``（``handle_error`` 與 web／stock 的報表、匯出錯誤
都以模組屬性呼叫它，所以一處涵蓋全部）。以下情況保留原樣：

- ``--dev`` 模式（``config['dev_mode']`` 非空）：開發環境本來就要看 traceback。
- 呼叫者是內部使用者（``base.group_user``）：Odoo 後台的錯誤視窗照舊有「詳細資料」。
  判斷失敗（cursor 已關、交易已中止、沒有 request）一律當成**不是**內部使用者。
- 目前資料庫沒有安裝本 addon（同一個 process 服務多個 DB 時，不改別的 DB 的行為）。

其餘情況：``debug`` 換成 ``HIDDEN_DEBUG``；``name``／``message`` 保留給前端；
Odoo 給使用者看的例外（``UserError`` 及子類，以及 ``RedirectWarning``——它不是 ``UserError``
的子類，前端從 ``arguments`` 取 action id 與按鈕文字）保留 ``arguments``／``context``，其他例外清空這兩格（它們不是給使用者看的，可能帶內部值）。
完整 traceback 仍然由 ``odoo.http`` 寫進伺服器日誌，不受影響。

本模組不 import ORM model／controller：``models/ir_http.py`` 在載入時呼叫 ``install()``。
"""

import logging

import odoo.http
from odoo.exceptions import RedirectWarning, UserError
from odoo.tools import config
from werkzeug.exceptions import HTTPException

_logger = logging.getLogger(__name__)

HIDDEN_DEBUG = 'Server error details are hidden. The full traceback is in the server log.'

# 給使用者看的例外：arguments／context 是前端要用的內容（RedirectWarning 不是 UserError 子類）。
_USER_FACING_EXCEPTIONS = (UserError, RedirectWarning, HTTPException)

# 用來判斷「目前 DB 有沒有裝本 addon」的 model。
_ADDON_MARKER_MODEL = 'woow_paas_platform.workspace'

# 模組載入當下的原始實作（Odoo 不會重新 import addon 的 Python 模組）。
ORIGINAL_SERIALIZE_EXCEPTION = getattr(
    odoo.http.serialize_exception, '_woow_original', odoo.http.serialize_exception)


def redact_exception_payload(payload, exception, keep_debug):
    """回傳要送給前端的 ``error.data``。``keep_debug`` 為真時原物件照回；否則回新 dict。"""
    if keep_debug or not isinstance(payload, dict):
        return payload
    redacted = dict(payload)
    redacted['debug'] = HIDDEN_DEBUG
    if not isinstance(exception, _USER_FACING_EXCEPTIONS):
        redacted['arguments'] = []
        redacted['context'] = {}
    return redacted


def _addon_installed_in_current_db():
    try:
        registry = odoo.http.request.registry
        return registry is not None and _ADDON_MARKER_MODEL in registry
    except Exception:  # noqa: BLE001 — 沒有 request／registry 尚未開：不屬於本 addon 的 DB
        return False


def _caller_may_see_debug():
    if config.get('dev_mode'):
        return True
    try:
        request = odoo.http.request
        if not request or not request.session.uid:
            return False
        env = request.env
        if env is None or env.cr.closed:
            return False
        return bool(env.user._is_internal())
    except Exception:  # noqa: BLE001 — 錯誤處理路徑上不能再丟例外；判斷不了就當外部使用者
        return False


def serialize_exception(exception):
    payload = ORIGINAL_SERIALIZE_EXCEPTION(exception)
    if not _addon_installed_in_current_db():
        return payload
    return redact_exception_payload(payload, exception, keep_debug=_caller_may_see_debug())


serialize_exception._woow_original = ORIGINAL_SERIALIZE_EXCEPTION


def install():
    """冪等：重複呼叫不會一層包一層。"""
    if odoo.http.serialize_exception is not serialize_exception:
        odoo.http.serialize_exception = serialize_exception
        _logger.debug('odoo.http.serialize_exception wrapped (X-SEC-006 traceback redaction)')

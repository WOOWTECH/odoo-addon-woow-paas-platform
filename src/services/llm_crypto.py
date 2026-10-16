"""AI 模型帳戶的加密欄位（DESIGN §2.9、〔REV〕3、16、19）。

PaaS 為每個 AI 帳戶保存兩個秘密：new-api 密碼（開戶與重簽 UP 用）與 UP（消費者自己的
PAT）。兩者都用 ``MultiFernet`` 加密後才進 DB：

* 金鑰來自 env ``WOOW_LLM_SECRET_KEYS``（逗號分隔，第一把加密、全部可解密；每把 strip
  後驗證是合法的 Fernet key）。沒設或格式不合 ⇒ :class:`LlmCryptoUnavailable`，錯誤
  訊息只有固定的原因碼、**不帶值**。
* **密文綁定所屬列與欄位**：加密的明文是 ``v1|<account_id>|<newapi_username>|<field>|<secret>``；
  解密後逐段比對，不一致 ⇒ :class:`LlmSecretBindingMismatch`。有 DB 寫入權的人把 A 的
  密文複製到 B 的列，解出來也不能用。
* DB 值是 ``'neutralized'``（prod clone 中和後的 sentinel）⇒ :class:`LlmSecretNeutralized`，
  呼叫端一律 fail closed：不重簽、不重新開戶。
* 解密結果一律 strip 並驗證是 header-safe 的形狀（UP：``nap_`` 開頭的英數；密碼：base62），
  不合格 ⇒ :class:`LlmSecretMalformed`。

所有例外都是 ``raise … from None``，``args`` 只有原因碼——cryptography 與 base64 的
例外文字不會跟著 traceback 出現在 log 或 JSON-RPC ``error.data``。

純 Python（只依賴 ``cryptography``，已在 ``src/requirements.txt``）、不 import odoo。
"""
from __future__ import annotations

import os
import re
import secrets
import string

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

ENV_SECRET_KEYS = 'WOOW_LLM_SECRET_KEYS'
NEUTRALIZED = 'neutralized'
VERSION = 'v1'

FIELD_PASSWORD = 'newapi_password'
FIELD_UP_TOKEN = 'up_token'
FIELDS = frozenset({FIELD_PASSWORD, FIELD_UP_TOKEN})

PASSWORD_LENGTH = 64
_BASE62 = string.ascii_letters + string.digits
_PASSWORD_RE = re.compile(r'[A-Za-z0-9]{%d}' % PASSWORD_LENGTH, re.ASCII)
#: 與代理的 ``Authorization`` 形狀一致（``extra/newapi-proxy`` ``_BEARER_RE``）。
_UP_RE = re.compile(r'nap_[A-Za-z0-9]{20,128}', re.ASCII)
_USERNAME_RE = re.compile(r'w[a-z]{1,3}[0-9]{10}', re.ASCII)
_VALIDATORS = {FIELD_PASSWORD: _PASSWORD_RE, FIELD_UP_TOKEN: _UP_RE}


class LlmCryptoError(Exception):
    """加密層的錯誤；``code`` 是可以寫 log 與回應的固定字串。"""

    code = 'llm_crypto_error'

    def __init__(self, code=None):
        if code:
            self.code = code
        super().__init__(self.code)


class LlmCryptoUnavailable(LlmCryptoError):
    """env 金鑰沒設或格式不合。"""

    code = 'llm_crypto_unavailable'


class LlmSecretNeutralized(LlmCryptoError):
    """DB 裡是 neutralize 的 sentinel。"""

    code = 'llm_secret_neutralized'


class LlmSecretBindingMismatch(LlmCryptoError):
    """密文屬於別的列或別的欄位（或被竄改）。"""

    code = 'llm_secret_binding'


class LlmSecretMalformed(LlmCryptoError):
    """解得開，但內容不是預期的形狀。"""

    code = 'llm_secret_malformed'


def generate_password() -> str:
    """64 字元 base62 隨機密碼（DESIGN §1.3.1 P0；代理要求 32–128 個可見 ASCII）。"""
    return ''.join(secrets.choice(_BASE62) for _ in range(PASSWORD_LENGTH))


def _parse_keys(raw):
    if raw is None:
        raise LlmCryptoUnavailable('llm_crypto_key_missing') from None
    entries = [part.strip() for part in raw.strip().split(',')]
    entries = [part for part in entries if part]
    if not entries:
        raise LlmCryptoUnavailable('llm_crypto_key_missing') from None
    fernets = []
    for entry in entries:
        try:
            fernets.append(Fernet(entry.encode('ascii')))
        except Exception:  # noqa: BLE001 - ValueError/UnicodeError/binascii: value must not leak
            raise LlmCryptoUnavailable('llm_crypto_key_invalid') from None
    return MultiFernet(fernets)


def load(environ=None) -> MultiFernet:
    """從 env 讀出 ``MultiFernet``；每次呼叫都重讀（輪替時不必重啟就生效的前提是 pod env 已更新）。"""
    environ = os.environ if environ is None else environ
    return _parse_keys(environ.get(ENV_SECRET_KEYS))


def available(environ=None) -> bool:
    try:
        load(environ)
    except LlmCryptoError:
        return False
    return True


def _check_binding_args(account_id, username, field):
    if type(account_id) is not int or account_id <= 0:
        raise LlmSecretBindingMismatch() from None
    if not isinstance(username, str) or not _USERNAME_RE.fullmatch(username):
        raise LlmSecretBindingMismatch() from None
    if field not in FIELDS:
        raise LlmSecretBindingMismatch() from None


def _validate_secret(field, secret):
    if not isinstance(secret, str):
        raise LlmSecretMalformed() from None
    secret = secret.strip()
    if not _VALIDATORS[field].fullmatch(secret):
        raise LlmSecretMalformed() from None
    return secret


def encrypt(account_id, username, field, secret, environ=None) -> str:
    """加密 ``secret``，密文綁定 ``(account_id, username, field)``。"""
    _check_binding_args(account_id, username, field)
    secret = _validate_secret(field, secret)
    fernet = load(environ)
    plaintext = '|'.join((VERSION, str(account_id), username, field, secret))
    try:
        return fernet.encrypt(plaintext.encode('ascii')).decode('ascii')
    except Exception:  # noqa: BLE001
        raise LlmCryptoUnavailable('llm_crypto_encrypt_failed') from None


def decrypt(ciphertext, account_id, username, field, environ=None) -> str:
    """解密並驗證綁定與形狀；任何不對都 fail closed（例外不帶值）。"""
    _check_binding_args(account_id, username, field)
    if not isinstance(ciphertext, str) or not ciphertext.strip():
        raise LlmSecretMalformed('llm_secret_missing') from None
    ciphertext = ciphertext.strip()
    if ciphertext == NEUTRALIZED:
        raise LlmSecretNeutralized() from None
    fernet = load(environ)
    try:
        plaintext = fernet.decrypt(ciphertext.encode('ascii')).decode('ascii')
    except (InvalidToken, UnicodeError, ValueError, TypeError):
        raise LlmSecretBindingMismatch('llm_secret_undecryptable') from None
    parts = plaintext.split('|', 4)
    if len(parts) != 5:
        raise LlmSecretMalformed() from None
    version, got_id, got_username, got_field, secret = parts
    if (version != VERSION or got_id != str(account_id)
            or got_username != username or got_field != field):
        raise LlmSecretBindingMismatch() from None
    return _validate_secret(field, secret)

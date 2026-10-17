"""AI 模型分頁共用的遮罩（DESIGN §5.5、〔REV〕3）。

所有會落地的字串——Odoo log、例外 traceback、日後的 ``llm_event.details_json``、
訂單 ``evidence_json``、incident snapshot——都先過 :func:`redact`。遮的是「拿到就能
用」的東西：

* ``Bearer <任何值>``、JWT（三段 base64url）、``X-Security-Proof``／``proof_token``；
* new-api 的 PAT ``nap_…``、消費者金鑰 ``sk-…``；
* ``password=…``／``"password": "…"``／``token: …`` 這類 key=value 形狀；
* new-api reveal 回的 48 字元裸金鑰只在 ``"key": "…"`` 形狀出現時遮（單獨一串英數
  無法和一般 id 區分，所以 client 端本來就不把 reveal 的回應寫進 log）。

純 Python、不 import odoo：``tests/test_llm_services.py`` 在沒有 Odoo 的環境直接載入。
LLM-02b 會把 ``models/cloud_service_init_attempt.py`` 的同型 regex 收斂到這裡。
"""
from __future__ import annotations

import re

MASK = '<redacted>'

_JWT_RE = re.compile(r'eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}')
_GENERIC_JWT_RE = re.compile(r'\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b')
_BEARER_RE = re.compile(r'(?i)(bearer\s+)[^\s"\',;}]+')
_NAP_RE = re.compile(r'nap_[A-Za-z0-9_-]{4,}')
_SK_RE = re.compile(r'sk-[A-Za-z0-9_-]{4,}')
_SECRET_KV_RE = re.compile(
    r'(?i)("?(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|'
    r'proof[_-]?token|token|key|x-security-proof|x-woow-proxy-sig|authorization)"?)'
    r'(\s*(?::|=|%3D)\s*)'
    r'("[^"]*"|\'[^\']*\'|[^\s"\',}&]+)')


def redact(value) -> str:
    """回傳遮罩後的字串；非字串先 ``str()``。永不拋例外（遮罩失敗就整段換掉）。"""
    try:
        text = value if isinstance(value, str) else str(value)
        text = _SECRET_KV_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
        text = _BEARER_RE.sub(lambda m: m.group(1) + MASK, text)
        text = _JWT_RE.sub(MASK, text)
        text = _GENERIC_JWT_RE.sub(MASK, text)
        text = _NAP_RE.sub(MASK, text)
        text = _SK_RE.sub(MASK, text)
        return text
    except Exception:  # noqa: BLE001 - a redaction helper must never raise
        return MASK

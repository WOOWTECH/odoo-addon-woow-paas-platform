"""平台共用的遮罩（DESIGN §5.5、〔REV〕3；票 LLM-01b 建立、LLM-02b 收斂成共用）。

所有會落地的字串——Odoo log、例外 traceback、``llm_event.details_json``、訂單
``evidence_json``、incident snapshot、post-deploy init 的 attempt 記錄
（``models/cloud_service_init_attempt.mask_init_error``）——都先過 :func:`redact`。
遮的是「拿到就能用」的東西：

* ``Bearer <任何值>``、JWT（三段 base64url）、``X-Security-Proof``／``proof_token``；
* ``Authorization``／``Proxy-Authorization`` 的**整個值**，不論 scheme（``Basic``、``Token``、``Digest``
  的參數列……；審查 LLM-02b 第 2 輪：原本只遮到 scheme 那個字，``Basic`` 的憑證留下來）；
* new-api 的 PAT ``nap_…``、消費者金鑰 ``sk-…``；
* ``password=…``／``"password": "…"``／``'api_key': '…'``／``token: …``／URL-encoded
  ``password%3D…`` 這類 key=value 形狀（key 可以被單引號或雙引號包住，值可以是含空白的
  引號字串）；
* new-api reveal 回的 48 字元裸金鑰只在 ``"key": "…"`` 形狀出現時遮（單獨一串英數
  無法和一般 id 區分，所以 client 端本來就不把 reveal 的回應寫進 log）。

LLM-02b 把 ``models/cloud_service_init_attempt.py`` 原本自己的三條 regex（JWT、Bearer、
key=value）收斂到這裡：那三條的每一種形狀這裡都涵蓋（``tests/test_llm_services.py``
``TestRedact`` 與 ``test_init_execution_control_1257`` 兩邊都驗），另外多遮 ``sk-``、``nap_``、
``proof_token``、``authorization``（任何 scheme）、``x-woow-proxy-sig`` 與一般的 ``key``。寧可多遮：
這裡的輸出只給人讀 log，不會被程式解析。

純 Python、不 import odoo：``tests/test_llm_services.py`` 在沒有 Odoo 的環境直接載入。
"""
from __future__ import annotations

import re

MASK = '<redacted>'

_JWT_RE = re.compile(r'eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}')
_GENERIC_JWT_RE = re.compile(r'\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b')
_BEARER_RE = re.compile(r'(?i)(bearer\s+)[^\s"\',;}]+')
# 左邊界（審查 LLM-02b finding 12）：``disk-pressure``、``task-runner``、``snap_…`` 這種一般的字不能被
# 遮成 ``di***``。真的金鑰是 ``sk-`` 加 48 字元（DESIGN §5.5 的掃描式是 ``sk-[A-Za-z0-9]{20,}``）。
_NAP_RE = re.compile(r'(?<![A-Za-z0-9])nap_[A-Za-z0-9_-]{4,}')
_SK_RE = re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}')
# key=value：名稱前面不能緊接英數（``monkey=1`` 不是 ``key=1``）——只限裸的 ``key``。``token``、
# ``secret``、``password`` 刻意維持「後綴也算」：post-deploy init 原本的 regex 就是這樣遮
# ``authtoken=…``（ngrok）、``apitoken=…`` 的，收斂到這裡不能少遮。
_SECRET_KV_RE = re.compile(
    r'(?i)(["\']?(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|'
    r'proof[_-]?token|token|(?<![A-Za-z0-9])key|x-security-proof|x-woow-proxy-sig)["\']?)'
    r'(\s*(?::|=|%3D)\s*)'
    r'("[^"]*"|\'[^\']*\'|[^\s"\',}&]+)')
# Authorization 的值是「scheme＋憑證」：通用 key=value 的值遇到空白就停，只會遮到 scheme 那個字
# （``Authorization: <redacted> dXNlcjpwYXNz``）。這一條吃掉整個值——引號字串（含跳脫的引號）、
# 或「可選的 scheme＋一串 ``k=v`` 參數（Digest）或一個 token（Basic、Token、Bearer 之後的值）」。
_AUTH_PARAM = r'[A-Za-z0-9_-]+\s*=\s*(?:"(?:[^"\\]|\\.)*"|[^\s",;}]*)'
_AUTHORIZATION_RE = re.compile(
    r'(?i)((?:proxy-)?authorization["\']?\s*(?::|=|%3D)\s*)'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|'
    r'(?:[A-Za-z][A-Za-z0-9_-]*\s+)?(?:' + _AUTH_PARAM + r'(?:\s*,\s*' + _AUTH_PARAM + r')*'
    r'|[^\s"\',;}]+))')


def redact(value, mask: str = MASK) -> str:
    """回傳遮罩後的字串；非字串先 ``str()``。永不拋例外（遮罩失敗就整段換成 ``mask``）。

    ``mask``：替換用的字樣（預設 ``<redacted>``；post-deploy init 的 attempt 記錄沿用它
    原本的 ``***``）。
    """
    try:
        text = value if isinstance(value, str) else str(value)
        # Bearer 一定要在 key=value 之前：``Authorization: Bearer <值>`` 先過 key=value 的話，
        # 被遮的是「Bearer」這個字，真正的值反而留下來（LLM-02b 收斂時抓到的順序問題）。
        text = _BEARER_RE.sub(lambda m: m.group(1) + mask, text)
        # Authorization 也要在通用 key=value 之前（同上的理由，而且它吃掉整個值、不論 scheme）。
        text = _AUTHORIZATION_RE.sub(lambda m: m.group(1) + mask, text)
        text = _SECRET_KV_RE.sub(lambda m: m.group(1) + m.group(2) + mask, text)
        text = _JWT_RE.sub(mask, text)
        text = _GENERIC_JWT_RE.sub(mask, text)
        text = _NAP_RE.sub(mask, text)
        text = _SK_RE.sub(mask, text)
        return text
    except Exception:  # noqa: BLE001 - a redaction helper must never raise
        return mask

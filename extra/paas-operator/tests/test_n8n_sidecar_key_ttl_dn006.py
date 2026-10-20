"""DATA-N8N-006-1（後半）：mcp-sidecar 的 n8n API key 要有有限期限，並在 init 重跑時輪替。

商用驗收（n8n 250）：sidecar key 的 ``expiresAt`` 是 4102444800（2100-01-01）＝永不過期；key 的明文
在 Secret 與租戶可能複製過去的地方，一旦外洩就永久有效。#1766 收斂了 scope，期限沒動。

修法：
- 兩條簽發路徑（首次開通的 ``POST /rest/api-keys``、owner 已存在時的 ``ensure_api_key.js``）都改用
  「現在＋``_API_KEY_TTL_SECONDS``」當期限。
- ``ensure_api_key.js`` 沿用既有 key 前先看它的 JWT ``exp``：沒有期限、期限比政策上限還遠（2100 的舊
  key）、或剩不到 ``_API_KEY_ROTATE_BEFORE_SECONDS`` ⇒ 不沿用，換一把新的。Redeploy／重試開通都會
  經過這裡，所以 key 在 init 重跑時輪替。
"""
import base64
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.services import n8n_init
from tests.test_init_error_kind_1263 import _http, _n8n_k8s, _post_init_n8n
from tests.test_n8n_init_owner_changed_1534 import _FakeN8n

_JS = Path(n8n_init.__file__).parent / "n8n_scripts" / "ensure_api_key.js"
_DAY = 86400
_NOW = 1_792_000_000  # 2026-10-14 前後；固定下來讓期限可預測


def test_policy_constants_are_finite_and_sane():
    ttl = n8n_init._API_KEY_TTL_SECONDS
    rotate = n8n_init._API_KEY_ROTATE_BEFORE_SECONDS
    # 驗收條件：期限 ≤ 1 年（DATA-N8N-006 checks.json「expiry <= 1 year」）
    assert 0 < ttl <= 365 * _DAY
    # 輪替窗口要比期限短，否則每次 init 都換；也要夠長，讓「偶爾 Redeploy」的服務來得及換
    assert 0 < rotate < ttl
    assert rotate >= 30 * _DAY


def test_expiry_is_now_plus_ttl():
    assert n8n_init.api_key_expires_at(_NOW) == _NOW + n8n_init._API_KEY_TTL_SECONDS
    with patch("src.services.n8n_init.time.time", return_value=float(_NOW)):
        assert n8n_init.api_key_expires_at() == _NOW + n8n_init._API_KEY_TTL_SECONDS


def test_the_2100_constant_is_gone():
    source = Path(n8n_init.__file__).read_text(encoding="utf-8")
    assert "4102444800" not in source
    assert "4102444800" not in _JS.read_text(encoding="utf-8")


# ---- 首次開通（REST）路徑 -------------------------------------------------------


def _posted_key_body(k8s):
    for call in k8s.exec_in_pod.call_args_list:
        script = call.kwargs.get("command", call.args[3] if len(call.args) > 3 else [""])[-1]
        if 'method:"POST"' in script and 'path:"/rest/api-keys"' in script:
            literal = re.search(r"const d=(\".*?\");const req", script, re.S).group(1)
            return json.loads(json.loads(literal))
    raise AssertionError("no POST /rest/api-keys was made")


def test_rest_path_key_expires_within_the_ttl():
    k8s = _n8n_k8s({("GET", "/rest/api-keys/scopes"): _http(200, json.dumps({"data": ["workflow:read"]}))})
    before = int(time.time())
    body = _post_init_n8n(k8s).json()
    after = int(time.time())

    assert body["success"] is True
    expires = _posted_key_body(k8s)["expiresAt"]
    ttl = n8n_init._API_KEY_TTL_SECONDS
    assert before + ttl <= expires <= after + ttl


# ---- owner 已存在（ensure_api_key.js）路徑 ---------------------------------------


def test_ensure_path_sends_a_finite_expiry_and_the_rotation_window():
    fake = _FakeN8n()
    before = int(time.time())
    fake.post(owner_confirmed=True)
    after = int(time.time())

    stdin = json.loads(fake.ensure_calls[0]["stdin"])
    ttl = n8n_init._API_KEY_TTL_SECONDS
    assert before + ttl <= stdin["expires_at"] <= after + ttl
    assert stdin["rotate_before_seconds"] == n8n_init._API_KEY_ROTATE_BEFORE_SECONDS


# ---- 腳本的沿用判斷（node 實跑）------------------------------------------------


def _jwt(payload) -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(payload)}.c2ln"


def _js_policy_source() -> str:
    text = _JS.read_text(encoding="utf-8")
    start = text.index("// ---- sidecar key expiry policy")
    end = text.index("// ---- end sidecar key expiry policy ----")
    return text[start:end]


def _run_js(cases):
    program = (
        _js_policy_source()
        + "\nconst cases = JSON.parse(process.argv[1]);"
        + "\nconsole.log(JSON.stringify(cases.map((c) => ({"
        + " exp: apiKeyExpiry(c.key),"
        + " fresh: isFreshEnough(c.key, c.now, c.expiresAt, c.rotateBefore) }))));"
    )
    out = subprocess.run(["node", "-e", program, json.dumps(cases)],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_reuse_decision():
    ttl = n8n_init._API_KEY_TTL_SECONDS
    rotate = n8n_init._API_KEY_ROTATE_BEFORE_SECONDS
    upper = _NOW + ttl
    base = {"now": _NOW, "expiresAt": upper, "rotateBefore": rotate}
    cases = {
        # 2100 的舊 key：比政策上限還遠 ⇒ 換
        "legacy_2100": {**base, "key": _jwt({"sub": "u", "exp": 4102444800})},
        # 沒有 exp 的 key（永不過期）⇒ 換
        "no_exp": {**base, "key": _jwt({"sub": "u"})},
        # 剩 10 天 ⇒ 換
        "near_expiry": {**base, "key": _jwt({"sub": "u", "exp": _NOW + 10 * _DAY})},
        # 已過期 ⇒ 換
        "expired": {**base, "key": _jwt({"sub": "u", "exp": _NOW - _DAY})},
        # 剛好在輪替窗口邊界 ⇒ 換（不留到最後一刻）
        "at_window": {**base, "key": _jwt({"sub": "u", "exp": _NOW + rotate})},
        # 剛簽不久（剩接近完整期限）⇒ 沿用
        "fresh": {**base, "key": _jwt({"sub": "u", "exp": upper - _DAY})},
        # 剛簽、operator 與 pod 時鐘差幾分鐘 ⇒ 仍沿用
        "fresh_clock_skew": {**base, "key": _jwt({"sub": "u", "exp": upper + 300})},
        # 不是 JWT、payload 壞掉、exp 不是數字 ⇒ 換
        "not_jwt": {**base, "key": "n8n_api_abcdef"},
        "bad_payload": {**base, "key": "aaa.%%%.bbb"},
        "exp_string": {**base, "key": _jwt({"sub": "u", "exp": "4102444800"})},
        "null_key": {**base, "key": None},
    }
    names = list(cases)
    result = dict(zip(names, _run_js([cases[n] for n in names])))

    assert result["legacy_2100"]["exp"] == 4102444800
    assert result["fresh"]["exp"] == upper - _DAY
    assert result["not_jwt"]["exp"] is None and result["exp_string"]["exp"] is None
    reusable = sorted(n for n, r in result.items() if r["fresh"])
    assert reusable == ["fresh", "fresh_clock_skew"], result


def test_js_reuse_path_checks_the_expiry_and_reports_it():
    text = _JS.read_text(encoding="utf-8")
    # 沿用前三道檢查都要有：scope（#1766）、期限（本修正）、n8n 公開 API 自我驗證
    assert re.search(
        r"hasOnlySidecarScopes\(row\.scopes\)\s*&&\s*isFreshEnough\(row\.apiKey, now, expiresAt, rotateBefore\)"
        r"\s*&&\s*\(await apiCheck\(row\.apiKey\)\) === 200",
        text,
    ), "reuse condition must check scope, expiry, then n8n itself"
    # 腳本回報期限（operator log 用；不含 key 本身）
    assert "expires_at: apiKeyExpiry(" in text
    # 簽新 key 時一定帶 exp：期限缺漏時腳本要拒絕，不要退回永不過期
    assert "payload.exp = expiresAt" in text
    assert re.search(r'EnsureFailure\("invalid_input", "expires_at', text)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_script_still_parses():
    out = subprocess.run(["node", "--check", str(_JS)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_operator_logs_the_expiry_but_never_the_key():
    fake = _FakeN8n()
    fake.ensure = type(fake.ensure)(stdout=json.dumps(
        {"success": True, "api_key": "key-secret-xyz", "reused": False, "expires_at": _NOW + 365 * _DAY}) + "\n")
    with patch.object(n8n_init.logger, "info") as info:
        body = fake.post(owner_confirmed=True).json()
    assert body["success"] is True
    logged = " ".join(str(a) for call in info.call_args_list for a in call.args)
    assert "key-secret-xyz" not in logged
    assert "expires" in logged

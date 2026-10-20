"""INFRA-TS-005-5：登入失敗的原因要能從 /woow 看到，而不是只在容器日誌。

商用驗收 INFRA-TS-005（Tailscale 242）：

* 節點需要登入時，key 過期／用過／不對的錯誤只出現在容器日誌
  （``Received error: authkey expired``）；畫面只有通用的「需要重新登入」。
* 已登入的節點會**靜默忽略**設定裡的 auth key（entrypoint 日誌
  ``authkey is set but the node is already logged in (Running); not using it``），
  租戶以為換了 key 有效。

tailscaled 自己把「上一次登入錯誤」放在 ``tailscale status --json`` 的 ``Health``
（日誌那行 ``health(warnable=login-state): error: You are logged out. The last login error
was: authkey expired`` 就是同一句），所以不必讀日誌：operator 把它對應成**封閉代碼**
``login_error``，原文永不回傳。auth key 有沒有設定則由探測腳本只回 ``set``／``unset``
（不讀出 key 本身）。
"""
import json
import subprocess

import pytest

from src.models.schemas import VpnNodeStatusResponse
from src.services import vpn_node_status as v

NS, REL = "paas-ws-5e41efa6", "svc-f06c5feb"
HS_URL = f"http://svc-0b4a8def-headscale.{NS}.svc.cluster.local:8080"
LOGGED_OUT = "You are logged out. The last login error was: "


def _status(state="NeedsLogin", health=()):
    return {"BackendState": state, "AuthURL": "", "TailscaleIPs": [], "Self": {},
            "Health": list(health)}


def _output(status, authkey=None):
    parts = [v.SENTINEL_STATUS, json.dumps(status), v.SENTINEL_SERVE, "", v.SENTINEL_WANTED,
             v.NO_PARSER_MARK]
    if authkey is not None:
        parts += [v.SENTINEL_AUTHKEY, authkey]
    return "\n".join(parts) + "\n"


def _report(status, authkey=None):
    return v.build_report(pod=f"{REL}-0", probe=v.PROBE_OK, output=_output(status, authkey),
                          service_names=frozenset())


class TestLoginErrorCodes:
    @pytest.mark.parametrize("message, code", [
        # TS-005 p10 的原文（Headscale 0.29）。
        ("authkey expired", "authkey_expired"),
        ("AuthKey expired", "authkey_expired"),
        ("authkey already used", "authkey_used"),
        ("AuthKey has already been used", "authkey_used"),
        ("AuthKey not found", "authkey_invalid"),
        ("invalid authkey", "authkey_invalid"),
        ("invalid key: unable to validate API key", "authkey_invalid"),
        ("user mismatch", "authkey_invalid"),
        # TS-005 p3／p8 的原文：控制面連不到（含 CrashLoop 時的 :443 陷阱）。
        (f'register request: Post "https://svc-0b4a8def-headscale.{NS}.svc.cluster.local:8080/'
         'machine/register": all connection attempts failed (HTTP: TLS forced: no port 80 dialed, '
         'HTTPS: dial tcp 10.43.199.119:443: connect: connection refused)', "control_unreachable"),
        (f'fetch control key: Get "{HS_URL}/key?v=142": dial tcp 10.43.199.119:8080: '
         'connect: connection refused', "control_unreachable"),
        ('fetch control key: Get "https://h.example.com/key": dial tcp: lookup h.example.com: '
         'no such host', "control_unreachable"),
        ("something the node has never said before", "other"),
    ])
    def test_last_login_error_becomes_a_closed_code(self, message, code):
        r = _report(_status(health=[LOGGED_OUT + message]))
        assert r["login_error"] == code

    def test_the_raw_message_never_reaches_the_response(self):
        r = _report(_status(health=[LOGGED_OUT + "authkey expired 10.43.199.119 secret-ish"]))
        dumped = json.dumps(r)
        assert "last login error" not in dumped.lower()
        assert "10.43.199.119" not in dumped
        assert "secret-ish" not in dumped

    def test_logged_out_without_an_error_is_not_an_error(self):
        assert _report(_status(health=["You are logged out."]))["login_error"] is None

    def test_unrelated_health_warnings_are_not_login_errors(self):
        health = ["Tailscale could not connect to the 'nyc' relay server.",
                  "Some peers are advertising routes but --accept-routes is false"]
        assert _report(_status(health=health))["login_error"] is None

    def test_a_running_node_has_no_login_error(self):
        """Running 後 tailscaled 會清掉 login-state；就算殘留也不能讓綠燈節點顯示登入失敗。"""
        r = _report(_status("Running", health=[LOGGED_OUT + "authkey expired"]))
        assert r["login_error"] is None

    def test_health_shape_is_not_trusted(self):
        for junk in (None, "authkey expired", {"x": LOGGED_OUT + "authkey expired"},
                     [None, 5, {"a": 1}]):
            status = _status()
            status["Health"] = junk
            assert _report(status)["login_error"] is None

    def test_no_status_means_no_login_error(self):
        r = v.build_report(pod=None, probe=v.PROBE_NO_POD)
        assert r["login_error"] is None


class TestAuthKeyConfigured:
    @pytest.mark.parametrize("raw, expected", [
        ("set", True), ("unset", False), ("garbage", None), ("", None), (None, None),
    ])
    def test_only_set_or_unset_is_believed(self, raw, expected):
        assert _report(_status("Running"), authkey=raw)["authkey_configured"] is expected

    def test_probe_script_never_prints_the_key(self):
        """腳本只判斷「檔案有沒有非空白內容」，不 cat、不 echo 檔案內容。"""
        script = v.PROBE_SCRIPT
        assert v.SENTINEL_AUTHKEY in script
        tail = script.split(v.SENTINEL_AUTHKEY, 1)[1]
        assert "cat " not in tail and "head " not in tail and "$(" not in tail
        assert "grep -q" in tail

    def test_probe_script_reports_set_and_unset_without_leaking(self, tmp_path):
        key_file = tmp_path / "authkey"
        tail = "echo '" + v.SENTINEL_AUTHKEY + "'" + v.PROBE_SCRIPT.split(
            "echo '" + v.SENTINEL_AUTHKEY + "'", 1)[1]
        for content, expected in (("hskey-auth-SECRET123\n", "set"), (" \n", "unset"), (None, "unset")):
            if content is None:
                key_file.unlink(missing_ok=True)
            else:
                key_file.write_text(content)
            out = subprocess.run(["sh", "-c", tail], capture_output=True, text=True, timeout=10,
                                 env={"PATH": "/usr/bin:/bin", "TS_AUTHKEY_FILE": str(key_file)})
            assert out.stdout.split() == [v.SENTINEL_AUTHKEY, expected]
            assert "SECRET123" not in out.stdout + out.stderr


class TestResponseModel:
    def test_new_fields_are_part_of_the_declared_model(self):
        r = _report(_status(health=[LOGGED_OUT + "authkey expired"]), authkey="set")
        model = VpnNodeStatusResponse(**r)
        assert model.login_error == "authkey_expired"
        assert model.authkey_configured is True

    def test_defaults_for_an_old_report_shape(self):
        model = VpnNodeStatusResponse(probe="no_pod")
        assert model.login_error is None and model.authkey_configured is None

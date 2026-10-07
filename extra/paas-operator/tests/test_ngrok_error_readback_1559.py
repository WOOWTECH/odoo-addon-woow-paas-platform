"""#1559：ngrok agent 連不上時，原因從 sidecar log 讀回（封閉集合的代碼），服務頁才不會只寫「尚未取得」。

2026-10-07 實測（EMQX、Headscale，假 authtoken）：原因 ``ERR_NGROK_105`` 只在 sidecar log。
守線同 ``test_public_address_readback``：**永不回 log 內容**——ngrok 認證失敗時會把 authtoken
印回來（``Your authtoken: …``），EMQX 的 sidecar 沒有過濾；所以只回代碼與固定文案。
"""
from unittest.mock import MagicMock, patch

import pytest

from src.api.releases import get_mqtt_address, get_public_address
from src.models.schemas import PodPhase
from src.services.helm import KubernetesService
from src.services.ngrok_errors import describe_ngrok_error

NS, REL = "paas-ws-x", "svc-aaaa1111"
TOKEN = "2abcDEFghiJKLmnoPQRstuVWXyz_0123456789abcdEF"

# ngrok 3.x 對假 token 的實際輸出（EMQX：未過濾；Headscale：經 redact）＋ sidecar 自己的行。
EMQX_BAD_TOKEN = f"""t=2026-10-07T13:40:01+0000 lvl=info msg="open config file" path=/var/lib/ngrok/ngrok.yml err=nil
ERROR:  authentication failed: The authtoken you specified does not look like a proper ngrok tunnel authtoken.
ERROR:  Your authtoken: {TOKEN}
ERROR:  Instructions to install your authtoken are on your ngrok dashboard:
ERROR:  https://dashboard.ngrok.com/get-started/your-authtoken
ERROR:
ERROR:  ERR_NGROK_105
ERROR:  https://ngrok.com/docs/errors/err_ngrok_105
ERROR:
WOOW_MQTT_PUBLIC_URL=
ngrok agent exited; retrying in 80s (check the authtoken and reserved address in the service settings)"""

HEADSCALE_BAD_TOKEN = """[ngrok] 啟動 ngrok：https://p2-invalid-test.ngrok.app → 127.0.0.1:8080
ERROR:  Your authtoken: [REDACTED]
ERROR:  ERR_NGROK_105
ERROR:  https://ngrok.com/docs/errors/err_ngrok_105
WOOW_PUBLIC_URL=
[ngrok] ngrok agent 已結束：authtoken 不正確（請到 ngrok 後台 Your Authtoken 頁複製後重新填入服務設定）；10 秒後重試
WOOW_PUBLIC_URL="""


def _pod(name="svc-aaaa1111-emqx-0"):
    pod = MagicMock()
    pod.name = name
    pod.phase = PodPhase.RUNNING
    pod.deletion_timestamp = None
    return pod


def _svc(stdout):
    svc = KubernetesService()
    svc.get_pods = MagicMock(return_value=[_pod()])
    result = MagicMock()
    result.stdout = stdout
    svc._run_command = MagicMock(return_value=result)
    return svc


def _status(stdout, **kw):
    return _svc(stdout).get_public_address_status(NS, REL, **kw)


class TestErrorCodes:

    def test_bad_token_emqx(self):
        assert _status(EMQX_BAD_TOKEN) == (None, "ERR_NGROK_105")

    def test_bad_token_headscale(self):
        assert _status(HEADSCALE_BAD_TOKEN) == (None, "ERR_NGROK_105")

    @pytest.mark.parametrize("line", [
        "ngrok authtoken is not set: enter your own ngrok authtoken in the service settings to get a public MQTT address",
        "[ngrok] 未設定 ngrok authtoken：請在服務設定填入你自己的 ngrok authtoken（https://dashboard.ngrok.com/get-started/your-authtoken）。在那之前 Headscale 只用叢集內網址。",
    ])
    def test_no_token(self, line):
        assert _status(f"WOOW_PUBLIC_URL=\n{line}\nWOOW_PUBLIC_URL=") == (None, "NO_AUTHTOKEN")

    def test_server_url_mismatch(self):
        log = ("[ngrok] ngrok 已連上 https://a.ngrok-free.app，但 Headscale 目前使用的 server_url 是 "
               "http://svc-x-headscale.paas-ws-x.svc.cluster.local:8080（啟動時 ngrok 還沒連上，或網址變了）："
               "重新啟動服務後才會改用 ngrok 網址\nWOOW_PUBLIC_URL=")
        assert _status(log) == (None, "SERVER_URL_MISMATCH")

    def test_last_error_wins(self):
        log = "ERROR:  ERR_NGROK_105\nWOOW_PUBLIC_URL=\nERROR:  ERR_NGROK_334\nWOOW_PUBLIC_URL="
        assert _status(log) == (None, "ERR_NGROK_334")

    def test_reconnected_clears_the_error(self):
        log = EMQX_BAD_TOKEN + "\nWOOW_MQTT_PUBLIC_URL=tcp://6.tcp.ngrok.io:22222"
        assert _status(log) == ("tcp://6.tcp.ngrok.io:22222", None)

    def test_error_after_success_with_retraction(self):
        log = "WOOW_PUBLIC_URL=https://a.ngrok-free.app\n" + HEADSCALE_BAD_TOKEN
        assert _status(log) == (None, "ERR_NGROK_105")

    def test_no_sidecar_is_not_an_error(self):
        from src.services.helm import KubectlException
        svc = _svc("")
        svc._run_command = MagicMock(side_effect=KubectlException(
            message="container ngrok not found", command="kubectl logs", stderr=""))
        assert svc.get_public_address_status(NS, REL) == (None, None)

    def test_plain_retraction_has_no_reason(self):
        assert _status("WOOW_PUBLIC_URL=") == (None, None)


class TestErrorDetectionIsAnchored:
    """原因碼只認整行的形狀：訪客可控的字串混在 ngrok 的 logfmt 行裡不算。"""

    @pytest.mark.parametrize("line", [
        't=2026 lvl=info msg="join connections" obj=join id=1 l=127.0.0.1:8080 r=1.2.3.4 path="/ERROR:  ERR_NGROK_105"',
        "x ERROR:  ERR_NGROK_105",
        "ERROR:  ERR_NGROK_105 trailing-garbage",
        "ERROR:  ERR_NGROK_",
        "ERROR:  ERR_NGROK_1234567",
        "  ngrok authtoken is not set",
    ])
    def test_not_an_error_line(self, line):
        assert KubernetesService._ngrok_error_code(line) is None

    def test_wrapper_still_returns_only_the_address(self):
        assert _svc(EMQX_BAD_TOKEN).get_advertised_public_address(NS, REL) is None


class TestEndpoint:

    def test_bad_token_is_reported_without_log_content(self):
        svc = _svc(EMQX_BAD_TOKEN)
        with patch("src.api.releases.k8s_service", new=svc):
            resp = get_public_address(NS, REL)
        assert resp.public_url is None
        assert resp.error_code == "ERR_NGROK_105"
        assert "authtoken 不正確" in resp.error
        body = resp.model_dump_json()
        assert TOKEN not in body, "ngrok 印回來的 authtoken 不得經 operator 外流"
        assert "Your authtoken" not in body

    def test_address_present_has_no_error(self):
        svc = _svc("ERROR:  ERR_NGROK_105\nWOOW_PUBLIC_URL=https://a.ngrok-free.app")
        with patch("src.api.releases.k8s_service", new=svc):
            resp = get_public_address(NS, REL)
        assert resp.public_url == "https://a.ngrok-free.app"
        assert resp.error_code is None and resp.error is None

    def test_legacy_endpoint_shape_unchanged(self):
        svc = _svc(EMQX_BAD_TOKEN)
        with patch("src.api.releases.k8s_service", new=svc):
            resp = get_mqtt_address(NS, REL)
        assert resp.model_dump() == {"public_url": None}


class TestDescriptions:

    @pytest.mark.parametrize("code", [
        "NO_AUTHTOKEN", "ERR_NGROK_105", "ERR_NGROK_107", "ERR_NGROK_108",
        "ERR_NGROK_334", "ERR_NGROK_4018", "SERVER_URL_MISMATCH",
    ])
    def test_known_codes_have_chinese_text(self, code):
        text = describe_ngrok_error(code)
        assert text and any("一" <= ch <= "鿿" for ch in text)

    def test_unknown_ngrok_code_links_to_docs(self):
        text = describe_ngrok_error("ERR_NGROK_9999")
        assert "https://ngrok.com/docs/errors/err_ngrok_9999" in text

    @pytest.mark.parametrize("code", [None, "", "SOMETHING_ELSE", "ERR_NGROK_x"])
    def test_nothing_for_unknown_shapes(self, code):
        assert describe_ngrok_error(code) is None

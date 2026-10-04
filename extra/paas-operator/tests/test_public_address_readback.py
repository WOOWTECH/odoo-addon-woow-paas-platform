"""對外位址回讀（ngrok sidecar 的 log 契約；charts/emqx 0.1.0 起、charts/headscale）。

## 契約（chart 端照這個印）

sidecar 容器名固定 ``ngrok``，每 60 秒在**行首**印一行：

- ``WOOW_PUBLIC_URL=https://<host>[:<port>]``——ngrok HTTP endpoint（Headscale 控制網址）
- ``WOOW_PUBLIC_URL=tcp://<host>:<port>``——ngrok TCP endpoint（EMQX 原生 MQTT）
- ``WOOW_PUBLIC_URL=``（空值）——撤回：agent 退出、沒填 token、連線失敗
- ``WOOW_MQTT_PUBLIC_URL=…``——EMQX 0.1.x 的舊 marker，語意相同，繼續認

## 為什麼讀 container log，而不是別的

把 agent 的 ``:4040`` 開成 Service 等於讓同一租戶 namespace 的其他服務去**建立**
隧道（那個 API 可以）；讓 sidecar 寫 ConfigMap 則要掛 SA token ＋ RBAC，而 chart 是
``automountToken=false``。

## 守線

1. **只回位址、永不回 log 內容**：來源是第三方（ngrok agent）的輸出，整段轉出去
   等於把不受控字串接到 Odoo 的 HTTP 回應上。形狀對不上就當沒有（fail-closed）。
2. **marker 只認行首**：同一個 stdout 也有 ngrok agent 自己的 log，HTTP endpoint 的
   那幾行可能帶外部訪客可控的字串。marker 能出現在行中間的話，訪客打一個
   ``/WOOW_PUBLIC_URL=https://evil.example`` 就能讓平台把攻擊者的網址顯示成租戶的
   Headscale 控制網址。
3. **取最後一筆**：sidecar 會重印，最後一筆才是 agent 當前持有的位址。最後一筆是
   空值（撤回）就回 None。兩種 marker 一視同仁、依出現順序比。
4. **「沒有位址」不是錯誤**：租戶沒開（pod 裡沒有 ngrok 容器 ⇒ kubectl logs 失敗）、
   agent 還沒連上、pod 還在起來——都回 ``public_url: null``。
5. **舊端點 ``/mqtt-address`` 行為不變**：平台 ≤ .205 仍會呼叫，operator 先上線；
   它只回 ``tcp://``。
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.releases import get_mqtt_address, get_public_address
from src.models.schemas import PodPhase
from src.services.helm import KubectlException, KubernetesService

MARKER = "WOOW_PUBLIC_URL="
LEGACY = "WOOW_MQTT_PUBLIC_URL="
NS, REL = "paas-ws-x", "svc-aaaa1111"


def _pod(name="svc-aaaa1111-headscale-0", deletion_timestamp=None):
    pod = MagicMock()
    pod.name = name
    pod.phase = PodPhase.RUNNING
    pod.deletion_timestamp = deletion_timestamp
    return pod


def _svc_with_log(stdout, pods=None):
    svc = KubernetesService()
    svc.get_pods = MagicMock(return_value=pods if pods is not None else [_pod()])
    result = MagicMock()
    result.stdout = stdout
    svc._run_command = MagicMock(return_value=result)
    return svc


def _read(stdout, **kw):
    return _svc_with_log(stdout).get_advertised_public_address(NS, REL, **kw)


class TestAcceptedShapes:

    @pytest.mark.parametrize("url", [
        "https://abc-123.ngrok-free.app",
        "https://vpn.example.com",
        "https://vpn.example.com:8443",
        "https://A-B.Example.COM",          # 大小寫原樣回傳（DNS 不分大小寫）
        "tcp://0.tcp.ngrok.io:11111",
        "tcp://5.tcp.eu.ngrok.io:65535",
    ])
    def test_generic_marker(self, url):
        assert _read(f"{MARKER}{url}") == url

    def test_legacy_marker_still_works(self):
        """EMQX 0.1.x 的舊 marker——operator 先上線，既有 EMQX 不得受影響。"""
        assert _read(f"{LEGACY}tcp://6.tcp.ngrok.io:22222") == "tcp://6.tcp.ngrok.io:22222"

    def test_surrounding_whitespace_and_crlf_are_tolerated(self):
        assert _read(f"{MARKER}https://a.ngrok-free.app \r\n") == "https://a.ngrok-free.app"


class TestLastValueWins:

    def test_returns_the_last_advertised_address(self):
        assert _read("\n".join([
            't=2026-10-02T03:00:00+0000 lvl=info msg="started tunnel"',
            f"{MARKER}https://old.ngrok-free.app",
            f"{MARKER}https://new.ngrok-free.app",
        ])) == "https://new.ngrok-free.app"

    def test_markers_are_compared_in_log_order(self):
        """chart 升版途中兩種 marker 可能並存：以出現順序取最後一筆，不偏袒任一種。"""
        assert _read("\n".join([
            f"{LEGACY}tcp://1.tcp.ngrok.io:11111",
            f"{MARKER}tcp://2.tcp.ngrok.io:22222",
        ])) == "tcp://2.tcp.ngrok.io:22222"
        assert _read("\n".join([
            f"{MARKER}tcp://2.tcp.ngrok.io:22222",
            f"{LEGACY}tcp://1.tcp.ngrok.io:11111",
        ])) == "tcp://1.tcp.ngrok.io:11111"

    @pytest.mark.parametrize("retract", [MARKER, LEGACY, f"{MARKER}   "])
    def test_an_empty_announcement_retracts_the_address(self, retract):
        """agent 死掉時 sidecar 印空值撤回；之後的最後一筆才算數。"""
        assert _read("\n".join([
            f"{MARKER}https://a.ngrok-free.app",
            retract,
            "ngrok agent exited; retrying in 10s (check the authtoken …)",
        ])) is None

    def test_an_address_after_a_retraction_wins(self):
        assert _read("\n".join([
            f"{MARKER}",
            f"{MARKER}https://back.ngrok-free.app",
        ])) == "https://back.ngrok-free.app"

    def test_a_malformed_line_does_not_erase_the_last_good_one(self):
        """格式錯誤只是丟掉那一行（記 warning），不是撤回——撤回只有空值一種寫法。"""
        assert _read("\n".join([
            f"{MARKER}https://good.ngrok-free.app",
            f"{MARKER}https://evil.example.com/phish",
        ])) == "https://good.ngrok-free.app"


class TestMalformedIsDiscarded:
    """形狀對不上就當沒有——不得把來源字串原樣轉出去。"""

    @pytest.mark.parametrize("value", [
        "http://plain.example.com",                  # 只收 https（控制面不走明文）
        "https://evil.example.com/",                 # 結尾 /
        "https://evil.example.com/path",             # 路徑
        "https://evil.example.com?x=1",              # query
        "https://evil.example.com#frag",             # fragment
        "https://user:pw@evil.example.com",          # userinfo
        "https://evil.example.com.",                 # 結尾點
        "https://localhost",                         # 單一 label
        "https://-bad.example.com",                  # label 以 - 開頭
        "https://bad-.example.com",                  # label 以 - 結尾
        "https://a..example.com",                    # 空 label
        "https://under_score.example.com",           # 底線
        "https://exa mple.com",                      # 空白
        "https://evil.example.com:0",                # 埠超出範圍
        "https://evil.example.com:65536",
        "https://evil.example.com:",                 # 空埠
        "https://ëxample.com",                       # 非 ASCII（同形字）
        "https://example‮.com",                 # bidi 控制字元
        "tcp://host.example.com",                    # tcp 沒有埠
        "tcp://host.example.com:notaport",
        "tcp://6.tcp.ngrok.io:22222 ; rm -rf /",     # shell 注入
        "tcp://6.tcp.ngrok.io:22222\x1b[31m",        # ANSI escape
        "https://a.ngrok-free.app\x00",              # NUL
        "javascript:alert(1)",
        "<script>alert(1)</script>",
        "https://" + "a" * 64 + ".example.com",      # label > 63
        "https://" + ".".join(["a" * 63] * 4) + ".com",  # 總長 > 253
    ])
    def test_malformed_candidates_are_discarded(self, value):
        assert _read(f"{MARKER}{value}") is None

    def test_malformed_is_logged_without_the_value(self, caplog):
        with caplog.at_level("WARNING"):
            assert _read(f"{MARKER}https://evil.example.com/phish") is None
        assert "Discarding malformed" in caplog.text
        assert "evil.example.com" not in caplog.text


class TestMarkerMustStartTheLine:
    """ngrok agent 自己的 log 可能帶訪客可控字串：marker 不在行首就不認。"""

    @pytest.mark.parametrize("line", [
        f't=2026-10-04 lvl=info msg="GET /{MARKER}https://evil.example.com" ',
        f"path=/{MARKER}https://evil.example.com",
        f"x{MARKER}https://evil.example.com",
        f"garbage {LEGACY}tcp://6.tcp.ngrok.io:22222",
        f" {MARKER}https://evil.example.com",               # 行首有空白也不算
        f"\t{MARKER}https://evil.example.com",
        # Unicode／CR 換行字元不得在行中間造出新的「行首」（splitlines／strip 的坑）
        f'lvl=info msg="GET /x\u2028{MARKER}https://evil.example.com"',
        f'lvl=info msg="GET /x\x85{MARKER}https://evil.example.com"',
        f'lvl=info msg="GET /x\x1e{MARKER}https://evil.example.com"',
        f'lvl=info msg="GET /x\r{MARKER}https://evil.example.com"',
        f"\u2028{MARKER}https://evil.example.com",
    ])
    def test_mid_line_marker_is_ignored(self, line):
        assert _read(line) is None

    def test_mid_line_marker_does_not_override_a_real_announcement(self):
        assert _read("\n".join([
            f"{MARKER}https://real.ngrok-free.app",
            f'lvl=info msg="GET /{MARKER}https://evil.example.com"',
        ])) == "https://real.ngrok-free.app"

    def test_log_content_is_never_returned(self):
        secret_ish = 't=... lvl=info msg="authtoken 2abc…" sess=xyz'
        got = _read(f"{secret_ish}\n{MARKER}https://a.ngrok-free.app\n{secret_ish}")
        assert got == "https://a.ngrok-free.app"
        assert "authtoken" not in got


class TestSchemeFilter:

    def test_tcp_only_drops_https(self):
        """舊端點的過濾：https 對它而言是格式不符，丟掉、保留前一筆 tcp。"""
        assert _read("\n".join([
            f"{MARKER}tcp://6.tcp.ngrok.io:22222",
            f"{MARKER}https://a.ngrok-free.app",
        ]), schemes=("tcp",)) == "tcp://6.tcp.ngrok.io:22222"

    def test_tcp_only_with_https_alone_is_none(self):
        assert _read(f"{MARKER}https://a.ngrok-free.app", schemes=("tcp",)) is None


class TestPodsAndContainers:

    def test_reads_the_ngrok_container_of_that_release_only(self):
        svc = _svc_with_log(f"{MARKER}https://a.ngrok-free.app")
        svc.get_advertised_public_address(NS, REL)
        assert svc.get_pods.call_args.kwargs["label_selector"] == (
            "app.kubernetes.io/instance=svc-aaaa1111"
        )
        args = svc._run_command.call_args.args[0]
        assert args[0] == "logs"
        assert "--container" in args and args[args.index("--container") + 1] == "ngrok"
        assert any(a.startswith("--tail=") for a in args)

    def test_no_ngrok_container_is_not_an_error(self):
        """租戶沒開：pod 裡沒有 ngrok 容器 ⇒ kubectl logs 失敗 ⇒ None。"""
        svc = KubernetesService()
        svc.get_pods = MagicMock(return_value=[_pod()])
        svc._run_command = MagicMock(side_effect=KubectlException(
            message="kubectl command failed with code 1",
            command="kubectl logs …",
            stderr='container "ngrok" not found in pod',
        ))
        assert svc.get_advertised_public_address(NS, REL) is None

    def test_terminating_pods_are_skipped(self):
        """重新部署時的舊 pod 還在 Terminating，它的位址已經不是當前的那一個。"""
        svc = _svc_with_log(
            f"{MARKER}https://old.ngrok-free.app",
            pods=[_pod(deletion_timestamp="2026-10-02T03:00:00Z")],
        )
        assert svc.get_advertised_public_address(NS, REL) is None
        svc._run_command.assert_not_called()

    def test_no_pods_at_all(self):
        svc = _svc_with_log("", pods=[])
        assert svc.get_advertised_public_address(NS, REL) is None

    def test_namespace_outside_the_operator_scope_raises(self):
        svc = _svc_with_log(f"{MARKER}https://a.ngrok-free.app")
        with pytest.raises(ValueError):
            svc.get_advertised_public_address("kube-system", REL)


class TestPublicAddressEndpoint:

    def test_returns_the_address_with_both_schemes(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_public_address.return_value = "https://a.ngrok-free.app"
            resp = get_public_address(NS, REL)
        assert resp.public_url == "https://a.ngrok-free.app"
        assert k8s.get_advertised_public_address.call_args.kwargs["schemes"] == (
            "tcp", "https")

    def test_absent_address_is_200_with_null(self):
        """正常狀態（沒開／還沒連上）必須是 200 null，不是 4xx/5xx。"""
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_public_address.return_value = None
            resp = get_public_address(NS, REL)
        assert resp.public_url is None

    def test_namespace_out_of_scope_is_403(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_public_address.side_effect = ValueError(
                "Namespace 'kube-system' is not managed by this operator"
            )
            with pytest.raises(HTTPException) as e:
                get_public_address("kube-system", "whatever")
        assert e.value.status_code == 403

    def test_kubectl_failure_is_500_without_leaking_stderr(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_public_address.side_effect = KubectlException(
                message="kubectl command timed out after 15s",
                command="kubectl logs …",
                stderr="connection to the server was refused",
            )
            with pytest.raises(HTTPException) as e:
                get_public_address(NS, REL)
        assert e.value.status_code == 500
        assert "refused" not in str(e.value.detail)


class TestLegacyMqttAddressEndpoint:
    """平台 ≤ .205 呼叫的舊端點：行為與改版前相同（只回 tcp://）。"""

    def test_asks_for_tcp_only(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_public_address.return_value = "tcp://6.tcp.ngrok.io:22222"
            resp = get_mqtt_address(NS, REL)
        assert resp.public_url == "tcp://6.tcp.ngrok.io:22222"
        assert k8s.get_advertised_public_address.call_args.kwargs["schemes"] == ("tcp",)

    def test_end_to_end_with_legacy_marker(self):
        svc = _svc_with_log(f"{LEGACY}tcp://6.tcp.ngrok.io:22222")
        with patch("src.api.releases.k8s_service", new=svc):
            assert get_mqtt_address(NS, REL).public_url == "tcp://6.tcp.ngrok.io:22222"

    def test_end_to_end_never_returns_https(self):
        svc = _svc_with_log(f"{MARKER}https://a.ngrok-free.app")
        with patch("src.api.releases.k8s_service", new=svc):
            assert get_mqtt_address(NS, REL).public_url is None

    def test_absent_address_is_200_with_null(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_public_address.return_value = None
            assert get_mqtt_address(NS, REL).public_url is None

    def test_namespace_out_of_scope_is_403(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_public_address.side_effect = ValueError("nope")
            with pytest.raises(HTTPException) as e:
                get_mqtt_address("kube-system", "whatever")
        assert e.value.status_code == 403

    def test_kubectl_failure_is_500_without_leaking_stderr(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_public_address.side_effect = KubectlException(
                message="kubectl command timed out after 15s",
                command="kubectl logs …",
                stderr="connection to the server was refused",
            )
            with pytest.raises(HTTPException) as e:
                get_mqtt_address(NS, REL)
        assert e.value.status_code == 500
        assert "refused" not in str(e.value.detail)


class TestRoutesAreRegistered:

    def test_both_paths_are_mounted(self):
        from src.main import app
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/api/releases/{namespace}/{name}/public-address" in paths
        assert "/api/releases/{namespace}/{name}/mqtt-address" in paths

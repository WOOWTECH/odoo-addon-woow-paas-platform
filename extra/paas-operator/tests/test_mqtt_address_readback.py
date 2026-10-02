"""EMQX 對外 MQTT 位址回讀（charts/emqx 0.1.0 契約）。

## 為什麼讀 container log，而不是別的

chart 的 ngrok sidecar 每 60 秒把當前公開位址印一次到自己的 stdout。另外兩條路
都被刻意否決：把 agent 的 ``:4040`` 開成 Service 等於讓同一租戶 namespace 的其他
服務去**建立**隧道（那個 API 可以）；讓 sidecar 寫 ConfigMap 則要掛 SA token ＋
RBAC，而 chart 是 ``automountToken=false``。

## 守線

1. **只回位址、永不回 log 內容**：來源是第三方（ngrok agent）的輸出，整段轉出去
   等於把不受控字串接到 Odoo 的 HTTP 回應上。形狀對不上就當沒有（fail-closed）。
2. **取最後一筆**：sidecar 會重印，最後一筆才是 agent 當前持有的位址
   （舊的那些是上一次 session 的，連過去會失敗）。agent 死掉時 sidecar 印一行
   **空值**撤回，最後一筆是空值就回 None。
3. **「沒有位址」不是錯誤**：租戶沒開對外 MQTT（pod 裡沒有 ngrok 容器 ⇒ kubectl
   logs 失敗）、agent 還沒連上、pod 還在起來——三者都回 ``public_url: null``。
   把這些當 5xx 會讓服務詳情頁在完全正常的狀態下顯示錯誤。
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.releases import get_mqtt_address
from src.models.schemas import PodPhase
from src.services.helm import KubectlException, KubernetesService

MARKER = "WOOW_MQTT_PUBLIC_URL="


def _pod(name="svc-aaaa1111-emqx-0", deletion_timestamp=None):
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


class TestAddressExtraction:

    def test_returns_the_last_advertised_address(self):
        svc = _svc_with_log(
            "\n".join([
                "t=2026-10-02T03:00:00+0000 lvl=info msg=\"started tunnel\"",
                f"{MARKER}tcp://0.tcp.ngrok.io:11111",
                f"{MARKER}tcp://6.tcp.ngrok.io:22222",
            ])
        )
        assert svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111") == (
            "tcp://6.tcp.ngrok.io:22222"
        )

    def test_reads_the_ngrok_container_of_that_release_only(self):
        svc = _svc_with_log(f"{MARKER}tcp://1.tcp.ngrok.io:33333")
        svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111")
        assert svc.get_pods.call_args.kwargs["label_selector"] == (
            "app.kubernetes.io/instance=svc-aaaa1111"
        )
        args = svc._run_command.call_args.args[0]
        assert args[0] == "logs"
        assert "--container" in args and args[args.index("--container") + 1] == "ngrok"
        assert any(a.startswith("--tail=") for a in args)

    @pytest.mark.parametrize("line", [
        f"{MARKER}https://evil.example.com/",
        f"{MARKER}tcp://host:notaport",
        f"{MARKER}tcp://6.tcp.ngrok.io:22222 ; rm -rf /",
        f"{MARKER}",
    ])
    def test_malformed_candidates_are_discarded(self, line):
        """形狀對不上就當沒有——不得把來源字串原樣轉出去。"""
        svc = _svc_with_log(line)
        assert svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111") is None

    def test_an_empty_announcement_retracts_the_address(self):
        """agent 死掉時 sidecar 印空值撤回；之後的最後一筆才算數。"""
        svc = _svc_with_log("\n".join([
            f"{MARKER}tcp://6.tcp.ngrok.io:22222",
            f"{MARKER}",
            "ngrok agent exited; retrying in 10s (check the authtoken …)",
        ]))
        assert svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111") is None

    def test_an_address_after_a_retraction_wins(self):
        svc = _svc_with_log("\n".join([
            f"{MARKER}",
            f"{MARKER}tcp://4.tcp.ngrok.io:44444",
        ]))
        assert svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111") == (
            "tcp://4.tcp.ngrok.io:44444"
        )

    def test_log_content_is_never_returned(self):
        secret_ish = "t=... lvl=info msg=\"authtoken 2abc…\" sess=xyz"
        svc = _svc_with_log(
            f"{secret_ish}\n{MARKER}tcp://6.tcp.ngrok.io:22222\n{secret_ish}"
        )
        got = svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111")
        assert got == "tcp://6.tcp.ngrok.io:22222"
        assert "authtoken" not in got

    def test_no_ngrok_container_is_not_an_error(self):
        """租戶沒開對外 MQTT：pod 裡沒有 ngrok 容器 ⇒ kubectl logs 失敗 ⇒ None。"""
        svc = KubernetesService()
        svc.get_pods = MagicMock(return_value=[_pod()])
        svc._run_command = MagicMock(side_effect=KubectlException(
            message="kubectl command failed with code 1",
            command="kubectl logs …",
            stderr='container "ngrok" not found in pod',
        ))
        assert svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111") is None

    def test_terminating_pods_are_skipped(self):
        """重新部署時的舊 pod 還在 Terminating，它的位址已經不是當前的那一個。"""
        svc = _svc_with_log(
            f"{MARKER}tcp://9.tcp.ngrok.io:99999",
            pods=[_pod(deletion_timestamp="2026-10-02T03:00:00Z")],
        )
        assert svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111") is None
        svc._run_command.assert_not_called()

    def test_no_pods_at_all(self):
        svc = _svc_with_log("", pods=[])
        assert svc.get_advertised_mqtt_address("paas-ws-x", "svc-aaaa1111") is None


class TestEndpoint:

    def test_returns_the_address(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_mqtt_address.return_value = "tcp://6.tcp.ngrok.io:22222"
            resp = get_mqtt_address("paas-ws-x", "svc-aaaa1111")
        assert resp.public_url == "tcp://6.tcp.ngrok.io:22222"

    def test_absent_address_is_200_with_null(self):
        """正常狀態（沒開／還沒連上）必須是 200 null，不是 4xx/5xx。"""
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_mqtt_address.return_value = None
            resp = get_mqtt_address("paas-ws-x", "svc-aaaa1111")
        assert resp.public_url is None

    def test_namespace_out_of_scope_is_403(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_mqtt_address.side_effect = ValueError(
                "Namespace 'kube-system' is not managed by this operator"
            )
            with pytest.raises(HTTPException) as e:
                get_mqtt_address("kube-system", "whatever")
        assert e.value.status_code == 403

    def test_kubectl_failure_is_500_without_leaking_stderr(self):
        with patch("src.api.releases.k8s_service") as k8s:
            k8s.get_advertised_mqtt_address.side_effect = KubectlException(
                message="kubectl command timed out after 15s",
                command="kubectl logs …",
                stderr="connection to the server was refused",
            )
            with pytest.raises(HTTPException) as e:
                get_mqtt_address("paas-ws-x", "svc-aaaa1111")
        assert e.value.status_code == 500
        assert "refused" not in str(e.value.detail)

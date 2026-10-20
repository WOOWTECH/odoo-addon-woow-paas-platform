"""INFRA-FIX-005：VPN 節點（charts/tailscale）的即時狀態回讀。

INFRA-TS-007（FAIL）三項缺口的 operator 這一半：

1. serve 轉發項目被 entrypoint 略過的原因只在容器 stderr ⇒ 要能以封閉代碼回報；
   Service 不存在的項目不會被略過（建成 NXDOMAIN 轉發）⇒ 要用 Service 清單對帳；
2. 節點需要登入時，登入入口是叢集內部網址（瀏覽器打不開）⇒ 不回內部網址，只回註冊碼；
3. 節點被控制面到期／刪除後 BackendState 是 NeedsLogin，pod 仍 Ready ⇒ 要回 BackendState。

fixture 的 stderr／stdout 來自 INFRA-TS-007 對真 entrypoint 的離線執行
（evidence/INFRA-TS-007/offline-parse-serve.*），狀態 JSON 來自 INFRA-TS-005 的 NeedsLogin 觀測。
守線：**永不回原始 log／stderr**、不回叢集內部網址、不信任探測輸出的形狀。
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.releases import get_vpn_node_status
from src.models.schemas import PodPhase, VpnNodeStatusResponse
from src.services import vpn_node_status as v

NS, REL = "paas-ws-5e41efa6", "svc-f06c5feb"
HS = "svc-0b4a8def-headscale"

STATUS_RUNNING = {
    "BackendState": "Running", "AuthURL": "",
    "TailscaleIPs": ["100.64.0.3", "fd7a:115c:a1e0::3"],
    "Self": {"Online": True, "DNSName": "paasqa-service.tailnet.internal.",
             "TailscaleIPs": ["100.64.0.3"]},
    "Health": [],
}
STATUS_NEEDS_LOGIN = {
    "BackendState": "NeedsLogin", "AuthURL": "",
    "TailscaleIPs": ["100.64.0.3"], "Self": {"Online": True}, "Health": [],
}
STATUS_LOGIN_PENDING_INTERNAL = {
    "BackendState": "NeedsLogin",
    "AuthURL": f"http://{HS}.{NS}.svc.cluster.local:8080/register/mkey:abc123"
               .replace("mkey:", "authreq-"),
}
SERVE_9004 = {"TCP": {"9004": {"TCPForward": f"{HS}.{NS}.svc.cluster.local:8080"}}}

# 真 entrypoint 對 `9101=no-such-svc:8080,9102=svc-x.other-ns.svc.cluster.local:80,
# 9103=10.0.0.5:80,70000=a:1,9004=<hs>:8080` 的輸出（stdout 有效行＋stderr 略過行合併）。
WANTED_MIXED = f"""\
9101 no-such-svc.{NS}.svc.cluster.local:8080
[woow-tailscale] serve: skipping '9102=svc-x.other-ns.svc.cluster.local:80': only Services of this namespace ({NS}) are allowed
[woow-tailscale] serve: skipping '9103=10.0.0.5:80': only Services of this namespace ({NS}) are allowed
[woow-tailscale] serve: skipping '70000=a:1': ports must be 1-65535
9004 {HS}.{NS}.svc.cluster.local:8080"""


def _output(status=None, serve=None, wanted=""):
    parts = [v.SENTINEL_STATUS, json.dumps(status) if status is not None else "",
             v.SENTINEL_SERVE, json.dumps(serve) if serve is not None else "",
             v.SENTINEL_WANTED, wanted]
    return "\n".join(parts) + "\n"


def _report(status=None, serve=None, wanted="", services=(HS,), probe=v.PROBE_OK):
    return v.build_report(
        pod="svc-f06c5feb-0", probe=probe, output=_output(status, serve, wanted),
        service_names=None if services is None else frozenset(services))


# --------------------------------------------------------------------------- 狀態
class TestBackendState:
    def test_running_is_online_with_ip_and_dns_name(self):
        r = _report(STATUS_RUNNING, SERVE_9004, f"9004 {HS}.{NS}.svc.cluster.local:8080")
        assert (r["probe"], r["backend_state"], r["online"]) == ("ok", "Running", True)
        assert r["tailnet_ips"] == ["100.64.0.3", "fd7a:115c:a1e0::3"]
        assert r["dns_name"] == "paasqa-service.tailnet.internal"

    def test_needs_login_is_reported_even_though_the_pod_is_ready(self):
        """TS-005 p2／p7：到期或被刪除後 12 筆樣本 BackendState 全是 NeedsLogin。"""
        r = _report(STATUS_NEEDS_LOGIN)
        assert r["backend_state"] == "NeedsLogin"
        assert r["login"]["kind"] == "none"  # 還沒有進行中的登入：入口要靠平台文案說明

    @pytest.mark.parametrize("state", sorted(v.BACKEND_STATES))
    def test_every_known_state_passes_through(self, state):
        assert _report({"BackendState": state})["backend_state"] == state

    @pytest.mark.parametrize("junk", ["", "running", "Running; rm -rf", None, 7, ["Running"]])
    def test_unknown_state_text_never_passes_through(self, junk):
        assert _report({"BackendState": junk})["backend_state"] == "Unknown"

    def test_ips_are_validated_not_echoed(self):
        r = _report({"BackendState": "Running",
                     "TailscaleIPs": ["100.64.0.3", "<script>", "not-an-ip", 5, "fd7a::3"]})
        assert r["tailnet_ips"] == ["100.64.0.3", "fd7a::3"]

    def test_dns_name_with_odd_characters_is_dropped(self):
        r = _report({"BackendState": "Running", "Self": {"DNSName": "a b\n<x>"}})
        assert r["dns_name"] is None

    def test_no_status_output_degrades_to_no_output(self):
        r = v.build_report(pod="p", probe=v.PROBE_OK, output="", service_names=frozenset())
        assert r["probe"] == v.PROBE_NO_OUTPUT
        assert r["backend_state"] == "Unknown" and r["serve"]["forwards"] == []

    def test_garbage_status_json_degrades_to_no_output(self):
        out = f"{v.SENTINEL_STATUS}\n{{not json\n{v.SENTINEL_SERVE}\n\n{v.SENTINEL_WANTED}\n"
        assert v.build_report(pod="p", probe=v.PROBE_OK, output=out)["probe"] == v.PROBE_NO_OUTPUT


# --------------------------------------------------------------------------- 登入入口
class TestLoginEntrance:
    def test_cluster_internal_register_url_is_not_returned_only_the_key(self):
        """TS-005-4：原生 Reauthenticate 的網址是 *.svc.cluster.local，瀏覽器打不開。"""
        r = _report(STATUS_LOGIN_PENDING_INTERNAL)
        assert r["login"] == {"kind": "cluster_internal", "url": None, "register_key": "authreq-abc123"}
        assert "cluster.local" not in json.dumps(r)

    @pytest.mark.parametrize("url", [
        "http://svc-x:8080/register/abcd1234",
        "http://svc-x.ns.svc/register/abcd1234",
        "https://hs.internal/register/abcd1234",
        "http://10.42.1.5:8080/register/abcd1234",
        "http://100.64.0.1/register/abcd1234",
        "http://127.0.0.1/register/abcd1234",
        "http://[::1]:8080/register/abcd1234",
        "http://printer.local/register/abcd1234",
        "http://localhost/register/abcd1234",
    ])
    def test_internal_hosts_are_all_treated_as_internal(self, url):
        out = v.classify_auth_url(url)
        assert out["kind"] == "cluster_internal" and out["url"] is None

    def test_external_login_url_is_returned_as_is(self):
        url = "https://login.tailscale.com/a/0123456789abcd"
        assert v.classify_auth_url(url) == {"kind": "external_url", "url": url, "register_key": None}

    def test_public_headscale_url_is_external(self):
        url = "https://hs-tenant.woowtech.io/register/authreq-xyz987"
        out = v.classify_auth_url(url)
        assert out["kind"] == "external_url" and out["url"] == url

    def test_internal_url_without_register_path_gives_no_key(self):
        out = v.classify_auth_url("http://svc-x.ns.svc.cluster.local:8080/health")
        assert out == {"kind": "cluster_internal", "url": None, "register_key": None}

    @pytest.mark.parametrize("bad", [
        "", None, 5, "javascript:alert(1)", "ftp://x.io/a", "https://", "https://x.io/a b",
        "https://x.io/a\nHeader: x", 'https://x.io/"onmouseover=', "https://x.io/" + "a" * 3000,
    ])
    def test_malformed_urls_are_dropped(self, bad):
        assert v.classify_auth_url(bad)["kind"] == "none"

    def test_register_key_charset_is_closed(self):
        out = v.classify_auth_url("http://svc-x.ns.svc.cluster.local:8080/register/a%20b")
        assert out["register_key"] is None


# --------------------------------------------------------------------------- serve
class TestServeForwards:
    def test_skip_reasons_become_closed_codes_for_the_ts007_inputs(self):
        """TS-007：他 namespace、IP、埠超界三種都被略過、且只印 stderr。"""
        r = _report(STATUS_RUNNING, SERVE_9004, WANTED_MIXED)
        assert r["serve"]["skipped"] == [
            {"entry": "9102=svc-x.other-ns.svc.cluster.local:80", "reason": "other_namespace"},
            {"entry": "9103=10.0.0.5:80", "reason": "other_namespace"},
            {"entry": "70000=a:1", "reason": "port_range"},
        ]
        assert r["serve"]["wanted_known"] is True

    def test_missing_service_is_not_skipped_by_the_entrypoint_so_it_is_reported(self):
        """TS-007-1：no-such-svc 不會被略過，會被建成指向 NXDOMAIN 的轉發。"""
        applied = {"TCP": {
            "9004": {"TCPForward": f"{HS}.{NS}.svc.cluster.local:8080"},
            "9101": {"TCPForward": f"no-such-svc.{NS}.svc.cluster.local:8080"},
        }}
        r = _report(STATUS_RUNNING, applied, WANTED_MIXED)
        by_port = {f["port"]: f["status"] for f in r["serve"]["forwards"]}
        assert by_port == {9101: "service_missing", 9004: "applied"}

    def test_valid_but_not_yet_applied_forward(self):
        r = _report(STATUS_NEEDS_LOGIN, {}, f"9004 {HS}.{NS}.svc.cluster.local:8080")
        assert [f["status"] for f in r["serve"]["forwards"]] == ["not_applied"]

    def test_applied_to_a_different_target_is_a_mismatch(self):
        serve = {"TCP": {"9004": {"TCPForward": f"other.{NS}.svc.cluster.local:8080"}}}
        r = _report(STATUS_RUNNING, serve, f"9004 {HS}.{NS}.svc.cluster.local:8080")
        assert r["serve"]["forwards"][0]["status"] == "mismatch"

    def test_unreadable_service_list_cannot_confirm_the_target(self):
        r = _report(STATUS_RUNNING, SERVE_9004, f"9004 {HS}.{NS}.svc.cluster.local:8080", services=None)
        assert r["serve"]["forwards"][0]["status"] == "unverified"

    @pytest.mark.parametrize("why,code", [
        ("expected <tailnet port>=<service>:<port>", "bad_format"),
        ("ports must be 1-65535", "port_range"),
        ("only Services of this namespace (ns) are allowed", "other_namespace"),
        ("'a_b' is not a Service name", "not_service_name"),
        ("tailnet port 9001 is already forwarded", "duplicate_port"),
        ("at most 32 forwards", "too_many"),
        ("something the entrypoint says in a later version", "other"),
    ])
    def test_every_entrypoint_reason_maps_to_a_code(self, why, code):
        text = f"[woow-tailscale] serve: skipping 'x=y:1': {why}"
        assert v.parse_wanted(text)[1] == [{"entry": "x=y:1", "reason": code}]

    def test_namespace_unknown_ignores_the_whole_list(self):
        text = "[woow-tailscale] serve: own namespace unknown (TS_SERVE_NAMESPACE / POD_NAMESPACE unset); ignoring the whole list"
        assert v.parse_wanted(text) == ([], [], "namespace_unknown")

    def test_unknown_parser_is_reported_as_unknown_not_as_empty(self):
        r = _report(STATUS_RUNNING, SERVE_9004, v.NO_PARSER_MARK)
        assert r["serve"]["wanted_known"] is False
        assert [(f["port"], f["status"]) for f in r["serve"]["forwards"]] == [(9004, "applied")]

    def test_extra_serve_config_is_flagged(self):
        r = _report(STATUS_RUNNING, {**SERVE_9004, "Web": {"x": {}}}, "")
        assert r["serve"]["extra_config"] is True

    def test_entry_is_limited_to_the_tenants_own_safe_characters(self):
        evil = "<img src=x onerror=alert(1)>" + "A" * 300
        text = f"[woow-tailscale] serve: skipping '{evil}': ports must be 1-65535"
        entry = v.parse_wanted(text)[1][0]["entry"]
        assert len(entry) <= 100 and "<" not in entry and ">" not in entry

    def test_arbitrary_stderr_lines_never_reach_the_response(self):
        noise = "\n".join([
            "[woow-tailscale] userspace networking enabled",
            "2026/10/16 15:41:37 Switching ipn state Running -> NeedsLogin",
            "Your authtoken: 2abcDEFghiJKLmnoPQRstuVWXyz_0123456789abcdEF",
            "tskey-auth-kABCDEF1234CNTRL-secretsecretsecret",
        ])
        r = _report(STATUS_RUNNING, SERVE_9004, noise + "\n" + WANTED_MIXED)
        dumped = json.dumps(r)
        assert "tskey" not in dumped and "authtoken" not in dumped and "Switching" not in dumped

    def test_item_counts_are_capped(self):
        lines = "\n".join(f"[woow-tailscale] serve: skipping '{p}=a:1': ports must be 1-65535"
                          for p in range(1, 400))
        assert len(v.parse_wanted(lines)[1]) == v.MAX_ITEMS


class TestSplitOutput:
    def test_sentinel_like_text_inside_a_section_does_not_forge_a_section(self):
        out = _output(STATUS_RUNNING, SERVE_9004, "[woow-tailscale] serve: skipping 'a=b:1': ports must be 1-65535")
        sections = v.split_probe_output(out)
        assert set(sections) == {"status", "serve", "wanted"}

    def test_probe_script_has_no_interpolation_and_a_fixed_socket_default(self):
        assert "/var/run/tailscale/tailscaled.sock" in v.PROBE_SCRIPT
        assert "WOOW_TS_SOURCE_ONLY=1" in v.PROBE_SCRIPT
        assert "parse_serve" in v.PROBE_SCRIPT


# --------------------------------------------------------------------------- collect（kubectl 層）
def _pod(name="svc-f06c5feb-0", phase=PodPhase.RUNNING, deleting=None):
    pod = MagicMock()
    pod.name, pod.phase, pod.deletion_timestamp = name, phase, deleting
    return pod


def _k8s(pods, exec_out="", services=HS, exec_exc=None, svc_exc=None):
    k8s = MagicMock()
    k8s.get_pods.return_value = pods
    if exec_exc:
        k8s.exec_in_pod.side_effect = exec_exc
    else:
        k8s.exec_in_pod.return_value = MagicMock(stdout=exec_out)
    if svc_exc:
        k8s._run_command.side_effect = svc_exc
    else:
        k8s._run_command.return_value = MagicMock(stdout=services)
    return k8s


class TestCollect:
    def test_happy_path_execs_the_fixed_script_in_the_tailscale_container(self):
        k8s = _k8s([_pod()], _output(STATUS_RUNNING, SERVE_9004, f"9004 {HS}.{NS}.svc.cluster.local:8080"))
        r = v.collect(k8s, NS, REL)
        assert r["probe"] == "ok" and r["backend_state"] == "Running"
        args = k8s.exec_in_pod.call_args
        assert args.args[:3] == (NS, "svc-f06c5feb-0", "tailscale")
        assert args.args[3] == ["sh", "-c", v.PROBE_SCRIPT]
        assert args.kwargs["timeout"] == v.EXEC_TIMEOUT_SECONDS
        assert k8s.get_pods.call_args.kwargs["label_selector"] == f"app.kubernetes.io/instance={REL}"

    def test_no_pod(self):
        assert v.collect(_k8s([]), NS, REL)["probe"] == "no_pod"

    def test_terminating_pod_is_not_probed(self):
        k8s = _k8s([_pod(deleting="2026-10-16T00:00:00Z")])
        assert v.collect(k8s, NS, REL)["probe"] == "no_pod"
        k8s.exec_in_pod.assert_not_called()

    def test_pending_pod_is_not_probed(self):
        k8s = _k8s([_pod(phase=PodPhase.PENDING)])
        r = v.collect(k8s, NS, REL)
        assert (r["probe"], r["pod"]) == ("pod_not_running", "svc-f06c5feb-0")
        k8s.exec_in_pod.assert_not_called()

    def test_exec_failure_degrades_without_leaking_stderr(self):
        k8s = _k8s([_pod()], exec_exc=RuntimeError("tskey-auth-kSECRET stderr text"))
        r = v.collect(k8s, NS, REL)
        assert r["probe"] == "exec_failed" and "SECRET" not in json.dumps(r)

    def test_pod_listing_failure_degrades(self):
        k8s = _k8s([])
        k8s.get_pods.side_effect = RuntimeError("boom")
        assert v.collect(k8s, NS, REL)["probe"] == "no_pod"

    def test_service_listing_failure_leaves_targets_unverified(self):
        k8s = _k8s([_pod()], _output(STATUS_RUNNING, SERVE_9004, f"9004 {HS}.{NS}.svc.cluster.local:8080"),
                   svc_exc=RuntimeError("forbidden"))
        assert v.collect(k8s, NS, REL)["serve"]["forwards"][0]["status"] == "unverified"

    def test_only_reads_nothing_else_is_executed(self):
        k8s = _k8s([_pod()], _output(STATUS_RUNNING))
        v.collect(k8s, NS, REL)
        verbs = [c.args[0][0] for c in k8s._run_command.call_args_list]
        assert verbs == ["get"]
        assert k8s.exec_in_pod.call_count == 1


# --------------------------------------------------------------------------- 端點
class TestEndpoint:
    def test_foreign_namespace_is_403(self):
        with pytest.raises(HTTPException) as err:
            get_vpn_node_status("kube-system", REL)
        assert err.value.status_code == 403

    def test_illegal_release_name_is_422_and_never_reaches_kubectl(self):
        with pytest.raises(HTTPException) as err:
            get_vpn_node_status(NS, "a,b=c")
        assert err.value.status_code == 422

    def test_response_matches_the_declared_model(self, monkeypatch):
        from src.api import releases
        monkeypatch.setattr(releases.vpn_node_status, "collect", lambda *_: _report(
            STATUS_NEEDS_LOGIN, {}, f"9004 {HS}.{NS}.svc.cluster.local:8080"))
        resp = get_vpn_node_status(NS, REL)
        assert isinstance(resp, VpnNodeStatusResponse)
        assert resp.backend_state == "NeedsLogin" and resp.serve.forwards[0].status == "not_applied"

    def test_degraded_probe_is_still_a_normal_response(self, monkeypatch):
        from src.api import releases
        monkeypatch.setattr(releases.vpn_node_status, "collect",
                            lambda *_: v.build_report(pod=None, probe=v.PROBE_NO_POD))
        resp = get_vpn_node_status(NS, REL)
        assert resp.probe == "no_pod" and resp.serve.forwards == []


class TestRealEntrypointOutput:
    """fixture＝本 PR 的 PROBE_SCRIPT 在本機真的跑一次（source 真的 charts/tailscale 映像 entrypoint.sh、
    假的 tailscale CLI）所得的完整輸出：不是手寫的，格式漂移（例如 entrypoint 改了 log 句型）會在這裡變紅。"""

    FIXTURE = Path(__file__).parent / "fixtures" / "vpn_node_probe_real_entrypoint.txt"

    def test_full_report_from_the_real_script_output(self):
        r = v.build_report(
            pod="p", probe=v.PROBE_OK, output=self.FIXTURE.read_text(encoding="utf-8"),
            service_names=frozenset({HS}))
        assert r["backend_state"] == "NeedsLogin" and r["online"] is True
        assert {(f["port"], f["status"]) for f in r["serve"]["forwards"]} == {
            (9101, "not_applied"), (9004, "applied")}
        assert [(s["entry"], s["reason"]) for s in r["serve"]["skipped"]] == [
            ("9102=svc-x.other-ns.svc.cluster.local:80", "other_namespace"),
            ("9103=10.0.0.5:80", "other_namespace"),
            ("70000=a:1", "port_range"),
            ("9004=x:1", "duplicate_port"),
            ("bad", "bad_format"),
        ]
        assert "other" not in {s["reason"] for s in r["serve"]["skipped"]}


class TestProbeScriptRunsSafely:
    """PROBE_SCRIPT 本身真的跑一次（sh）：舊映像的 entrypoint 沒有 WOOW_TS_SOURCE_ONLY，source 它會整支
    跑起來（多開一個 tailscaled）——腳本必須先確認旗標存在，否則退回「不知道期望清單」。"""

    @staticmethod
    def _run(tmp_path, entrypoint_body, env=None):
        entry = tmp_path / "entrypoint.sh"
        entry.write_text(entrypoint_body)
        fake = tmp_path / "bin"
        fake.mkdir()
        (fake / "tailscale").write_text(
            "#!/bin/sh\ncase \"$*\" in\n *'serve status'*) echo '{}';;\n"
            " *'status --json'*) echo '{\"BackendState\":\"Running\"}';;\nesac\n")
        (fake / "tailscale").chmod(0o755)
        script = v.PROBE_SCRIPT.replace("/entrypoint.sh", str(entry))
        out = subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, timeout=20,
            env={"PATH": f"{fake}:/usr/bin:/bin", **(env or {})},
        )
        return out.stdout

    def test_entrypoint_without_the_source_only_flag_is_never_executed(self, tmp_path):
        marker = tmp_path / "ran"
        body = f"#!/usr/bin/env bash\ntouch {marker}\n"
        stdout = self._run(tmp_path, body)
        assert not marker.exists(), "舊映像的 entrypoint 被整支執行了"
        assert v.NO_PARSER_MARK in stdout
        assert v.parse_wanted(v.split_probe_output(stdout)["wanted"])[0] is None

    def test_entrypoint_with_the_flag_is_sourced_and_parse_serve_output_is_read(self, tmp_path):
        body = (
            "#!/usr/bin/env bash\n"
            "log() { printf '[woow-tailscale] %s\\n' \"$*\" >&2; }\n"
            "parse_serve() { echo \"9001 a.$2.svc.$3:1\"; log \"serve: skipping '9002=x:1': ports must be 1-65535\"; }\n"
            "if [[ \"${WOOW_TS_SOURCE_ONLY:-}\" == \"1\" ]]; then return 0 2>/dev/null || exit 0; fi\n"
            f"touch {tmp_path}/main-ran\n"
        )
        stdout = self._run(tmp_path, body, env={"TS_SERVE": "9001=a:1", "TS_SERVE_NAMESPACE": "ns1"})
        assert not (tmp_path / "main-ran").exists(), "只能 source 到旗標的 return，不得往下跑主流程"
        report = v.build_report(pod="p", probe=v.PROBE_OK, output=stdout, service_names=frozenset({"a"}))
        assert report["backend_state"] == "Running"
        assert report["serve"]["skipped"] == [{"entry": "9002=x:1", "reason": "port_range"}]
        assert [f["port"] for f in report["serve"]["forwards"]] == [9001]

"""#1441／#1436／#514：工作區＝自己的區域網路——egress baseline 與 Pod Security。"""
import ipaddress
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from src.config import Settings
from src.services.helm import KubernetesService
from src.services.tenant_baseline import (
    BASELINE_EGRESS_BLOCKED_CIDRS,
    BASELINE_EGRESS_NP_NAME,
    DEFAULT_PSA_EXEMPT_NAMESPACES,
    PSA_EXEMPT_ANNOTATION,
    build_baseline_egress_netpol,
    build_pod_security_labels,
    parse_namespace_list,
    psa_label_keys,
)

NS = "paas-ws-abc12345"


def _internet_rule(np):
    rules = [r for r in np["spec"]["egress"]
             if any("ipBlock" in p for p in r.get("to", []))]
    assert len(rules) == 1
    return rules[0]


def _blocked(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return any(addr in ipaddress.ip_network(c) for c in BASELINE_EGRESS_BLOCKED_CIDRS)


# --- build_baseline_egress_netpol -----------------------------------------------


def test_egress_np_shape():
    np = build_baseline_egress_netpol(NS)
    assert np["kind"] == "NetworkPolicy"
    assert np["metadata"] == {"name": BASELINE_EGRESS_NP_NAME, "namespace": NS,
                              "labels": {"paas-managed": "true"}}
    # 選到 ns 內每一個 pod（含沒有 chart NP 的 n8n、omnigent runner、CDI importer）。
    assert np["spec"]["podSelector"] == {}
    # 只管 Egress：ingress 仍由既有 4 條 baseline 管，這條不能意外變成 ingress deny。
    assert np["spec"]["policyTypes"] == ["Egress"]
    assert len(np["spec"]["egress"]) == 3


def test_egress_same_namespace_all_ports():
    """同工作區＝區網：同 ns 全部 pod、全部埠，不能被 port 或 namespaceSelector 收窄。"""
    np = build_baseline_egress_netpol(NS)
    same = np["spec"]["egress"][0]
    assert same == {"to": [{"podSelector": {}}]}


def test_egress_dns_only_kube_dns_pods_port_53():
    np = build_baseline_egress_netpol(NS)
    dns = np["spec"]["egress"][1]
    assert dns["to"] == [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
    }]
    assert sorted((p["protocol"], p["port"]) for p in dns["ports"]) == [
        ("TCP", 53), ("UDP", 53)]


def test_egress_no_rule_wider_than_policy():
    """沒有空的 to（＝全通）、沒有 namespaceSelector {}（＝所有 ns）。"""
    np = build_baseline_egress_netpol(NS)
    for rule in np["spec"]["egress"]:
        assert rule.get("to"), rule
        for peer in rule["to"]:
            assert peer.get("namespaceSelector") != {}, rule


def test_egress_internet_rule_any_port_with_excepts():
    rule = _internet_rule(build_baseline_egress_netpol(NS))
    assert "ports" not in rule  # 公網任意埠（git over ssh、MQTT bridge、SMTP …）
    block = rule["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0"
    assert block["except"] == BASELINE_EGRESS_BLOCKED_CIDRS


@pytest.mark.parametrize("ip,why", [
    ("10.42.0.17", "pod CIDR（其他工作區、平台、非 PaaS ns 的 pod）"),
    ("10.42.3.71", "pod CIDR（kube-system coredns 以外的 kube-system pod 由 DNS 規則限定）"),
    ("10.43.0.1", "kube-apiserver ClusterIP"),
    ("10.43.200.5", "Service CIDR"),
    ("10.42.0.255", "z0v3w pod 子網廣播"),
    ("10.42.1.255", "59mqj pod 子網廣播"),
    ("10.42.2.255", "pggrg pod 子網廣播"),
    ("10.42.3.255", "31zix pod 子網廣播"),
    ("192.168.10.21", "node IP（kube-apiserver :6443、NodePort）"),
    ("192.168.10.255", "主機 LAN 廣播"),
    ("172.20.0.1", "RFC1918"),
    ("169.254.169.254", "雲端 metadata"),
    ("100.100.100.100", "CGNAT／tailnet"),
    ("224.0.0.251", "mDNS"),
    ("239.255.255.250", "SSDP"),
    ("255.255.255.255", "limited broadcast"),
])
def test_cluster_private_multicast_targets_blocked(ip, why):
    assert _blocked(ip), why


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "104.16.0.1", "140.82.112.3"])
def test_public_targets_allowed(ip):
    assert not _blocked(ip)


# --- _apply_baseline_netpol wiring ---------------------------------------------


def _capture(flag: bool):
    k = KubernetesService()
    k.apply_manifest = MagicMock()
    with patch("src.services.helm.settings") as s:
        s.paas_env = "prod"
        s.cloudflared_namespace = ""
        s.namespace_prefix = "paas-ws-"
        s.tenant_baseline_egress_enabled = flag
        s.tenant_baseline_egress_blocked_ports = ""
        s.tenant_baseline_egress_web_profile = False
        k._apply_baseline_netpol(NS, 7)
    return [c.args[0] for c in k.apply_manifest.call_args_list]


def test_bundle_with_egress_flag_on_appends_egress_np_last():
    m = _capture(True)
    names = [x["metadata"]["name"] for x in m]
    assert names == [
        "paas-baseline-default-deny-ingress",
        "paas-baseline-allow-same-ns",
        "paas-baseline-allow-cloudflared",
        "paas-baseline-allow-mcp-reach",
        BASELINE_EGRESS_NP_NAME,
    ]
    assert m[-1] == build_baseline_egress_netpol(NS)


def test_bundle_with_egress_flag_off_is_unchanged():
    names = [x["metadata"]["name"] for x in _capture(False)]
    assert BASELINE_EGRESS_NP_NAME not in names
    assert len(names) == 4


def test_ingress_same_ns_still_present():
    """同工作區 ingress 全通由既有 allow-same-ns 保證（全部埠）。"""
    m = _capture(True)
    same = next(x for x in m if x["metadata"]["name"] == "paas-baseline-allow-same-ns")
    assert same["spec"]["ingress"] == [{"from": [{"podSelector": {}}]}]


# --- Pod Security ------------------------------------------------------------------


def test_pod_security_labels_default():
    assert build_pod_security_labels(version="v1.34") == {
        "pod-security.kubernetes.io/enforce": "baseline",
        "pod-security.kubernetes.io/enforce-version": "v1.34",
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/warn-version": "v1.34",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/audit-version": "v1.34",
    }


def test_pod_security_labels_empty_mode_omitted():
    labels = build_pod_security_labels(enforce="baseline", warn="", audit="", version="v1.34")
    assert set(labels) == {"pod-security.kubernetes.io/enforce",
                           "pod-security.kubernetes.io/enforce-version"}


def test_parse_namespace_list():
    assert parse_namespace_list(" a, b ,,a,c ") == ["a", "b", "c"]
    assert parse_namespace_list("") == []
    assert parse_namespace_list(None) == []


def _psa(ns, enabled=True, exempt="paas-ws-ab6564da"):
    k = KubernetesService()
    k._run_command = MagicMock()
    with patch("src.services.helm.settings") as s:
        s.tenant_pod_security_enabled = enabled
        s.psa_exempt_namespaces = exempt
        s.tenant_psa_enforce_level = "baseline"
        s.tenant_psa_warn_level = "restricted"
        s.tenant_psa_audit_level = "restricted"
        s.tenant_psa_version = "v1.34"
        s.namespace_prefix = "paas-ws-"
        out = k.apply_pod_security(ns)
    return k, out


def test_apply_pod_security_flag_off_noop():
    k, out = _psa(NS, enabled=False)
    assert out == {}
    k._run_command.assert_not_called()


def test_apply_pod_security_tenant_ns_enforce_baseline():
    k, out = _psa(NS)
    assert out["pod-security.kubernetes.io/enforce"] == "baseline"
    cmd = k._run_command.call_args.args[0]
    assert cmd[:4] == ["label", "ns", NS, "--overwrite"]
    assert "pod-security.kubernetes.io/enforce=baseline" in cmd
    assert "pod-security.kubernetes.io/enforce-version=v1.34" in cmd
    assert "pod-security.kubernetes.io/warn=restricted" in cmd
    assert k._run_command.call_count == 1


def test_apply_pod_security_exempt_ns_removes_labels_and_annotates():
    k, out = _psa("paas-ws-ab6564da")
    assert out == {}
    first, second = (c.args[0] for c in k._run_command.call_args_list)
    assert first[:3] == ["label", "ns", "paas-ws-ab6564da"]
    assert sorted(first[3:]) == sorted(f"{key}-" for key in psa_label_keys())
    assert not any("=" in a for a in first[3:])  # 只移除，不寫任何 enforce
    assert second[:4] == ["annotate", "ns", "paas-ws-ab6564da", "--overwrite"]
    assert second[4].startswith(f"{PSA_EXEMPT_ANNOTATION}=")


def test_apply_pod_security_exempt_list_is_configurable():
    """例外清單只認設定值：預設例外 ns 從清單拿掉後，就和一般租戶一樣 enforce。"""
    k, out = _psa("paas-ws-ab6564da", exempt="paas-ws-other")
    assert out["pod-security.kubernetes.io/enforce"] == "baseline"


def test_apply_pod_security_rejects_non_tenant_ns():
    k = KubernetesService()
    k._run_command = MagicMock()
    with pytest.raises(ValueError):
        k.apply_pod_security("kube-system")
    k._run_command.assert_not_called()


@patch("src.services.helm.settings")
@patch("subprocess.run")
def test_create_namespace_psa_before_netpol(mock_run, mock_settings):
    """PSA 先於 NP：之後任何一步失敗重跑，ns 也已經不收特權 pod。"""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.paas_env = "prod"
    mock_settings.tenant_ephemeral_storage_default_request = "128Mi"
    mock_settings.tenant_ephemeral_storage_default_limit = "4Gi"
    mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
    k = KubernetesService()
    order = MagicMock()
    k.apply_pod_security = order.psa
    k._apply_baseline_netpol = order.np
    k.ensure_registry_secret = MagicMock()
    k.ensure_default_sa_image_pull_secret = MagicMock()
    k.create_namespace(NS, cpu_limit="1", memory_limit="1Gi", storage_limit="1Gi",
                       workspace_id=7)
    assert order.mock_calls == [call.psa(NS), call.np(NS, 7)]


# --- Settings ---------------------------------------------------------------------


def test_settings_defaults_are_kill_switch_off(monkeypatch):
    for key in ("TENANT_BASELINE_EGRESS_ENABLED", "TENANT_POD_SECURITY_ENABLED",
                "PSA_EXEMPT_NAMESPACES", "TENANT_PSA_VERSION"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    assert s.tenant_baseline_egress_enabled is False
    assert s.tenant_pod_security_enabled is False
    assert s.tenant_psa_enforce_level == "baseline"
    assert s.tenant_psa_version == "v1.34"
    assert parse_namespace_list(s.psa_exempt_namespaces) == list(DEFAULT_PSA_EXEMPT_NAMESPACES)
    assert list(DEFAULT_PSA_EXEMPT_NAMESPACES) == ["paas-ws-ab6564da"]


def test_settings_rejects_unknown_psa_level(monkeypatch):
    monkeypatch.setenv("TENANT_PSA_ENFORCE_LEVEL", "strict")
    with pytest.raises(ValueError):
        Settings(_env_file=None)

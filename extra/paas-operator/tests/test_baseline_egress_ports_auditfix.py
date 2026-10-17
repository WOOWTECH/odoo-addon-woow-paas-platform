"""商用驗收 egress-all-ports-open：baseline egress 的公網規則可以封埠，agent 類 pod 可只拿 80/443。

現況（正式站 paas-ws-0ca9a7d3 唯讀）：``paas-baseline-egress``（``podSelector: {}``）的公網規則
沒有 ``ports`` ⇒ 公網全埠放行；NetworkPolicy 是聯集，所以 chart 自己的 80/443 限制
（hermes-agent-allow、open-design-allow …）被抵銷。

兩個新開關（預設關＝manifest 與修補前逐字相同）：

* ``TENANT_BASELINE_EGRESS_BLOCKED_PORTS``（例："22,25"）：公網規則改成「1–65535 扣掉這些埠」
  的 TCP／UDP 範圍（``endPort``），其他合法用途（SMTP submission 587／465、IMAPS、DoT、MQTT
  bridge …）照通。
* ``TENANT_BASELINE_EGRESS_WEB_PROFILE``：帶 ``paas.woowtech.io/egress-profile=web`` 的 pod
  不再被一般 baseline 選到，改由 ``paas-baseline-egress-web`` 管：同 ns＋DNS＋公網 TCP 80/443。
  chart 的 80/443 限制因此生效（chart 要在 agent 類 pod 加這個 label）。
"""
import ipaddress
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.services.helm import KubernetesService
from src.services.tenant_baseline import (
    BASELINE_EGRESS_BLOCKED_CIDRS,
    BASELINE_EGRESS_NP_NAME,
    BASELINE_EGRESS_WEB_NP_NAME,
    EGRESS_PROFILE_LABEL,
    EGRESS_PROFILE_WEB,
    WEB_PROFILE_TCP_PORTS,
    build_baseline_egress_netpol,
    build_baseline_egress_netpols,
    build_baseline_egress_web_netpol,
    parse_port_list,
)

NS = "paas-ws-abc12345"


def _internet_rule(np):
    rules = [r for r in np["spec"]["egress"]
             if any("ipBlock" in p for p in r.get("to", []))]
    assert len(rules) == 1
    return rules[0]


def _allowed_ports(rule, protocol):
    """展開 rule.ports 成允許的埠集合；沒有 ports＝全部埠。"""
    if "ports" not in rule:
        return set(range(1, 65536))
    out = set()
    for p in rule["ports"]:
        if p["protocol"] != protocol:
            continue
        out.update(range(p["port"], p.get("endPort", p["port"]) + 1))
    return out


# --- parse_port_list ---------------------------------------------------------------


def test_parse_port_list_sorts_dedups_and_strips():
    assert parse_port_list(" 25, 22,25 ,") == [22, 25]
    assert parse_port_list("") == []
    assert parse_port_list(None) == []


@pytest.mark.parametrize("raw", ["0", "65536", "abc", "22-25", "-1"])
def test_parse_port_list_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_port_list(raw)


# --- 預設＝修補前逐字相同 ---------------------------------------------------------------


def test_default_manifest_is_byte_identical_to_pre_fix():
    """沒開開關時 manifest 不能有任何差異——既有 54 個 ns reconcile 不能出現 diff。"""
    np = build_baseline_egress_netpol(NS)
    assert np["spec"]["podSelector"] == {}
    rule = _internet_rule(np)
    assert rule == {"to": [{"ipBlock": {"cidr": "0.0.0.0/0",
                                        "except": BASELINE_EGRESS_BLOCKED_CIDRS}}]}
    assert build_baseline_egress_netpols(NS) == [np]


# --- 封埠 ---------------------------------------------------------------------------


@pytest.mark.parametrize("protocol", ["TCP", "UDP"])
def test_blocked_ports_removed_everything_else_allowed(protocol):
    rule = _internet_rule(build_baseline_egress_netpol(NS, blocked_ports=[22, 25]))
    allowed = _allowed_ports(rule, protocol)
    assert 22 not in allowed and 25 not in allowed
    assert allowed == set(range(1, 65536)) - {22, 25}
    # 合法用途：HTTP(S)、SMTP submission、IMAPS、DoT、MQTT(S)、git 以外的 ssh 不在此列
    for port in (80, 443, 465, 587, 993, 853, 1883, 8883):
        assert port in allowed


def test_blocked_ports_uses_endport_ranges_not_65k_entries():
    rule = _internet_rule(build_baseline_egress_netpol(NS, blocked_ports=[22, 25]))
    tcp = [(p["port"], p.get("endPort")) for p in rule["ports"] if p["protocol"] == "TCP"]
    assert tcp == [(1, 21), (23, 24), (26, 65535)]


def test_blocked_edge_ports_1_and_65535():
    rule = _internet_rule(build_baseline_egress_netpol(NS, blocked_ports=[1, 2, 65535]))
    tcp = [(p["port"], p.get("endPort")) for p in rule["ports"] if p["protocol"] == "TCP"]
    assert tcp == [(3, 65534)]


def test_single_port_gap_has_no_endport():
    rule = _internet_rule(build_baseline_egress_netpol(NS, blocked_ports=[22, 24]))
    tcp = [p for p in rule["ports"] if p["protocol"] == "TCP"]
    assert {"protocol": "TCP", "port": 23} in tcp


def test_blocked_ports_keep_cidr_excepts_same_ns_and_dns():
    np = build_baseline_egress_netpol(NS, blocked_ports=[25])
    base = build_baseline_egress_netpol(NS)
    assert np["spec"]["egress"][0] == base["spec"]["egress"][0]   # 同 ns 全埠
    assert np["spec"]["egress"][1] == base["spec"]["egress"][1]   # kube-dns 53
    assert _internet_rule(np)["to"] == _internet_rule(base)["to"]   # 私網排除不變


# --- web profile（agent 類 SKU 只拿 80/443）--------------------------------------------


def test_web_profile_general_policy_excludes_web_pods():
    np = build_baseline_egress_netpol(NS, web_profile=True)
    assert np["spec"]["podSelector"] == {"matchExpressions": [{
        "key": EGRESS_PROFILE_LABEL, "operator": "NotIn", "values": [EGRESS_PROFILE_WEB],
    }]}


def test_web_policy_selects_only_web_pods_and_only_tcp_80_443():
    np = build_baseline_egress_web_netpol(NS)
    assert np["metadata"]["name"] == BASELINE_EGRESS_WEB_NP_NAME
    assert np["metadata"]["labels"] == {"paas-managed": "true"}
    assert np["spec"]["podSelector"] == {
        "matchLabels": {EGRESS_PROFILE_LABEL: EGRESS_PROFILE_WEB}}
    assert np["spec"]["policyTypes"] == ["Egress"]
    rule = _internet_rule(np)
    assert _allowed_ports(rule, "TCP") == set(WEB_PROFILE_TCP_PORTS) == {80, 443}
    assert _allowed_ports(rule, "UDP") == set()
    assert rule["to"][0]["ipBlock"]["except"] == BASELINE_EGRESS_BLOCKED_CIDRS
    base = build_baseline_egress_netpol(NS)
    assert np["spec"]["egress"][0] == base["spec"]["egress"][0]   # 同工作區仍是區網
    assert np["spec"]["egress"][1] == base["spec"]["egress"][1]   # DNS


def test_netpols_bundle_orders_web_first_then_general():
    """先套 web 再收窄一般 baseline：web pod 任何時刻都至少被一條政策選到（不會短暫全開）。"""
    nps = build_baseline_egress_netpols(NS, blocked_ports=[25], web_profile=True)
    assert [n["metadata"]["name"] for n in nps] == [
        BASELINE_EGRESS_WEB_NP_NAME, BASELINE_EGRESS_NP_NAME]
    assert nps[1] == build_baseline_egress_netpol(NS, blocked_ports=[25], web_profile=True)


# --- operator wiring ----------------------------------------------------------------


def _capture(enabled=True, blocked="", web=False):
    k = KubernetesService()
    k.apply_manifest = MagicMock()
    with patch("src.services.helm.settings") as s:
        s.paas_env = "prod"
        s.cloudflared_namespace = ""
        s.namespace_prefix = "paas-ws-"
        s.tenant_baseline_egress_enabled = enabled
        s.tenant_baseline_egress_blocked_ports = blocked
        s.tenant_baseline_egress_web_profile = web
        k._apply_baseline_netpol(NS, 7)
    return [c.args[0] for c in k.apply_manifest.call_args_list]


def test_wiring_defaults_unchanged():
    m = _capture()
    assert m[-1] == build_baseline_egress_netpol(NS)
    assert BASELINE_EGRESS_WEB_NP_NAME not in [x["metadata"]["name"] for x in m]


def test_wiring_blocked_ports_applied():
    m = _capture(blocked="25,22")
    assert m[-1] == build_baseline_egress_netpol(NS, blocked_ports=[22, 25])


def test_wiring_web_profile_appends_both():
    m = _capture(blocked="25", web=True)
    assert m[-2:] == build_baseline_egress_netpols(NS, blocked_ports=[25], web_profile=True)


def test_wiring_master_switch_off_ignores_new_knobs():
    names = [x["metadata"]["name"] for x in _capture(enabled=False, blocked="25", web=True)]
    assert BASELINE_EGRESS_NP_NAME not in names
    assert BASELINE_EGRESS_WEB_NP_NAME not in names


# --- config -------------------------------------------------------------------------


def test_settings_defaults_off():
    s = Settings()
    assert s.tenant_baseline_egress_blocked_ports == ""
    assert s.tenant_baseline_egress_web_profile is False


def test_settings_reject_bad_blocked_ports():
    with pytest.raises(ValueError):
        Settings(tenant_baseline_egress_blocked_ports="25,smtp")


def test_settings_normalise_blocked_ports():
    assert Settings(tenant_baseline_egress_blocked_ports=" 25, 22 ").tenant_baseline_egress_blocked_ports == "22,25"

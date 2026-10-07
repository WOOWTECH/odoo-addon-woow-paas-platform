"""#279 O4: CloudService-ns baseline NetworkPolicy bundle (leg-2 tenant isolation)."""
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.services.helm import (
    REACH_WS_LABEL_VALUE,
    KubernetesService,
    _cloudflared_namespace,
    _env_match_labels,
    reach_ws_label_key,
)


# --- helpers -----------------------------------------------------------------


@patch("src.services.helm.settings")
def test_env_match_labels_set(mock_settings):
    mock_settings.paas_env = "stg"
    assert _env_match_labels() == {"paas.woowtech.io/env": "stg"}


@patch("src.services.helm.settings")
def test_env_match_labels_empty(mock_settings):
    """PAAS_ENV 空 → 空 dict（單 env dev，selector 不帶 env）。"""
    mock_settings.paas_env = ""
    assert _env_match_labels() == {}


@patch("src.services.helm.settings")
def test_cloudflared_namespace_explicit_wins(mock_settings):
    mock_settings.cloudflared_namespace = "custom-cf-ns"
    mock_settings.paas_env = "stg"
    assert _cloudflared_namespace() == "custom-cf-ns"


@pytest.mark.parametrize("env,expected", [
    ("prod", "paas-cloudflare-system"),       # grounded：prod connector ns
    ("stg", "paas-stg-cloudflare-system"),     # grounded：stg connector ns
    ("", ""),                                   # dev/unknown → 無 connector → 不渲染 NP3
])
@patch("src.services.helm.settings")
def test_cloudflared_namespace_env_default(mock_settings, env, expected):
    mock_settings.cloudflared_namespace = ""
    mock_settings.paas_env = env
    assert _cloudflared_namespace() == expected


# --- _apply_baseline_netpol --------------------------------------------------


def _capture(ns="paas-ws-stg-abc123", wid=3):
    k = KubernetesService()
    k.apply_manifest = MagicMock()
    k._apply_baseline_netpol(ns, wid)
    return [c.args[0] for c in k.apply_manifest.call_args_list]


@patch("src.services.helm.settings")
def test_baseline_bundle_stg_four_nps(mock_settings):
    """stg（有 cloudflared ns）→ 4 條 NP，名稱固定；全 ns-wide + ingress-only + paas-managed。"""
    mock_settings.paas_env = "stg"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture()
    names = [x["metadata"]["name"] for x in m]
    assert names == [
        "paas-baseline-default-deny-ingress",
        "paas-baseline-allow-same-ns",
        "paas-baseline-allow-cloudflared",
        "paas-baseline-allow-mcp-reach",
    ]
    for x in m:
        assert x["apiVersion"] == "networking.k8s.io/v1"
        assert x["kind"] == "NetworkPolicy"
        assert x["spec"]["podSelector"] == {}          # 蓋 n8n sidecar app pod
        assert x["spec"]["policyTypes"] == ["Ingress"]  # egress 不由 baseline 管（leg-1 管）
        assert x["metadata"]["labels"] == {"paas-managed": "true"}
        assert x["metadata"]["namespace"] == "paas-ws-stg-abc123"


@patch("src.services.helm.settings")
def test_default_deny_has_no_ingress(mock_settings):
    """default-deny：無 ingress key = deny-all。"""
    mock_settings.paas_env = "stg"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture()
    dd = next(x for x in m if x["metadata"]["name"] == "paas-baseline-default-deny-ingress")
    assert "ingress" not in dd["spec"]


@patch("src.services.helm.settings")
def test_same_ns_allows_podselector_empty(mock_settings):
    mock_settings.paas_env = "stg"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture()
    same = next(x for x in m if x["metadata"]["name"] == "paas-baseline-allow-same-ns")
    assert same["spec"]["ingress"] == [{"from": [{"podSelector": {}}]}]


@patch("src.services.helm.settings")
def test_cloudflared_selects_connector_ns(mock_settings):
    """NP3：namespaceSelector 指 env-specific connector ns（env-safe by construction）。"""
    mock_settings.paas_env = "stg"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture()
    cf = next(x for x in m if x["metadata"]["name"] == "paas-baseline-allow-cloudflared")
    assert cf["spec"]["ingress"] == [{"from": [{"namespaceSelector": {
        "matchLabels": {"kubernetes.io/metadata.name": "paas-stg-cloudflare-system"}}}]}]


@patch("src.services.helm.settings")
def test_mcp_reach_selector_reach_ws_plus_env(mock_settings):
    """leg-2：reach-ws-<W>=true AND env（cross-env 安全）；value=REACH_WS_LABEL_VALUE；無 ports。"""
    mock_settings.paas_env = "stg"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture(wid=3)
    mcp = next(x for x in m if x["metadata"]["name"] == "paas-baseline-allow-mcp-reach")
    ingress = mcp["spec"]["ingress"][0]
    sel = ingress["from"][0]["namespaceSelector"]["matchLabels"]
    assert sel == {reach_ws_label_key(3): REACH_WS_LABEL_VALUE, "paas.woowtech.io/env": "stg"}
    assert sel[reach_ws_label_key(3)] == "true"       # 契約鎖 O1/O3 同一 value
    assert "ports" not in ingress                      # 不鎖 port（3000 sidecar vs 8080 mcp-server）


@patch("src.services.helm.settings")
def test_dev_no_env_no_cloudflared_three_nps(mock_settings):
    """dev（paas_env=''）→ 無 cloudflared NP（3 條），mcp-reach selector 無 env key。"""
    mock_settings.paas_env = ""
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture()
    names = [x["metadata"]["name"] for x in m]
    assert "paas-baseline-allow-cloudflared" not in names
    assert len(m) == 3
    mcp = next(x for x in m if x["metadata"]["name"] == "paas-baseline-allow-mcp-reach")
    sel = mcp["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]
    assert sel == {reach_ws_label_key(3): REACH_WS_LABEL_VALUE}  # 無 env key


# --- create_namespace wiring -------------------------------------------------


@patch("src.services.helm.settings")
@patch("subprocess.run")
def test_create_namespace_wires_baseline_when_workspace_id(mock_run, mock_settings):
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.paas_env = "stg"
    mock_settings.registry_username = ""
    mock_settings.registry_password = ""
    # #1434：LimitRange 讀這兩個 settings；整顆 MagicMock 化時給字串，否則 yaml 炸開。
    mock_settings.tenant_ephemeral_storage_default_request = "128Mi"
    mock_settings.tenant_ephemeral_storage_default_limit = "4Gi"
    mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
    k = KubernetesService()
    k._apply_baseline_netpol = MagicMock()
    k.ensure_registry_secret = MagicMock()
    # #423：本組只驗 baseline NP 接線，default-SA 收斂另有專屬測試；
    # 這裡 mock 掉，避免 settings 被整顆 MagicMock 化後 json.dumps 炸開。
    k.ensure_default_sa_image_pull_secret = MagicMock()
    k.apply_pod_security = MagicMock()  # #1441：PSA 另有專屬測試
    k.create_namespace("paas-ws-stg-abc", cpu_limit="1", memory_limit="1Gi",
                       storage_limit="1Gi", workspace_id=7)
    k._apply_baseline_netpol.assert_called_once_with("paas-ws-stg-abc", 7)
    k.apply_pod_security.assert_called_once_with("paas-ws-stg-abc")


@patch("src.services.helm.settings")
@patch("subprocess.run")
def test_create_namespace_skips_baseline_when_no_workspace_id(mock_run, mock_settings):
    """無 workspace_id → warn + skip（不 raise，向後相容；tenant-boundary fail-loud 屬 Odoo 層）。"""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.paas_env = "stg"
    mock_settings.registry_username = ""
    mock_settings.registry_password = ""
    # #1434：LimitRange 讀這兩個 settings；整顆 MagicMock 化時給字串，否則 yaml 炸開。
    mock_settings.tenant_ephemeral_storage_default_request = "128Mi"
    mock_settings.tenant_ephemeral_storage_default_limit = "4Gi"
    mock_run.return_value = Mock(returncode=0, stdout="{}", stderr="")
    k = KubernetesService()
    k._apply_baseline_netpol = MagicMock()
    k.ensure_registry_secret = MagicMock()
    # #423：本組只驗 baseline NP 接線，default-SA 收斂另有專屬測試；
    # 這裡 mock 掉，避免 settings 被整顆 MagicMock 化後 json.dumps 炸開。
    k.ensure_default_sa_image_pull_secret = MagicMock()
    k.apply_pod_security = MagicMock()
    k.create_namespace("paas-ws-dev", cpu_limit="1", memory_limit="1Gi", storage_limit="1Gi")
    k._apply_baseline_netpol.assert_not_called()
    k.apply_pod_security.assert_not_called()  # 非租戶 ns 不加 PSA（與 NP 同一個判準）


# --- P5 backfill: stamp_managed_ns_labels ------------------------------------


@patch("src.services.helm.settings")
def test_stamp_managed_ns_labels_cs_ns(mock_settings):
    """CS-ns backfill：kubectl label --overwrite 帶 build_managed_ns_labels 全集。"""
    mock_settings.paas_env = "stg"
    mock_settings.namespace_prefix = "paas-ws-"
    k = KubernetesService()
    k._run_command = MagicMock()
    labels = k.stamp_managed_ns_labels("paas-ws-stg-x", workspace_id=7)
    assert labels["woow.tw/workspace-id"] == "7"
    assert labels["paas.woowtech.io/env"] == "stg"
    assert labels["paas-managed"] == "true"
    cmd = k._run_command.call_args.args[0]
    assert cmd[:4] == ["label", "ns", "paas-ws-stg-x", "--overwrite"]
    assert "woow.tw/workspace-id=7" in cmd
    assert "paas.woowtech.io/env=stg" in cmd


@patch("src.services.helm.settings")
def test_stamp_managed_ns_labels_agent_ns_add_only(mock_settings):
    """agent-ns backfill：加 env + reach-ws，全為 k=v（add-only，無 'key-' delete）。"""
    mock_settings.paas_env = "stg"
    mock_settings.namespace_prefix = "paas-ws-"
    k = KubernetesService()
    k._run_command = MagicMock()
    k.stamp_managed_ns_labels("paas-ws-pa-abc", reach_workspace_ids=[3, 5])
    cmd = k._run_command.call_args.args[0]
    assert "woow.tw/reach-ws-3=true" in cmd
    assert "woow.tw/reach-ws-5=true" in cmd
    assert "paas.woowtech.io/env=stg" in cmd
    assert all("=" in a for a in cmd[4:])   # 全 add-only、無 delete syntax

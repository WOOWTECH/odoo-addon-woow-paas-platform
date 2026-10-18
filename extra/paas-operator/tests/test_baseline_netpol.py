"""#279 O4: CloudService-ns baseline NetworkPolicy bundle (leg-2 tenant isolation)."""
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.services.tenant_baseline import (
    DATA_STORE_COMPONENTS,
    build_cross_ns_ingress_pod_selector,
)
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
    mock_settings.namespace_prefix = "paas-ws-stg-"
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
        assert x["spec"]["policyTypes"] == ["Ingress"]  # egress 不由 baseline 管（leg-1 管）
        assert x["metadata"]["labels"] == {"paas-managed": "true"}
        assert x["metadata"]["namespace"] == "paas-ws-stg-abc123"
    by_name = {x["metadata"]["name"]: x for x in m}
    # 同 ns 兩條仍是整個 ns（蓋 n8n sidecar app pod）
    assert by_name["paas-baseline-default-deny-ingress"]["spec"]["podSelector"] == {}
    assert by_name["paas-baseline-allow-same-ns"]["spec"]["podSelector"] == {}
    # R13：跨 ns 兩條排除資料庫 pod
    for name in ("paas-baseline-allow-cloudflared", "paas-baseline-allow-mcp-reach"):
        assert by_name[name]["spec"]["podSelector"] == build_cross_ns_ingress_pod_selector()


@patch("src.services.helm.settings")
def test_default_deny_has_no_ingress(mock_settings):
    """default-deny：無 ingress key = deny-all。"""
    mock_settings.paas_env = "stg"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-stg-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture()
    dd = next(x for x in m if x["metadata"]["name"] == "paas-baseline-default-deny-ingress")
    assert "ingress" not in dd["spec"]


@patch("src.services.helm.settings")
def test_same_ns_allows_podselector_empty(mock_settings):
    mock_settings.paas_env = "stg"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-stg-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture()
    same = next(x for x in m if x["metadata"]["name"] == "paas-baseline-allow-same-ns")
    assert same["spec"]["ingress"] == [{"from": [{"podSelector": {}}]}]


@patch("src.services.helm.settings")
def test_cloudflared_selects_connector_ns(mock_settings):
    """NP3：namespaceSelector 指 env-specific connector ns（env-safe by construction）。"""
    mock_settings.paas_env = "stg"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-stg-"
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
    mock_settings.namespace_prefix = "paas-ws-stg-"
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
    mock_settings.namespace_prefix = "dev-sandbox-netpol-"
    mock_settings.tenant_baseline_egress_enabled = False  # #1441 egress 另測
    m = _capture(ns="dev-sandbox-netpol-example")
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
    mock_settings.namespace_prefix = "paas-ws-stg-"
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
    mock_settings.namespace_prefix = "paas-ws-stg-"
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
    k.create_namespace("paas-ws-stg-dev", cpu_limit="1", memory_limit="1Gi", storage_limit="1Gi")
    k._apply_baseline_netpol.assert_not_called()
    k.apply_pod_security.assert_not_called()  # 非租戶 ns 不加 PSA（與 NP 同一個判準）


# --- P5 backfill: stamp_managed_ns_labels ------------------------------------


@patch("src.services.helm.settings")
def test_stamp_managed_ns_labels_cs_ns(mock_settings):
    """CS-ns backfill：kubectl label --overwrite 帶 build_managed_ns_labels 全集。"""
    mock_settings.paas_env = "stg"
    mock_settings.namespace_prefix = "paas-ws-stg-"
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
    mock_settings.namespace_prefix = "paas-ws-stg-"
    k = KubernetesService()
    k._run_command = MagicMock()
    k.stamp_managed_ns_labels("paas-ws-stg-pa-abc", reach_workspace_ids=[3, 5])
    cmd = k._run_command.call_args.args[0]
    assert "woow.tw/reach-ws-3=true" in cmd
    assert "woow.tw/reach-ws-5=true" in cmd
    assert "paas.woowtech.io/env=stg" in cmd
    assert all("=" in a for a in cmd[4:])   # 全 add-only、無 delete syntax


# --- R13：跨 ns 入口不放行資料庫 pod ------------------------------------------------


def _selects(selector: dict, labels: dict) -> bool:
    """最小的 LabelSelector 求值（matchLabels＋In／NotIn／Exists／DoesNotExist），語意同 k8s。"""
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


# 2026-10-15 正式站 53 個 paas-ws-* 的實際 pod 標籤（只取 selector 會看的三個鍵）＋
# woow-paas-charts main ffcf2cd 裡還沒部署的資料庫 component。
_REACHABLE_POD_LABELS = [
    # (說明, labels)
    ("litellm app :4000", {"app.kubernetes.io/name": "litellm", "app.kubernetes.io/component": "app"}),
    ("n8n（無 component 標籤）", {"app.kubernetes.io/name": "n8n"}),
    ("odoo server（nginx :8080）", {"app.kubernetes.io/name": "odoo", "app.kubernetes.io/component": "server"}),
    ("haos virt-launcher", {"app.kubernetes.io/name": "haos", "app.kubernetes.io/component": "haos"}),
    ("haos xff-proxy", {"app.kubernetes.io/name": "haos", "app.kubernetes.io/component": "xff-proxy"}),
    ("home-assistant", {"app.kubernetes.io/name": "home-assistant", "app.kubernetes.io/component": "home-assistant"}),
    ("home-assistant-full", {"app.kubernetes.io/name": "home-assistant-full", "app.kubernetes.io/component": "home-assistant-full"}),
    ("mcp-server", {"app.kubernetes.io/name": "mcp-server", "app.kubernetes.io/component": "mcp-server"}),
    ("emqx broker", {"app.kubernetes.io/name": "emqx", "app.kubernetes.io/component": "broker"}),
    ("headscale", {"app.kubernetes.io/name": "headscale", "app.kubernetes.io/component": "vpn-control"}),
    ("tailscale", {"app.kubernetes.io/name": "tailscale", "app.kubernetes.io/component": "vpn-node"}),
    ("code-server", {"app.kubernetes.io/name": "code-server", "app.kubernetes.io/component": "code-server"}),
    ("pi-agent", {"app.kubernetes.io/name": "pi-agent", "app.kubernetes.io/component": "pi-agent"}),
    ("omnigent server", {"app.kubernetes.io/name": "omnigent", "app.kubernetes.io/component": "server"}),
    ("omnigent llm-proxy", {"app.kubernetes.io/name": "omnigent-llm-proxy", "app.kubernetes.io/component": "llm-proxy"}),
    ("open-design daemon", {"app.kubernetes.io/name": "open-design", "app.kubernetes.io/component": "daemon"}),
    ("open-design console", {"app.kubernetes.io/name": "open-design-console", "app.kubernetes.io/component": "console"}),
    ("hermes-agent daemon", {"app.kubernetes.io/name": "hermes-agent", "app.kubernetes.io/component": "daemon"}),
    ("nextcloud app", {"app.kubernetes.io/name": "nextcloud", "app.kubernetes.io/component": "app"}),
    ("vibe-kanban electric", {"app.kubernetes.io/name": "vibe-kanban", "app.kubernetes.io/component": "electric"}),
    ("omnigent sandbox host（無 app.kubernetes.io 標籤）", {"app.kubernetes.io/managed-by": "omnigent"}),
    ("完全沒有標籤的 pod", {}),
]

_DATA_STORE_POD_LABELS = [
    ("litellm postgresql", {"app.kubernetes.io/name": "litellm", "app.kubernetes.io/component": "postgresql"}),
    ("woow-odoo postgresql", {"app.kubernetes.io/name": "odoo", "app.kubernetes.io/component": "postgresql"}),
    ("woow-odoo db-backup Job", {"app.kubernetes.io/name": "odoo", "app.kubernetes.io/component": "db-backup"}),
    ("woow-odoo db-restore Job", {"app.kubernetes.io/name": "odoo", "app.kubernetes.io/component": "db-restore"}),
    ("nextcloud postgresql", {"app.kubernetes.io/name": "nextcloud", "app.kubernetes.io/component": "postgresql"}),
    ("nextcloud redis", {"app.kubernetes.io/name": "nextcloud", "app.kubernetes.io/component": "redis"}),
    ("hermes-agent database", {"app.kubernetes.io/name": "hermes-agent", "app.kubernetes.io/component": "database"}),
    ("hermes-agent cache", {"app.kubernetes.io/name": "hermes-agent", "app.kubernetes.io/component": "cache"}),
    ("vibe-kanban remote-db", {"app.kubernetes.io/name": "vibe-kanban", "app.kubernetes.io/component": "remote-db"}),
    ("Bitnami 式 postgresql 子 chart", {"app.kubernetes.io/name": "postgresql", "app.kubernetes.io/component": "primary"}),
    ("Bitnami 式 redis 子 chart", {"app.kubernetes.io/name": "redis", "app.kubernetes.io/component": "master"}),
    ("管理者 Omnigent 手動 PostgreSQL", {"app": "omnigent-pg", "woow.tw/manual": "omnigent-cutover"}),
    ("管理者 Omnigent 手動備份 Job", {"app": "omnigent-pg-backup", "woow.tw/manual": "omnigent-cutover"}),
]


@pytest.mark.parametrize("desc,labels", _REACHABLE_POD_LABELS, ids=[d for d, _ in _REACHABLE_POD_LABELS])
def test_cross_ns_selector_keeps_every_app_pod(desc, labels):
    """R13 不可讓任何現有服務的公開網址斷線：所有 app／proxy pod（含沒標籤的）仍被選到。"""
    assert _selects(build_cross_ns_ingress_pod_selector(), labels), desc


@pytest.mark.parametrize("desc,labels", _DATA_STORE_POD_LABELS, ids=[d for d, _ in _DATA_STORE_POD_LABELS])
def test_cross_ns_selector_excludes_data_stores(desc, labels):
    """R13：cloudflared／agent ns 進不到任何資料庫 pod（只剩 allow-same-ns 放行）。"""
    assert not _selects(build_cross_ns_ingress_pod_selector(), labels), desc


def test_cross_ns_selector_only_notin():
    """只用 NotIn：沒有該標籤的 pod 一律照舊放行（不可改成 In 列舉，會漏 chart 而斷線）。"""
    sel = build_cross_ns_ingress_pod_selector()
    assert "matchLabels" not in sel
    assert {e["operator"] for e in sel["matchExpressions"]} == {"NotIn"}
    assert all(e["values"] for e in sel["matchExpressions"])  # 空 values 的 NotIn 會被 API 拒絕


def test_cross_ns_selector_returns_fresh_lists():
    """呼叫端改回傳值不會污染模組常數（manifest 之後會被 yaml dump／比對）。"""
    sel = build_cross_ns_ingress_pod_selector()
    sel["matchExpressions"][0]["values"].append("x")
    assert "x" not in DATA_STORE_COMPONENTS


@patch("src.services.helm.settings")
def test_ports_not_restricted_on_cross_ns_rules(mock_settings):
    """不限 port（同 ns 多服務、對外埠各異；逐 release 收斂是後續項）——鎖住目前行為，改的人要看 PR 說明。"""
    mock_settings.paas_env = "prod"
    mock_settings.cloudflared_namespace = ""
    mock_settings.namespace_prefix = "paas-ws-"
    mock_settings.tenant_baseline_egress_enabled = False
    m = _capture(ns="paas-ws-abc123")
    for x in m:
        for rule in x["spec"].get("ingress") or []:
            assert "ports" not in rule

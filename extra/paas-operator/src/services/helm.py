"""Helm CLI wrapper service."""
import base64
import json
import logging
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings
from src.services.helm_limiter import helm_operation_slot
from src.services.tenant_baseline import (
    BASELINE_EGRESS_NP_NAME,
    PSA_EXEMPT_ANNOTATION,
    PSA_EXEMPT_REASON,
    build_baseline_egress_netpol,
    build_cross_ns_ingress_pod_selector,
    build_pod_security_labels,
    is_psa_exempt,
    parse_namespace_list,
    psa_label_keys,
)
from src.models.schemas import (
    ContainerWaitingInfo,
    DataVolumeInfo,
    PodInfo,
    PodPhase,
    PodWarningEvent,
    ReleaseInfo,
    ReleaseRevision,
    ReleaseStatus,
    VmiInfo,
    WorkloadInfo,
    WorkloadReplicaFailure,
)

logger = logging.getLogger(__name__)


# #461：helm CLI 的**非-wait** 開銷（OCI pull ＋ render ＋ apply），供呼叫端推算
# subprocess 預算用。**必須與 `--timeout` 分開記**：`--timeout` 只約束 helm 等待
# Kubernetes 資源就緒的那一段，不約束 chart 下載與 render。
#
# 依據（#459 stg 實測）：Secret-only 的 `helm upgrade` 整次牆鐘 10.7s／11.9s，
# 而其中 wait 段 ≈0（不動 pod template ⇒ 既有 pod 已 Ready，--wait 立即通過）
# ⇒ 非-wait 開銷實測上界 ≈12s。`--atomic` 失敗時的自動 rollback 走 stored
# manifest、不重拉 chart，其 apply 再 ~1–2s ⇒ 15s 蓋住兩次 apply。
HELM_CLI_OVERHEAD_SECONDS = 15


# #317: post-renderer that stamps helm.sh/resource-policy: keep on every PVC so
# chart-owned PVCs survive `helm uninstall`. helm decides deletion from the STORED
# manifest, not live objects, so keep must be present at install/upgrade time.
# Run via the operator's own interpreter (sys.executable) so PyYAML is guaranteed,
# regardless of $PATH / shebang / executable bit.
_PVC_KEEP_POST_RENDERER = str(
    Path(__file__).resolve().parent.parent / "pvc_keep_post_renderer.py"
)


def _pvc_keep_post_renderer_args(
    live_snapshot: Optional[Dict[str, Any]] = None,
    tenant: Optional[Dict[str, Any]] = None,
    tenant_live: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Helm CLI flags to run the PVC-keep post-renderer (shared by install/upgrade).

    #1407: ``live_snapshot`` (``pvc_keep_post_renderer.live_snapshot`` shape) is handed
    to the renderer as a second arg so objects that already exist render with the live
    values of their immutable fields (PVC storageClassName/accessModes/volumeMode/size,
    StatefulSet volumeClaimTemplates). Omitted when there is nothing live to reuse.

    AI-FIX-NET-1: ``tenant`` (``_tenant_policy`` shape) switches on the egress gate /
    node-avoidance stage of the same renderer; ``tenant_live`` is the mirror-live facts.
    Both ride in the same JSON arg, so with the policy off the argv is byte-identical to
    before.
    """
    args = [
        "--post-renderer", sys.executable,
        "--post-renderer-args", _PVC_KEEP_POST_RENDERER,
    ]
    payload: Dict[str, Any] = {}
    if live_snapshot and (live_snapshot.get("pvc") or live_snapshot.get("sts")):
        payload.update(live_snapshot)
    if tenant:
        payload["tenant"] = tenant
        if tenant_live:
            payload["tenant_live"] = tenant_live
    if payload:
        args.extend([
            "--post-renderer-args",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        ])
    return args


def _has_baseline_egress(namespace: str) -> bool:
    """該 ns 是否真的有 ``paas-baseline-egress``（gate 等的就是它）；查不到一律 False。"""
    try:
        KubernetesService()._run_command([
            "get", "networkpolicy", BASELINE_EGRESS_NP_NAME,
            "--namespace", namespace, "--output", "name",
        ])
        return True
    except Exception as e:  # noqa: BLE001 — NotFound 與查詢失敗都不插 gate
        logger.info("no %s in %s (%s); egress gate not injected", BASELINE_EGRESS_NP_NAME, namespace, e)
        return False


def _tenant_policy(namespace: str, mirror_live: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """AI-FIX-NET-1：這個 ns 的 install／upgrade 要套的 render 期政策 ``(options, live_facts)``。

    兩個開關都關、或 ns 不是租戶 ns、或在 PSA 例外清單（管理者用的 Omnigent）→ ``({}, {})``。
    ``mirror_live``（reset-config 這類「不得改 pod template」的路徑）：只有叢集上已經有的項目
    才套，避免一次密碼重設變成整個服務滾動重啟；讀不到叢集現況時整個略過（寧可不套）。
    """
    from src.tenant_workload_policy import live_facts

    raw_avoid = settings.tenant_avoid_nodes
    avoid = parse_namespace_list(raw_avoid) if isinstance(raw_avoid, str) else []
    # `is True`：只有明確的布林 True 才開（測試用的 mock settings、未設定都視為關）。
    gate_on = (settings.tenant_egress_gate_enabled is True
               and settings.tenant_baseline_egress_enabled is True)
    if not avoid and not gate_on:
        return {}, {}
    if not namespace.startswith(settings.namespace_prefix):
        return {}, {}
    if is_psa_exempt(namespace, parse_namespace_list(settings.psa_exempt_namespaces)):
        return {}, {}
    options: Dict[str, Any] = {}
    if avoid:
        options["avoid_nodes"] = avoid
    if gate_on and _has_baseline_egress(namespace):
        options["egress_gate"] = {
            "image": settings.tenant_egress_gate_image,
            "timeout_seconds": settings.tenant_egress_gate_timeout_seconds,
            "on_timeout": settings.tenant_egress_gate_on_timeout,
            "target": settings.tenant_egress_gate_target,
        }
    if not options:
        return {}, {}
    if not mirror_live:
        return options, {}
    items = _live_objects(namespace, "deployments,statefulsets,daemonsets,cronjobs")
    if items is None:
        logger.warning("tenant policy skipped for %s: live workloads unreadable (mirror mode)", namespace)
        return {}, {}
    options["mirror_live"] = True
    return options, live_facts(items, avoid)


def _openapi_validation_args() -> List[str]:
    """#1552：install／upgrade 不讓 helm 下載＋解析整份 ``/openapi/v2``（見 settings 註解）。

    k8s ≥ 1.27 的 API server 支援 fieldValidation，helm 3.13 本來就略過 client 端 schema
    驗證，下載的 schema 只佔記憶體（本叢集每個 helm 約 +150MiB）。``HELM_DISABLE_OPENAPI_VALIDATION=false``
    回到舊行為。
    """
    return ["--disable-openapi-validation"] if settings.helm_disable_openapi_validation else []


def _live_objects(namespace: str, kinds: str = "pvc,statefulsets") -> Optional[List[Dict[str, Any]]]:
    """Live objects of ``kinds`` in ``namespace`` (kubectl ``items``); None on failure.

    #1407. Callers treat None as "unknown" and fall back to the pre-#1407 behaviour,
    so a failed lookup can never make an install/upgrade fail that would otherwise
    have succeeded.
    """
    try:
        result = KubernetesService()._run_command(
            ["get", kinds, "--namespace", namespace, "--output", "json"]
        )
        items = json.loads(result.stdout or "{}").get("items")
        return items if isinstance(items, list) else []
    except Exception as e:  # noqa: BLE001 — degrade to pre-#1407 behaviour
        logger.warning(
            "Could not list %s in %s for live-value reuse (#1407): %s", kinds, namespace, e,
        )
        return None


def _live_snapshot(namespace: str) -> Dict[str, Any]:
    """Renderer snapshot of the namespace's PVCs and StatefulSets ({} when unknown)."""
    from src.pvc_keep_post_renderer import live_snapshot

    items = _live_objects(namespace)
    return live_snapshot(items) if items else {}


# issue #29: the private Helm repo token is passed via ``--password`` on the helm
# argv. Redact credential values BEFORE they reach the "Executing:" log line and
# HelmException.command, so the token never persists in operator stdout / log
# aggregation / SIEM. (The argv is still briefly visible via /proc/<pid>/cmdline
# during the subprocess lifetime — a narrower, transient exposure tracked as a
# follow-up; this change kills the persistent, broadly-readable log leak.)
#
# Second audit pass (same issue): the flag-oriented rules above only see secrets
# that sit in their OWN argv token. Two call paths carry the secret *inside a
# payload token* and were therefore still logged verbatim:
#   1. ``kubectl patch secret --patch={"data":{"N8N_API_KEY":"<b64>"}}``
#      (patch_secret ← n8n_init) — base64 is encoding, not protection. The same
#      shape carries tenant MCP credentials through patch_configmap.
#   2. ``kubectl exec … -- node -e '<script>'`` where the inline script embeds
#      the tenant's n8n owner password / auth cookie (n8n_init._node_request).
# Both are masked below by payload-aware rules.
_SECRET_VALUE_FLAGS = frozenset({"--password", "--username"})
_SECRET_SET_KEY_HINTS = ("password", "token", "secret", "key", "credential")
_SET_FLAGS = ("--set", "--set-string", "--set-json", "--set-literal")
_PATCH_VALUE_FLAGS = frozenset({"--patch", "-p"})
# Fields whose *every* value is a secret by definition, whatever the key is
# named (a Secret's ``data`` needs no hint to justify masking).
_OPAQUE_PAYLOAD_FIELDS = frozenset({"data", "stringData"})
# JSON keys inside a payload whose value must be masked. Superset of the --set
# hints: an inline HTTP script carries credentials as headers too.
_PAYLOAD_KEY_HINTS = _SECRET_SET_KEY_HINTS + ("passwd", "cookie", "authorization")
# ``valueFrom`` subtrees (secretKeyRef / configMapKeyRef) name *where* a value
# lives — ``{"name": "n8n-release", "key": "N8N_API_KEY"}`` is a public pointer,
# never the value itself. Masking it by key-name substring would hide the only
# readable half while the real secret sits in a sibling ``value`` slot.
_REFERENCE_ONLY_FIELDS = frozenset({"valueFrom", "secretKeyRef", "configMapKeyRef",
                                    "secretRef", "configMapRef", "fieldRef"})
# ``"password": "v"`` and its backslash-escaped twin ``\"password\": \"v\"`` —
# the n8n script embeds the request body as an escaped JSON string literal, so
# both quote forms must match. The value runs lazily up to the closing quote
# *plus a JSON delimiter*, which is what lets it step over an escaped quote
# inside the value (a tenant password may legally contain " and \).
_INLINE_KEY_RE = (
    r'\\?"[\w.\-]*(?:' + "|".join(_PAYLOAD_KEY_HINTS) + r')[\w.\-]*\\?"\s*:\s*'
)
_INLINE_SECRET_RE = re.compile(
    r'(' + _INLINE_KEY_RE + r'\\?")'
    r'(.*?)'
    r'(\\?"\s*[,}])',
    re.IGNORECASE | re.DOTALL,
)
# Presence of a secret-ish key *with a string value* — the pair regex above must
# account for every one of these; any shortfall means a value whose end could not
# be delimited, and an undelimited secret must not be logged.
_INLINE_KEY_PRESENT_RE = re.compile(_INLINE_KEY_RE + r'\\?"', re.IGNORECASE)


def _redact_inline_secrets(text: str) -> str:
    """Mask ``"<secret-ish key>": "<value>"`` pairs embedded in a free-form token.

    Fails closed in the two cases where the value's boundary is not trustworthy
    — a secret-ish key the pair regex could not match at all, or a value that
    itself contains a quote (a tenant password may legally contain " and \\, and
    an escaped quote makes the lazy match's stopping point a guess). Then the
    whole token is replaced instead of logged: a regex that stops matching must
    never degrade into plaintext.
    """
    ambiguous = False

    def _mask(match: "re.Match[str]") -> str:
        nonlocal ambiguous
        if '"' in match.group(2):
            ambiguous = True
        return f"{match.group(1)}<redacted>{match.group(3)}"

    masked, replaced = _INLINE_SECRET_RE.subn(_mask, text)
    if ambiguous or replaced < len(_INLINE_KEY_PRESENT_RE.findall(text)):
        return "<redacted-payload>"
    return masked


def _mask_payload_tree(node: Any, opaque: bool = False) -> Any:
    """Mask secret leaves of a parsed JSON payload, keeping keys visible.

    ``opaque`` propagates into ``data`` / ``stringData`` subtrees, where every
    value is masked regardless of its key name.
    """
    if isinstance(node, dict):
        # k8s EnvVar: the secret lives in the ``value`` slot and the key that
        # names it is a *sibling* (``{"name": "AUTH_TOKEN", "value": "…"}``), so
        # key-name matching alone can never reach it. This is how the MCP
        # sidecar's bearer token rides a `--type=json` patch.
        if ("name" in node and "value" in node
                and not isinstance(node["value"], (dict, list))
                and any(h in str(node["name"]).lower() for h in _PAYLOAD_KEY_HINTS)):
            return {k: ("<redacted>" if k == "value"
                        else _mask_payload_tree(v, opaque=opaque))
                    for k, v in node.items()}
        masked = {}
        for key, value in node.items():
            key_lower = str(key).lower()
            if key in _REFERENCE_ONLY_FIELDS:
                masked[key] = value
            elif opaque or any(h in key_lower for h in _PAYLOAD_KEY_HINTS):
                masked[key] = "<redacted>"
            elif key in _OPAQUE_PAYLOAD_FIELDS:
                masked[key] = _mask_payload_tree(value, opaque=True)
            else:
                masked[key] = _mask_payload_tree(value, opaque=opaque)
        return masked
    if isinstance(node, list):
        return [_mask_payload_tree(item, opaque=opaque) for item in node]
    return "<redacted>" if opaque else node


def _redact_payload(value: str) -> str:
    """Mask secrets in a ``--patch`` payload, preserving its non-secret shape.

    Falls back to the inline regex when the payload isn't parseable JSON, so an
    unexpected payload form degrades to *masked*, never to *leaked*.
    """
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return _redact_inline_secrets(value)
    masked = _mask_payload_tree(parsed)
    # Unchanged payload → return the caller's exact string so a non-secret patch
    # (e.g. the VM runStrategy patch) stays byte-identical in the log.
    return value if masked == parsed else json.dumps(masked)


def _redact_set_pairs(value: str) -> str:
    """Mask secret pairs of a ``--set`` value, which may hold several pairs.

    helm accepts ``--set a=1,auth.password=x``; masking only the first pair
    would leak every later one.
    """
    if "=" not in value:
        return value
    out = []
    for pair in value.split(","):
        key = pair.split("=", 1)[0]
        if "=" in pair and any(h in key.lower() for h in _SECRET_SET_KEY_HINTS):
            out.append(f"{key}=<redacted>")
        else:
            out.append(pair)
    return ",".join(out)


def _redact_cmd(cmd: List[str]) -> List[str]:
    """Return a copy of a CLI argv with credential values masked for logging.

    Handles ``--password value`` / ``--password=value`` (and ``--username``),
    ``--set key=value`` family where the key hints at a secret, ``--patch``
    JSON payloads (Secret/ConfigMap ``data``), and secrets embedded in any
    remaining token — notably the inline ``kubectl exec … -- node -e`` script.
    """
    redacted: List[str] = []
    mask_next = False   # next token is a bare credential value
    set_next = False    # next token is a `--set key=value` pair
    patch_next = False  # next token is a `--patch` JSON payload
    for tok in cmd:
        if mask_next:
            redacted.append("<redacted>")
            mask_next = False
            continue
        if set_next:
            set_next = False
            redacted.append(_redact_set_pairs(tok))
            continue
        if patch_next:
            patch_next = False
            redacted.append(_redact_payload(tok))
            continue
        if tok in _SECRET_VALUE_FLAGS:
            redacted.append(tok)
            mask_next = True
            continue
        if tok in _SET_FLAGS:
            redacted.append(tok)
            set_next = True
            continue
        if tok in _PATCH_VALUE_FLAGS:
            redacted.append(tok)
            patch_next = True
            continue
        if "=" in tok and tok.split("=", 1)[0] in _SET_FLAGS:
            flag, value = tok.split("=", 1)
            redacted.append(f"{flag}={_redact_set_pairs(value)}")
            continue
        if "=" in tok and tok.split("=", 1)[0] in _PATCH_VALUE_FLAGS:
            flag, value = tok.split("=", 1)
            redacted.append(f"{flag}={_redact_payload(value)}")
            continue
        if "=" in tok and tok.split("=", 1)[0] in _SECRET_VALUE_FLAGS:
            redacted.append(f"{tok.split('=', 1)[0]}=<redacted>")
            continue
        # Bare `KEY=value` positional (e.g. `kubectl set env … SKILL_BUNDLE_TOKEN=<tok>`)
        # whose key hints at a secret — issue #29 audit found resume injects the
        # signed SKILL_BUNDLE_TOKEN via kubectl set env, also logged via _run_command.
        if "=" in tok and not tok.startswith("-"):
            key = tok.split("=", 1)[0]
            if any(h in key.lower() for h in _SECRET_SET_KEY_HINTS):
                redacted.append(f"{key}=<redacted>")
                continue
        # Catch-all: a secret embedded in an otherwise ordinary token — the
        # `kubectl exec … -- node -e '<script>'` one-liner whose body/headers
        # carry the tenant password and auth cookie.
        redacted.append(_redact_inline_secrets(tok))
    return redacted


def _deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _sanitize_release_values(values: Any) -> Any:
    def _mask_tree(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: _mask_tree(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_mask_tree(item) for item in node]
        return "***"

    def _walk(node: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(node, dict):
            result: Dict[str, Any] = {}
            for key, value in node.items():
                child_path = path + (key,)
                if key == "secret" or child_path[-2:] == ("config", "sensitive"):
                    result[key] = _mask_tree(value)
                else:
                    result[key] = _walk(value, child_path)
            return result
        if isinstance(node, list):
            return [_walk(item, path) for item in node]
        return node

    return _walk(values)


def validate_namespace(namespace: str) -> None:
    """Validate the tenant prefix without crossing the reserved staging boundary."""
    prefix = settings.namespace_prefix
    if not prefix:
        # #284 W0-hardening: an empty prefix makes startswith('') always true,
        # silently disabling the single app-layer tenant boundary (RBAC does not
        # enforce the prefix). Fail closed rather than accept every namespace.
        raise ValueError(
            "namespace_prefix is empty — tenant isolation would be disabled "
            "(startswith('') matches every namespace). Refusing."
        )
    if not namespace.startswith(prefix):
        raise ValueError(
            f"Namespace must start with '{prefix}'"
        )
    # prod's broad prefix also matches every staging tenant on the shared cluster.
    # Keep this in the common guard so Helm reads and Kubernetes writes both stop
    # before invoking their CLI. Custom isolated test prefixes remain unchanged.
    if prefix == "paas-ws-" and namespace.startswith("paas-ws-stg-"):
        raise ValueError("Production operator cannot access staging namespaces")


# k8s label VALUE syntax: empty, or <=63 chars of alnum / '-' / '_' / '.' that
# begin and end with an alphanumeric. A ':' (e.g. from an RFC3339 timestamp) is
# illegal and makes `kubectl label` reject the WHOLE command atomically.
_K8S_LABEL_VALUE_RE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")


def validate_label_value(key: str, value: str) -> None:
    """Fail loud on a label value kubectl would reject.

    Operator #208: an illegal value was handed straight to `kubectl label`, where
    the atomic rejection was swallowed as a warning and the broken PVC GC went
    unnoticed for 74 days. Validate in pure Python so the failure surfaces at a
    unit-catchable boundary instead of only against real kubectl.
    """
    if len(value) > 63 or not _K8S_LABEL_VALUE_RE.match(value):
        raise ValueError(
            f"Invalid k8s label value for {key!r}: {value!r} — must be <=63 "
            f"chars matching {_K8S_LABEL_VALUE_RE.pattern} (e.g. ':' is illegal)"
        )


# k8s label KEY syntax: an optional DNS-subdomain prefix (<=253 chars) + '/', then a
# required name segment (<=63 chars of alnum / '-' / '_' / '.' that begins and ends
# with an alphanumeric). Symmetric to _K8S_LABEL_VALUE_RE.
_K8S_LABEL_NAME_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_K8S_LABEL_PREFIX_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


def validate_label_key(key: str) -> None:
    """Fail loud on a label key kubectl would reject.

    Symmetric to :func:`validate_label_value`. The #279 tenant-isolation keys
    (``woow.tw/workspace-id``, ``woow.tw/reach-ws-<W>``) are derived from a fixed
    prefix + an int, so a bad key means a code/config typo — and a NetworkPolicy
    ``namespaceSelector`` that references a key nothing carries matches nothing
    (silent deny-all), the same quiet break kubectl's atomic rejection let slip for
    74 days (operator #208). Surface it at this pure-Python boundary.
    """
    prefix, sep, name = key.rpartition("/")
    if sep and (len(prefix) > 253 or not _K8S_LABEL_PREFIX_RE.match(prefix)):
        raise ValueError(
            f"Invalid k8s label key prefix {prefix!r} in {key!r} — must be a "
            f"<=253 char lowercase DNS subdomain"
        )
    if not name or len(name) > 63 or not _K8S_LABEL_NAME_RE.match(name):
        raise ValueError(
            f"Invalid k8s label key {key!r}: name segment {name!r} must be 1–63 "
            f"chars matching {_K8S_LABEL_NAME_RE.pattern}"
        )


# #279 tenant-isolation label keys — the operator is the SINGLE SOURCE OF TRUTH for
# these keys, which are also matched by NetworkPolicy selectors:
#   - WORKSPACE_ID_LABEL: stamped on a CloudService ns; matched by the agent-runtime
#     chart's leg-1 egress selector (woow-paas-charts) AND by the operator's own
#     leg-2 baseline ingress. The chart selector MUST use this exact key byte-for-byte
#     or it silently matches nothing (F3/F4).
#   - REACH_WS_LABEL_PREFIX + <workspace id>: stamped on an agent-runtime ns, one per
#     reachable workspace; matched only by the operator's leg-2 baseline NP (this repo).
# The workspace int id is per-Odoo-DB (NOT cluster-unique) and prod/stg share one
# cluster, so every selector that matches these keys MUST also match the env label
# (build_managed_ns_labels stamps paas.woowtech.io/env) — else a stg agent reaches a
# prod CloudService of the same numeric id (F6 cross-env leak).
WORKSPACE_ID_LABEL = "woow.tw/workspace-id"
REACH_WS_LABEL_PREFIX = "woow.tw/reach-ws-"
# The reach-ws label VALUE is a shared constant too: O1 stamps it here and O4's leg-2
# baseline NP ``namespaceSelector.matchLabels`` must match the SAME value — "true" vs
# "1" silently matches nothing (deny-all), the hardest kind of gap to spot. Lock it so
# the stamp side and the selector side reference one source (same rationale as the keys).
REACH_WS_LABEL_VALUE = "true"
OWNER_LABEL = "woow.tw/owner"  # #277：孤兒 ns 失去 Odoo record 後唯一的擁有者線索


def reach_ws_label_key(workspace_id: int) -> str:
    """``woow.tw/reach-ws-<W>`` for one reachable workspace, validated fail-loud."""
    key = f"{REACH_WS_LABEL_PREFIX}{int(workspace_id)}"
    validate_label_key(key)
    return key


def build_managed_ns_labels(
    workspace_id: Optional[int] = None,
    reach_workspace_ids: Optional[List[int]] = None,
    owner: Optional[str] = None,
) -> Dict[str, str]:
    """Labels stamped on every platform-managed tenant namespace.

    Single source for both the marketplace path (create_namespace) and the
    agent-runtime path (agent_runtimes._ensure_namespace) so the two never diverge:

    - ``paas-managed=true`` — existing, name-agnostic NetworkPolicy selector.
    - ``paas.woowtech.io/env=<env>`` — only when PAAS_ENV is set, so prod/stg
      namespaces sharing one cluster are ``kubectl get ns -l`` selectable by env.
      Omitted when PAAS_ENV is empty (dev / unknown stays label-free, backward
      compatible).

    #279 tenant-isolation labels (opt-in via the two params; both env-qualified by
    the paas.woowtech.io/env label above so a per-DB workspace id can't match across
    the shared prod/stg cluster):

    - ``woow.tw/workspace-id=<W>`` — pass ``workspace_id`` for a CloudService ns (a
      CloudService belongs to exactly one workspace). The chart's leg-1 egress
      selector matches it; the value is ``str()``-cast because egressWorkspaceIds is
      a list of ints.
    - ``woow.tw/reach-ws-<W>=true`` (one per id) — pass ``reach_workspace_ids`` for an
      agent-runtime ns (= the project's reachable workspaces). The leg-2 baseline
      ingress on each CloudService ns matches the agent ns by its own workspace key.

    Every env/#279 value is validated as a legal k8s label value (and #279 keys as a
    legal key) BEFORE it can reach a manifest, surfacing an illegal value at this
    pure-Python boundary instead of as a swallowed kubectl rejection (operator #208).
    """
    labels = {"paas-managed": "true"}
    if settings.paas_env:
        validate_label_value("paas.woowtech.io/env", settings.paas_env)
        labels["paas.woowtech.io/env"] = settings.paas_env
    if workspace_id is not None:
        value = str(int(workspace_id))
        validate_label_value(WORKSPACE_ID_LABEL, value)
        labels[WORKSPACE_ID_LABEL] = value
    for wid in reach_workspace_ids or ():
        labels[reach_ws_label_key(wid)] = REACH_WS_LABEL_VALUE
    if owner is not None:
        # owner 須為合法 label value（int-as-str 或 slug，無冒號）
        validate_label_value(OWNER_LABEL, owner)
        labels[OWNER_LABEL] = owner
    return labels


def _env_match_labels() -> Dict[str, str]:
    """#279 NP-selector env qualifier — byte-identical to the env label that
    build_managed_ns_labels() stamps. Empty when PAAS_ENV is unset (single-env dev, no
    collision); on the shared prod/stg cluster it AND-composes into every #279 selector
    so a per-Odoo-DB workspace id can't match across environments (F6 cross-env leak).
    """
    return {"paas.woowtech.io/env": settings.paas_env} if settings.paas_env else {}


def _cloudflared_namespace() -> str:
    """The dedicated namespace hosting THIS env's Cloudflare Tunnel connector, whose
    ingress the CS-ns baseline NP allows so the user-facing CF web path survives the
    ns-wide default-deny. Explicit CLOUDFLARED_NAMESPACE wins; otherwise the grounded
    per-env default (woow-k3s: prod=``paas-cloudflare-system``,
    stg=``paas-stg-cloudflare-system``; each is a dedicated, hostNetwork=False ns, so a
    bare namespaceSelector on it is safe and env-safe by construction). Empty on
    dev/unknown => no cloudflared allow is rendered (no connector exists there).
    """
    if settings.cloudflared_namespace:
        return settings.cloudflared_namespace
    if settings.paas_env == "stg":
        return "paas-stg-cloudflare-system"
    if settings.paas_env == "prod":
        return "paas-cloudflare-system"
    return ""


# 每個 namespace 都有的內建 ServiceAccount。不設定 serviceAccountName 的 pod 一律
# 跑在它上面——包含 CDI importer 這種由 controller 生成、不屬於任何 chart podSpec
# 的 pod（見 KubernetesService.ensure_default_sa_image_pull_secret）。
_DEFAULT_SERVICE_ACCOUNT = "default"

# ns 剛建出來時 default SA 由 ServiceAccount controller 非同步補上，中間有一小段
# 空窗。實測通常 <1s；5 次 × 0.5s 的上限（最多 2s 等待）足以吸收，又不會把 ns
# 建立卡住太久。
_SA_LOOKUP_ATTEMPTS = 5
_SA_LOOKUP_BACKOFF_SECONDS = 0.5


def _dry_run_manifest(release_data: Dict[str, Any]) -> str:
    """``helm … --dry-run --output json`` 的 release → manifest 字串（含 hooks 的 manifest）。

    #1415：hook 的物件（例 pre-install Job 帶的 PVC）不在 ``manifest`` 裡，另放在
    ``hooks[].manifest``；容量預檢要看到它們，所以接在後面。
    """
    parts = [release_data.get("manifest") or ""]
    for hook in release_data.get("hooks") or []:
        if isinstance(hook, dict) and hook.get("manifest"):
            parts.append(hook["manifest"])
    return "\n---\n".join(p for p in parts if p)


# Opt-in label a chart puts on a StatefulSet whose pods consume the admin credential
# at start, so the helm_secret reset rolls it too (hermes-agent >= 0.3.0;
# headscale/tailscale >= 0.1.1, which have no Deployment at all).
CREDENTIAL_RESET_RESTART_LABEL = "paas.woowtech.io/restart-on-credential-reset"


class HelmException(Exception):
    """Exception raised when Helm command fails."""

    def __init__(self, message: str, command: str, stderr: str):
        self.message = message
        self.command = command
        self.stderr = stderr
        super().__init__(self.message)


class KubectlException(Exception):
    """Exception raised when kubectl command fails.

    #1263 的兩個 additive 屬性，讓呼叫端不必 parse ``message`` 字串：

    - ``timed_out``：本地 subprocess timeout 時為 True。timeout 不證明遠端 exec 已停止，
      也就沒有可信的輸出 ⇒ 呼叫端一律歸成「結果未知」。
    - ``stdout``：非 0 結束時命令已印出的 stdout。``kubectl exec`` 沿用容器內的
      exit code，腳本「先印 JSON 回報、再 exit 1」時，失敗的明確回報就在這裡。
    """

    def __init__(
        self, message: str, command: str, stderr: str,
        timed_out: bool = False, stdout: str = "",
    ):
        self.message = message
        self.command = command
        self.stderr = stderr
        self.timed_out = timed_out
        self.stdout = stdout
        super().__init__(self.message)


class HelmService:
    """Service for executing Helm CLI operations."""

    def __init__(self):
        self.helm_bin = settings.helm_binary
        self.timeout = settings.helm_timeout

    def _run_command(
        self,
        args: List[str],
        input_data: Optional[str] = None,
        capture_output: bool = True,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """Execute a Helm command.

        Args:
            args: Command arguments (without 'helm' prefix)
            input_data: Optional stdin input
            capture_output: Whether to capture stdout/stderr
            timeout: Per-call subprocess timeout override (seconds). ``None``
                keeps the service-wide ``settings.helm_timeout``.

        Returns:
            CompletedProcess object

        Raises:
            HelmException: If command fails
        """
        effective_timeout = timeout if timeout is not None else self.timeout
        cmd = [self.helm_bin] + args
        # issue #29: redact the repo token (--password) before logging / before it
        # lands in HelmException.command. NEVER log/raise the raw cmd.
        safe_cmd = " ".join(_redact_cmd(cmd))
        logger.info(f"Executing: {safe_cmd}")

        try:
            result = subprocess.run(
                cmd,
                # text=True → subprocess expects a *str* and encodes it itself;
                # do NOT pre-encode (a bytes input would hit `bytes.encode` in
                # subprocess._save_input). Dormant until the first input_data
                # caller (_ensure_oci_login's `helm registry login --password-stdin`).
                input=input_data if input_data else None,
                capture_output=capture_output,
                text=True,
                timeout=effective_timeout,
                check=False,
            )

            if result.returncode != 0:
                raise HelmException(
                    message=f"Helm command failed with code {result.returncode}",
                    command=safe_cmd,
                    stderr=result.stderr,
                )

            return result

        except subprocess.TimeoutExpired:
            # NOT str(e): TimeoutExpired stringifies to "Command '<raw cmd list>'
            # timed out after Ns", which puts the very password safe_cmd just
            # masked back into api/releases.py's `Stderr: {e.stderr}` log line.
            # helm --atomic timing out is a routine path (the rollback waits a
            # second time), so this is the leak's most-travelled route.
            raise HelmException(
                message=f"Helm command timed out after {effective_timeout}s",
                command=safe_cmd,
                stderr=f"Command '{safe_cmd}' timed out after {effective_timeout} seconds",
            )
        except FileNotFoundError:
            raise HelmException(
                message=f"Helm binary not found: {self.helm_bin}",
                command=safe_cmd,
                stderr="",
            )

    def _ensure_oci_login(self, chart: str) -> None:
        """Authenticate to a private OCI registry before pulling an ``oci://`` chart.

        GA blocker found in stg layer-3 e2e (2026-06-30): the operator's helm
        pulled ``oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server``
        anonymously → ``401 Unauthorized``. OCI auth lives in helm's *registry
        config* (``helm registry login``); it is **not** the classic-repo
        ``--username/--password`` install flag — those are silently ignored for
        ``oci://`` refs. We reuse the same registry credentials the operator
        already uses for the tenant imagePullSecret (``settings.registry_*``).

        No-op when the chart is not an ``oci://`` ref, when no registry
        credentials are configured, or when the chart lives on a host that is
        NOT the operator's own registry (public OCI charts like bitnami — and
        third-party ones like ``oci://8gears.container-registry.com/library/n8n``
        — stay anonymous). Password is passed via ``--password-stdin`` so it
        never lands in argv / logs.

        The ``host == settings.registry_server`` guard is load-bearing: the JCR
        creds only authenticate against JCR. Logging into a *third-party* public
        registry with them (stg live e2e 2026-07-01) makes the otherwise-anonymous
        pull fail ``401 unauthorized``, so the n8n full-app service could never
        deploy. Only authenticate to our own registry; everyone else stays
        anonymous.
        """
        if not chart.startswith("oci://"):
            return
        if not (settings.registry_username and settings.registry_password):
            return
        host = chart[len("oci://"):].split("/", 1)[0]
        # Only log into the operator's OWN registry — never a third-party host.
        if host != settings.registry_server:
            return
        self._run_command(
            [
                "registry",
                "login",
                host,
                "--username",
                settings.registry_username,
                "--password-stdin",
            ],
            input_data=settings.registry_password,
        )

    def install(
        self,
        namespace: str,
        name: str,
        chart: str,
        values: Optional[Dict[str, Any]] = None,
        sensitive_values: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        create_namespace: bool = False,
        repo: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        dry_run: bool = False,
        subprocess_timeout_seconds: Optional[int] = None,
    ) -> ReleaseInfo:
        """Install a Helm chart.

        ``dry_run=True`` (#1415 capacity precheck) runs the exact same command with
        ``--dry-run`` — same chart, version, values and post-renderer, nothing applied —
        and returns the rendered manifest **string** (hook manifests appended) instead of a
        ``ReleaseInfo``. Mirrors ``upgrade(dry_run=True)`` (#1407). The manifest can hold
        rendered Secrets: callers must never log it.

        Args:
            namespace: Target namespace
            name: Release name
            chart: Chart reference
            values: Values override
            sensitive_values: Sensitive values merged in memory only
            version: Chart version
            create_namespace: Create namespace if not exists
            repo: Optional classic Helm repo URL for stateless fetch (--repo).
                Leave None for direct/OCI chart refs (marketplace default).
            username: Optional repo username for a private Helm repo (--username).
            password: Optional repo password/token for a private repo (--password).
            dry_run: Render only (``--dry-run``); returns the manifest string.
            subprocess_timeout_seconds: Per-call subprocess ceiling. ``None`` keeps
                ``settings.helm_timeout``.

        Returns:
            Release information

        Raises:
            ValueError: If namespace is invalid
            HelmException: If installation fails
        """
        validate_namespace(namespace)

        # #1552：整個 install（registry login、讀 live 物件、helm 本身）佔一個 helm 名額。
        with helm_operation_slot("install --dry-run" if dry_run else "install"):
            return self._install_locked(
                namespace, name, chart, values=values, sensitive_values=sensitive_values,
                version=version, create_namespace=create_namespace, repo=repo,
                username=username, password=password, dry_run=dry_run,
                subprocess_timeout_seconds=subprocess_timeout_seconds,
            )

    def _install_locked(
        self,
        namespace: str,
        name: str,
        chart: str,
        values: Optional[Dict[str, Any]] = None,
        sensitive_values: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        create_namespace: bool = False,
        repo: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        dry_run: bool = False,
        subprocess_timeout_seconds: Optional[int] = None,
    ) -> ReleaseInfo:
        """``install`` 的本體；呼叫端已持有 helm 名額（#1552）。"""
        # Private OCI charts need `helm registry login` before pull (GA blocker
        # fix): OCI auth != classic-repo --username/--password. No-op for
        # non-OCI refs or public OCI (no creds).
        self._ensure_oci_login(chart)

        args = [
            "install",
            name,
            chart,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]
        # #317: PVC-keep post-renderer — before ALL conditional flags.
        # #1407: a retried install can meet PVCs a failed attempt left behind
        # (keep-annotated, same names) — reuse their live immutable fields too.
        tenant, tenant_live = _tenant_policy(namespace)
        args.extend(_pvc_keep_post_renderer_args(_live_snapshot(namespace), tenant, tenant_live))
        args.extend(_openapi_validation_args())

        if create_namespace and not dry_run:
            args.append("--create-namespace")
        if dry_run:
            args.append("--dry-run")

        if version:
            args.extend(["--version", version])

        # Classic Helm repo (stateless): resolve the chart directly from a repo
        # URL without a persistent `helm repo add` — used by the agent-runtime
        # path (private Gitea classic Helm repo over in-cluster HTTP). Unset for
        # the existing marketplace OCI refs, so their behaviour is unchanged.
        if repo:
            args.extend(["--repo", repo])
        if username:
            args.extend(["--username", username])
        if password:
            args.extend(["--password", password])

        merged_values = values or {}
        if sensitive_values:
            merged_values = _deep_merge_dicts(merged_values, sensitive_values)

        # Write values to temp file if provided
        if merged_values:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                import yaml

                yaml.dump(merged_values, f)
                args.extend(["--values", f.name])
                temp_file = f.name

        try:
            result = self._run_command(args, timeout=subprocess_timeout_seconds)
            release_data = json.loads(result.stdout)
            if dry_run:
                return _dry_run_manifest(release_data)
            return self._parse_release_info(release_data)
        finally:
            if merged_values:
                Path(temp_file).unlink(missing_ok=True)

    def get(self, namespace: str, name: str) -> ReleaseInfo:
        """Get information about a release.

        Args:
            namespace: Release namespace
            name: Release name

        Returns:
            Release information
        """
        validate_namespace(namespace)

        # Use 'helm list --filter' which provides all needed info in one call
        args = [
            "list",
            "--namespace",
            namespace,
            "--filter",
            f"^{name}$",
            "--output",
            "json",
        ]

        result = self._run_command(args)
        releases = json.loads(result.stdout)

        if not releases:
            raise HelmException(
                message=f"Release {name} not found",
                command=" ".join(args),
                stderr=f"Release {name} not found in namespace {namespace}",
            )

        return self._parse_list_release_info(releases[0])

    def upgrade(
        self,
        namespace: str,
        name: str,
        chart: Optional[str] = None,
        values: Optional[Dict[str, Any]] = None,
        sensitive_values: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        reset_values: bool = False,
        reuse_values: bool = True,
        repo: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        atomic: bool = False,
        timeout_seconds: Optional[int] = None,
        subprocess_timeout_seconds: Optional[int] = None,
        dry_run: bool = False,
        description: Optional[str] = None,
        tenant_policy_mirror: bool = False,
    ) -> ReleaseInfo:
        """Upgrade a Helm release.

        ``dry_run=True`` (#1407 preflight) runs the exact same command with
        ``--dry-run`` — same values, same post-renderer, nothing applied — and returns
        the rendered manifest **string** instead of a ``ReleaseInfo``.

        Args:
            namespace: Release namespace
            name: Release name
            chart: Chart reference (full URL like oci://... or repo/chart)
            values: Values override
            sensitive_values: Sensitive values merged in memory only
            version: Chart version
            reset_values: Reset to chart default values
            reuse_values: Reuse last release values
            repo: Optional classic Helm repo URL for stateless fetch (--repo).
            username: Optional repo username for a private Helm repo (--username).
            password: Optional repo password/token for a private repo (--password).
            timeout_seconds: Renders ``--timeout {n}s`` — bounds how long helm waits
                for *one* Kubernetes operation. ``None`` omits the flag entirely
                (helm's own 5m default) ＝ pre-#461 behaviour.
            subprocess_timeout_seconds: Per-call subprocess ceiling. ``None`` keeps
                ``settings.helm_timeout``.
            tenant_policy_mirror: AI-FIX-NET-1 — render the tenant egress-gate / node-avoidance
                policy only where the live workload already has it (reset-config and other
                pod-template-neutral paths); the default adopts it, which restarts pods once.

        Returns:
            Updated release information

        Raises:
            ValueError: If chart is not provided (Helm doesn't store original chart URL)

        Note:
            #461 — a caller passing both MUST size the subprocess ceiling for the
            **failure** path, not the success path. ``--timeout`` is *per
            operation*: when ``--atomic`` fails, helm runs a second waiting
            operation (its automatic rollback), so the worst case is
            ``2 * timeout_seconds + HELM_CLI_OVERHEAD_SECONDS``. A ceiling sized
            ``timeout_seconds + slack`` SIGKILLs helm **mid-rollback**, leaving the
            release ``pending-*`` with an undefined Secret — i.e. it breaks the very
            invariant atomic exists to provide. That arithmetic is deliberately NOT
            done here: it lives with the caller that owns the budget ladder
            (``api/releases.py`` RESET_* constants) so one authority is readable by
            both the unit test and the repo-lint gate.
        """
        validate_namespace(namespace)

        # Chart is required for upgrade - Helm doesn't store the original chart URL
        if not chart:
            raise ValueError(
                "Chart reference is required for upgrade. "
                "Helm does not store the original chart URL. "
                "Please provide the full chart reference (e.g., oci://registry-1.docker.io/bitnamicharts/postgresql)"
            )

        # #1552：整個 upgrade 佔一個 helm 名額（端點已持有時可重入，不重複排隊）。
        with helm_operation_slot("upgrade --dry-run" if dry_run else "upgrade"):
            return self._upgrade_locked(
                namespace, name, chart, values=values, sensitive_values=sensitive_values,
                version=version, reset_values=reset_values, reuse_values=reuse_values,
                repo=repo, username=username, password=password, atomic=atomic,
                timeout_seconds=timeout_seconds,
                subprocess_timeout_seconds=subprocess_timeout_seconds,
                dry_run=dry_run, description=description,
                tenant_policy_mirror=tenant_policy_mirror,
            )

    def _upgrade_locked(
        self,
        namespace: str,
        name: str,
        chart: str,
        values: Optional[Dict[str, Any]] = None,
        sensitive_values: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        reset_values: bool = False,
        reuse_values: bool = True,
        repo: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        atomic: bool = False,
        timeout_seconds: Optional[int] = None,
        subprocess_timeout_seconds: Optional[int] = None,
        dry_run: bool = False,
        description: Optional[str] = None,
        tenant_policy_mirror: bool = False,
    ) -> ReleaseInfo:
        """``upgrade`` 的本體；呼叫端已持有 helm 名額（#1552）。"""
        # Private OCI charts need `helm registry login` before pull (GA blocker
        # fix) — symmetric with install(), covers retry/upgrade paths.
        self._ensure_oci_login(chart)

        args = [
            "upgrade",
            name,
            chart,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]
        # #317: PVC-keep post-renderer — MUST match install. Placed before ALL
        # conditional flags (incl. both the --reset-values and --reuse-values
        # branches) so every upgrade variant rewrites the stored manifest WITH
        # keep; otherwise the next uninstall would delete the PVC.
        # #1407: existing PVCs/StatefulSets render with their live immutable fields.
        # AI-FIX-NET-1: egress gate / node avoidance (off unless the operator switches are on).
        tenant, tenant_live = _tenant_policy(namespace, mirror_live=tenant_policy_mirror)
        args.extend(_pvc_keep_post_renderer_args(_live_snapshot(namespace), tenant, tenant_live))
        args.extend(_openapi_validation_args())

        if version:
            args.extend(["--version", version])

        if reset_values:
            args.append("--reset-values")
        elif reuse_values:
            args.append("--reuse-values")
        if atomic:
            args.append("--atomic")
        if dry_run:
            args.append("--dry-run")
        # #461: omitted when None so every pre-existing caller keeps helm's 5m
        # default verbatim (behaviour-neutral); only the reset path bounds it.
        if timeout_seconds is not None:
            args.extend(["--timeout", f"{timeout_seconds}s"])
        # #1433: the preserving rollback is an upgrade; without a description the
        # revision list would read "Upgrade complete" and the tenant could not tell
        # it was a rollback.
        if description:
            args.extend(["--description", description])

        # Classic Helm repo (stateless) — mirrors install(); see its note.
        if repo:
            args.extend(["--repo", repo])
        if username:
            args.extend(["--username", username])
        if password:
            args.extend(["--password", password])

        merged_values = values or {}
        if sensitive_values:
            merged_values = _deep_merge_dicts(merged_values, sensitive_values)

        if merged_values:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                import yaml

                yaml.dump(merged_values, f)
                args.extend(["--values", f.name])
                temp_file = f.name

        try:
            result = self._run_command(args, timeout=subprocess_timeout_seconds)
            release_data = json.loads(result.stdout)
            if dry_run:
                return release_data.get("manifest") or ""
            return self._parse_release_info(release_data)
        finally:
            if merged_values:
                Path(temp_file).unlink(missing_ok=True)

    def uninstall(self, namespace: str, name: str) -> Dict[str, str]:
        """Uninstall a Helm release.

        Args:
            namespace: Release namespace
            name: Release name

        Returns:
            Uninstall result message
        """
        validate_namespace(namespace)

        args = [
            "uninstall",
            name,
            "--namespace",
            namespace,
        ]

        with helm_operation_slot("uninstall"):  # #1552
            result = self._run_command(args)
        return {"message": result.stdout.strip()}

    def rollback(
        self, namespace: str, name: str, revision: Optional[int] = None
    ) -> Dict[str, str]:
        """Rollback a Helm release.

        Args:
            namespace: Release namespace
            name: Release name
            revision: Target revision number. If not provided, rolls back to the
                previous revision.

        Returns:
            Rollback result message
        """
        validate_namespace(namespace)

        args = [
            "rollback",
            name,
            "--namespace",
            namespace,
        ]

        if revision is not None:
            args.append(str(revision))

        with helm_operation_slot("rollback"):  # #1552
            result = self._run_command(args)
        return {"message": result.stdout.strip()}

    def get_config_projection(self, namespace, name, revision, paths, excluded_paths=()):
        """固定 revision + chart computed defaults；raw values 不離開此方法。"""
        from src.services.config_projection import project_values, validate_paths
        validate_namespace(namespace)
        validate_paths(paths, excluded_paths)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError('configuration revision must be positive')
        result = self._run_command([
            'get', 'values', name, '--namespace', namespace, '--all',
            '--revision', str(revision), '--output', 'json',
        ])
        values = json.loads(result.stdout or 'null')
        if values is not None and not isinstance(values, dict):
            raise ValueError('configuration values must be an object')
        return project_values(values or {}, paths, excluded_paths)

    def get_values(
        self, namespace: str, name: str, revision: Optional[int] = None
    ) -> Dict[str, Any]:
        """#1433: user-supplied values of a release revision (``helm get values``).

        Returns the **raw** values — including ``config.sensitive.*``. Callers must
        never log or return them; the preserving rollback only feeds them back into
        ``helm upgrade --values <tempfile>``. ``revision=None`` ＝ the current one.
        """
        validate_namespace(namespace)
        args = ["get", "values", name, "--namespace", namespace, "--output", "json"]
        if revision is not None:
            args.extend(["--revision", str(int(revision))])
        result = self._run_command(args)
        data = json.loads(result.stdout or "null")
        # helm prints `null` for a revision installed without any values.
        return data if isinstance(data, dict) else {}

    def get_manifest(self, namespace: str, name: str, revision: int) -> str:
        """#1685: stored manifest of a revision (``helm get manifest --revision``).

        This is what a plain ``helm rollback`` to that revision re-applies (hooks excluded),
        so the compute quota pre-check can judge it without a dry-run.

        ⛔ The manifest may contain Secret objects in plain text: callers must never log or
        return it. ``_run_command`` itself only logs the command line and, on failure, stderr.
        """
        validate_namespace(namespace)
        args = [
            "get", "manifest", name,
            "--namespace", namespace,
            "--revision", str(int(revision)),
        ]
        return self._run_command(args).stdout or ""

    def get_revision_chart_version(
        self, namespace: str, name: str, revision: int
    ) -> Optional[str]:
        """#1433: the chart version a revision was rendered from.

        Read from the release object itself (``Chart.Metadata.Version``) instead of
        splitting ``helm history``'s ``<name>-<version>`` string, which is ambiguous
        for chart names or prerelease versions containing dashes.
        """
        validate_namespace(namespace)
        args = [
            "get", "all", name,
            "--namespace", namespace,
            "--revision", str(int(revision)),
            "--template", "{{.Release.Chart.Metadata.Version}}",
        ]
        result = self._run_command(args)
        version = (result.stdout or "").strip()
        return version or None

    def history(self, namespace: str, name: str) -> List[ReleaseRevision]:
        """Get release revision history.

        Args:
            namespace: Release namespace
            name: Release name

        Returns:
            List of revisions
        """
        validate_namespace(namespace)

        args = [
            "history",
            name,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        result = self._run_command(args)
        revisions_data = json.loads(result.stdout)

        return [
            ReleaseRevision(
                revision=rev["revision"],
                updated=rev["updated"],
                status=ReleaseStatus(rev["status"].lower()),
                chart=rev["chart"],
                app_version=rev.get("app_version", ""),
                description=rev.get("description", ""),
            )
            for rev in revisions_data
        ]

    def get_version(self) -> str:
        """Get Helm version.

        Returns:
            Helm version string
        """
        args = ["version", "--short"]
        result = self._run_command(args)
        return result.stdout.strip()

    def _parse_release_info(self, data: Dict[str, Any]) -> ReleaseInfo:
        """Parse Helm install/upgrade JSON output to ReleaseInfo.

        Args:
            data: Helm JSON output from install/upgrade

        Returns:
            ReleaseInfo object
        """
        info = data.get("info", {})
        chart_metadata = data.get("chart", {}).get("metadata", {})

        return ReleaseInfo(
            name=data.get("name", ""),
            namespace=data.get("namespace", ""),
            revision=info.get("revision", 0),
            status=ReleaseStatus(info.get("status", "unknown").lower()),
            chart=chart_metadata.get("name", ""),
            # #552: the resolved chart version sits right next to the name in the
            # same metadata block and was simply never read. Without it the
            # platform ledger can never learn which version an empty `--version`
            # (⇒ registry latest) actually produced.
            chart_version=chart_metadata.get("version") or None,
            app_version=chart_metadata.get("appVersion", ""),
            updated=info.get("last_deployed", ""),
            description=info.get("description"),
            values=_sanitize_release_values(data.get("config")),
        )

    def _parse_list_release_info(self, data: Dict[str, Any]) -> ReleaseInfo:
        """Parse Helm list JSON output to ReleaseInfo.

        Args:
            data: Single release from helm list output

        Returns:
            ReleaseInfo object
        """
        # helm list output: name, namespace, revision, updated, status, chart, app_version
        # `chart` is "<name>-<version>" (e.g. "postgresql-18.2.3").
        #
        # #552: the version half used to be thrown away here — the old comment
        # literally read "Extract chart name without version". Deriving BOTH
        # halves from the same single rsplit is deliberate: two separate splits
        # could disagree about where the boundary is, and this is the value the
        # platform ledger records as "what is actually deployed".
        chart_with_version = data.get("chart", "")
        if "-" in chart_with_version:
            chart_name, chart_version = chart_with_version.rsplit("-", 1)
        else:
            chart_name, chart_version = chart_with_version, None

        return ReleaseInfo(
            name=data.get("name", ""),
            namespace=data.get("namespace", ""),
            revision=int(data.get("revision", 0)),
            status=ReleaseStatus(data.get("status", "unknown").lower()),
            chart=chart_name,
            chart_version=chart_version or None,
            app_version=data.get("app_version", ""),
            updated=data.get("updated", ""),
            description=None,
            values=None,
        )


# F-GC-1 (#955): key-name markers that make an env var too sensitive for
# `kubectl set env` (argv + literal pod-spec exposure — see set_deployment_env).
# Deliberately NOT a bare "KEY": that matches innocuous names like SORT_KEY and a
# guard that cries wolf gets deleted.
_SENSITIVE_ENV_KEY_MARKERS = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API_KEY", "APIKEY",
    "PRIVATE_KEY",
)


def _sensitive_env_key_marker(key: str) -> Optional[str]:
    """Return the marker that makes ``key`` sensitive, or None."""
    upper = key.upper()
    for marker in _SENSITIVE_ENV_KEY_MARKERS:
        if marker in upper:
            return marker
    return None


def _controller_owner_kind(metadata: Dict[str, Any]) -> Optional[str]:
    """Pod 直接 owner（``controller: true`` 那筆 ownerReference）的 kind（#1263）。

    只認 controller=true：被收養或附掛的非 controller owner 不是建立它的 workload。
    沒有 controller owner（bare Pod、缺旗標）回 None，不猜。
    """
    for ref in metadata.get("ownerReferences") or []:
        if isinstance(ref, dict) and ref.get("controller") is True:
            return ref.get("kind")
    return None


# #1417：這些 waiting reason 只說「還沒輪到」（前一個容器／volume 還沒好），本身不是
# 原因。有其他容器帶著實質 reason（ImagePullBackOff、CrashLoopBackOff…）時優先回報那個。
_GENERIC_WAITING_REASONS = frozenset({"PodInitializing", "ContainerCreating"})


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    """診斷文字截短：事件訊息可能很長（Longhorn 的 attach 錯誤一行數百字）。"""
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pod_waiting(status: Dict[str, Any], limit: int) -> Optional[ContainerWaitingInfo]:
    """#1417：Pod 卡住的容器（``state.waiting``）；先看 init 容器再看主要容器。

    回第一個帶實質 reason 的容器；都只有「還沒輪到」的 reason 時回第一個；沒有任何容器
    在 waiting 回 None。
    """
    first_generic = None
    for is_init, key in ((True, "initContainerStatuses"), (False, "containerStatuses")):
        for cs in status.get(key) or []:
            waiting = (cs.get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason")
            if not reason:
                continue
            info = ContainerWaitingInfo(
                container=cs.get("name") or "",
                init_container=is_init,
                reason=reason,
                message=_truncate(waiting.get("message"), limit),
            )
            if reason not in _GENERIC_WAITING_REASONS:
                return info
            if first_generic is None:
                first_generic = info
    return first_generic


def _replica_failure(status: Dict[str, Any], limit: int) -> Optional[WorkloadReplicaFailure]:
    """#1647：workload 的 ``ReplicaFailure=True`` condition；沒有回 None。

    Deployment controller 在 ReplicaSet 建 Pod 被 API server 拒絕（quota、admission…）時
    設這個 condition，``message`` 就是拒絕原文（``… is forbidden: exceeded quota: …``）。
    """
    for condition in status.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        if condition.get("type") == "ReplicaFailure" and condition.get("status") == "True":
            return WorkloadReplicaFailure(
                reason=condition.get("reason") or "",
                message=_truncate(condition.get("message"), limit),
                last_transition=condition.get("lastTransitionTime"),
            )
    return None


def _pod_is_unready(phase: str, metadata: Dict[str, Any], ready_count: int, total: int) -> bool:
    """與平台 ``_is_container_ready`` 同一套「有效 Pod」定義：收尾中的 Pod 不算。"""
    if phase in ("Succeeded", "Failed") or metadata.get("deletionTimestamp"):
        return False
    return phase != "Running" or ready_count < total


def _event_time(event: Dict[str, Any]) -> str:
    """事件「最後一次發生」的時間字串（RFC 3339）；排序用，取不到回空字串。

    core/v1 Event 依產生者不同，時間落在不同欄位：舊式 ``lastTimestamp``、新式
    ``series.lastObservedTime``／``eventTime``，最後退回 ``metadata.creationTimestamp``。
    """
    series = event.get("series") or {}
    return (
        series.get("lastObservedTime")
        or event.get("lastTimestamp")
        or event.get("eventTime")
        or (event.get("metadata") or {}).get("creationTimestamp")
        or ""
    )


def _event_sort_key(event: Dict[str, Any]) -> str:
    """時間字串正規化成可比較的形狀（去掉小數秒，``Z`` 對齊）。"""
    stamp = _event_time(event)
    return re.sub(r"\.\d+", "", stamp).replace("+00:00", "Z")


class KubernetesService:
    """Service for Kubernetes operations (via kubectl)."""

    def __init__(self):
        self.kubectl_bin = "kubectl"
        # MINOR-4: per-process CDI-CRD probe cache (see has_datavolume_crd).
        self._has_dv_crd: Optional[bool] = None

    def _run_command(
        self,
        args: List[str],
        input_data: Optional[str] = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess:
        """Execute a kubectl command.

        Args:
            args: Command arguments (without 'kubectl' prefix)

        Returns:
            CompletedProcess object

        Raises:
            KubectlException: If command fails or times out
        """
        cmd = [self.kubectl_bin] + args
        # issue #29 audit: `kubectl set env … SKILL_BUNDLE_TOKEN=<tok>` (resume)
        # passes a secret on argv; redact before logging / raising.
        safe_cmd = " ".join(_redact_cmd(cmd))
        logger.info(f"Executing: {safe_cmd}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                input=input_data,
                timeout=timeout,
                check=False,
            )

            if result.returncode != 0:
                raise KubectlException(
                    message=f"kubectl command failed with code {result.returncode}",
                    command=safe_cmd,
                    stderr=result.stderr,
                    stdout=result.stdout or "",
                )

            return result

        except subprocess.TimeoutExpired:
            # See the helm twin above. This one's raw cmd is the `node -e` script
            # holding the tenant's n8n owner password, and the text travels
            # further than a log line: n8n_init wraps e.stderr into N8nInitError,
            # which api/init.py returns as N8nInitResponse.error — an HTTP body.
            raise KubectlException(
                message=f"kubectl command timed out after {timeout}s",
                command=safe_cmd,
                stderr=f"Command '{safe_cmd}' timed out after {timeout} seconds",
                timed_out=True,
            )
        except FileNotFoundError:
            raise KubectlException(
                message=f"kubectl binary not found: {self.kubectl_bin}",
                command=safe_cmd,
                stderr="",
            )

    # ── 對外位址回讀（ngrok sidecar；charts/emqx 0.1.0 起、charts/headscale）──
    #
    # chart 的 ngrok sidecar 把當前的公開位址每 60 秒印一次到自己的 stdout，
    # 一行一筆：`WOOW_PUBLIC_URL=<url>`（通用 marker）或 `WOOW_MQTT_PUBLIC_URL=<url>`
    # （EMQX 0.1.x 的舊 marker，繼續認）。`<url>` 是
    #   * `tcp://host:port` —— ngrok TCP endpoint（EMQX 原生 MQTT）；
    #   * `https://host[:port]` —— ngrok HTTP endpoint（Headscale 控制網址）。
    # 值是空的（`WOOW_PUBLIC_URL=`）＝撤回：agent 退出、沒填 token、連線失敗。
    #
    # 平台從 container log 讀它，而不是
    #   * 開一個 :4040 的 Service —— agent 的本機 API 能**建立**隧道，開成 Service
    #     等於讓同一個租戶 namespace 內的其他服務操作它；
    #   * 讓 sidecar 寫 ConfigMap —— 那需要掛 SA token ＋ RBAC（chart 目前
    #     automountToken=false）。
    # log 這條路零 RBAC、零額外埠、零額外 image，而「每 60 秒重印」正是讓
    # log rotation 永遠轉不掉唯一一筆位址的原因。
    #
    # 刻意**不**回傳任何原始 log 內容：只回傳比對成功的那個位址。容器 log 是
    # 第三方（ngrok agent）的輸出，整段轉出去等於把一個不受控的字串管道接到
    # Odoo 的 HTTP 回應上。
    #
    # marker 必須在**行首**（sidecar 是 `echo "WOOW_PUBLIC_URL=${url}"`）。同一個
    # stdout 也印 ngrok agent 自己的 log（`--log stdout`），而 HTTP endpoint 的那幾行
    # 可能帶外部訪客可控的字串（請求路徑之類）；marker 若能出現在行中間，訪客打一個
    # `/WOOW_PUBLIC_URL=https://evil.example` 就可能讓平台把攻擊者的網址顯示成
    # 「你的 Headscale 控制網址」，租戶照著設定裝置＝把整個 tailnet 交出去。
    NGROK_CONTAINER_NAME = "ngrok"
    # 順序無關：同一份 log 裡兩種 marker 一視同仁，取**最後一筆**。
    NGROK_ADDRESS_MARKERS = ("WOOW_PUBLIC_URL=", "WOOW_MQTT_PUBLIC_URL=")
    NGROK_LOG_TAIL_LINES = 200
    NGROK_LOG_TIMEOUT_SECONDS = 15
    # 對外位址的形狀。fail-closed：對不上就當沒有，不把來源字串轉出去。
    # 主機名逐段驗（RFC 1123 label、至少一個點、總長 ≤ 253），只收 ASCII；不收
    # userinfo（`user@`）、路徑、query、fragment、結尾的 `/` 或 `.`。`https` 的埠
    # 可省略（ngrok HTTP endpoint 的 public_url 本來就不帶埠），`tcp` 必須有埠。
    _HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    _PUBLIC_ADDRESS_RE = re.compile(
        r"(?P<scheme>tcp|https)://"
        r"(?P<host>%s(?:\.%s)+)"
        r"(?::(?P<port>[0-9]{1,5}))?" % (_HOST_LABEL, _HOST_LABEL)
    )
    PUBLIC_ADDRESS_SCHEMES = ("tcp", "https")

    @classmethod
    def _parse_public_address(cls, candidate: str, schemes) -> Optional[str]:
        """``candidate`` 是合法的對外位址就原樣回傳，否則 None（fail-closed）。"""
        match = cls._PUBLIC_ADDRESS_RE.fullmatch(candidate)
        if not match or match.group("scheme") not in schemes:
            return None
        if len(match.group("host")) > 253:
            return None
        port = match.group("port")
        if port is None:
            # TCP endpoint 沒有埠就連不上——那不是一個位址。
            return None if match.group("scheme") == "tcp" else candidate
        return candidate if 0 < int(port) < 65536 else None

    @classmethod
    def _marker_value(cls, line: str) -> Optional[str]:
        """行首是 marker 就回傳 marker 後面的值（去掉 ASCII 空白；空字串＝撤回），否則 None。

        行首就是第 0 個字元：前面有任何東西（含空白）都不算。只去 ASCII 的空白與
        ``\r``——``str.strip()`` 會連 U+2028／U+0085 之類一起吃掉，等於讓行中間的
        Unicode 換行字元重新造出一個「行首」。
        """
        line = line.rstrip("\r")
        for marker in cls.NGROK_ADDRESS_MARKERS:
            if line.startswith(marker):
                return line[len(marker):].strip(" \t")
        return None

    # #1559：agent 連不上時，原因只在 sidecar log（例：authtoken 錯 ⇒ ERR_NGROK_105），服務頁
    # 只能寫「尚未取得」。這裡讀回**原因碼**——同樣不回任何原始 log 內容（ngrok 在認證失敗時
    # 會把 authtoken 印回來，EMQX 的 sidecar 沒有過濾），只回封閉集合裡的代碼：
    #   * ``ERR_NGROK_<n>``：只認 ngrok 自己的錯誤區塊那一行（整行就是
    #     ``ERROR:  ERR_NGROK_<n>``）。ngrok 的一般 log 是 logfmt（行首 ``t=``），訪客可控的
    #     字串進不了「整行完全等於」這個形狀。
    #   * ``NO_AUTHTOKEN``：chart 的 sidecar 在「開了 ngrok 卻沒填 token」時印的固定句（行首）。
    #   * ``SERVER_URL_MISMATCH``：Headscale 的 sidecar：ngrok 已連上，但 Headscale 啟動時用的
    #     server_url 不是它（啟動時 ngrok 還沒連上）⇒ 要重新啟動服務。
    # 只有「最後一次宣告的位址是空的」時才回原因：之後又連上（印出非空位址）就清掉。
    NGROK_ERROR_LINE_RE = re.compile(r"ERROR:[ \t]+(ERR_NGROK_[0-9]{1,5})[ \t]*")
    NGROK_NO_TOKEN_PREFIXES = (
        "ngrok authtoken is not set",          # charts/emqx
        "[ngrok] 未設定 ngrok authtoken",       # charts/headscale（woow_log）
    )
    NGROK_SERVER_URL_MISMATCH_PREFIX = "[ngrok] ngrok 已連上 "  # charts/headscale ngrok.sh

    @classmethod
    def _ngrok_error_code(cls, line: str) -> Optional[str]:
        """一行 sidecar log → 原因碼（封閉集合）或 None。"""
        line = line.rstrip("\r")
        match = cls.NGROK_ERROR_LINE_RE.fullmatch(line)
        if match:
            return match.group(1)
        if line.startswith(cls.NGROK_NO_TOKEN_PREFIXES):
            return "NO_AUTHTOKEN"
        if line.startswith(cls.NGROK_SERVER_URL_MISMATCH_PREFIX) and "server_url" in line:
            return "SERVER_URL_MISMATCH"
        return None

    def get_advertised_public_address(
        self,
        namespace: str,
        release: str,
        schemes=PUBLIC_ADDRESS_SCHEMES,
    ) -> Optional[str]:
        """Read the public address the release's ngrok sidecar advertises.

        Thin wrapper over :meth:`get_public_address_status` (address only).
        """
        return self.get_public_address_status(namespace, release, schemes)[0]

    def get_public_address_status(
        self,
        namespace: str,
        release: str,
        schemes=PUBLIC_ADDRESS_SCHEMES,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Read the advertised public address and, when there is none, why (#1559).

        Args:
            namespace: Release namespace
            release: Helm release name (``app.kubernetes.io/instance``)
            schemes: Accepted URL schemes. The generic endpoint accepts both
                ``tcp`` and ``https``; the legacy ``/mqtt-address`` endpoint
                passes ``("tcp",)`` so it keeps returning exactly what it did.

        Returns:
            ``(address, error_code)``. ``address`` is the advertised
            ``tcp://host:port`` / ``https://host[:port]`` or None when there is
            nothing to report — no pod, no ngrok sidecar (the tenant left it off),
            the agent has not connected yet, the sidecar retracted the address, or
            the line did not match the expected shape. All of those are ordinary
            states, NOT errors: the caller must not treat None as a failure.
            ``error_code`` is only set when ``address`` is None and the sidecar log
            says why (closed set, see ``_ngrok_error_code``); never raw log text.
        """
        validate_namespace(namespace)

        pods = self.get_pods(
            namespace, label_selector=f"app.kubernetes.io/instance={release}"
        )
        last_error: Optional[str] = None
        for pod in pods:
            if pod.deletion_timestamp:
                continue
            try:
                result = self._run_command(
                    [
                        "logs",
                        pod.name,
                        "--namespace",
                        namespace,
                        "--container",
                        self.NGROK_CONTAINER_NAME,
                        f"--tail={self.NGROK_LOG_TAIL_LINES}",
                    ],
                    timeout=self.NGROK_LOG_TIMEOUT_SECONDS,
                )
            except KubectlException as e:
                # The usual case: this pod has no `ngrok` container because the
                # tenant has not enabled the tunnel. Also covers a pod that is
                # still initialising. Debug-level on purpose — it is not a fault.
                logger.debug(
                    "No ngrok log for %s/%s: %s", namespace, pod.name, e.message
                )
                continue

            address = None
            error = None
            # 只以 "\n" 切行（kubectl logs 的行界）。`splitlines()` 還會在 U+2028、
            # U+0085、\x1c–\x1e、\r 切開——ngrok 自己那幾行若夾帶這些字元，行中間的
            # marker 就會被切成「行首」，行首錨定等於失效。
            for line in (result.stdout or "").split("\n"):
                candidate = self._marker_value(line)
                if candidate is None:
                    code = self._ngrok_error_code(line)
                    if code:
                        error = code
                    continue
                parsed = self._parse_public_address(candidate, schemes) if candidate else None
                if parsed:
                    # Keep scanning: the sidecar re-prints on an interval, so the
                    # LAST match is the address the agent currently holds.
                    address = parsed
                    # #1559：連上了 ⇒ 之前的失敗原因不再描述現況。
                    error = None
                elif not candidate:
                    # An EMPTY announcement is a retraction: the sidecar prints it
                    # whenever the agent exits (bad token, expired account…)
                    # before backing off and retrying. Without honouring it the
                    # platform would keep showing the last address the dead agent
                    # held, and the tenant would configure devices against a
                    # tunnel that no longer exists.
                    address = None
                else:
                    logger.warning(
                        "Discarding malformed advertised address from %s/%s",
                        namespace,
                        pod.name,
                    )
            if address:
                return address, None
            last_error = error or last_error

        return None, last_error

    # #1417：診斷文字（waiting message、事件訊息）的長度上限；事件查詢的 subprocess 上限。
    # 事件只是錦上添花，查不到不能拖垮 status 端點（平台背景 poll 每分鐘逐台打一次）。
    DIAGNOSTIC_TEXT_LIMIT = 300
    POD_EVENTS_TIMEOUT_SECONDS = 10

    def get_pod_warning_events(self, namespace: str) -> Dict[str, PodWarningEvent]:
        """#1417：namespace 內每顆 Pod 最近一筆 Warning 事件，以 Pod UID 與名字為鍵。

        一次 list（field selector 只取 ``type=Warning`` 且 ``involvedObject.kind=Pod``），
        再依 ``involvedObject`` 對應——不逐顆 Pod 查。鍵是 ``uid:<uid>``；事件本身沒帶 UID
        時才用 ``name:<name>``——同名重建的 Pod（StatefulSet）不會拿到前一顆的事件。

        Raises:
            KubectlException: kubectl 失敗或逾時（呼叫端負責吞掉，見 ``get_pods``）
        """
        validate_namespace(namespace)
        result = self._run_command(
            [
                "get",
                "events",
                "--namespace",
                namespace,
                "--field-selector",
                "type=Warning,involvedObject.kind=Pod",
                "--output",
                "json",
            ],
            timeout=self.POD_EVENTS_TIMEOUT_SECONDS,
        )
        items = json.loads(result.stdout or "{}").get("items") or []
        latest: Dict[str, Dict[str, Any]] = {}
        for event in sorted(items, key=_event_sort_key):
            obj = event.get("involvedObject") or {}
            if not event.get("reason"):
                continue
            if obj.get("uid"):
                key = f"uid:{obj['uid']}"
            elif obj.get("name"):
                key = f"name:{obj['name']}"
            else:
                continue
            # 排序後逐筆覆寫 ⇒ 留下的是每顆 Pod 最後一筆。
            latest[key] = event
        limit = self.DIAGNOSTIC_TEXT_LIMIT
        out: Dict[str, PodWarningEvent] = {}
        for key, event in latest.items():
            count = (event.get("series") or {}).get("count") or event.get("count")
            out[key] = PodWarningEvent(
                reason=event["reason"],
                message=_truncate(event.get("message") or event.get("note"), limit),
                count=count if isinstance(count, int) else None,
                last_seen=_event_time(event) or None,
            )
        return out

    def get_pods(
        self,
        namespace: str,
        label_selector: Optional[str] = None,
        include_warning_events: bool = False,
    ) -> List[PodInfo]:
        """Get pods in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector
            include_warning_events: #1417——對未就緒的 Pod 附上最近一筆 Warning 事件
                （``last_warning``）。只有 status 端點開；全部就緒時不查事件。事件查詢
                失敗只記 log、``last_warning`` 留 None，**不**讓本方法失敗。

        Returns:
            List of pod information
        """
        validate_namespace(namespace)

        args = [
            "get",
            "pods",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        pods_data = json.loads(result.stdout)

        pods = []
        # #1417：未就緒 Pod 的事件查詢鍵（UID 優先、名字備援），與 pods 同序。
        unready_keys: List[Optional[tuple]] = []
        for pod in pods_data.get("items", []):
            metadata = pod.get("metadata", {})
            status = pod.get("status", {})

            # Calculate ready status
            container_statuses = status.get("containerStatuses", [])
            ready_count = sum(1 for c in container_statuses if c.get("ready"))
            total_count = len(container_statuses)
            ready_str = f"{ready_count}/{total_count}"
            phase = status.get("phase", "Unknown")
            unready_keys.append(
                (metadata.get("uid"), metadata.get("name"))
                if _pod_is_unready(phase, metadata, ready_count, total_count) else None
            )

            # Calculate restarts
            restarts = sum(c.get("restartCount", 0) for c in container_statuses)

            # Calculate age
            created = metadata.get("creationTimestamp", "")
            age = self._calculate_age(created)

            labels = metadata.get("labels") or {}
            pods.append(
                PodInfo(
                    name=metadata.get("name", ""),
                    phase=PodPhase(status.get("phase", "Unknown")),
                    ready=ready_str,
                    restarts=restarts,
                    age=age,
                    deletion_timestamp=metadata.get("deletionTimestamp"),
                    component=labels.get("app.kubernetes.io/component"),
                    task=labels.get("woow.paas/task"),
                    owner_kind=_controller_owner_kind(metadata),
                    creation_timestamp=created or None,
                    # #1408：spec 上寫的 image 參照（不是 imageID）——平台拿來核對
                    # Redeploy 之後 workload 真的換成範本釘的 image。
                    images=[
                        c.get("image") for c in (pod.get("spec") or {}).get("containers") or []
                        if c.get("image")
                    ],
                    # #1417：卡住的容器（就緒的 Pod 自然沒有 waiting 容器）。
                    waiting=_pod_waiting(status, self.DIAGNOSTIC_TEXT_LIMIT),
                )
            )

        if include_warning_events and any(unready_keys):
            # 一次 list namespace 的 Warning 事件，再對到未就緒的 Pod；就緒的 Pod 不附
            # （它們的舊事件只會誤導）。查詢失敗只記 log：status 端點不能因此失敗。
            try:
                events = self.get_pod_warning_events(namespace)
            except Exception as e:  # noqa: BLE001 —— 見上
                logger.warning(
                    "Could not list pod warning events in %s (pod status still "
                    "reported without them): %s", namespace, getattr(e, "message", e),
                )
                events = {}
            for pod_info, keys in zip(pods, unready_keys):
                if not keys:
                    continue
                uid, name = keys
                pod_info.last_warning = (
                    (events.get(f"uid:{uid}") if uid else None)
                    or events.get(f"name:{name}")
                )

        return pods

    # #1647：status 端點（``?include_workloads=true``）要看的 workload 種類，以及診斷文字
    # （ReplicaFailure message、controller 事件訊息）的長度上限。比 Pod 診斷的 300 寬：
    # quota 的拒絕原文帶 quota 名稱與 requested／used／limited 三組數字，截太短會把
    # 「是哪一項資源超了」切掉。
    WORKLOAD_KINDS = "deployments,statefulsets"
    WORKLOAD_DIAGNOSTIC_TEXT_LIMIT = 500

    def get_workloads(self, namespace: str, label_selector: str) -> List[WorkloadInfo]:
        """#1647：release 的 Deployment／StatefulSet 副本數與「建不出 Pod」的證據。

        一次 ``kubectl get deployments,statefulsets``（不逐種類查）。只要有任一個 workload
        副本不足（``replicas < desired_replicas`` 或帶 ``ReplicaFailure``），再查一次
        namespace 的 Warning 事件、把對得上的填進不足者的 ``last_warning``；全部充足時
        不查事件。事件查詢失敗只記 log、``last_warning`` 留 None，**不**讓本方法失敗。

        Args:
            namespace: Namespace name
            label_selector: Label selector（status 端點傳 ``app.kubernetes.io/instance=<release>``）

        Returns:
            依 ``(kind, name)`` 排序的 workload 清單；沒有 workload 回 ``[]``。

        Raises:
            KubectlException: workload 查詢本身失敗或逾時（呼叫端決定怎麼回報）
        """
        validate_namespace(namespace)

        result = self._run_command(
            [
                "get",
                self.WORKLOAD_KINDS,
                "--namespace",
                namespace,
                "--selector",
                label_selector,
                "--output",
                "json",
            ]
        )
        items = json.loads(result.stdout or "{}").get("items") or []

        limit = self.WORKLOAD_DIAGNOSTIC_TEXT_LIMIT
        workloads: List[WorkloadInfo] = []
        short: List[WorkloadInfo] = []
        for item in items:
            kind = item.get("kind")
            if kind not in ("Deployment", "StatefulSet"):
                continue
            metadata = item.get("metadata") or {}
            spec = item.get("spec") or {}
            status = item.get("status") or {}
            # spec.replicas 缺值＝API server 的預設 1；status 的計數在 0 時整個 key 不出現。
            desired = spec.get("replicas")
            strategy_key = "strategy" if kind == "Deployment" else "updateStrategy"
            info = WorkloadInfo(
                kind=kind,
                name=metadata.get("name", ""),
                desired_replicas=1 if desired is None else desired,
                replicas=status.get("replicas") or 0,
                ready_replicas=status.get("readyReplicas") or 0,
                updated_replicas=status.get("updatedReplicas") or 0,
                strategy=(spec.get(strategy_key) or {}).get("type"),
                replica_failure=_replica_failure(status, limit),
            )
            workloads.append(info)
            if info.replicas < info.desired_replicas or info.replica_failure:
                short.append(info)

        if short:
            # 一次 list namespace 的 Warning 事件，再對到副本不足的 workload；充足的不附
            # （它們的舊事件只會誤導）。查詢失敗只記 log：status 端點不能因此失敗。
            try:
                events = self.get_workload_warning_events(namespace)
            except Exception as e:  # noqa: BLE001 —— 見上
                logger.warning(
                    "Could not list workload warning events in %s (workload status still "
                    "reported without them): %s", namespace, getattr(e, "message", e),
                )
                events = {}
            for info in short:
                info.last_warning = events.get((info.kind, info.name))

        return sorted(workloads, key=lambda w: (w.kind, w.name))

    def get_workload_warning_events(
        self, namespace: str
    ) -> Dict[Tuple[str, str], PodWarningEvent]:
        """#1647：namespace 內每個 workload 最近一筆 controller 層 Warning 事件。

        鍵是 ``(kind, name)``，``kind`` 只有 ``Deployment``／``StatefulSet``：

        - ``involvedObject.kind == "Deployment"`` → ``("Deployment", name)``；
        - ``"ReplicaSet"`` → ``("Deployment", <去掉最後一段 -hash 的名字>)``。quota 的
          ``FailedCreate`` 掛在 ReplicaSet 上，而 ReplicaSet 的名字是
          ``<deployment>-<pod-template-hash>``；
        - ``"StatefulSet"`` → ``("StatefulSet", name)``（StatefulSet controller 直接建 Pod，
          事件掛在 StatefulSet 自己身上）；
        - 其他 kind（Pod、PVC…）跳過——Pod 的事件由 ``get_pod_warning_events`` 管。

        field selector 只取 ``type=Warning``，不加 ``involvedObject.kind``：三種 kind 要
        三次查詢，而 namespace 的 Warning 事件量本來就小。

        Raises:
            KubectlException: kubectl 失敗或逾時（呼叫端負責吞掉，見 ``get_workloads``）
        """
        validate_namespace(namespace)
        result = self._run_command(
            [
                "get",
                "events",
                "--namespace",
                namespace,
                "--field-selector",
                "type=Warning",
                "--output",
                "json",
            ],
            timeout=self.POD_EVENTS_TIMEOUT_SECONDS,
        )
        items = json.loads(result.stdout or "{}").get("items") or []
        latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for event in sorted(items, key=_event_sort_key):
            if not event.get("reason"):
                continue
            obj = event.get("involvedObject") or {}
            kind, name = obj.get("kind"), obj.get("name") or ""
            if kind == "Deployment":
                key = ("Deployment", name)
            elif kind == "ReplicaSet":
                key = ("Deployment", name.rpartition("-")[0])
            elif kind == "StatefulSet":
                key = ("StatefulSet", name)
            else:
                continue
            # 排序後逐筆覆寫 ⇒ 留下的是每個 workload 最後一筆。
            latest[key] = event
        limit = self.WORKLOAD_DIAGNOSTIC_TEXT_LIMIT
        out: Dict[Tuple[str, str], PodWarningEvent] = {}
        for key, event in latest.items():
            count = (event.get("series") or {}).get("count") or event.get("count")
            out[key] = PodWarningEvent(
                reason=event["reason"],
                message=_truncate(event.get("message") or event.get("note"), limit),
                count=count if isinstance(count, int) else None,
                last_seen=_event_time(event) or None,
            )
        return out

    def create_namespace(
        self,
        name: str,
        cpu_limit: str,
        memory_limit: str,
        storage_limit: str,
        workspace_id: Optional[int] = None,
        ephemeral_storage_request: Optional[str] = None,
        ephemeral_storage_limit: Optional[str] = None,
    ) -> Dict[str, str]:
        """Create namespace with resource quota.

        Args:
            name: Namespace name
            cpu_limit: CPU limit
            memory_limit: Memory limit
            storage_limit: Storage limit
            workspace_id: Owning CloudService's Odoo workspace id; stamped as the
                ``woow.tw/workspace-id`` NetworkPolicy label (#279 tenant isolation).
                None (default) keeps the ns label-free for that key (backward compat).

        Returns:
            Creation result
        """
        validate_namespace(name)

        import yaml

        # Create namespace manifest using yaml.safe_dump to prevent injection
        ns_manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": name,
                "labels": build_managed_ns_labels(
                    workspace_id=workspace_id,
                    owner=str(workspace_id) if workspace_id is not None else None,
                ),
            },
        }

        # Create quota manifest using yaml.safe_dump to prevent injection
        quota_manifest = self._build_quota_manifest(
            name, cpu_limit, memory_limit, storage_limit,
            ephemeral_storage_request, ephemeral_storage_limit,
        )

        # Apply namespace
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(ns_manifest, f)
            ns_file = f.name

        try:
            self._run_command(["apply", "-f", ns_file])
        finally:
            Path(ns_file).unlink(missing_ok=True)

        # #278 A2: CS ns LimitRange — #1434 起排在 quota **之前**：quota 一旦 hard
        # 宣告 limits.ephemeral-storage，admission 就拒收沒宣告它的容器；既有 ns 重跑
        # 本流程（每次開通都會）時若先換 quota、後補 LimitRange 預設，中間那一瞬間建的
        # Pod 會被拒。先補預設、再收緊額度，任何時刻都不會出現「額度要求、預設缺席」。
        self._apply_limitrange(name)

        # Apply quota
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(quota_manifest, f)
            quota_file = f.name

        try:
            self._run_command(["apply", "-f", quota_file])
        finally:
            Path(quota_file).unlink(missing_ok=True)

        self.ensure_registry_secret(name)

        # #423：secret 佈進 ns 之後，緊接著讓 ns 的 default SA 指向它。CDI importer
        # 這種由 controller 生成、不經任何 chart podSpec 的 pod，只有這條路拿得到
        # 憑證。放在 ns 準備階段是為了讓不變式從 ns 出生就成立（不必等到第一次
        # install）；install / upgrade / reset 各路徑另有同一個冪等收斂當後手。
        self.ensure_default_sa_image_pull_secret(name)

        # #279 O4: apply the leg-2 tenant-isolation baseline NetworkPolicy bundle.
        if workspace_id is not None:
            # #1441：PSA 排在 NP 之前——之後任何一步失敗重跑，ns 也已經不收特權 pod。
            self.apply_pod_security(name)
            self._apply_baseline_netpol(name, workspace_id)
        else:
            # A tenant CloudService ns should always carry a workspace_id; None here is
            # either a genuine non-tenant/dev ns (fine) or a provisioning bug — the
            # operator can't tell them apart, so it warns + skips rather than silently
            # leaving a no-isolation ns OR breaking a legit dev caller. The tenant-side
            # fail-loud guarantee (a CloudService MUST have a workspace) belongs in the
            # Odoo caller (cloud_service.py deploy path), where the context is known.
            logger.warning(
                "create_namespace(%s): no workspace_id — skipping #279 baseline "
                "NetworkPolicy (no leg-2 isolation applied to this namespace).", name
            )

        return {"message": f"Namespace {name} created with quota"}

    def _build_limitrange_manifest(self, name: str) -> dict:
        """CS ns LimitRange（#278 A2）：ns quota hard-宣告 limits.cpu/memory 後，
        admission 拒收未設 limits 的容器；default 補上安全預設讓其可建。

        #1434：再補 ephemeral-storage。R8 實測所有租戶容器都沒有 ephemeral-storage
        上限，/tmp 等 overlay 路徑可無限寫進節點系統碟（含正式站節點）。LimitRange 的
        default 只補「容器沒宣告」的格子，chart 自己有設的照 chart。"""
        return {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": f"{name}-limits", "namespace": name},
            "spec": {"limits": [{
                "type": "Container",
                "default": {
                    "cpu": "500m",
                    "memory": "512Mi",
                    "ephemeral-storage": settings.tenant_ephemeral_storage_default_limit,
                },
                "defaultRequest": {
                    "cpu": "500m",
                    "memory": "512Mi",
                    "ephemeral-storage": settings.tenant_ephemeral_storage_default_request,
                },
            }]},
        }

    def _apply_limitrange(self, name: str) -> None:
        import yaml
        limits_manifest = self._build_limitrange_manifest(name)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(limits_manifest, f)
            limits_file = f.name
        try:
            self._run_command(["apply", "-f", limits_file])
        finally:
            Path(limits_file).unlink(missing_ok=True)

    @staticmethod
    def _build_quota_manifest(
        name: str,
        cpu_limit: str,
        memory_limit: str,
        storage_limit: str,
        ephemeral_storage_request: Optional[str] = None,
        ephemeral_storage_limit: Optional[str] = None,
    ) -> dict:
        """ns ResourceQuota 的唯一組裝點（create 與 update 共用，避免兩份 hard map 漂移）。

        #1434：``ephemeral_storage_*`` 為 None（舊平台沒送）時**不加**這兩個鍵——
        行為與 #1434 之前完全相同，operator 可以先於平台發版。"""
        hard = {
            "requests.cpu": cpu_limit,
            "requests.memory": memory_limit,
            "limits.cpu": cpu_limit,
            "limits.memory": memory_limit,
            "persistentvolumeclaims": "10",
            "requests.storage": storage_limit,
        }
        if ephemeral_storage_request:
            hard["requests.ephemeral-storage"] = ephemeral_storage_request
        if ephemeral_storage_limit:
            hard["limits.ephemeral-storage"] = ephemeral_storage_limit
        return {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": f"{name}-quota", "namespace": name},
            "spec": {"hard": hard},
        }

    def update_namespace_quota(self, name: str, cpu_limit: str,
                               memory_limit: str, storage_limit: str,
                               ephemeral_storage_request: Optional[str] = None,
                               ephemeral_storage_limit: Optional[str] = None,
                               ) -> Dict[str, str]:
        """#278 A1：整組 hard overwrite ns ResourceQuota（不動 ns/NP/secret）。
        apply 完整 hard map（含 pvc=10），無 stale key。

        #1434：quota 帶 ephemeral-storage 時先重套 LimitRange（補預設），理由見
        ``create_namespace`` 同一段——既有 ns 第一次收到含 ephemeral 的 quota 時，
        LimitRange 可能還是舊的（沒有 ephemeral 預設）。"""
        validate_namespace(name)
        import yaml
        if ephemeral_storage_request or ephemeral_storage_limit:
            self._apply_limitrange(name)
        quota_manifest = self._build_quota_manifest(
            name, cpu_limit, memory_limit, storage_limit,
            ephemeral_storage_request, ephemeral_storage_limit,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(quota_manifest, f)
            quota_file = f.name
        try:
            self._run_command(["apply", "-f", quota_file])
        finally:
            Path(quota_file).unlink(missing_ok=True)
        return {"message": f"Namespace {name} quota updated"}

    def _apply_baseline_netpol(self, namespace: str, workspace_id: int) -> None:
        """#279 O4 leg-2: apply the CloudService-namespace baseline NetworkPolicy bundle
        so tenant isolation holds once #280 removes the chart's from-less allows.

        Additive, ingress-only NPs. 1–2 are ``podSelector: {}`` (every pod in the ns —
        including the n8n app pod that hosts the mcp SIDECAR, which carries no chart NP
        of its own, so an ns-wide rule is the only mechanism that covers both the
        sidecar and standalone mcp-server shapes); 3–4 (the CROSS-ns entries) select
        every pod EXCEPT data stores (M4 R13, ``build_cross_ns_ingress_pod_selector``):

          1. ``default-deny-ingress`` — isolate every pod.
          2. ``allow-same-ns`` — a workspace ns is shared by its CloudServices; intra-ns
             reach is within-tenant, not a cross-tenant boundary. The ONLY rule that
             lets anything reach a tenant DB pod (Workspace = LAN, DB only same-ns).
          3. ``allow-cloudflared`` — ingress from THIS env's dedicated CF Tunnel
             connector ns, so the user-facing web path survives the default-deny.
             Env-safe by construction (the connector ns name is env-specific); skipped
             when no connector ns is configured/derivable (dev/local). Data-store pods
             (PostgreSQL／Redis／DB backup jobs) are NOT selected — before R13 this rule
             let paas-cloudflare-system (unrestricted egress) open DB:5432.
          4. ``allow-mcp-reach`` (the leg-2 crux) — from any agent ns stamped BOTH this
             workspace's ``reach-ws`` key AND the matching env label. Same data-store
             exclusion as 3. No port filter: the n8n sidecar (:3000) and standalone
             mcp-server (:8080) diverge, and within-workspace-ns reach is within-tenant.

        5. ``paas-baseline-egress``（#1441／#1436／#514，``TENANT_BASELINE_EGRESS_ENABLED``
           打開時才套）— 工作區＝自己的區網：egress 只剩同 ns 全部 pod 全部埠、kube-dns
           53、公網（排除私網／metadata／CGNAT／多播／240/4）。單一一條、原子套用；
           內容見 ``tenant_baseline.build_baseline_egress_netpol``。開關關著時**不刪**已
           存在的這條（關開關＝不再新增，不是回滾；回滾用 scripts/ws_lan_backfill.py
           --rollback）。

        Idempotent via ``kubectl apply``.
        """
        validate_namespace(namespace)

        def _np(name: str, ingress: Optional[list],
                pod_selector: Optional[dict] = None) -> dict:
            spec = {
                "podSelector": pod_selector if pod_selector is not None else {},
                "policyTypes": ["Ingress"],
            }
            if ingress is not None:
                spec["ingress"] = ingress
            return {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {"paas-managed": "true"},
                },
                "spec": spec,
            }

        env_ml = _env_match_labels()
        cross_ns_targets = build_cross_ns_ingress_pod_selector()
        manifests = [
            _np("paas-baseline-default-deny-ingress", None),
            _np("paas-baseline-allow-same-ns", [{"from": [{"podSelector": {}}]}]),
        ]
        cf_ns = _cloudflared_namespace()
        if cf_ns:
            manifests.append(_np(
                "paas-baseline-allow-cloudflared",
                [{"from": [{"namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": cf_ns},
                }}]}],
                pod_selector=cross_ns_targets,
            ))
        manifests.append(_np(
            "paas-baseline-allow-mcp-reach",
            [{"from": [{"namespaceSelector": {"matchLabels": {
                reach_ws_label_key(workspace_id): REACH_WS_LABEL_VALUE,
                **env_ml,
            }}}]}],
            pod_selector=cross_ns_targets,
        ))
        if settings.tenant_baseline_egress_enabled:
            manifests.append(build_baseline_egress_netpol(namespace))
        for manifest in manifests:
            self.apply_manifest(manifest)

    def apply_pod_security(self, namespace: str) -> Dict[str, str]:
        """#1441：租戶 ns 的 Pod Security Admission 標籤（``TENANT_POD_SECURITY_ENABLED``）。

        * 一般租戶 ns：``kubectl label --overwrite`` enforce／warn／audit（＋各自的
          -version），內容見 ``tenant_baseline.build_pod_security_labels``。
        * 例外 ns（``PSA_EXEMPT_NAMESPACES``，預設只有管理者用的 Omnigent
          paas-ws-ab6564da）：移除所有 PSA 標籤（不設 enforce），並註記理由。

        開關關著時什麼都不做（回傳空 dict）。冪等。回傳實際寫入的標籤（例外 ns 回傳空
        dict），供測試與回填端點觀察。
        """
        validate_namespace(namespace)
        if not settings.tenant_pod_security_enabled:
            return {}
        exempt = parse_namespace_list(settings.psa_exempt_namespaces)
        if is_psa_exempt(namespace, exempt):
            # `key-` 對不存在的標籤是 no-op（kubectl 只回 "not labeled"），所以可以重跑。
            self._run_command(
                ["label", "ns", namespace] + [f"{k}-" for k in psa_label_keys()]
            )
            self._run_command([
                "annotate", "ns", namespace, "--overwrite",
                f"{PSA_EXEMPT_ANNOTATION}={PSA_EXEMPT_REASON}",
            ])
            logger.warning(
                "apply_pod_security(%s): PSA exempt namespace — no enforce label", namespace
            )
            return {}
        labels = build_pod_security_labels(
            enforce=settings.tenant_psa_enforce_level,
            warn=settings.tenant_psa_warn_level,
            audit=settings.tenant_psa_audit_level,
            version=settings.tenant_psa_version,
        )
        for key, value in labels.items():
            validate_label_key(key)
            validate_label_value(key, value)
        self._run_command(
            ["label", "ns", namespace, "--overwrite"]
            + [f"{k}={v}" for k, v in labels.items()]
        )
        return labels

    def ensure_registry_secret(self, namespace: str) -> Optional[str]:
        """Create/update a dockerconfigjson secret for private registry access.

        Reads registry credentials from settings. If not configured, skips.
        Uses kubectl replace (falling back to create when absent) so it's
        idempotent **without** leaving the credential in the
        ``kubectl.kubernetes.io/last-applied-configuration`` annotation
        that ``kubectl apply`` would write — see #823 CP-12 and the comment
        at the call site below.

        Args:
            namespace: Target namespace

        Returns:
            Secret name if created, None if registry not configured
        """
        # #284 W0-hardening: this mutate method (it writes a dockerconfigjson
        # Secret — via kubectl replace/create since #823 CP-12) previously relied on
        # caller discipline for the namespace. Validate it at the entry point so a
        # future caller can't write into an out-of-prefix ns (RBAC does not enforce
        # the prefix — this app layer is the only boundary).
        validate_namespace(namespace)
        if not settings.registry_server or not settings.registry_username:
            return None

        import yaml

        auth_raw = f"{settings.registry_username}:{settings.registry_password}"
        auth_b64 = base64.b64encode(auth_raw.encode()).decode()
        docker_config = json.dumps({
            "auths": {
                settings.registry_server: {
                    "username": settings.registry_username,
                    "password": settings.registry_password,
                    "auth": auth_b64,
                }
            }
        })
        docker_config_b64 = base64.b64encode(docker_config.encode()).decode()

        secret_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": settings.registry_secret_name,
                "namespace": namespace,
            },
            "type": "kubernetes.io/dockerconfigjson",
            "data": {
                ".dockerconfigjson": docker_config_b64,
            },
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.safe_dump(secret_manifest, f)
            secret_file = f.name

        try:
            # 🔴 #823 CP-12：⛔ 不可用 `kubectl apply`。client-side apply 會把**整份
            # manifest**（含 `.data[".dockerconfigjson"]` 的憑證 base64）寫進
            # `kubectl.kubernetes.io/last-applied-configuration` annotation
            # ⇒ 同一份憑證在 etcd 裡多存一份、且是任何有 get secret 權限的人都讀得到的
            # metadata。`replace` 是整份 PUT：送出的 manifest 沒有 `annotations` 欄位，
            # 所以舊的 annotation 連同其中的憑證會一併被清掉。
            #
            # 為什麼不是別的做法（docker-desktop 實測，見 PR 描述）：
            #   * `apply --server-side`：對**既有** CSA 物件只是把 annotation 轉交給
            #     `kubectl-last-applied` manager 繼續維護（實測 189B → 172B），**清不掉**；
            #     只有全新物件才乾淨。既有租戶的憑證副本會留著。
            #   * `apply` 後補 `annotate ...last-applied-configuration-`：曝光窗每次呼叫
            #     重開一次，而且把三方 merge 的 baseline 永久清空 ⇒ apply 從此無法偵測
            #     「欄位被從 manifest 拿掉了要刪除」，退化成只能新增／覆蓋。
            #   * `replace --force`：那是 delete + re-create，對正被 pod 當
            #     `imagePullSecrets` 引用的 secret 會開一個真實的存在性空窗。
            self._run_command(["replace", "-f", secret_file])
        except KubectlException as exc:
            if not self.is_not_found_error(exc):
                raise
            # 全新 namespace 的第一次：secret 還不存在，`replace` 會 404。
            # `create` 同樣不寫 last-applied annotation。
            try:
                self._run_command(["create", "-f", secret_file])
            except KubectlException as exc_create:
                if not self.is_already_exists_error(exc_create):
                    raise
                # TOCTOU：`replace` 探到 NotFound 與這次 `create` 之間，另一個併發
                # 呼叫（API 重試／同時建同一租戶）先把它建出來了。內容同源（都來自
                # settings）⇒ 良性，收斂成一次 replace 把內容確定寫對。
                # ⚠ 原本單一 `apply` 具備原子的「不存在則建、存在則更新」語意，
                # 拆成兩步後這個窗才出現——所以這個分支不是防禦性冗餘，是必要的。
                self._run_command(["replace", "-f", secret_file])
        finally:
            Path(secret_file).unlink(missing_ok=True)

        logger.info(
            "Registry secret '%s' ensured in namespace '%s'",
            settings.registry_secret_name,
            namespace,
        )

        return settings.registry_secret_name

    @staticmethod
    def is_not_found_error(exc: "KubectlException") -> bool:
        """True only when kubectl failed because the object does not exist.

        ``KubectlException`` 對**每一種** kubectl 失敗都會拋（RBAC Forbidden、30s
        timeout、binary 不存在），所以想把「物件還沒被建出來」當成良性、可重試的
        狀態時必須收窄到這裡。把整個例外類別當成 NotFound，會讓一個 Forbidden 偽裝
        成「等一下就好」，靜默重試到放棄——與 ``is_crd_absent_error`` 同一道理。
        """
        stderr = (exc.stderr or "").lower()
        return "(notfound)" in stderr.replace(" ", "") or "not found" in stderr

    @staticmethod
    def is_already_exists_error(exc: "KubectlException") -> bool:
        """True only when kubectl failed because the object already exists.

        與 ``is_not_found_error`` 同一道收窄：``ensure_registry_secret`` 的
        replace → create fallback 之間有 TOCTOU 窗，併發呼叫時後到的那個會拿到
        AlreadyExists。那是**良性**的（先到的已經寫進同一份內容，兩者同源自
        settings），但⛔ 不能把整個例外類別當成它——否則一個 Forbidden 會被
        當成「別人剛好建好了」而靜默吞掉。
        """
        stderr = (exc.stderr or "").lower()
        return (
            "(alreadyexists)" in stderr.replace(" ", "")
            or "already exists" in stderr
        )

    def _get_default_service_account(
        self, namespace: str
    ) -> Optional[Dict[str, Any]]:
        """讀 ns 的 default ServiceAccount；**只**對 NotFound 做短重試。

        ns 剛被 ``create_namespace`` 建出來時 default SA 尚未由 controller 補上，
        這是本方法唯一容忍的失敗。重試耗盡回 ``None`` 讓呼叫端容忍：SA 出現之後，
        任何一次 install / upgrade / reset 都會再收斂一次，不需要為了這個空窗把整個
        ns 建立或部署弄失敗。其餘 kubectl 失敗一律往上拋。
        """
        for attempt in range(_SA_LOOKUP_ATTEMPTS):
            try:
                result = self._run_command([
                    "get",
                    "serviceaccount",
                    _DEFAULT_SERVICE_ACCOUNT,
                    "--namespace",
                    namespace,
                    "--output",
                    "json",
                ])
                return json.loads(result.stdout)
            except KubectlException as e:
                if not self.is_not_found_error(e):
                    raise
                if attempt < _SA_LOOKUP_ATTEMPTS - 1:
                    time.sleep(_SA_LOOKUP_BACKOFF_SECONDS)
        return None

    def ensure_default_sa_image_pull_secret(self, namespace: str) -> bool:
        """把 registry pull secret 併進租戶 ns 的 default ServiceAccount（冪等）。

        **為什麼 chart podSpec 的 imagePullSecrets 不夠**（#423 HAOS）：VM 型 cloud
        service 開通時，golden image 是由 CDI 的 *importer pod* 匯進 PVC 的，而那顆
        pod 由 CDI controller 依 DataVolume 生成、不屬於任何 chart 的 podSpec——
        我們注入 values 的 imagePullSecrets（``_ensure_image_pull_secrets`` 第一層）
        完全碰不到它。registry source + ``pullMethod=node`` 時 image 由節點的
        containerd 拉，憑證只能來自 importer pod 的 pod-level imagePullSecrets，而
        importer 跑在該 ns 的 **SA default** 上（stg 實查），靠 ServiceAccount
        admission 把 SA 的 imagePullSecrets 併進 pod。default SA 是空的 ⇒ 拉不到
        golden image ⇒ 開通永久卡在 ImportScheduled。

        stg 案發 ns 熱線實證：patch 上 ``paas-registry`` 後 importer pod 的 spec 真的
        長出 ``.spec.imagePullSecrets=[{"name": "paas-registry"}]``，node-pull 過
        auth，DataVolume import 74 秒完成（#423 comment 3696）。

        **冪等**：先讀再決定。已含同名項目就完全不發 patch；否則把新項目 append 到
        現有清單後**整份**送出——JSON merge patch 對 array 是整份覆寫，不帶著既有
        項目一起送就會把租戶／其他機制放進去的 pull secret 洗掉。

        Args:
            namespace: 目標 namespace（前綴守衛同其他 mutate 方法）

        Returns:
            True 表示這次真的補了；False 表示本來就有，或 default SA 在重試視窗內
            仍未出現（容忍，見 ``_get_default_service_account``）。

        Raises:
            KubectlException: NotFound 以外的任何 kubectl 失敗（RBAC、timeout…）。
            ValueError: namespace 不在允許前綴內。
        """
        # #284 W0-hardening 同款守衛：這是 mutate 方法，RBAC 不擋前綴，app 層是唯一
        # 邊界。
        validate_namespace(namespace)
        secret_name = settings.registry_secret_name

        service_account = self._get_default_service_account(namespace)
        if service_account is None:
            logger.warning(
                "default ServiceAccount absent in namespace '%s' after %d attempts; "
                "skipping imagePullSecrets convergence — a later install/upgrade/"
                "reset will retry. VM-type provisioning in this ns would stall at "
                "ImportScheduled until then (#423).",
                namespace,
                _SA_LOOKUP_ATTEMPTS,
            )
            return False

        existing = service_account.get("imagePullSecrets") or []
        if any(
            isinstance(entry, dict) and entry.get("name") == secret_name
            for entry in existing
        ):
            logger.debug(
                "default ServiceAccount in '%s' already carries imagePullSecret "
                "'%s'; no patch issued.",
                namespace,
                secret_name,
            )
            return False

        merged = list(existing) + [{"name": secret_name}]
        self._run_command([
            "patch",
            "serviceaccount",
            _DEFAULT_SERVICE_ACCOUNT,
            "--namespace",
            namespace,
            "--type",
            "merge",
            "--patch",
            json.dumps({"imagePullSecrets": merged}),
        ])
        logger.info(
            "default ServiceAccount in '%s' now carries imagePullSecret '%s' "
            "(%d entr%s total, pre-existing ones preserved)",
            namespace,
            secret_name,
            len(merged),
            "y" if len(merged) == 1 else "ies",
        )
        return True

    def suspended_page_service_exists(self) -> bool:
        """woow_paas_platform #1563：停權說明頁的 Service 在不在。

        刻意**不經**租戶 namespace 前綴驗證（``validate_namespace``）：說明頁住在
        cloudflared 的 namespace，不是租戶 ns。也因此 namespace 與 Service 名**只**
        取自 settings（``_cloudflared_namespace()``／``settings.suspended_page_service``），
        沒有任何呼叫端輸入能流進這條 kubectl——它無法被拿來探別的 namespace。
        （ClusterRole 的 core ``services`` get 是叢集範圍，不必另加 RBAC。）

        Service 不存在（CD 的 paas-suspended-page 還沒上）時回 False，讓 park 拒絕，
        不會把 hostname 指到一個不存在的位址（那會從「慢慢逾時」變成「立刻 502」）。

        Raises:
            KubectlException: 查詢本身失敗（權限、逾時）——不是「不存在」。
        """
        namespace = _cloudflared_namespace()
        if not namespace or not settings.suspended_page_service:
            return False
        result = self._run_command([
            "get", "service", settings.suspended_page_service,
            "--namespace", namespace,
            "--ignore-not-found",
            "--output", "name",
        ])
        return bool((result.stdout or "").strip())

    def get_services(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get services in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector

        Returns:
            List of service information dicts with name, ports, type
        """
        validate_namespace(namespace)

        args = [
            "get",
            "services",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        services_data = json.loads(result.stdout)

        services = []
        for svc in services_data.get("items", []):
            metadata = svc.get("metadata", {})
            spec = svc.get("spec", {})

            ports = []
            for port in spec.get("ports", []):
                ports.append({
                    "name": port.get("name", ""),
                    "port": port.get("port"),
                    "targetPort": port.get("targetPort"),
                    "protocol": port.get("protocol", "TCP"),
                })

            services.append({
                "name": metadata.get("name", ""),
                "namespace": namespace,
                "type": spec.get("type", "ClusterIP"),
                "clusterIP": spec.get("clusterIP"),
                "ports": ports,
            })

        return services

    def get_deployments(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get deployments in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector

        Returns:
            List of deployment information dicts
        """
        validate_namespace(namespace)

        args = [
            "get",
            "deployments",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        deployments_data = json.loads(result.stdout)

        deployments = []
        for deploy in deployments_data.get("items", []):
            metadata = deploy.get("metadata", {})
            deployments.append({
                "name": metadata.get("name", ""),
                "namespace": namespace,
            })

        return deployments

    def patch_deployment(
        self,
        namespace: str,
        deployment_name: str,
        patch: str,
        patch_type: str = "json",
    ) -> Dict[str, Any]:
        """Patch a Kubernetes deployment.

        Args:
            namespace: Namespace name
            deployment_name: Name of the deployment to patch
            patch: JSON patch string
            patch_type: Patch type ('json', 'merge', or 'strategic')

        Returns:
            Patched deployment info

        Raises:
            KubectlException: If patching fails
        """
        validate_namespace(namespace)

        args = [
            "patch",
            "deployment",
            deployment_name,
            "--namespace",
            namespace,
            f"--type={patch_type}",
            f"--patch={patch}",
            "--output",
            "json",
        ]

        result = self._run_command(args)
        return json.loads(result.stdout)

    def scale_deployment(
        self,
        namespace: str,
        deployment_name: str,
        replicas: int,
    ) -> None:
        """Scale a Kubernetes deployment to the given replica count.

        Args:
            namespace: Namespace name
            deployment_name: Name of the deployment to scale
            replicas: Target replica count

        Raises:
            KubectlException: If scaling fails
        """
        validate_namespace(namespace)

        args = [
            "scale",
            "deployment",
            deployment_name,
            "--namespace",
            namespace,
            f"--replicas={replicas}",
        ]

        self._run_command(args)

    def get_deployment_replicas(
        self, namespace: str, deployment_name: str
    ) -> int:
        """Return the desired replica count of a deployment (0 if reaped).

        Used by the agent-runtime lifecycle to distinguish attach (replicas>=1)
        from resume (replicas==0).
        """
        validate_namespace(namespace)

        args = [
            "get", "deployment", deployment_name,
            "--namespace", namespace,
            "--output", "jsonpath={.spec.replicas}",
        ]
        result = self._run_command(args)
        output = result.stdout.strip()
        return int(output) if output else 0

    def get_deployment_annotation(
        self, namespace: str, deployment_name: str, annotation_key: str
    ) -> Optional[str]:
        """Return a metadata annotation value on a deployment (None if absent).

        Used by the agent-runtime lifecycle to detect whether the LLM credential
        changed since the pod was spawned: the checksum is stamped as a
        *metadata* annotation (which, unlike a pod-template annotation, does not
        trigger a rollout), and attach/resume compares against it.
        """
        validate_namespace(namespace)

        args = [
            "get", "deployment", deployment_name,
            "--namespace", namespace,
            "--output", "json",
        ]
        result = self._run_command(args)
        data = json.loads(result.stdout)
        return (
            data.get("metadata", {}).get("annotations", {}).get(annotation_key)
        )

    def set_deployment_env(
        self,
        namespace: str,
        deployment_name: str,
        env: Dict[str, str],
        allow_sensitive_keys: frozenset = frozenset(),
    ) -> None:
        """Set/replace NON-SENSITIVE environment variables via ``kubectl set env``.

        ⛔ **Never pass a secret through here** (F-GC-1 / #955). ``kubectl set env``
        leaves the value on two plaintext surfaces that no amount of log redaction
        reaches:

        1. the kubectl **argv** — readable in ``/proc/<pid>/cmdline`` for the life of
           the process (``_redact_cmd`` only masks the log line, not the argv);
        2. the Deployment spec's literal ``env[].value`` — readable forever by anyone
           with ``get deployment``/``get pod`` in that namespace.

        Secrets belong in a per-release Secret consumed via ``valueFrom.secretKeyRef``
        — see ``api/agent_runtimes._provision_skill_bundle_secret``. The guard below
        is fail-fast on the KEY NAME rather than on the value: values are arbitrary
        strings with no reliable shape, so a value-shaped test would be mostly false
        negatives.

        ``allow_sensitive_keys`` is the one deliberate hole, and it exists solely so
        the ``agent_runtime_skill_bundle_secret_enabled=False`` rollback path can keep
        doing exactly what it does today. It is opt-in per call and per key name, so
        adding a NEW sensitive use means writing the key into a caller — visible in
        review — rather than inheriting an exemption. It disappears together with the
        flag and the plain-env branch (#955 rollout step 7).
        """
        validate_namespace(namespace)
        for key in env:
            if key in allow_sensitive_keys:
                continue
            marker = _sensitive_env_key_marker(key)
            if marker:
                raise ValueError(
                    f"refusing to set sensitive env {key!r} via `kubectl set env` "
                    f"(matched {marker!r}): the value would land on the kubectl argv "
                    "and in the Deployment spec's literal env[].value. Use a "
                    "per-release Secret + valueFrom.secretKeyRef instead (#955)."
                )

        args = [
            "set", "env",
            f"deployment/{deployment_name}",
            "--namespace", namespace,
        ] + [f"{key}={value}" for key, value in env.items()]
        self._run_command(args)

    def get_statefulsets(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get statefulsets in a namespace.

        Bitnami charts (Redis, PostgreSQL, Odoo, ...) deploy their primary
        workload as a StatefulSet, so subscription suspend/resume must
        cover this kind too.
        """
        validate_namespace(namespace)

        args = [
            "get",
            "statefulsets",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]
        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        sts_data = json.loads(result.stdout)
        statefulsets = []
        for sts in sts_data.get("items", []):
            metadata = sts.get("metadata", {})
            statefulsets.append({
                "name": metadata.get("name", ""),
                "namespace": namespace,
            })
        return statefulsets

    def scale_statefulset(
        self,
        namespace: str,
        statefulset_name: str,
        replicas: int,
    ) -> None:
        """Scale a Kubernetes statefulset to the given replica count."""
        validate_namespace(namespace)

        args = [
            "scale",
            "statefulset",
            statefulset_name,
            "--namespace",
            namespace,
            f"--replicas={replicas}",
        ]

        self._run_command(args)

    # ================= KubeVirt / CDI (#423 W4.1 + W4.3) ==================
    # VM-type CloudServices (HAOS) are delivered as VirtualMachine +
    # DataVolume CRs inside the same Helm chart as everything else, so the
    # delivery/expose/isolation layers are untouched. Three things DO differ
    # and live here (design §4):
    #   1. readiness  — virt-launcher pod Running != guest booted  -> get_vmis
    #   2. start/stop — VMs have no replicas                       -> patch_vm_run_strategy
    #   3. delete     — CDI DataVolume owns the PVC                -> label_datavolumes
    #
    # ⚠ These methods deliberately let KubectlException propagate when the
    # CRDs are absent. Returning [] would make "this cluster has no KubeVirt"
    # indistinguishable from "this release has no VM", and the platform's
    # readiness gate is fail-CLOSED on exactly that distinction: an empty
    # vmis list means "not ready yet", so a swallowed error would park every
    # VM service in `deploying` forever with no clue why.

    def has_datavolume_crd(self) -> bool:
        """Is CDI installed on this cluster?  (cached per process — MINOR-4)

        Gates the teardown-path DataVolume lookup. Without it EVERY
        container-service delete pays an extra synchronous kubectl (inside an
        async handler, up to a 30s timeout) and, on a cluster with no CDI,
        logs a failure every single time. That routine noise is the problem:
        the same log line is the ONLY signal that a real VM disk was left
        unreclaimable on a VM-capable cluster, so making it constant blunts
        the alarm.

        Cached because CRD registration does not change within a process
        lifetime in any way the operator needs to react to; a restart
        re-probes. Failure to probe returns True (do the lookup) — better a
        redundant call than a skipped orphan stamp.
        """
        if self._has_dv_crd is None:
            try:
                self._run_command(
                    ["get", "crd", "datavolumes.cdi.kubevirt.io", "-o", "name"]
                )
                self._has_dv_crd = True
            except KubectlException as e:
                # Only a definitive "not found" means absent. Forbidden or a
                # timeout leaves us unsure → assume present and let the real
                # call report properly.
                self._has_dv_crd = not (
                    "notfound" in (e.stderr or "").lower().replace(" ", "")
                    or "not found" in (e.stderr or "").lower()
                )
        return self._has_dv_crd

    @staticmethod
    def is_crd_absent_error(exc: "KubectlException") -> bool:
        """True only when kubectl failed because the resource type is unknown.

        A ``KubectlException`` is raised for EVERY kubectl failure —
        RBAC Forbidden, a 30s timeout, a missing binary — so callers that
        want to treat "this cluster has no KubeVirt" as a benign, expected
        condition MUST narrow to this. Treating the whole exception class as
        "no CRD" turns a Forbidden into "no VM workload", which is how a
        VM-type service silently fails to suspend.

        ⚠ This is a stderr string match because kubectl gives no machine
        readable discriminator for it. It is deliberately narrow: anything
        unrecognised is NOT a CRD-absent error, so the caller re-raises.
        Cross-checked by an integration test that asserts a known built-in
        type never matches while an unknown type always does.
        """
        stderr = (exc.stderr or "").lower()
        return (
            "the server doesn't have a resource type" in stderr
            or "could not find the requested resource" in stderr
            # kubectl >= 1.29 wording for an unknown type in an explain/get
            or "doesn't have a resource type" in stderr
        )

    def get_vmis(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[VmiInfo]:
        """Get KubeVirt VirtualMachineInstances in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector

        Returns:
            List of VMI information

        Raises:
            KubectlException: If the CRD is absent or the command fails —
                see the fail-loud note above; do NOT convert this to [].
        """
        validate_namespace(namespace)

        args = [
            "get",
            "virtualmachineinstances",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        vmis_data = json.loads(result.stdout)

        vmis = []
        for vmi in vmis_data.get("items", []):
            metadata = vmi.get("metadata", {})
            status = vmi.get("status", {})

            conditions = {
                c.get("type"): c.get("status")
                for c in status.get("conditions", []) or []
            }

            vmis.append(
                VmiInfo(
                    name=metadata.get("name", ""),
                    phase=status.get("phase", "Unknown"),
                    # Ready is a separate condition from phase — see VmiInfo.
                    ready=conditions.get("Ready") == "True",
                    node_name=status.get("nodeName"),
                    agent_connected=conditions.get("AgentConnected") == "True",
                )
            )

        return vmis

    def get_datavolumes(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[DataVolumeInfo]:
        """Get CDI DataVolumes in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector

        Returns:
            List of DataVolume information (phase + import progress)

        Raises:
            KubectlException: If the CRD is absent or the command fails.
        """
        validate_namespace(namespace)

        args = [
            "get",
            "datavolumes",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        dvs_data = json.loads(result.stdout)

        return [
            DataVolumeInfo(
                name=dv.get("metadata", {}).get("name", ""),
                phase=dv.get("status", {}).get("phase", "Unknown"),
                progress=dv.get("status", {}).get("progress"),
            )
            for dv in dvs_data.get("items", [])
        ]

    def get_virtualmachines(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get KubeVirt VirtualMachines (the CR, not the running instance).

        Shape mirrors ``get_deployments`` / ``get_statefulsets`` so the scale
        endpoint can treat all three workload kinds uniformly.

        Raises:
            KubectlException: If the CRD is absent or the command fails.
        """
        validate_namespace(namespace)

        args = [
            "get",
            "virtualmachines",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        vms_data = json.loads(result.stdout)

        return [
            {
                "name": vm.get("metadata", {}).get("name", ""),
                "namespace": namespace,
                "run_strategy": vm.get("spec", {}).get("runStrategy"),
            }
            for vm in vms_data.get("items", [])
        ]

    def patch_vm_run_strategy(
        self, namespace: str, vm_name: str, run_strategy: str
    ) -> None:
        """Set a VirtualMachine's runStrategy (the VM analogue of scaling).

        ``Halted`` stops the guest but keeps the VM definition and its disk;
        ``Always`` starts it back up. This is the VM-shaped mapping of the
        subscription suspend/resume path — a VM has no ``replicas`` to scale.

        ⚠ Surgical patch, NOT ``helm upgrade``: suspend/resume sits on the
        billing path and re-rendering the chart would also re-evaluate the
        DataVolume. The chart therefore ALSO exposes ``vm.runStrategy`` as a
        value, and the Odoo side feeds the current suspend state into it, so
        a later ``helm upgrade`` cannot silently wake a suspended (unpaid)
        VM back up. Both paths must stay in sync — see design §4.2.

        Args:
            namespace: Namespace name
            vm_name: VirtualMachine name
            run_strategy: ``Halted`` or ``Always``

        Raises:
            ValueError: If run_strategy is not one of the accepted values.
            KubectlException: If the patch fails.
        """
        validate_namespace(namespace)

        # Allow-list rather than pass-through: this value lands in a JSON
        # patch built by string interpolation, and KubeVirt silently ignores
        # an unknown runStrategy (leaving the VM in its previous state while
        # the API reports success).
        if run_strategy not in ("Halted", "Always"):
            raise ValueError(
                f"Unsupported runStrategy {run_strategy!r} "
                "(expected 'Halted' or 'Always')"
            )

        patch = json.dumps({"spec": {"runStrategy": run_strategy}})

        self._run_command([
            "patch",
            "virtualmachine",
            vm_name,
            "--namespace",
            namespace,
            "--type=merge",
            "--patch",
            patch,
        ])

    def apply_manifest(self, manifest: dict) -> Dict[str, Any]:
        """Apply a Kubernetes manifest via kubectl apply.

        Args:
            manifest: Kubernetes resource manifest dict

        Returns:
            Applied resource info

        Raises:
            KubectlException: If apply fails
        """
        import yaml

        # #284 W0-hardening: apply_manifest is a mutate choke-point but the namespace
        # lives inside the manifest, so it was NOT covered by the entry-point
        # validate_namespace that guards the sibling shell-out methods. Validate it
        # here too — a Namespace-kind manifest carries the ns in metadata.name; every
        # other (namespaced) resource in metadata.namespace. (RBAC does not enforce
        # the prefix, so this single app layer is the only tenant boundary.)
        meta = manifest.get("metadata") or {}
        if manifest.get("kind") == "Namespace":
            validate_namespace(meta.get("name") or "")
        elif meta.get("namespace"):
            validate_namespace(meta["namespace"])

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(manifest, f)
            manifest_file = f.name

        try:
            result = self._run_command(["apply", "-f", manifest_file, "--output", "json"])
            return json.loads(result.stdout)
        finally:
            Path(manifest_file).unlink(missing_ok=True)

    def delete_service(self, namespace: str, service_name: str) -> bool:
        """Delete a Kubernetes Service.

        Args:
            namespace: Namespace name
            service_name: Name of the service to delete

        Returns:
            True if deleted, False if not found

        Raises:
            KubectlException: If deletion fails (other than not found)
        """
        validate_namespace(namespace)

        try:
            self._run_command([
                "delete", "service", service_name,
                "--namespace", namespace,
                "--ignore-not-found",
            ])
            return True
        except KubectlException as e:
            if "not found" in e.stderr.lower():
                return False
            raise

    def delete_namespace(self, namespace: str) -> bool:
        """Delete a namespace (two-step teardown §8.4.1 step 2).

        Removes the namespace AND everything left inside it that the helm
        uninstall did not own — PVC, Secret (incl. the plaintext LLM Secret
        ``{release}-llm`` per §8.4.2), NetworkPolicy. Always called AFTER
        ``HelmService.uninstall`` so the helm release-state Secret is gone
        before the namespace it lives in is removed.

        Fire-and-return (``--wait=false``): K8s marks the namespace
        ``Terminating`` and reaps contained objects in the background, so the
        caller is not blocked on finalizers. Idempotent via
        ``--ignore-not-found``.

        Returns:
            True if the delete was issued (or the namespace was already gone).

        Raises:
            KubectlException: deletion failed for a reason other than not-found.
        """
        validate_namespace(namespace)

        try:
            self._run_command([
                "delete", "namespace", namespace,
                "--ignore-not-found",
                "--wait=false",
            ])
            return True
        except KubectlException as e:
            if "not found" in e.stderr.lower():
                return True
            raise

    def list_pvcs(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[str]:
        """List PVC names in a namespace, optionally scoped by a label selector.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector. Teardown passes
                ``app.kubernetes.io/instance={release}`` so orphan-labeling only
                touches the deleted release's OWN PVCs — a per-workspace ns is
                shared by multiple services, and an unscoped list would sweep
                other live services' data PVCs into the cron's delete set (#281).

        Returns:
            List of PVC names
        """
        validate_namespace(namespace)

        args = [
            "get", "pvc",
            "--namespace", namespace,
            "-o", "jsonpath={.items[*].metadata.name}",
        ]
        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)

        output = result.stdout.strip()
        if not output:
            return []
        return output.split()

    #: #1513：一次 server dry-run 的上限。逾時＝「不知道」（呼叫端放行），所以寧短勿長——
    #: 這段時間會疊加在升級（含 reset 密碼）的預算上（reset-async-budget-ladder）。
    PVC_RESIZE_DRY_RUN_TIMEOUT_SECONDS = 10

    def dry_run_pvc_resize(self, namespace: str, name: str, size: str) -> None:
        """#1513：問 API server「把這顆 PVC 加大到 ``size`` 會不會被接受」，什麼都不改。

        ``kubectl patch --dry-run=server`` 會跑完整的 admission 鏈：Longhorn 的
        ``validator.longhorn.io`` 宣告 ``sideEffects: None``，dry-run 照樣呼叫它，所以
        「replica 所在磁碟放不放得下增量」由 Longhorn 自己判斷（``CheckReplicasSizeExpansion``），
        operator 不必重寫排程規則、也不需要讀 ``replicas.longhorn.io`` 的權限。

        Raises:
            KubectlException: 被拒絕或 kubectl 失敗（解讀在
                ``upgrade_preflight.classify_expansion_rejection``）。
            ValueError: namespace 不在允許範圍。
        """
        validate_namespace(namespace)
        patch = json.dumps({"spec": {"resources": {"requests": {"storage": size}}}},
                           separators=(",", ":"))
        self._run_command(
            ["patch", "pvc", name, "--namespace", namespace, "--type", "merge",
             "--patch", patch, "--dry-run=server", "--output", "name"],
            timeout=self.PVC_RESIZE_DRY_RUN_TIMEOUT_SECONDS,
        )

    def get_longhorn_volume(self, volume_name: str) -> Dict[str, Any]:
        """#1513：讀一顆 Longhorn volume（``volumes.longhorn.io``，唯讀）。

        operator ClusterRole 需要 longhorn.io ``volumes`` get（woow-paas-charts
        charts/paas-operator）；沒有時 raise，呼叫端當「不知道」放行。

        Raises:
            KubectlException: 不存在、權限不足、逾時。
        """
        result = self._run_command(
            ["get", "volumes.longhorn.io", volume_name,
             "--namespace", settings.longhorn_namespace, "-o", "json"],
            timeout=self.PVC_RESIZE_DRY_RUN_TIMEOUT_SECONDS,
        )
        return json.loads(result.stdout or "{}")

    #: #1647：工作區頁同步讀 quota 的 kubectl 上限。階梯：前端 15s ＞ 平台 (3, 8)s ＞ 本常數。
    NS_QUOTA_READ_TIMEOUT_SECONDS = 5

    def get_resource_quotas(self, namespace: str, timeout: int = 30) -> List[Dict[str, Any]]:
        """#1647：namespace 的 ResourceQuota（原始 JSON items）。純唯讀、一次 kubectl get。

        namespace 還不存在時回空 list（kubectl 對不存在的 ns 做 list 不報錯）。hard／used
        的解讀在 ``namespace_quota.quota_report``。

        Raises:
            KubectlException: kubectl 失敗、權限不足、逾時。
            ValueError: namespace 不在允許範圍，或 kubectl 的輸出不是 JSON
                （``json.JSONDecodeError`` 是它的子類）。兩者都原樣外拋，呼叫端自己分辨。
        """
        validate_namespace(namespace)
        result = self._run_command(
            ["get", "resourcequota", "--namespace", namespace, "-o", "json"], timeout=timeout)
        return json.loads(result.stdout or "{}").get("items", [])

    #: #1679：找不到受管 quota 時才多做的 namespace 探測上限。與上面的 quota 讀取相加
    #: （5＋3）不超過平台端的讀取逾時 8s；逾時就回「未知」，不拖垮整個讀取。
    NS_EXISTS_PROBE_TIMEOUT_SECONDS = 3

    def namespace_exists(self, namespace: str,
                         timeout: int = NS_EXISTS_PROBE_TIMEOUT_SECONDS) -> Optional[bool]:
        """#1679：namespace 是否存在。只有明確的 NotFound 回 ``False``。

        Forbidden、逾時、binary 不存在等任何其他失敗都回 ``None``（未知）——平台據此
        判斷「確認尚無 namespace」，把未知當成不存在會讓呼叫端跳過安全檢查。
        """
        validate_namespace(namespace)
        try:
            self._run_command(["get", "namespace", namespace, "-o", "name"], timeout=timeout)
        except KubectlException as exc:
            if not exc.timed_out and self.is_not_found_error(exc):
                return False
            return None
        return True

    def get_storage_quota_snapshot(
        self, namespace: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """#1348：讀 namespace 的 ResourceQuota 與 PVC（原始 JSON items），供開通前檢查。

        純唯讀（兩次 ``kubectl get``）。namespace 還不存在時兩者都回空 list（kubectl
        對不存在的 ns 做 list 不報錯）。用量的解讀在 ``storage_quota.summarize``。

        Returns:
            ``{"quotas": [...ResourceQuota items], "pvcs": [...PVC items]}``

        Raises:
            KubectlException: kubectl 失敗（呼叫端決定 fail-open）。
        """
        validate_namespace(namespace)
        quotas = self._run_command([
            "get", "resourcequota", "--namespace", namespace, "-o", "json",
        ])
        pvcs = self._run_command([
            "get", "pvc", "--namespace", namespace, "-o", "json",
        ])
        return {
            "quotas": json.loads(quotas.stdout or "{}").get("items", []),
            "pvcs": json.loads(pvcs.stdout or "{}").get("items", []),
        }

    # #1415：叢集儲存（Longhorn）排程額度。全部唯讀；解讀在 ``storage_capacity``。
    CAPACITY_KUBECTL_TIMEOUT_SECONDS = 15

    def get_longhorn_capacity_snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        """#1415：讀 Longhorn 排程需要的原始資料（三次唯讀 ``kubectl get``）。

        * ``nodes.longhorn.io``（``settings.longhorn_namespace``）：各磁碟的
          storageMaximum／Available／Scheduled、storageReserved、allowScheduling、tags；
        * ``settings.longhorn.io``：**只留** ``storage_capacity.LONGHORN_SETTING_NAMES``
          （其他設定與排程無關，不往外帶）；
        * StorageClass：provisioner、parameters（numberOfReplicas、selector）、預設註記。

        Raises:
            KubectlException: CRD 不存在、權限不足（operator ClusterRole 需要 longhorn.io
                nodes／settings get/list，見 woow-paas-charts charts/paas-operator）、逾時。
                呼叫端一律當「不知道」放行。
        """
        from src.services.storage_capacity import LONGHORN_SETTING_NAMES

        ns = settings.longhorn_namespace
        timeout = self.CAPACITY_KUBECTL_TIMEOUT_SECONDS
        nodes = self._run_command(
            ["get", "nodes.longhorn.io", "--namespace", ns, "-o", "json"], timeout=timeout)
        lh_settings = self._run_command(
            ["get", "settings.longhorn.io", "--namespace", ns, "-o", "json"], timeout=timeout)
        classes = self._run_command(["get", "storageclasses", "-o", "json"], timeout=timeout)
        wanted = set(LONGHORN_SETTING_NAMES)
        return {
            "nodes": json.loads(nodes.stdout or "{}").get("items", []),
            "settings": [
                item for item in json.loads(lh_settings.stdout or "{}").get("items", [])
                if (item.get("metadata") or {}).get("name") in wanted
            ],
            "storage_classes": json.loads(classes.stdout or "{}").get("items", []),
        }

    def list_orphaned_pvcs_all_namespaces(self) -> List[Dict[str, Any]]:
        """#1415：全叢集帶 ``paas.woowtech.io/orphaned-at`` 的 PVC（唯讀，供額度回報）。

        不呼 validate_namespace：這是跨 namespace 的唯讀盤點，範圍由 label selector 圈定
        （同 ``list_managed_namespaces``）。
        """
        from src.services.storage_quota import ORPHANED_AT_LABEL

        result = self._run_command(
            ["get", "pvc", "--all-namespaces", "-l", ORPHANED_AT_LABEL, "-o", "json"],
            timeout=self.CAPACITY_KUBECTL_TIMEOUT_SECONDS,
        )
        return json.loads(result.stdout or "{}").get("items", [])

    def list_orphaned_release_resources(
        self, namespace: str, release_name: str
    ) -> Dict[str, List[str]]:
        """#1157：namespace 內屬於 ``release_name`` 且帶孤兒標記的 PVC／DataVolume（唯讀）。

        選取條件＝uninstall 時 ``build_orphan_pvc_labels`` 打上的兩個 label：
        ``paas.woowtech.io/release-name=<release>`` **且**有 ``paas.woowtech.io/orphaned-at``。
        同名重裝會讓 helm 靜默接回這些資源，而回收 CronJob 只看孤兒標記 ⇒ 保留期一到就刪掉
        活站資料；``create_release`` 據此回 409（#1157 裁決 A）。

        叢集沒有 CDI（DataVolume CRD 不存在）時 ``datavolumes`` 回空 list；其他 kubectl 失敗
        一律 raise，由呼叫端決定（install 前的檢查走 fail-closed）。

        Returns:
            ``{"pvcs": [...names], "datavolumes": [...names]}``

        Raises:
            ValueError: namespace 越界，或 release name 不是合法的 label 值（會改變 selector 語意）。
            KubectlException: 查詢失敗（CDI 不存在除外）。
        """
        from src.services.storage_quota import ORPHANED_AT_LABEL, RELEASE_NAME_LABEL

        validate_namespace(namespace)
        # release name 直接拼進 selector：含 ',' 等字元會變成另一個 selector，
        # 先用 label 值語法擋掉（合法的 helm release name 一定也是合法的 label 值）。
        validate_label_value(RELEASE_NAME_LABEL, release_name)
        selector = f"{RELEASE_NAME_LABEL}={release_name},{ORPHANED_AT_LABEL}"

        pvcs = self.list_pvcs(namespace, label_selector=selector)
        datavolumes: List[str] = []
        if self.has_datavolume_crd():
            try:
                datavolumes = self.list_datavolumes(namespace, label_selector=selector)
            except KubectlException as e:
                if not self.is_crd_absent_error(e):
                    raise
        return {"pvcs": pvcs, "datavolumes": datavolumes}

    #: #1647：compute 配額預檢的一次讀取上限。逾時＝「不知道」（呼叫端放行）。
    COMPUTE_QUOTA_SNAPSHOT_TIMEOUT_SECONDS = 10

    def get_compute_quota_snapshot(self, namespace: str) -> Dict[str, List[Dict[str, Any]]]:
        """#1647：一次讀 namespace 的 ResourceQuota、LimitRange、Pod（原始 JSON items）。純唯讀。

        一次 ``kubectl get`` 讀三種 kind：quota 的 ``status.used`` 與 pod 清單取自同一個時間點
        附近，兩者的落差越小，預檢越不會被過期的用量誤導。解讀在 ``compute_quota.evaluate``。

        Returns:
            ``{"quotas": [...], "limitranges": [...], "pods": [...]}``

        Raises:
            KubectlException: kubectl 失敗、權限不足、逾時（呼叫端一律當「不知道」放行）。
            ValueError: namespace 不在允許範圍；或 kubectl 的輸出不是預期的 List 形狀
                （不是 JSON、頂層不是物件、``items`` 不是陣列）——形狀不對就不回一份
                「什麼都沒有」的 snapshot，那會被解讀成「沒有 quota」。
        """
        validate_namespace(namespace)
        result = self._run_command(
            ["get", "resourcequotas,limitranges,pods", "--namespace", namespace, "-o", "json"],
            timeout=self.COMPUTE_QUOTA_SNAPSHOT_TIMEOUT_SECONDS)
        payload = json.loads(result.stdout or "{}")
        if not isinstance(payload, dict):
            raise ValueError("unexpected kubectl output: the top level is not an object")
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise ValueError("unexpected kubectl output: items is not a list")
        by_kind = {"ResourceQuota": [], "LimitRange": [], "Pod": []}
        for item in items:
            if isinstance(item, dict) and item.get("kind") in by_kind:
                by_kind[item["kind"]].append(item)
        return {"quotas": by_kind["ResourceQuota"], "limitranges": by_kind["LimitRange"],
                "pods": by_kind["Pod"]}

    # ── woow_paas_platform #1540：限時唯讀資料取回入口（純規則在 data_retrieval）──────────

    RETRIEVAL_KUBECTL_TIMEOUT_SECONDS = 20

    def get_items(
        self, kinds: str, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """``kubectl get <kinds> -o json`` 的 items（namespace 先過前綴驗證）。"""
        validate_namespace(namespace)
        args = ["get", kinds, "--namespace", namespace, "--output", "json"]
        if label_selector:
            args.extend(["--selector", label_selector])
        result = self._run_command(args, timeout=self.RETRIEVAL_KUBECTL_TIMEOUT_SECONDS)
        items = json.loads(result.stdout or "{}").get("items")
        return items if isinstance(items, list) else []

    def helm_release_exists(self, namespace: str, release: str) -> bool:
        """release 的 helm 狀態 Secret（任一 status）還在不在——與 pvc-cleanup 的 live guard 同一判準。"""
        validate_namespace(namespace)
        validate_label_value("name", release)
        result = self._run_command(
            ["get", "secret", "--namespace", namespace,
             "--selector", f"owner=helm,name={release}", "--output", "name"],
            timeout=self.RETRIEVAL_KUBECTL_TIMEOUT_SECONDS,
        )
        return bool((result.stdout or "").strip())

    def create_manifests(self, manifests: List[Dict[str, Any]]) -> None:
        """依序 ``kubectl create -f -``（內容走 stdin，不上 argv；撞名 raise AlreadyExists）。"""
        for manifest in manifests:
            meta = manifest.get("metadata") or {}
            validate_namespace(meta.get("namespace") or "")
            self._run_command(
                ["create", "-f", "-", "--output", "name"],
                input_data=json.dumps(manifest),
                timeout=self.RETRIEVAL_KUBECTL_TIMEOUT_SECONDS,
            )

    def delete_by_selector(self, kinds: str, namespace: str, label_selector: str) -> None:
        """刪掉 namespace 內符合 selector 的物件（冪等、不等 finalizer）。selector 不可為空。"""
        validate_namespace(namespace)
        if not label_selector:
            raise ValueError("delete_by_selector refuses an empty label selector")
        self._run_command(
            ["delete", kinds, "--namespace", namespace, "--selector", label_selector,
             "--ignore-not-found", "--wait=false"],
            timeout=self.RETRIEVAL_KUBECTL_TIMEOUT_SECONDS,
        )

    def list_items_all_namespaces(self, kind: str, label_selector: str) -> List[Dict[str, Any]]:
        """跨 namespace 唯讀盤點（範圍由 selector 圈定；呼叫端再逐筆過 ``validate_namespace``）。"""
        if not label_selector:
            raise ValueError("list_items_all_namespaces refuses an empty label selector")
        result = self._run_command(
            ["get", kind, "--all-namespaces", "--selector", label_selector, "--output", "json"],
            timeout=self.RETRIEVAL_KUBECTL_TIMEOUT_SECONDS,
        )
        items = json.loads(result.stdout or "{}").get("items")
        return items if isinstance(items, list) else []

    def get_cdi_filesystem_overhead(self) -> Optional[Dict[str, Any]]:
        """#1415：CDIConfig 的 ``status.filesystemOverhead``（``{"global": "0.06",
        "storageClass": {...}}``）；讀不到回 None（呼叫端用 CDI 預設值）。

        叢集內 ``system:authenticated`` 有 ``cdi.kubevirt.io:config-reader``，不需要另給 RBAC。
        """
        try:
            result = self._run_command(
                ["get", "cdiconfig", "config", "-o", "json"],
                timeout=self.CAPACITY_KUBECTL_TIMEOUT_SECONDS,
            )
            overhead = (json.loads(result.stdout or "{}").get("status") or {}).get(
                "filesystemOverhead")
            return overhead if isinstance(overhead, dict) else None
        except (KubectlException, ValueError) as exc:
            logger.warning("CDI filesystem overhead unavailable, using CDI default: %s",
                           getattr(exc, "stderr", None) or exc)
            return None

    def list_managed_namespaces(self) -> List[str]:
        """#277 唯讀反向發現：列本 env 帶 paas-managed=true 的 ns 名。

        不呼 validate_namespace（反向發現全部、無單一輸入 ns 可驗），邊界靠 label
        selector 圈定。純唯讀 get，供 reconcile inventory 當右集 ground truth。

        **env-scoped**：prod/stg 共用同一 cluster，故 PAAS_ENV 有設時 AND
        ``paas.woowtech.io/env=<env>``，只回自 env 的 ns（否則 stg inventory 會回
        prod 的 paas-ws-* → 反向對帳把別 env 活躍 ns 灌成孤兒噪音）。ns 是 env-labeled
        資源（與帳號級共用的 CF 資源不同），可在 source 分 env；未帶 env label 的舊 ns
        被略過＝safe-miss（少報，非誤報）。
        """
        selector = "paas-managed=true"
        if settings.paas_env:
            selector += ",paas.woowtech.io/env=%s" % settings.paas_env
        result = self._run_command([
            "get", "ns", "-l", selector, "-o", "json",
        ])
        data = json.loads(result.stdout or "{}")
        return [
            item["metadata"]["name"]
            for item in data.get("items", [])
            if item.get("metadata", {}).get("name")
        ]

    def label_pvcs(
        self, namespace: str, pvc_names: List[str], labels: Dict[str, str]
    ) -> None:
        """Add labels to PVCs.

        Args:
            namespace: Namespace name
            pvc_names: List of PVC names to label
            labels: Dict of label key-value pairs
        """
        validate_namespace(namespace)

        # Reject illegal values up front — all labels go in ONE atomic kubectl
        # command, so a single bad value (e.g. a ':'-bearing timestamp) would
        # silently drop the entire batch (operator #208).
        for k, v in labels.items():
            validate_label_value(k, v)

        label_args = [f"{k}={v}" for k, v in labels.items()]

        for pvc_name in pvc_names:
            try:
                self._run_command([
                    "label", "pvc", pvc_name,
                    "--namespace", namespace,
                    "--overwrite",
                ] + label_args)
            except KubectlException as e:
                logger.warning(f"Failed to label PVC {pvc_name}: {e.stderr}")

    def list_datavolumes(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[str]:
        """List DataVolume names, optionally scoped by a label selector.

        Same per-release scoping rule as ``list_pvcs`` (#281): a workspace ns
        is shared, so teardown must only ever touch the deleted release's own
        objects.

        Raises:
            KubectlException: If the CDI CRD is absent or the command fails.
                Callers in the teardown path treat this as best-effort (a
                container-only cluster legitimately has no CDI) — but they
                must log it, not silently continue.
        """
        validate_namespace(namespace)

        args = [
            "get", "datavolumes",
            "--namespace", namespace,
            "-o", "jsonpath={.items[*].metadata.name}",
        ]
        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)

        output = result.stdout.strip()
        if not output:
            return []
        return output.split()

    def label_datavolumes(
        self, namespace: str, dv_names: List[str], labels: Dict[str, str]
    ) -> List[str]:
        """Add labels to DataVolumes — the VM-shaped counterpart of ``label_pvcs``.

        WHY this exists (design §4.3, proven by the W1 PoC):
        a CDI DataVolume OWNS the PVC it provisions. Labeling only the PVC is
        not enough for teardown, because the deferred cleanup cron must delete
        the **DataVolume** — deleting the PVC alone makes CDI recreate it and
        re-run the entire multi-GiB import (measured in the PoC, #423).

        Args:
            namespace: Namespace name
            dv_names: List of DataVolume names to label
            labels: Dict of label key-value pairs

        Returns:
            The names that were ACTUALLY labelled. Callers must log this list
            rather than the input: an unlabelled DataVolume survives forever
            (the chart marks it resource-policy: keep) and is never picked up
            by the cleanup cron — a silent multi-GiB storage leak, the same
            family as #208. Reporting the requested count would hide it.
        """
        validate_namespace(namespace)

        # Same up-front validation as label_pvcs: one bad value would drop the
        # whole atomic batch silently (operator #208).
        for k, v in labels.items():
            validate_label_value(k, v)

        label_args = [f"{k}={v}" for k, v in labels.items()]

        labelled: List[str] = []
        for dv_name in dv_names:
            try:
                self._run_command([
                    "label", "datavolume", dv_name,
                    "--namespace", namespace,
                    "--overwrite",
                ] + label_args)
                labelled.append(dv_name)
            except KubectlException as e:
                # error, not warning: a miss here leaks storage silently.
                logger.error(
                    f"Failed to label DataVolume {namespace}/{dv_name} — it will "
                    f"survive teardown and never be reclaimed: {e.stderr}"
                )
        return labelled

    def get_namespace_labels(self, namespace: str) -> Dict[str, str]:
        """Return the namespace's current labels ({} if it carries none)."""
        validate_namespace(namespace)
        result = self._run_command(["get", "ns", namespace, "-o", "json"])
        data = json.loads(result.stdout or "{}")
        return data.get("metadata", {}).get("labels", {}) or {}

    def reconcile_ns_reach_labels(
        self, namespace: str, desired_workspace_ids: List[int]
    ) -> Dict[str, List[str]]:
        """Reconcile an agent-runtime ns's ``woow.tw/reach-ws-<W>`` labels to EXACTLY
        the desired reachable-workspace set — adding missing keys and EXPLICITLY
        REMOVING stale ones (#279 leg-2 reconcile primitive, O3).

        ``_ensure_namespace``/``apply_manifest`` (cold-start stamping) only MERGES
        labels, so it can grow the reach set but never shrink it. Unbinding a
        CloudService therefore needs this explicit-delete path (``kubectl label ns
        <ns> <key>-``) — without it a revoked workspace stays reachable for the pod's
        whole lifetime (F1). Add + delete go in ONE atomic ``kubectl label`` command
        so a partial apply can't leave the ns half-reconciled. Idempotent: an already
        reconciled ns issues no command.

        NOTE: only ``reach-ws-*`` keys are diffed — ``paas-managed`` / env /
        ``workspace-id`` are never touched.

        Returns ``{"added": [keys], "removed": [keys]}`` for observability / tests.
        """
        validate_namespace(namespace)

        desired = {
            reach_ws_label_key(w): REACH_WS_LABEL_VALUE for w in desired_workspace_ids
        }
        current_reach = {
            k: v
            for k, v in self.get_namespace_labels(namespace).items()
            if k.startswith(REACH_WS_LABEL_PREFIX)
        }

        to_add = {k: v for k, v in desired.items() if current_reach.get(k) != v}
        to_remove = sorted(k for k in current_reach if k not in desired)

        if not to_add and not to_remove:
            return {"added": [], "removed": []}

        # trailing '-' on a key is kubectl's delete-label syntax; --overwrite lets an
        # add correct a drifted value. One atomic command for the whole diff.
        label_args = [f"{k}={v}" for k, v in to_add.items()] + [f"{k}-" for k in to_remove]
        self._run_command(["label", "ns", namespace, "--overwrite"] + label_args)

        return {"added": sorted(to_add), "removed": to_remove}

    def stamp_managed_ns_labels(
        self,
        namespace: str,
        workspace_id: Optional[int] = None,
        reach_workspace_ids: Optional[List[int]] = None,
    ) -> Dict[str, str]:
        """Re-stamp a managed namespace's #279 labels (``paas-managed`` + env +
        ``workspace-id`` and/or ``reach-ws-*``) via ``kubectl label --overwrite`` —
        the P5 BACKFILL path for namespaces created before O1/F6 that lack the env /
        workspace-id labels the NetworkPolicy selectors now depend on (a legacy CS ns
        without ``workspace-id``/env fails leg-1 egress's namespaceSelector; a legacy
        agent ns without env fails NP4's env predicate → fail-closed reach loss once
        #280 removes the chart from-less).

        Unlike ``reconcile_ns_reach_labels`` (which also DELETES stale reach-ws keys),
        this only ADDS/overwrites the managed set — a backfill must never drop a reach
        label the bind-path / cron legitimately put there. Idempotent.

        Returns the label set applied (for observability / tests).
        """
        validate_namespace(namespace)
        labels = build_managed_ns_labels(
            workspace_id=workspace_id, reach_workspace_ids=reach_workspace_ids
        )
        label_args = [f"{k}={v}" for k, v in labels.items()]
        self._run_command(["label", "ns", namespace, "--overwrite"] + label_args)
        return labels

    def get_pod(self, namespace: str, pod_name: str) -> Dict[str, Any]:
        """讀取單一 Pod 原始 metadata/spec，供 exec 候選驗證使用。"""
        validate_namespace(namespace)
        # 沿用既有 lowercase DNS subdomain 規則，不讓名稱變成 kubectl flag/resource。
        if len(pod_name) > 253 or not _K8S_LABEL_PREFIX_RE.fullmatch(pod_name):
            raise ValueError(f"Invalid Pod name: {pod_name!r}")
        result = self._run_command([
            "get", "pod", pod_name, "--namespace", namespace, "--output", "json",
        ])
        return json.loads(result.stdout)

    def get_pod_name(self, namespace: str, label_selector: str) -> str:
        """Get the first pod name matching a label selector.

        Args:
            namespace: Namespace name
            label_selector: Label selector (e.g., 'app.kubernetes.io/name=n8n')

        Returns:
            Pod name

        Raises:
            KubectlException: If no pods found or command fails
        """
        validate_namespace(namespace)

        args = [
            "get", "pods",
            "--namespace", namespace,
            "--selector", label_selector,
            "--output", "jsonpath={.items[0].metadata.name}",
        ]

        result = self._run_command(args)
        pod_name = result.stdout.strip()
        if not pod_name:
            raise KubectlException(
                message=f"No pods found matching selector '{label_selector}'",
                command=" ".join(args),
                stderr="",
            )
        return pod_name

    def get_deployment_names(
        self, namespace: str, label_selector: str
    ) -> List[str]:
        """Resolve **every** Deployment name matching a label selector.

        #449: multi-Deployment charts are the norm, not the exception —
        ``hermes-agent`` and ``open-design`` both render a daemon Deployment
        plus a console Deployment sharing one ``app.kubernetes.io/instance``
        label. A password rotation carried by the chart Secret has to restart
        all of them, so the singular ``get_deployment_name`` (which fails loudly
        on >1) cannot be the primitive here.

        Args:
            namespace: Namespace name
            label_selector: Label selector (e.g. 'app.kubernetes.io/instance=svc-abc')

        Returns:
            Deployment names, in the order the API server returned them.

        Raises:
            KubectlException: If no Deployment matches. Zero matches stays loud:
                silently restarting nothing would report a rotation that never
                reached a pod.
        """
        validate_namespace(namespace)

        args = [
            "get", "deployments",
            "--namespace", namespace,
            "--selector", label_selector,
            "--output", "jsonpath={.items[*].metadata.name}",
        ]

        result = self._run_command(args)
        names = result.stdout.split()
        if not names:
            raise KubectlException(
                message=f"No deployments found matching selector '{label_selector}'",
                command=" ".join(args),
                stderr="",
            )
        return names

    def get_credential_reset_statefulset_names(
        self, namespace: str, label_selector: str
    ) -> List[str]:
        """StatefulSets of a release that opted in to a restart on credential reset.

        hermes-agent 0.3.0 runs its agent as a StatefulSet (fixed pod name so the
        console Role can pin pods/exec with resourceNames — platform #1360 follow-up)
        and reads admin_password from env at start, so a helm_secret reset must roll
        it like the Deployments; headscale/tailscale 0.1.1 are StatefulSet-only
        charts that read it the same way. Only StatefulSets labelled
        ``CREDENTIAL_RESET_RESTART_LABEL=true`` are returned: open-design 0.3.0's daemon
        StatefulSet hot-reloads its htpasswd and must NOT be restarted, and a bundled
        database StatefulSet has nothing to do with the admin password.

        Returns an empty list when nothing matches (unlike ``get_deployment_names``):
        the caller decides whether "no workload at all" is an error.
        """
        validate_namespace(namespace)

        args = [
            "get", "statefulsets",
            "--namespace", namespace,
            "--selector", f"{label_selector},{CREDENTIAL_RESET_RESTART_LABEL}=true",
            "--output", "jsonpath={.items[*].metadata.name}",
        ]
        result = self._run_command(args)
        return result.stdout.split()

    def get_deployment_name(self, namespace: str, label_selector: str) -> str:
        """Resolve **exactly one** Deployment name from a label selector.

        A Helm release name is NOT its Deployment name: the chart fullname
        renders ``{release}-{chart}``. Callers must discover the real name via
        the ``app.kubernetes.io/instance`` label rather than assume they match.

        Args:
            namespace: Namespace name
            label_selector: Label selector (e.g. 'app.kubernetes.io/instance=svc-abc')

        Returns:
            Deployment name

        Raises:
            KubectlException: If no Deployment matches, or if more than one does.
                An ambiguous selector fails loudly instead of restarting an
                arbitrary workload — unlike ``get_pod_name``, which takes
                ``.items[0]`` because a release's pods are interchangeable
                replicas. Callers that legitimately span several Deployments
                (#449 helm_secret reset) must use ``get_deployment_names``
                instead of loosening this contract.
        """
        names = self.get_deployment_names(namespace, label_selector)
        if len(names) > 1:
            raise KubectlException(
                message=(
                    f"Ambiguous selector '{label_selector}': expected exactly one "
                    f"deployment, found {len(names)} ({', '.join(names)})"
                ),
                command=(
                    f"get deployments --namespace {namespace} "
                    f"--selector {label_selector} "
                    "--output jsonpath={.items[*].metadata.name}"
                ),
                stderr="",
            )
        return names[0]

    def exec_in_pod(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        command: List[str],
        timeout: int = 30,
        input_data: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Execute a command in a pod container.

        Args:
            namespace: Namespace name
            pod_name: Pod name
            container: Container name (empty → kubectl default = first container)
            command: Command to execute
            timeout: Timeout in seconds

        Returns:
            CompletedProcess object

        Raises:
            KubectlException: If command fails
        """
        validate_namespace(namespace)

        args = ["exec", pod_name, "--namespace", namespace]
        # kubectl ``--container <empty>`` is rejected; fall through to its default
        # (first container) when caller doesn't pin one — needed for pods whose
        # release name matches the chart main container but we don't want to
        # hardcode a per-chart container name in this generic helper.
        if container:
            args.extend(["--container", container])
        if input_data is not None:
            args.append("--stdin")
        args.append("--")
        args.extend(command)

        try:
            return self._run_command(
                args,
                input_data=input_data,
                timeout=timeout,
            )
        except KubectlException as e:
            raise KubectlException(
                message=f"kubectl exec failed: {e.message}",
                command=e.command,
                stderr=e.stderr,
                timed_out=e.timed_out,
                stdout=e.stdout,
            ) from e

    def patch_secret(
        self,
        namespace: str,
        secret_name: str,
        data: Dict[str, str],
    ) -> Dict[str, Any]:
        """Patch a Kubernetes Secret with new data.

        Args:
            namespace: Namespace name
            secret_name: Secret name
            data: Dict of key -> base64-encoded value

        Returns:
            Patched secret info
        """
        validate_namespace(namespace)

        import base64

        patch_data = {
            "data": {
                k: base64.b64encode(v.encode()).decode() if not self._is_base64(v) else v
                for k, v in data.items()
            }
        }

        args = [
            "patch", "secret", secret_name,
            "--namespace", namespace,
            "--type=merge",
            f"--patch={json.dumps(patch_data)}",
        ]

        result = self._run_command(args)
        return {"message": f"Secret {secret_name} patched"}

    def patch_configmap(
        self,
        namespace: str,
        configmap_name: str,
        data: Dict[str, str],
    ) -> Dict[str, Any]:
        """Merge-patch a ConfigMap's ``data`` field.

        Touches ONLY ``data`` (strategic merge), so existing labels / annotations
        — including the Helm ownership metadata (``app.kubernetes.io/managed-by:
        Helm`` + ``meta.helm.sh/release-*``) that the chart-created ConfigMap
        carries — are preserved. A full ``kubectl apply`` would replace the object
        and strip that metadata, breaking the next ``helm upgrade`` with an
        ownership conflict.
        """
        validate_namespace(namespace)

        patch_data = {"data": data}
        args = [
            "patch", "configmap", configmap_name,
            "--namespace", namespace,
            "--type=merge",
            f"--patch={json.dumps(patch_data)}",
        ]

        self._run_command(args)
        return {"message": f"ConfigMap {configmap_name} patched"}

    @staticmethod
    def _is_base64(s: str) -> bool:
        """Check if a string is already base64-encoded."""
        import base64
        try:
            decoded = base64.b64decode(s, validate=True)
            return base64.b64encode(decoded).decode() == s
        except Exception:
            return False

    def patch_container_env(
        self,
        namespace: str,
        deployment_name: str,
        container_index: int,
        env_name: str,
        value: str,
    ) -> Dict[str, Any]:
        """Patch a specific environment variable in a deployment container.

        Uses JSON patch to update or add an env var by finding its index,
        or appending if it doesn't exist.

        Args:
            namespace: Namespace name
            deployment_name: Deployment name
            container_index: Index of the container in the pod spec
            env_name: Environment variable name
            value: New value

        Returns:
            Patch result
        """
        validate_namespace(namespace)

        # First, get the current env vars to find the index
        args = [
            "get", "deployment", deployment_name,
            "--namespace", namespace,
            "--output", f"jsonpath={{.spec.template.spec.containers[{container_index}].env}}",
        ]

        result = self._run_command(args)
        env_list = json.loads(result.stdout) if result.stdout.strip() else []

        # Find existing env var index
        env_index = None
        for i, env in enumerate(env_list):
            if env.get("name") == env_name:
                env_index = i
                break

        if env_index is not None:
            # Update existing
            patch = json.dumps([{
                "op": "replace",
                "path": f"/spec/template/spec/containers/{container_index}/env/{env_index}/value",
                "value": value,
            }])
        else:
            # Add new env var
            if env_list:
                patch = json.dumps([{
                    "op": "add",
                    "path": f"/spec/template/spec/containers/{container_index}/env/-",
                    "value": {"name": env_name, "value": value},
                }])
            else:
                patch = json.dumps([{
                    "op": "add",
                    "path": f"/spec/template/spec/containers/{container_index}/env",
                    "value": [{"name": env_name, "value": value}],
                }])

        return self.patch_deployment(
            namespace=namespace,
            deployment_name=deployment_name,
            patch=patch,
            patch_type="json",
        )

    def rollout_restart_deployment(
        self,
        namespace: str,
        deployment_name: str,
    ) -> None:
        """Perform a rolling restart of a deployment.

        This triggers a new rollout by patching the pod template annotation
        with the current timestamp, causing K8s to recreate all pods.

        Args:
            namespace: Namespace name
            deployment_name: Deployment name

        Raises:
            KubectlException: If rollout restart fails
        """
        validate_namespace(namespace)
        self._run_command([
            "rollout", "restart",
            f"deployment/{deployment_name}",
            "--namespace", namespace,
        ])

    def rollout_restart_statefulset(
        self,
        namespace: str,
        statefulset_name: str,
    ) -> None:
        """``kubectl rollout restart statefulset/<name>`` (pod template annotation bump).

        #1432: open-design's daemon is a StatefulSet; restarting only Deployments
        left it untouched through 17 helm revisions in R8 (restartCount 0).
        """
        validate_namespace(namespace)
        self._run_command([
            "rollout", "restart",
            f"statefulset/{statefulset_name}",
            "--namespace", namespace,
        ])

    def rollout_status_deployment(
        self,
        namespace: str,
        deployment_name: str,
        timeout_seconds: int = 120,
    ) -> None:
        """Wait until a deployment rollout has completed successfully."""
        validate_namespace(namespace)
        self._run_command(
            [
                "rollout", "status",
                f"deployment/{deployment_name}",
                "--namespace", namespace,
                f"--timeout={timeout_seconds}s",
            ],
            timeout=timeout_seconds + 10,
        )

    @staticmethod
    def _calculate_age(created_timestamp: str) -> str:
        """Calculate age from creation timestamp."""
        if not created_timestamp:
            return "unknown"

        from datetime import datetime, timezone

        try:
            created = datetime.fromisoformat(created_timestamp.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - created

            if delta.days > 0:
                return f"{delta.days}d"
            elif delta.seconds >= 3600:
                return f"{delta.seconds // 3600}h"
            elif delta.seconds >= 60:
                return f"{delta.seconds // 60}m"
            else:
                return f"{delta.seconds}s"
        except Exception:
            return "unknown"

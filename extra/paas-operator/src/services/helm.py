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
from typing import Any, Dict, List, Optional

from src.config import settings
from src.models.schemas import (
    DataVolumeInfo,
    PodInfo,
    PodPhase,
    ReleaseInfo,
    ReleaseRevision,
    ReleaseStatus,
    VmiInfo,
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


def _pvc_keep_post_renderer_args() -> List[str]:
    """Helm CLI flags to run the PVC-keep post-renderer (shared by install/upgrade)."""
    return [
        "--post-renderer", sys.executable,
        "--post-renderer-args", _PVC_KEEP_POST_RENDERER,
    ]

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
# ``"password": "v"`` and its backslash-escaped twin ``\"password\": \"v\"`` —
# the n8n script embeds the request body as an escaped JSON string literal.
_INLINE_SECRET_RE = re.compile(
    r'(\\?"[\w.\-]*(?:' + "|".join(_PAYLOAD_KEY_HINTS) + r')[\w.\-]*\\?"\s*:\s*\\?")'
    r'([^"\\]*)'
    r'(\\?")',
    re.IGNORECASE,
)


def _redact_inline_secrets(text: str) -> str:
    """Mask ``"<secret-ish key>": "<value>"`` pairs embedded in a free-form token."""
    return _INLINE_SECRET_RE.sub(r"\1<redacted>\3", text)


def _mask_payload_tree(node: Any, opaque: bool = False) -> Any:
    """Mask secret leaves of a parsed JSON payload, keeping keys visible.

    ``opaque`` propagates into ``data`` / ``stringData`` subtrees, where every
    value is masked regardless of its key name.
    """
    if isinstance(node, dict):
        masked = {}
        for key, value in node.items():
            key_lower = str(key).lower()
            if opaque or any(h in key_lower for h in _PAYLOAD_KEY_HINTS):
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
    """Validate that namespace starts with allowed prefix."""
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


class HelmException(Exception):
    """Exception raised when Helm command fails."""

    def __init__(self, message: str, command: str, stderr: str):
        self.message = message
        self.command = command
        self.stderr = stderr
        super().__init__(self.message)


class KubectlException(Exception):
    """Exception raised when kubectl command fails."""

    def __init__(self, message: str, command: str, stderr: str):
        self.message = message
        self.command = command
        self.stderr = stderr
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

        except subprocess.TimeoutExpired as e:
            raise HelmException(
                message=f"Helm command timed out after {effective_timeout}s",
                command=safe_cmd,
                stderr=str(e),
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
    ) -> ReleaseInfo:
        """Install a Helm chart.

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

        Returns:
            Release information

        Raises:
            ValueError: If namespace is invalid
            HelmException: If installation fails
        """
        validate_namespace(namespace)

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
        args.extend(_pvc_keep_post_renderer_args())

        if create_namespace:
            args.append("--create-namespace")

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
            result = self._run_command(args)
            release_data = json.loads(result.stdout)
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
    ) -> ReleaseInfo:
        """Upgrade a Helm release.

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
        args.extend(_pvc_keep_post_renderer_args())

        if version:
            args.extend(["--version", version])

        if reset_values:
            args.append("--reset-values")
        elif reuse_values:
            args.append("--reuse-values")
        if atomic:
            args.append("--atomic")
        # #461: omitted when None so every pre-existing caller keeps helm's 5m
        # default verbatim (behaviour-neutral); only the reset path bounds it.
        if timeout_seconds is not None:
            args.extend(["--timeout", f"{timeout_seconds}s"])

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

        result = self._run_command(args)
        return {"message": result.stdout.strip()}

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
        # Extract chart name without version (e.g., "postgresql-18.2.3" -> "postgresql")
        chart_with_version = data.get("chart", "")
        chart_name = chart_with_version.rsplit("-", 1)[0] if "-" in chart_with_version else chart_with_version

        return ReleaseInfo(
            name=data.get("name", ""),
            namespace=data.get("namespace", ""),
            revision=int(data.get("revision", 0)),
            status=ReleaseStatus(data.get("status", "unknown").lower()),
            chart=chart_name,
            app_version=data.get("app_version", ""),
            updated=data.get("updated", ""),
            description=None,
            values=None,
        )


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
                )

            return result

        except subprocess.TimeoutExpired as e:
            raise KubectlException(
                message=f"kubectl command timed out after {timeout}s",
                command=safe_cmd,
                stderr=str(e),
            )
        except FileNotFoundError:
            raise KubectlException(
                message=f"kubectl binary not found: {self.kubectl_bin}",
                command=safe_cmd,
                stderr="",
            )

    def get_pods(self, namespace: str, label_selector: Optional[str] = None) -> List[PodInfo]:
        """Get pods in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector

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
        for pod in pods_data.get("items", []):
            metadata = pod.get("metadata", {})
            status = pod.get("status", {})

            # Calculate ready status
            container_statuses = status.get("containerStatuses", [])
            ready_count = sum(1 for c in container_statuses if c.get("ready"))
            total_count = len(container_statuses)
            ready_str = f"{ready_count}/{total_count}"

            # Calculate restarts
            restarts = sum(c.get("restartCount", 0) for c in container_statuses)

            # Calculate age
            created = metadata.get("creationTimestamp", "")
            age = self._calculate_age(created)

            pods.append(
                PodInfo(
                    name=metadata.get("name", ""),
                    phase=PodPhase(status.get("phase", "Unknown")),
                    ready=ready_str,
                    restarts=restarts,
                    age=age,
                )
            )

        return pods

    def create_namespace(
        self,
        name: str,
        cpu_limit: str,
        memory_limit: str,
        storage_limit: str,
        workspace_id: Optional[int] = None,
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
        quota_manifest = {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {
                "name": f"{name}-quota",
                "namespace": name,
            },
            "spec": {
                "hard": {
                    "requests.cpu": cpu_limit,
                    "requests.memory": memory_limit,
                    "limits.cpu": cpu_limit,
                    "limits.memory": memory_limit,
                    "persistentvolumeclaims": "10",
                    "requests.storage": storage_limit,
                },
            },
        }

        # Apply namespace
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(ns_manifest, f)
            ns_file = f.name

        try:
            self._run_command(["apply", "-f", ns_file])
        finally:
            Path(ns_file).unlink(missing_ok=True)

        # Apply quota
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(quota_manifest, f)
            quota_file = f.name

        try:
            self._run_command(["apply", "-f", quota_file])
        finally:
            Path(quota_file).unlink(missing_ok=True)

        # #278 A2: CS ns LimitRange（quota 之後、registry secret 之前）
        limits_manifest = self._build_limitrange_manifest(name)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(limits_manifest, f)
            limits_file = f.name
        try:
            self._run_command(["apply", "-f", limits_file])
        finally:
            Path(limits_file).unlink(missing_ok=True)

        self.ensure_registry_secret(name)

        # #423：secret 佈進 ns 之後，緊接著讓 ns 的 default SA 指向它。CDI importer
        # 這種由 controller 生成、不經任何 chart podSpec 的 pod，只有這條路拿得到
        # 憑證。放在 ns 準備階段是為了讓不變式從 ns 出生就成立（不必等到第一次
        # install）；install / upgrade / reset 各路徑另有同一個冪等收斂當後手。
        self.ensure_default_sa_image_pull_secret(name)

        # #279 O4: apply the leg-2 tenant-isolation baseline NetworkPolicy bundle.
        if workspace_id is not None:
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
        admission 拒收未設 limits 的容器；default 補上安全預設讓其可建。"""
        return {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": f"{name}-limits", "namespace": name},
            "spec": {"limits": [{
                "type": "Container",
                "default": {"cpu": "500m", "memory": "512Mi"},
                "defaultRequest": {"cpu": "500m", "memory": "512Mi"},
            }]},
        }

    def update_namespace_quota(self, name: str, cpu_limit: str,
                               memory_limit: str, storage_limit: str) -> Dict[str, str]:
        """#278 A1：整組 hard overwrite ns ResourceQuota（不動 ns/NP/secret）。
        apply 完整 6-key hard map（含 pvc=10），無 stale key。"""
        validate_namespace(name)
        import yaml
        quota_manifest = {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": f"{name}-quota", "namespace": name},
            "spec": {"hard": {
                "requests.cpu": cpu_limit,
                "requests.memory": memory_limit,
                "limits.cpu": cpu_limit,
                "limits.memory": memory_limit,
                "persistentvolumeclaims": "10",
                "requests.storage": storage_limit,
            }},
        }
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

        Additive, ingress-only NPs, all ``podSelector: {}`` (every pod in the ns —
        including the n8n app pod that hosts the mcp SIDECAR, which carries no chart NP
        of its own, so an ns-wide rule is the only mechanism that covers both the
        sidecar and standalone mcp-server shapes):

          1. ``default-deny-ingress`` — isolate every pod.
          2. ``allow-same-ns`` — a workspace ns is shared by its CloudServices; intra-ns
             reach is within-tenant, not a cross-tenant boundary.
          3. ``allow-cloudflared`` — ingress from THIS env's dedicated CF Tunnel
             connector ns, so the user-facing web path survives the default-deny.
             Env-safe by construction (the connector ns name is env-specific); skipped
             when no connector ns is configured/derivable (dev/local).
          4. ``allow-mcp-reach`` (the leg-2 crux) — from any agent ns stamped BOTH this
             workspace's ``reach-ws`` key AND the matching env label. No port filter:
             the n8n sidecar (:3000) and standalone mcp-server (:8080) diverge, and
             within-workspace-ns reach is within-tenant.

        egress is intentionally NOT restricted here — leg-1 (agent-runtime chart) owns
        agent egress. Idempotent via ``kubectl apply``.
        """
        validate_namespace(namespace)

        def _np(name: str, ingress: Optional[list]) -> dict:
            spec = {"podSelector": {}, "policyTypes": ["Ingress"]}
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
            ))
        manifests.append(_np(
            "paas-baseline-allow-mcp-reach",
            [{"from": [{"namespaceSelector": {"matchLabels": {
                reach_ws_label_key(workspace_id): REACH_WS_LABEL_VALUE,
                **env_ml,
            }}}]}],
        ))
        for manifest in manifests:
            self.apply_manifest(manifest)

    def ensure_registry_secret(self, namespace: str) -> Optional[str]:
        """Create/update a dockerconfigjson secret for private registry access.

        Reads registry credentials from settings. If not configured, skips.
        Uses kubectl apply so it's idempotent.

        Args:
            namespace: Target namespace

        Returns:
            Secret name if created, None if registry not configured
        """
        # #284 W0-hardening: this mutate method (kubectl apply of a dockerconfigjson
        # Secret) previously relied on caller discipline for the namespace. Validate
        # it at the entry point so a future caller can't apply into an out-of-prefix
        # ns (RBAC does not enforce the prefix — this app layer is the only boundary).
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
            self._run_command(["apply", "-f", secret_file])
            logger.info(
                "Registry secret '%s' ensured in namespace '%s'",
                settings.registry_secret_name,
                namespace,
            )
        finally:
            Path(secret_file).unlink(missing_ok=True)

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
        self, namespace: str, deployment_name: str, env: Dict[str, str]
    ) -> None:
        """Set/replace environment variables on a deployment via ``kubectl set env``.

        Triggers a rollout; used on resume to inject a freshly-signed
        SKILL_BUNDLE_TOKEN BEFORE scaling back to 1 (design §2.11.1 — avoids
        the pod coming up with a stale token).
        """
        validate_namespace(namespace)

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

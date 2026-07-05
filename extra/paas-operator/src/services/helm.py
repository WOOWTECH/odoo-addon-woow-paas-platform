"""Helm CLI wrapper service."""
import base64
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.models.schemas import (
    PodInfo,
    PodPhase,
    ReleaseInfo,
    ReleaseRevision,
    ReleaseStatus,
)

logger = logging.getLogger(__name__)

# issue #29: the private Helm repo token is passed via ``--password`` on the helm
# argv. Redact credential values BEFORE they reach the "Executing:" log line and
# HelmException.command, so the token never persists in operator stdout / log
# aggregation / SIEM. (The argv is still briefly visible via /proc/<pid>/cmdline
# during the subprocess lifetime — a narrower, transient exposure tracked as a
# follow-up; this change kills the persistent, broadly-readable log leak.)
_SECRET_VALUE_FLAGS = frozenset({"--password", "--username"})
_SECRET_SET_KEY_HINTS = ("password", "token", "secret", "key", "credential")


def _redact_cmd(cmd: List[str]) -> List[str]:
    """Return a copy of a CLI argv with credential values masked for logging.

    Handles ``--password value`` / ``--password=value`` (and ``--username``),
    plus ``--set key=value`` / ``--set-string`` where the key hints at a secret
    (future-proofing — the operator currently passes secrets via ``--values``
    file, whose path is non-sensitive, not via ``--set``).
    """
    redacted: List[str] = []
    mask_next = False  # next token is a bare credential value
    set_next = False   # next token is a `--set key=value` pair
    for tok in cmd:
        if mask_next:
            redacted.append("<redacted>")
            mask_next = False
            continue
        if set_next:
            set_next = False
            key = tok.split("=", 1)[0]
            if "=" in tok and any(h in key.lower() for h in _SECRET_SET_KEY_HINTS):
                redacted.append(f"{key}=<redacted>")
            else:
                redacted.append(tok)
            continue
        if tok in _SECRET_VALUE_FLAGS:
            redacted.append(tok)
            mask_next = True
            continue
        if tok in ("--set", "--set-string"):
            redacted.append(tok)
            set_next = True
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
        redacted.append(tok)
    return redacted


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


def reach_ws_label_key(workspace_id: int) -> str:
    """``woow.tw/reach-ws-<W>`` for one reachable workspace, validated fail-loud."""
    key = f"{REACH_WS_LABEL_PREFIX}{int(workspace_id)}"
    validate_label_key(key)
    return key


def build_managed_ns_labels(
    workspace_id: Optional[int] = None,
    reach_workspace_ids: Optional[List[int]] = None,
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
    ) -> subprocess.CompletedProcess:
        """Execute a Helm command.

        Args:
            args: Command arguments (without 'helm' prefix)
            input_data: Optional stdin input
            capture_output: Whether to capture stdout/stderr

        Returns:
            CompletedProcess object

        Raises:
            HelmException: If command fails
        """
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
                timeout=self.timeout,
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
                message=f"Helm command timed out after {self.timeout}s",
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

        # Write values to temp file if provided
        if values:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                import yaml

                yaml.dump(values, f)
                args.extend(["--values", f.name])
                temp_file = f.name

        try:
            result = self._run_command(args)
            release_data = json.loads(result.stdout)
            return self._parse_release_info(release_data)
        finally:
            if values:
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
        version: Optional[str] = None,
        reset_values: bool = False,
        reuse_values: bool = True,
        repo: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> ReleaseInfo:
        """Upgrade a Helm release.

        Args:
            namespace: Release namespace
            name: Release name
            chart: Chart reference (full URL like oci://... or repo/chart)
            values: Values override
            version: Chart version
            reset_values: Reset to chart default values
            reuse_values: Reuse last release values
            repo: Optional classic Helm repo URL for stateless fetch (--repo).
            username: Optional repo username for a private Helm repo (--username).
            password: Optional repo password/token for a private repo (--password).

        Returns:
            Updated release information

        Raises:
            ValueError: If chart is not provided (Helm doesn't store original chart URL)
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

        if version:
            args.extend(["--version", version])

        if reset_values:
            args.append("--reset-values")
        elif reuse_values:
            args.append("--reuse-values")

        # Classic Helm repo (stateless) — mirrors install(); see its note.
        if repo:
            args.extend(["--repo", repo])
        if username:
            args.extend(["--username", username])
        if password:
            args.extend(["--password", password])

        if values:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                import yaml

                yaml.dump(values, f)
                args.extend(["--values", f.name])
                temp_file = f.name

        try:
            result = self._run_command(args)
            release_data = json.loads(result.stdout)
            return self._parse_release_info(release_data)
        finally:
            if values:
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
            values=data.get("config"),
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

    def _run_command(self, args: List[str]) -> subprocess.CompletedProcess:
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
                timeout=30,
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
                message="kubectl command timed out after 30s",
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
                "labels": build_managed_ns_labels(workspace_id=workspace_id),
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

        self.ensure_registry_secret(name)

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

    def exec_in_pod(
        self,
        namespace: str,
        pod_name: str,
        container: str,
        command: List[str],
        timeout: int = 30,
    ) -> subprocess.CompletedProcess:
        """Execute a command in a pod container.

        Args:
            namespace: Namespace name
            pod_name: Pod name
            container: Container name
            command: Command to execute
            timeout: Timeout in seconds

        Returns:
            CompletedProcess object

        Raises:
            KubectlException: If command fails
        """
        validate_namespace(namespace)

        args = [
            "exec", pod_name,
            "--namespace", namespace,
            "--container", container,
            "--",
        ] + command

        cmd = [self.kubectl_bin] + args
        logger.info(f"Executing: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

            if result.returncode != 0:
                raise KubectlException(
                    message=f"kubectl exec failed with code {result.returncode}",
                    command=" ".join(cmd),
                    stderr=result.stderr,
                )

            return result

        except subprocess.TimeoutExpired as e:
            raise KubectlException(
                message=f"kubectl exec timed out after {timeout}s",
                command=" ".join(cmd),
                stderr=str(e),
            )

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

"""API endpoints for per-project agent-runtime pod lifecycle (design §2.11).

URL keys use ``project_hash`` (never the raw project_id — avoids serial leak).
This router is additive: it reuses the existing helm/k8s services and never
touches the releases/namespaces/routes/tunnels routers (§2.11.2).

Pod identity (design §2.7 / §2.10):
    namespace  = {settings.namespace_prefix}pa-{hash}  (e.g. paas-ws-pa-{hash}
                 by default, paas-ws-stg-pa-{hash} when the operator is
                 env-scoped) — derived so it always nests inside validate_namespace
    release    = agent-pa-{hash}
"""
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter

from src.config import settings
from src.models.schemas import (
    InvalidateMcpRequest,
    ResumeRequest,
    SpawnOrAttachRequest,
    SyncWorkspacesRequest,
)
from src.services import jwt_signer, mcp_invalidator
from src.services.helm import HelmException, HelmService, KubernetesService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent-runtimes", tags=["agent-runtimes"])

helm_service = HelmService()
k8s_service = KubernetesService()

_POD_WS_PORT = 8000  # pod FastAPI WS (uvicorn) port — design §B / image runtime/transport/ws_server


def _names(project_hash: str):
    """Return (namespace, release) for a project hash.

    The namespace is DERIVED from ``settings.namespace_prefix`` (the operator's
    allowed-prefix guard) so it always nests inside validate_namespace. With the
    default prefix ``paas-ws-`` this stays ``paas-ws-pa-{hash}`` (unchanged);
    when the operator is env-scoped (e.g. ``paas-ws-stg-``) it becomes
    ``paas-ws-stg-pa-{hash}``. The release name is prefix-independent.
    """
    return f"{settings.namespace_prefix}pa-{project_hash}", f"agent-pa-{project_hash}"


def _pod_ws_url(namespace: str, release: str) -> str:
    # Must match design §B + agent-runtime chart Service: the chart names the
    # Service "{release}-svc" and the pod serves the WS at /v1/chat on 8000.
    return f"ws://{release}-svc.{namespace}.svc.cluster.local:{_POD_WS_PORT}/v1/chat"


def _release_exists(namespace: str, release: str) -> bool:
    """True if the helm release exists; False on 'not found'; re-raise otherwise."""
    try:
        helm_service.get(namespace, release)
        return True
    except HelmException as exc:
        if "not found" in (exc.stderr or "").lower():
            return False
        raise


def _ensure_namespace(namespace: str) -> None:
    """Create/patch the project namespace carrying the ``paas-managed=true``
    label (idempotent apply).

    The Odoo NetworkPolicy authorises agent-runtime <-> Odoo traffic by this
    LABEL (name-agnostic), and ``helm install --create-namespace`` creates a
    BARE (unlabelled) namespace — so the operator applies the labelled namespace
    itself and installs into it (create_namespace=False). Without the label,
    pod<->Odoo traffic is silently NetworkPolicy-dropped.
    """
    k8s_service.apply_manifest({
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace, "labels": {"paas-managed": "true"}},
    })


def _derive_odoo_namespace() -> Optional[str]:
    """Derive the Odoo namespace from ``settings.odoo_base_url`` for the chart's
    ``networkPolicy.odooNamespace`` (F-PILOT-5).

    The agent pod's NetworkPolicy must allow ingress/egress to Odoo's *real*
    namespace, else pod<->Odoo traffic (WS /v1/chat + skill-bundle) is dropped —
    the chart's default is a wrong placeholder. ``odoo_base_url`` is the in-cluster
    svc FQDN ``http://<svc>.<ns>.svc.cluster.local:<port>`` → ns = 2nd dotted
    segment of the host. Returns None when the URL isn't a recognisable k8s svc
    FQDN (external host etc.) so the chart keeps its own default rather than the
    operator forwarding a wrong guess.
    """
    from urllib.parse import urlparse
    host = urlparse(settings.odoo_base_url or "").hostname or ""
    parts = host.split(".")
    if len(parts) >= 3 and parts[2] == "svc":
        return parts[1]
    return None


def _build_install_values(request: SpawnOrAttachRequest, skill_bundle_token: str):
    """Helm values for a cold-start install (injects SKILL_BUNDLE_TOKEN env)."""
    values = {
        "image": {"tag": settings.agent_runtime_image_tag},
        "projectHash": request.project_hash,
        # projectId is a chart-required value (_helpers.tpl agent-runtime.validate):
        # the B-2 image uses it for the subprocess cwd. It rides in the pod env
        # only (never a URL/ns key — keeps the serial-leak guard intact).
        "projectId": request.project_id,
        # F-PILOT-4: forward the private-registry pull secret so the chart sets
        # imagePullSecrets and the kubelet can pull the private jcr-prod image
        # (the secret itself is provisioned into the ns by ensure_registry_secret).
        "imagePullSecrets": [{"name": settings.registry_secret_name}],
        "env": {
            "ODOO_BASE_URL": settings.odoo_base_url,
            "PROJECT_HASH": request.project_hash,
            "SKILL_BUNDLE_TOKEN": skill_bundle_token,
        },
        "networkPolicy": {"egressWorkspaceIds": request.project_workspace_ids},
    }
    # F-PILOT-5: forward the Odoo namespace so the pod's NetworkPolicy ingress
    # (Odoo->pod WS) + egress (pod->Odoo skill-bundle) point at Odoo's real ns,
    # not the chart's placeholder default. Omitted when not derivable (chart default).
    odoo_namespace = _derive_odoo_namespace()
    if odoo_namespace:
        values["networkPolicy"]["odooNamespace"] = odoo_namespace
    # D-CAVEAT-1: the pod verifies the inbound ws_token with the SAME HS256 key
    # the operator signs it with. Source the key from jwt_signer itself (not a
    # second env read) so pod-key == sign-key by construction; the chart's
    # auth.manageSecret renders the per-ns Secret + file-mount (/run/secrets).
    # Omitted when unset so unconfigured installs are unaffected.
    current_key = jwt_signer._current_secret()
    if current_key:
        values["auth"] = {"currentKey": current_key}
        previous_key = jwt_signer._previous_secret()
        if previous_key:
            values["auth"]["previousKey"] = previous_key
    # F-PILOT-10 (A): open the pod's external egress (e.g. to the LLM endpoint)
    # when this environment opts in. The chart renders the egress rule behind
    # agentRuntime.externalEgress.enabled; omitted (chart's closed default) when
    # disabled. 5th instance of the missed-forward class — the chart consumed
    # the value but the operator never set it, so real-LLM pods kept the closed
    # default. See [[spawn-install-values-missed-forward]] + the forward-contract test.
    if settings.agent_runtime_external_egress_enabled:
        values.setdefault("agentRuntime", {})["externalEgress"] = {"enabled": True}
    return values


def _chart_source_kwargs() -> dict:
    """Helm kwargs to fetch the agent-runtime chart from the private Gitea
    classic Helm repo (gap C): stateless ``--repo``/``--version`` + read creds.

    Empty settings collapse to ``None`` so helm omits the flags — keeping the
    call backward-compatible with the marketplace's direct/OCI chart refs.
    """
    return {
        "repo": settings.agent_runtime_repo_url or None,
        "version": settings.agent_runtime_chart_version or None,
        "username": settings.agent_runtime_repo_username or None,
        "password": settings.agent_runtime_repo_password or None,
    }


def _provision_llm_secret(namespace: str, release: str, request: SpawnOrAttachRequest) -> None:
    """Provision the per-release LLM credential Secret (design §8.2 / F-PILOT-8).

    The operator never queries the Odoo DB, so the tenant's LLM ``api_key``
    (+ optional model config) rides the spawn request; the operator stores it in
    a per-release Secret ``agent-pa-{hash}-llm`` — one per tenant, so a leak is
    confined to that tenant (INV-2). The chart secretKeyRefs ``LLM_API_KEY`` from
    it (no plaintext in the pod spec — T8.2, paas-charts). A request without an
    api_key (e.g. the echo agent that never calls an LLM) provisions no Secret.
    """
    if not request.api_key:
        return
    string_data = {"LLM_API_KEY": request.api_key}
    if request.llm_model_config:
        string_data["LLM_MODEL_CONFIG"] = json.dumps(request.llm_model_config)
    k8s_service.apply_manifest({
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"{release}-llm",
            "namespace": namespace,
            "labels": {"paas-managed": "true"},
        },
        "type": "Opaque",
        "stringData": string_data,
    })


def _resume_pod(namespace, release, project_hash, project_id, workspace_ids):
    """Re-sign SKILL_BUNDLE_TOKEN, patch pod env, THEN scale to 1 (§2.11.1).

    Env patch must precede scale-1 so K8s recreates the pod with the fresh
    token (avoids the pod racing up with a stale, expired token).
    """
    skill_bundle_token = jwt_signer.sign_skill_bundle_token(
        project_hash=project_hash,
        project_id=project_id,
        workspace_ids=workspace_ids,
    )
    k8s_service.set_deployment_env(
        namespace, release, {"SKILL_BUNDLE_TOKEN": skill_bundle_token},
    )
    k8s_service.scale_deployment(namespace, release, 1)


@router.post("/spawn-or-attach", summary="Spawn, attach, or resume a project pod")
def spawn_or_attach(request: SpawnOrAttachRequest):
    """Cold start / attach / resume the project's agent-runtime pod."""
    namespace, release = _names(request.project_hash)
    started = time.time()

    if not _release_exists(namespace, release):
        # Cold start — pre-create the labelled namespace (Odoo NP gate), ensure
        # the private-registry pull secret (F-PILOT-4, symmetric with the
        # marketplace create_namespace_with_quota path) + provision the per-tenant
        # LLM credential Secret (F-PILOT-8) BEFORE install, then sign + install.
        _ensure_namespace(namespace)
        k8s_service.ensure_registry_secret(namespace)
        _provision_llm_secret(namespace, release, request)
        skill_bundle_token = jwt_signer.sign_skill_bundle_token(
            project_hash=request.project_hash,
            project_id=request.project_id,
            workspace_ids=request.project_workspace_ids,
        )
        helm_service.install(
            namespace=namespace,
            name=release,
            chart=settings.agent_runtime_chart,
            values=_build_install_values(request, skill_bundle_token),
            create_namespace=False,  # ns already applied with paas-managed label
            **_chart_source_kwargs(),
        )
        pod_state = "spawning"
    else:
        replicas = k8s_service.get_deployment_replicas(namespace, release)
        if replicas == 0:
            # Resume — re-sign + patch env before scale-1 (§2.11.1).
            _resume_pod(
                namespace, release, request.project_hash,
                request.project_id, request.project_workspace_ids,
            )
        # else: attach — pod already running, no install / no re-sign.
        pod_state = "active"

    # Readiness poll (mocked in unit tests; real k8s in integration suite).
    k8s_service.get_pods(
        namespace=namespace,
        label_selector=f"app.kubernetes.io/instance={release}",
    )

    ws_token = jwt_signer.sign_ws_token(
        project_hash=request.project_hash,
        channel_id=request.channel_id,
        invoker_user_id=request.invoker_user_id,
        invoker_role=request.invoker_role,
    )
    return {
        "pod_ws_url": _pod_ws_url(namespace, release),
        "ws_token": ws_token,
        "auth_token": ws_token,
        "pod_state": pod_state,
        "spawn_age_ms": int((time.time() - started) * 1000),
    }


@router.post("/{project_hash}/reap", summary="Scale a project pod to zero")
def reap(project_hash: str):
    """Idempotently scale the pod to 0 + stamp reaped-since annotation."""
    namespace, release = _names(project_hash)

    if not _release_exists(namespace, release):
        return {"action": "not_found"}

    if k8s_service.get_deployment_replicas(namespace, release) == 0:
        return {"action": "already_reaped"}

    k8s_service.scale_deployment(namespace, release, 0)
    reaped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    k8s_service.patch_deployment(
        namespace=namespace,
        deployment_name=release,
        patch='{"metadata":{"annotations":{"agent.woow.tw/reaped-since":"%s"}}}'
        % reaped_at,
        patch_type="merge",
    )
    return {"action": "reaped"}


@router.post("/{project_hash}/resume", summary="Resume a reaped project pod")
def resume(project_hash: str, request: Optional[ResumeRequest] = None):
    """Re-sign SKILL_BUNDLE_TOKEN, patch pod env, then scale to 1 (§2.11.1)."""
    namespace, release = _names(project_hash)
    project_id = request.project_id if request else None
    workspace_ids = request.project_workspace_ids if request else []
    _resume_pod(namespace, release, project_hash, project_id, workspace_ids)
    return {"action": "resumed", "pod_state": "active"}


@router.post("/{project_hash}/invalidate-mcp", summary="Invalidate pod mcp.json")
def invalidate_mcp(project_hash: str, request: InvalidateMcpRequest):
    """Patch the pod's mcp.json ConfigMap with a fresh snapshot (§5.4)."""
    return mcp_invalidator.invalidate(
        project_hash=project_hash, mcp_snapshot=request.mcp_snapshot,
    )


@router.post("/{project_hash}/sync-workspaces", summary="Re-render egress allowlist")
def sync_workspaces(project_hash: str, request: SyncWorkspacesRequest):
    """Helm upgrade re-renders the pod's NetworkPolicy egress allowlist."""
    namespace, release = _names(project_hash)
    helm_service.upgrade(
        namespace=namespace,
        name=release,
        chart=settings.agent_runtime_chart,
        values={
            "networkPolicy": {"egressWorkspaceIds": request.project_workspace_ids},
        },
        reuse_values=True,
        **_chart_source_kwargs(),
    )
    return {"action": "synced", "workspace_ids": request.project_workspace_ids}


@router.delete("/{project_hash}", summary="Uninstall a project pod (long-TTL GC)")
def delete_runtime(project_hash: str):
    """Helm uninstall the project's agent-runtime release."""
    namespace, release = _names(project_hash)
    return helm_service.uninstall(namespace, release)


@router.get("/{project_hash}/status", summary="Get a project pod's lifecycle state")
def get_status(project_hash: str):
    """Return the pod's lifecycle snapshot for dashboard / drift detection."""
    namespace, release = _names(project_hash)

    if not _release_exists(namespace, release):
        return {
            "state": "absent",
            "last_active": None,
            "pod_ws_url": None,
            "spawn_age_ms": None,
        }

    replicas = k8s_service.get_deployment_replicas(namespace, release)
    k8s_service.get_pods(
        namespace=namespace,
        label_selector=f"app.kubernetes.io/instance={release}",
    )
    return {
        "state": "active" if replicas else "reaped",
        "last_active": None,
        "pod_ws_url": _pod_ws_url(namespace, release) if replicas else None,
        "spawn_age_ms": None,
    }

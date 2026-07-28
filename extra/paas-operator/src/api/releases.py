"""API endpoints for Helm release management."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src.config import settings
from src.models.schemas import (
    ReleaseCreateRequest,
    ReleaseInfo,
    ReleaseResetConfigRequest,
    ReleaseRevisionsResponse,
    ReleaseRollbackRequest,
    ReleaseStatusResponse,
    ReleaseUpgradeRequest,
    RouteInfo,
    SidecarPatchRequest,
    SidecarPatchResponse,
)
from src.services.cloudflare import CloudflareException, CloudflareService
from src.services.helm import HelmException, HelmService, KubectlException, KubernetesService
from src.services.helm import _sanitize_release_values


class _NoCdiInstalled(Exception):
    """Sentinel: this cluster has no CDI, so there is nothing to orphan-label.

    Not an error condition — keeps the teardown flow linear without an extra
    nesting level (MINOR-4).
    """

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["releases"])

helm_service = HelmService()
k8s_service = KubernetesService()
cloudflare_service = CloudflareService()


def _ensure_image_pull_secrets(values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """回傳補上預設 ``imagePullSecrets`` 的 values 淺複本（單一實作，install/upgrade 共用）。

    私有 image chart（如 mcp-server 拉 jcr-prod 的 hermes-mcp-admin）需要
    imagePullSecrets 讓 kubelet 通過認證；secret 本體由 ensure_registry_secret
    先佈建進 namespace。與 agent_runtimes.py（F-PILOT-4）同一 family——
    install 路徑在 a0e7c25a 補上，但 upgrade 路徑漏了：#411 之後 upgrade 走
    全量 reset values，私有 image 的 cloud service 任一次 redeploy /
    reconfigure / reset-password 都會掉 pull secret → ImagePullBackOff 401
    （#448）。抽成共用 helper 讓兩邊永不再漂移。

    用 setdefault：template 的 helm_default_values 可自行覆寫；public-image
    chart（bitnami n8n/odoo）不消費 top-level imagePullSecrets、多帶無害。
    """
    merged = dict(values or {})
    merged.setdefault(
        "imagePullSecrets", [{"name": settings.registry_secret_name}]
    )
    return merged


@router.post(
    "",
    response_model=ReleaseInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Install a Helm release",
)
async def create_release(request: ReleaseCreateRequest):
    """Install a new Helm chart release.

    Args:
        request: Release creation parameters

    Returns:
        Information about the created release

    Raises:
        HTTPException: If installation fails
    """
    try:
        # 注入預設 imagePullSecrets（理由見 _ensure_image_pull_secrets docstring）。
        values = _ensure_image_pull_secrets(request.values)
        release = helm_service.install(
            namespace=request.namespace,
            name=request.name,
            chart=request.chart,
            values=values,
            sensitive_values=request.sensitive_values,
            version=request.version,
            create_namespace=request.create_namespace,
        )
        if release.values is not None:
            release.values = _sanitize_release_values(release.values)
        logger.info(f"Installed release {request.name} in {request.namespace}")

        # Handle Cloudflare Tunnel route creation if expose is enabled
        route_info = None
        if request.expose and request.expose.enabled:
            route_info = await _create_cloudflare_route(
                namespace=request.namespace,
                release_name=request.name,
                expose_config=request.expose,
            )
            if route_info:
                release.route = route_info

        return release

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Helm install failed: {e.message}\nStderr: {e.stderr}")
        # Sanitize error message - don't expose internal details
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Helm installation failed. Check operator logs for details.",
        )


@router.get(
    "/{namespace}/{name}",
    response_model=ReleaseInfo,
    summary="Get release information",
)
def get_release(namespace: str, name: str):
    """Get detailed information about a Helm release.

    Args:
        namespace: Release namespace
        name: Release name

    Returns:
        Release information

    Raises:
        HTTPException: If release not found or access denied
    """
    try:
        release = helm_service.get(namespace, name)
        return release

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        if "not found" in e.stderr.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Release {name} not found in namespace {namespace}",
            )
        logger.error(f"Failed to get release: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get release. Check operator logs for details.",
        )


@router.patch(
    "/{namespace}/{name}",
    response_model=ReleaseInfo,
    summary="Upgrade a Helm release",
)
def upgrade_release(
    namespace: str,
    name: str,
    request: ReleaseUpgradeRequest,
):
    """Upgrade an existing Helm release.

    Args:
        namespace: Release namespace
        name: Release name
        request: Upgrade parameters

    Returns:
        Updated release information

    Raises:
        HTTPException: If upgrade fails
    """
    try:
        # #448：upgrade 也必須注入預設 imagePullSecrets。#411 之後 upgrade 走
        # 全量 reset values，沒有這行時私有 image 的 CS 任一次 redeploy /
        # reconfigure 都會掉 pull secret → ImagePullBackOff 401。
        release = helm_service.upgrade(
            namespace=namespace,
            name=name,
            chart=request.chart,
            values=_ensure_image_pull_secrets(request.values),
            sensitive_values=request.sensitive_values,
            version=request.version,
            reset_values=request.reset_values,
            reuse_values=request.reuse_values,
        )
        if release.values is not None:
            release.values = _sanitize_release_values(release.values)
        logger.info(f"Upgraded release {name} in {namespace}")
        return release

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Helm upgrade failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Helm upgrade failed. Check operator logs for details.",
        )


def build_orphan_pvc_labels(release_name: str, grace_period_days: int) -> Dict[str, str]:
    """Build the orphaned-PVC labels stamped at teardown for the pvc-cleanup cron.

    orphaned-at is epoch SECONDS (colon-free): an RFC3339 timestamp contains ':',
    an illegal k8s label value, which made `kubectl label` reject the whole batch
    atomically so the cron's `-l orphaned-at` selector never matched and data PVCs
    leaked forever (operator #208). Single source of truth so the integration
    test exercises the exact value production the teardown path uses.
    """
    now_epoch = str(int(datetime.now(timezone.utc).timestamp()))
    return {
        "paas.woowtech.io/orphaned-at": now_epoch,
        "paas.woowtech.io/release-name": release_name,
        "paas.woowtech.io/grace-period-days": str(grace_period_days),
    }


@router.delete(
    "/{namespace}/{name}",
    response_model=Dict[str, str],
    summary="Uninstall a Helm release",
)
async def delete_release(namespace: str, name: str, subdomain: Optional[str] = None):
    """Uninstall a Helm release.

    Args:
        namespace: Release namespace
        name: Release name
        subdomain: Optional subdomain to delete from Cloudflare (if not provided, auto-generated)

    Returns:
        Deletion confirmation message

    Raises:
        HTTPException: If uninstallation fails
    """
    try:
        result = helm_service.uninstall(namespace, name)
        logger.info(f"Uninstalled release {name} from {namespace}")

        # Label orphaned PVCs for deferred cleanup — scoped to THIS release's own
        # PVCs (#281). A per-workspace ns is shared by multiple services; an
        # unscoped list would stamp orphaned-at on other live services' data PVCs
        # → the pvc-cleanup cron then deletes them (data-loss). helm stamps
        # app.kubernetes.io/instance={release} on templated resources, and the
        # Bitnami StatefulSet volumeClaimTemplate data PVCs carry it too (verified
        # live on stg: data-svc-…-postgresql-0 has instance=svc-…).
        try:
            remaining_pvcs = k8s_service.list_pvcs(
                namespace, label_selector=f"app.kubernetes.io/instance={name}"
            )
            if remaining_pvcs:
                labels = build_orphan_pvc_labels(
                    name, settings.pvc_grace_period_days
                )
                k8s_service.label_pvcs(namespace, remaining_pvcs, labels)
                logger.info(
                    f"Labeled {len(remaining_pvcs)} orphaned PVCs in {namespace}: "
                    f"{remaining_pvcs}"
                )
        except Exception as e:
            logger.warning(f"Failed to label orphaned PVCs in {namespace}: {e}")

        # #423 W4.4: VM-type releases keep their disk in a CDI DataVolume that
        # OWNS the PVC. The chart marks the DataVolume helm.sh/resource-policy:
        # keep, so `helm uninstall` leaves it behind (verified in the W1 PoC) —
        # meaning it needs the same orphan stamp as the PVC, and the cleanup
        # cron must delete THE DATAVOLUME. Deleting the PVC alone makes CDI
        # recreate it and re-run the whole import (also measured in the PoC).
        # Separate try block: a DataVolume failure must not mask the PVC
        # labeling above, and vice versa.
        try:
            # MINOR-4: gate on a cached CRD probe so a container-only cluster
            # does not pay an extra synchronous kubectl on EVERY delete — and,
            # more importantly, so the failure log below stays rare enough to
            # mean something when it does fire.
            if not k8s_service.has_datavolume_crd():
                raise _NoCdiInstalled()
            remaining_dvs = k8s_service.list_datavolumes(
                namespace, label_selector=f"app.kubernetes.io/instance={name}"
            )
            if remaining_dvs:
                labels = build_orphan_pvc_labels(
                    name, settings.pvc_grace_period_days
                )
                labelled = k8s_service.label_datavolumes(
                    namespace, remaining_dvs, labels
                )
                # Report what actually succeeded — a DataVolume that missed
                # its stamp is kept forever by the chart's resource-policy and
                # is invisible to the cleanup cron (silent storage leak).
                if len(labelled) != len(remaining_dvs):
                    missed = sorted(set(remaining_dvs) - set(labelled))
                    logger.error(
                        f"Orphan-labelled only {len(labelled)}/{len(remaining_dvs)} "
                        f"DataVolumes in {namespace}; UNRECLAIMABLE: {missed}"
                    )
                else:
                    logger.info(
                        f"Labeled {len(labelled)} orphaned DataVolumes in "
                        f"{namespace}: {labelled}"
                    )
        except _NoCdiInstalled:
            logger.debug(
                f"No CDI CRD in this cluster; skipping DataVolume "
                f"orphan-labeling for {namespace}/{name}"
            )
        except KubectlException as e:
            # A cluster with no CDI is an expected, uninteresting case — but
            # anything else (Forbidden, timeout) on a VM-capable cluster is
            # the ONLY signal that a tenant's disk was left unreclaimable, so
            # it must not be buried at the same level as routine noise.
            if KubernetesService.is_crd_absent_error(e):
                logger.debug(
                    f"No CDI CRD in this cluster; skipping DataVolume "
                    f"orphan-labeling for {namespace}/{name}"
                )
            else:
                logger.error(
                    f"Failed to list DataVolumes for orphan-labeling in "
                    f"{namespace}/{name} — any VM disk here will NOT be "
                    f"reclaimed: {e.stderr}"
                )
        except Exception as e:
            logger.error(
                f"Failed to label orphaned DataVolumes in {namespace}: {e}"
            )

        # Delete Cloudflare Tunnel route if exists
        try:
            # Use provided subdomain or fall back to auto-generated
            route_subdomain = subdomain or cloudflare_service.generate_subdomain(namespace, name)
            await cloudflare_service.delete_route(route_subdomain)
            logger.info(f"Deleted Cloudflare route for {route_subdomain}")
        except CloudflareException as e:
            # Log but don't fail - release is already uninstalled
            logger.warning(f"Failed to delete Cloudflare route: {e.message}")

        # Delete MCP sidecar Cloudflare route and K8s Service if they exist
        try:
            # #301: deploy no longer builds a public mcp CF route (see
            # _create_sidecar_service). This delete leg is KEPT to converge any
            # residual {subdomain}-mcp route left by historical deploys (pre-#301);
            # it must use generate_subdomain (the same source those deploys used),
            # not the caller subdomain, or it targets a different hostname and
            # orphans the route (operator #207). delete_route is idempotent, so it
            # is a harmless no-op once no such route remains.
            mcp_subdomain = cloudflare_service.generate_subdomain(namespace, name) + "-mcp"
            await cloudflare_service.delete_route(mcp_subdomain)
            logger.info(f"Deleted MCP sidecar Cloudflare route for {mcp_subdomain}")
        except CloudflareException as e:
            logger.warning(f"Failed to delete MCP sidecar Cloudflare route: {e.message}")

        try:
            mcp_service_name = f"{name}-mcp"
            k8s_service.delete_service(namespace, mcp_service_name)
            logger.info(f"Deleted MCP sidecar K8s Service {mcp_service_name}")
        except Exception as e:
            logger.warning(f"Failed to delete MCP sidecar K8s Service: {e}")

        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        # If release not found, return 404 so client can clean up DB record
        if "not found" in e.stderr.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Release {name} not found in namespace {namespace}",
            )
        logger.error(f"Helm uninstall failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Helm uninstall failed. Check operator logs for details.",
        )


@router.post(
    "/{namespace}/{name}/rollback",
    response_model=Dict[str, str],
    summary="Rollback a Helm release",
)
def rollback_release(
    namespace: str,
    name: str,
    request: ReleaseRollbackRequest,
):
    """Rollback a Helm release to a previous revision.

    Args:
        namespace: Release namespace
        name: Release name
        request: Rollback parameters

    Returns:
        Rollback confirmation message

    Raises:
        HTTPException: If rollback fails
    """
    try:
        result = helm_service.rollback(namespace, name, request.revision)
        logger.info(f"Rolled back release {name} in {namespace}")
        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Helm rollback failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Helm rollback failed. Check operator logs for details.",
        )


@router.get(
    "/{namespace}/{name}/revisions",
    response_model=ReleaseRevisionsResponse,
    summary="Get release revision history",
)
def get_release_revisions(namespace: str, name: str):
    """Get the revision history of a Helm release.

    Args:
        namespace: Release namespace
        name: Release name

    Returns:
        List of release revisions

    Raises:
        HTTPException: If history retrieval fails
    """
    try:
        revisions = helm_service.history(namespace, name)
        return ReleaseRevisionsResponse(revisions=revisions)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Failed to get history: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get release history. Check operator logs for details.",
        )


@router.get(
    "/{namespace}/{name}/status",
    response_model=ReleaseStatusResponse,
    summary="Get release and pod status",
)
def get_release_status(
    namespace: str,
    name: str,
    include_vm: bool = Query(
        default=False,
        description=(
            "#423 W4.2: also return KubeVirt VMIs and CDI DataVolumes for this "
            "release. Defaults to false so existing callers are unaffected and "
            "container-only clusters never pay for the extra CR lookups."
        ),
    ),
):
    """Get detailed status including pod information.

    Args:
        namespace: Release namespace
        name: Release name
        include_vm: Include VMI/DataVolume status (VM-type workloads)

    Returns:
        Release and pod status information

    Raises:
        HTTPException: If status retrieval fails
    """
    try:
        # Get release info
        release = helm_service.get(namespace, name)

        selector = f"app.kubernetes.io/instance={name}"

        # Get pods in namespace (filter by app.kubernetes.io/instance label)
        pods = []
        pod_retrieval_error = None
        try:
            pods = k8s_service.get_pods(
                namespace=namespace,
                label_selector=selector,
            )
        except Exception as e:
            logger.error(f"Failed to get pods for {namespace}/{name}: {e}")
            pod_retrieval_error = str(e)

        # #423 W4.2: VM-type workloads. A virt-launcher pod being Running does
        # NOT mean the guest booted, so the platform's readiness gate reads the
        # VMI instead. Errors are surfaced (never swallowed) because the Odoo
        # side is fail-CLOSED on an empty vmis list: it keeps the service in
        # `deploying`, and without these fields a broken lookup would be
        # indistinguishable from "still importing".
        #
        # None (not []) while include_vm is false, so "not queried" and
        # "queried, none found" stay distinguishable — a caller that forgets
        # the flag gets a diagnosable None instead of a silently-stuck service.
        vmis = None
        datavolumes = None
        vmi_retrieval_error = None
        datavolume_retrieval_error = None
        if include_vm:
            # Separate try blocks per resource kind: a DataVolume failure must
            # not discard an already-known VMI readiness answer (and vice
            # versa). Readiness depends only on the VMI half.
            try:
                vmis = k8s_service.get_vmis(
                    namespace=namespace, label_selector=selector,
                )
            except Exception as e:
                logger.error(f"Failed to get VMIs for {namespace}/{name}: {e}")
                vmi_retrieval_error = str(e)
            try:
                datavolumes = k8s_service.get_datavolumes(
                    namespace=namespace, label_selector=selector,
                )
            except Exception as e:
                logger.error(
                    f"Failed to get DataVolumes for {namespace}/{name}: {e}"
                )
                datavolume_retrieval_error = str(e)

        return ReleaseStatusResponse(
            release=release,
            pods=pods,
            pod_retrieval_error=pod_retrieval_error,
            vm_included=include_vm,
            vmis=vmis,
            datavolumes=datavolumes,
            vmi_retrieval_error=vmi_retrieval_error,
            datavolume_retrieval_error=datavolume_retrieval_error,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Failed to get status: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get release status. Check operator logs for details.",
        )


@router.post(
    "/{namespace}/{name}/scale",
    response_model=Dict[str, str],
    summary="Scale a release's deployments",
)
def scale_release(namespace: str, name: str, replicas: int = Query(default=0, ge=0, le=10)):
    """Scale all workloads (Deployments + StatefulSets + VirtualMachines).

    Used for subscription suspend (replicas=0) and resume (replicas=1).
    Bitnami charts deploy their primary workload as a StatefulSet so we
    cover both kinds; releases that have none of the three are a 404.

    #423 W4.3 — VM-type releases (HAOS) have NO replicas to scale. Without
    the VirtualMachine branch below, suspending such a service hit the 404
    here, the Odoo caller caught the exception and wrote the service to
    `error` — i.e. every unpaid HAOS tenant would land in a bogus error
    state instead of being suspended. VMs map to `runStrategy` instead:
    Halted (stop the guest, keep the definition + disk) / Always.

    Args:
        namespace: Release namespace
        name: Release name
        replicas: Target replica count (0 to suspend, 1+ to resume)

    Returns:
        Confirmation message with scaled workload counts.

    Raises:
        HTTPException: If scaling fails
    """
    selector = f"app.kubernetes.io/instance={name}"
    try:
        deployments = k8s_service.get_deployments(
            namespace=namespace,
            label_selector=selector,
        )
        statefulsets = k8s_service.get_statefulsets(
            namespace=namespace,
            label_selector=selector,
        )

        # Only look for VMs when there is no container workload. Keeps the
        # existing (overwhelmingly common) path at exactly the same cost and
        # avoids a CRD lookup on clusters without KubeVirt.
        virtualmachines = []
        if not deployments and not statefulsets:
            try:
                virtualmachines = k8s_service.get_virtualmachines(
                    namespace=namespace,
                    label_selector=selector,
                )
            except KubectlException as e:
                # ⚠ Narrow to "the CRD does not exist". Everything else —
                # RBAC Forbidden, a 30s timeout, a missing kubectl — must
                # NOT be folded into "no VM workload".
                #
                # This is not hypothetical: KubeVirt CRDs are already
                # installed on woow-k3s while the operator ClusterRole has
                # not yet been republished with the kubevirt.io apiGroup
                # (chart 0.1.19 still in the CI queue). With a blanket
                # catch, the very first VM suspend would go
                #   Forbidden → 404 → _do_suspend writes state=error
                # leaving an unpaid VM running — precisely the failure this
                # branch exists to fix, re-created one layer up.
                #
                # Note the deliberate asymmetry with the container path
                # above: get_deployments/get_statefulsets do not catch at
                # all, so their failures surface as 500. VM lookups get one
                # narrow exemption and nothing more.
                if not KubernetesService.is_crd_absent_error(e):
                    logger.error(
                        f"VirtualMachine lookup failed for {namespace}/{name} "
                        f"(not a missing-CRD error — refusing to report "
                        f"'no workloads'): {e.stderr}"
                    )
                    raise
                logger.info(
                    f"No KubeVirt CRD in this cluster; skipping VirtualMachine "
                    f"lookup for {namespace}/{name}"
                )

        if not deployments and not statefulsets and not virtualmachines:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No deployments, statefulsets or virtualmachines found for "
                    f"release {name} in namespace {namespace}"
                ),
            )

        scaled_deps = []
        for dep in deployments:
            dep_name = dep["name"]
            k8s_service.scale_deployment(namespace, dep_name, replicas)
            scaled_deps.append(dep_name)
            logger.info(f"Scaled deployment {dep_name} in {namespace} to {replicas} replicas")

        scaled_sts = []
        for sts in statefulsets:
            sts_name = sts["name"]
            k8s_service.scale_statefulset(namespace, sts_name, replicas)
            scaled_sts.append(sts_name)
            logger.info(f"Scaled statefulset {sts_name} in {namespace} to {replicas} replicas")

        # replicas==0 is the suspend signal; anything >=1 is resume. A VM has
        # no notion of "2 replicas", so every non-zero value maps to Always.
        run_strategy = "Halted" if replicas == 0 else "Always"
        scaled_vms = []
        for vm in virtualmachines:
            vm_name = vm["name"]
            k8s_service.patch_vm_run_strategy(namespace, vm_name, run_strategy)
            scaled_vms.append(vm_name)
            logger.info(
                f"Set runStrategy={run_strategy} on VM {vm_name} in {namespace} "
                f"(replicas={replicas})"
            )

        total = len(scaled_deps) + len(scaled_sts) + len(scaled_vms)
        return {
            "message": f"Scaled {total} workload(s) to {replicas} replicas",
            "deployments": ", ".join(scaled_deps),
            "statefulsets": ", ".join(scaled_sts),
            "virtualmachines": ", ".join(scaled_vms),
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except KubectlException as e:
        logger.error(f"kubectl scale failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to scale workload. Check operator logs for details.",
        )


@router.post(
    "/{namespace}/{name}/reset-config",
    response_model=Dict[str, Any],
    summary="#357/#449：reset admin_password（依 admin_password_carrier 分流）",
)
def reset_release_config(
    namespace: str,
    name: str,
    request: ReleaseResetConfigRequest,
):
    """Reset the admin password, by whichever mechanism the chart actually uses.

    #449: this handler used to assume every chart keeps its password in
    ``/data/config.json``. That is only true of the mcp-server family; charts
    that carry the password in their own Secret (hermes-agent, open-design)
    have no such file, so the exec patch failed, its backup was never created,
    the compensating ``os.replace`` failed too, and every rotation ended in a
    500. ``request.admin_password_carrier`` now selects the path:

    ``helm_secret`` — Helm's Secret *is* the credential store. Upgrade it and
    restart the workloads that read it; see ``_reset_via_helm_secret``.

    ``config_json`` (default, and what an older platform sends) — Helm's Secret
    and the PVC-backed config file cannot be updated in one transaction, so a
    compensating saga is used:

    1. Atomically upgrade the Helm Secret and wait for success.
    2. Read-modify-write only ``admin_password`` in config.json, retaining a
       same-volume backup of the complete original file.
    3. Restart and wait for the deployment, then remove that backup.

    A failure after step 1 rolls Helm back to the prior revision.  A failure
    after step 2 additionally restores the original config file and rolls the
    workload back.  The password enters the container via exec stdin only, not
    process argv, logs, or a temporary file.
    """
    _validate_namespace_or_raise(namespace)

    if not request.sensitive_values:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reset-config requires sensitive_values containing new admin_password",
        )

    new_password = (
        (request.sensitive_values or {})
        .get("config", {})
        .get("sensitive", {})
        .get("admin_password")
    )
    if not new_password or not isinstance(new_password, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sensitive_values.config.sensitive.admin_password is required",
        )

    if request.admin_password_carrier == "helm_secret":
        return _reset_via_helm_secret(namespace, name, request)

    try:
        previous_revision = helm_service.get(namespace, name).revision
        release = helm_service.upgrade(
            namespace=namespace,
            name=name,
            chart=request.chart,
            # #448/#449：reset 也走全量 reset values，沒有這行時私有 image 的 CS
            # 每次 reset password 都會掉 pull secret → ImagePullBackOff 401。
            # 之前只因呼叫端沿用 reuse_values 預設才沒踩到（#449 comment）。
            values=_ensure_image_pull_secrets(request.values),
            sensitive_values=request.sensitive_values,
            version=request.version,
            reset_values=request.reset_values,
            reuse_values=request.reuse_values,
            atomic=True,
        )
    except (ValueError, HelmException) as e:
        logger.error("reset-config Helm upgrade failed for %s/%s: %s", namespace, name, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="reset-config: failed to update the chart Secret",
        )

    backup_name = f"/data/.config.json.reset-backup-{uuid.uuid4().hex}"
    # The chart's main image carries python3 for seed-config.py.  Deliberately
    # fail if config.json is absent: replacing it with a generated skeleton
    # would destroy GUI-owned connection/tools state.
    script = (
        "import json, os, sys, tempfile;"
        "p='/data/config.json';b=sys.argv[1];"
        "original=open(p,'rb').read();"
        "payload=json.load(sys.stdin);"
        "d=json.loads(original);"
        "d['admin_password']=payload['admin_password'];"
        "open(b,'xb').write(original);"
        "fd,tmp=tempfile.mkstemp(dir='/data',prefix='.config.json.reset-');"
        "os.write(fd,json.dumps(d,indent=2,ensure_ascii=False).encode());"
        "os.fsync(fd);os.close(fd);"
        "os.replace(tmp, p)"
    )

    try:
        pod_name = k8s_service.get_pod_name(
            namespace=namespace,
            label_selector=f"app.kubernetes.io/instance={name}",
        )
        k8s_service.exec_in_pod(
            namespace=namespace,
            pod_name=pod_name,
            container="",
            command=["python3", "-c", script, backup_name],
            timeout=30,
            input_data=json.dumps({"admin_password": new_password}),
        )
    except KubectlException as e:
        recovery_failed = _rollback_reset(
            # The exec connection can fail after the atomic rename. Treat the
            # write as applied unless proven otherwise, so recovery never leaves
            # a potentially patched config.json behind.
            namespace, name, previous_revision, backup_name, config_patched=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "reset-config: failed to patch config.json; previous password restored"
                if not recovery_failed else
                "reset-config: failed to patch config.json and recovery failed; operator intervention required"
            ),
        ) from e

    try:
        # The release name is not the Deployment name (chart fullname renders
        # '{release}-mcp-server'), so discover it the same way the pod lookup
        # above does.
        deployment_name = k8s_service.get_deployment_name(
            namespace=namespace,
            label_selector=f"app.kubernetes.io/instance={name}",
        )
        k8s_service.rollout_restart_deployment(
            namespace=namespace,
            deployment_name=deployment_name,
        )
        k8s_service.rollout_status_deployment(
            namespace=namespace,
            deployment_name=deployment_name,
        )
    except KubectlException as e:
        recovery_error = _rollback_reset(
            namespace, name, previous_revision, backup_name, config_patched=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "reset-config: rollout failed; previous password restored"
                if not recovery_error else
                "reset-config: rollout failed and recovery failed; operator intervention required"
            ),
        ) from e

    try:
        k8s_service.exec_in_pod(
            namespace=namespace,
            pod_name=k8s_service.get_pod_name(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            ),
            container="",
            command=["python3", "-c", "import os,sys;os.unlink(sys.argv[1])", backup_name],
            timeout=30,
        )
    except KubectlException as e:
        logger.warning("reset-config backup cleanup failed for %s/%s: %s", namespace, name, e)

    return {
        "action": "reset-config",
        "namespace": namespace,
        "release": name,
        "helm_revision": release.revision,
        "config_json_patched": True,
        "helm_upgraded": True,
        "rollout_restart_issued": True,
        "admin_password_carrier": "config_json",
        "note": "preserve connection/tools — read-modify-write only admin_password",
    }


def _reset_via_helm_secret(
    namespace: str,
    name: str,
    request: ReleaseResetConfigRequest,
) -> Dict[str, Any]:
    """#449：密碼載體＝chart Secret 的 reset 路（hermes-agent / open-design）。

    這條路**刻意不碰 config.json**：這些 chart 的 PVC 裡沒有那個檔（hermes-agent
    掛 ``/opt/data``、open-design 掛 ``/app/.od``），對它 exec patch 必炸。

    為什麼 helm upgrade 之後還一定要 rollout restart：兩個 chart 的 Deployment
    都**沒有** ``checksum/secret`` annotation（實查 charts/hermes-agent 與
    charts/open-design 的 deployment.yaml：只有 checksum/config 與
    checksum/authproxy-config）。改 Secret 不改 pod template ⇒ helm 不會滾動 pod，
    而密碼是**啟動期**被消費的（nginx basic-auth 的 htpasswd 由 initContainer 從
    Secret 算出、webui/dashboard 走 env）。少了 restart，upgrade 會「成功」但新密碼
    永遠不生效 —— 比 500 更糟的靜默失敗。

    為什麼是**全部** Deployment 而非一個：兩個 chart 都渲染 daemon + console 兩顆
    Deployment 共用同一個 ``app.kubernetes.io/instance`` label，單數版
    ``get_deployment_name`` 撞多筆會 raise「Ambiguous selector」→ 500（#449 brief
    的原始預測）。這裡用複數版一次收全，未來 chart 再長出第三顆也自動涵蓋。

    失敗補償與 config_json 路同形：先 helm rollback 回前一個 revision，再把
    Deployment 重新滾一次，讓**舊密碼**確實回到 pod。
    """
    label_selector = f"app.kubernetes.io/instance={name}"

    try:
        previous_revision = helm_service.get(namespace, name).revision
        release = helm_service.upgrade(
            namespace=namespace,
            name=name,
            chart=request.chart,
            # #448：全量 reset values 會抹掉 install 時注入的 pull secret，兩條
            # reset 路都必須自己補回來。
            values=_ensure_image_pull_secrets(request.values),
            sensitive_values=request.sensitive_values,
            version=request.version,
            reset_values=request.reset_values,
            reuse_values=request.reuse_values,
            atomic=True,
        )
    except (ValueError, HelmException) as e:
        logger.error(
            "reset-config (helm_secret) Helm upgrade failed for %s/%s: %s",
            namespace, name, e,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="reset-config: failed to update the chart Secret",
        )

    try:
        deployment_names = k8s_service.get_deployment_names(
            namespace=namespace,
            label_selector=label_selector,
        )
        # 先全部下 restart 再逐一等待：兩顆 Deployment 的 rollout 因此並行推進，
        # 總耗時 ≈ max 而非 sum —— 平台 client 只有 120s 預算，逐顆序列等待很容易
        # 超時（超時 ⇒ 平台不存新密碼、租戶得重按，見 cloud_service C11 註解）。
        for deployment_name in deployment_names:
            k8s_service.rollout_restart_deployment(
                namespace=namespace,
                deployment_name=deployment_name,
            )
        for deployment_name in deployment_names:
            k8s_service.rollout_status_deployment(
                namespace=namespace,
                deployment_name=deployment_name,
            )
    except KubectlException as e:
        recovery_failed = _rollback_helm_secret_reset(
            namespace, name, previous_revision, label_selector,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "reset-config: rollout failed; previous password restored"
                if not recovery_failed else
                "reset-config: rollout failed and recovery failed; operator intervention required"
            ),
        ) from e

    return {
        "action": "reset-config",
        "namespace": namespace,
        "release": name,
        "helm_revision": release.revision,
        "config_json_patched": False,
        "helm_upgraded": True,
        "rollout_restart_issued": True,
        "admin_password_carrier": "helm_secret",
        "restarted_deployments": deployment_names,
        "note": "password lives in the chart Secret — no config.json patch",
    }


def _rollback_helm_secret_reset(
    namespace: str,
    name: str,
    previous_revision: int,
    label_selector: str,
) -> bool:
    """Compensate a helm_secret reset failure. Returns True when recovery failed.

    Rolling Helm back restores the *old* password in the Secret, but a pod that
    already restarted is running the new one — so the workloads must be rolled
    again for the rollback to actually reach them.
    """
    recovery_failed = False
    try:
        helm_service.rollback(namespace, name, previous_revision)
    except HelmException as e:
        logger.error(
            "reset-config (helm_secret) Helm rollback failed for %s/%s: %s",
            namespace, name, e,
        )
        recovery_failed = True

    if recovery_failed:
        # Secret 還停在新密碼；再滾 pod 只會把「沒人持有的密碼」推上線，更糟。
        return recovery_failed

    try:
        deployment_names = k8s_service.get_deployment_names(
            namespace=namespace,
            label_selector=label_selector,
        )
        for deployment_name in deployment_names:
            k8s_service.rollout_restart_deployment(namespace, deployment_name)
        for deployment_name in deployment_names:
            k8s_service.rollout_status_deployment(namespace, deployment_name)
    except KubectlException as e:
        logger.error(
            "reset-config (helm_secret) rollback rollout failed for %s/%s: %s",
            namespace, name, e,
        )
        recovery_failed = True
    return recovery_failed


def _rollback_reset(
    namespace: str,
    name: str,
    previous_revision: int,
    backup_name: str,
    config_patched: bool,
) -> bool:
    """Compensate a reset failure without reading an old password value."""
    recovery_failed = False
    if config_patched:
        try:
            pod_name = k8s_service.get_pod_name(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            )
            k8s_service.exec_in_pod(
                namespace=namespace,
                pod_name=pod_name,
                container="",
                command=[
                    "python3", "-c",
                    "import os,sys;p,b=sys.argv[1:];os.replace(b,p)",
                    "/data/config.json", backup_name,
                ],
                timeout=30,
            )
        except KubectlException as e:
            logger.error("reset-config config rollback failed for %s/%s: %s", namespace, name, e)
            recovery_failed = True
    try:
        helm_service.rollback(namespace, name, previous_revision)
    except HelmException as e:
        logger.error("reset-config Helm rollback failed for %s/%s: %s", namespace, name, e)
        recovery_failed = True
    if config_patched and not recovery_failed:
        try:
            deployment_name = k8s_service.get_deployment_name(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            )
            k8s_service.rollout_restart_deployment(namespace, deployment_name)
            k8s_service.rollout_status_deployment(namespace, deployment_name)
        except KubectlException as e:
            logger.error("reset-config rollback rollout failed for %s/%s: %s", namespace, name, e)
            recovery_failed = True
    return recovery_failed


def _validate_namespace_or_raise(namespace: str) -> None:
    """Raise 403 if namespace fails prefix validation. Mirrors helm.py validate_namespace."""
    from src.services.helm import validate_namespace  # late import: avoid cycle in tests
    try:
        validate_namespace(namespace)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post(
    "/{namespace}/{name}/sidecar",
    response_model=SidecarPatchResponse,
    summary="Patch deployment with sidecar container",
)
async def patch_sidecar(
    namespace: str,
    name: str,
    request: SidecarPatchRequest,
):
    """Patch a release's deployment to add a sidecar container.

    This endpoint uses kubectl JSON patch to inject an additional container
    into the deployment's pod spec. This is useful when the Helm chart does
    not natively support sidecar/extra containers.

    After patching the deployment, this endpoint also:
    1. Creates a ClusterIP K8s Service for the sidecar port (e.g., {release}-mcp)
    2. Creates a Cloudflare Tunnel route so the sidecar is externally reachable

    Args:
        namespace: Release namespace
        name: Release name (used to find the deployment if deployment_name not provided)
        request: Sidecar container specification

    Returns:
        Confirmation of the sidecar patch including MCP endpoint URL

    Raises:
        HTTPException: If patching fails
    """
    try:
        # Determine which deployment to patch
        deployment_name = request.deployment_name
        if not deployment_name:
            # Auto-detect deployment from release label
            deployments = k8s_service.get_deployments(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            )
            if not deployments:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No deployments found for release {name} in namespace {namespace}",
                )
            deployment_name = deployments[0]["name"]

        # Build the sidecar container spec for JSON patch
        container_spec = {
            "name": request.container.name,
            "image": request.container.image,
        }
        if request.container.ports:
            container_spec["ports"] = request.container.ports
        if request.container.env:
            container_spec["env"] = request.container.env
        if request.container.resources:
            container_spec["resources"] = request.container.resources
        if request.container.liveness_probe:
            container_spec["livenessProbe"] = request.container.liveness_probe
        if request.container.readiness_probe:
            container_spec["readinessProbe"] = request.container.readiness_probe

        # Build JSON patch to add container to pod spec
        patch = json.dumps([{
            "op": "add",
            "path": "/spec/template/spec/containers/-",
            "value": container_spec,
        }])

        k8s_service.patch_deployment(
            namespace=namespace,
            deployment_name=deployment_name,
            patch=patch,
            patch_type="json",
        )

        logger.info(
            f"Patched deployment {deployment_name} in {namespace} "
            f"with sidecar container {request.container.name}"
        )

        # Create K8s Service and Cloudflare route for the sidecar
        mcp_service_name = None
        mcp_endpoint_url = None
        mcp_internal_url = None

        sidecar_port = _get_sidecar_port(request)
        if sidecar_port:
            mcp_service_name, mcp_internal_url, mcp_endpoint_url = (
                await _create_sidecar_service(
                    namespace=namespace,
                    release_name=name,
                    sidecar_port=sidecar_port,
                )
            )

        return SidecarPatchResponse(
            message=f"Sidecar container '{request.container.name}' added to deployment '{deployment_name}'",
            deployment=deployment_name,
            namespace=namespace,
            container_name=request.container.name,
            mcp_service_name=mcp_service_name,
            mcp_endpoint_url=mcp_endpoint_url,
            mcp_internal_url=mcp_internal_url,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except KubectlException as e:
        logger.error(f"kubectl patch failed: {e.message}\nStderr: {e.stderr}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to patch deployment with sidecar. Check operator logs for details.",
        )


# Helper functions for sidecar service and route creation


def _get_sidecar_port(request: SidecarPatchRequest) -> Optional[int]:
    """Extract the sidecar container port from the patch request.

    Args:
        request: Sidecar patch request

    Returns:
        First container port number, or None if no ports defined
    """
    if not request.container.ports:
        return None
    for port_spec in request.container.ports:
        port = port_spec.get("containerPort")
        if port:
            return port
    return None


async def _create_sidecar_service(
    namespace: str,
    release_name: str,
    sidecar_port: int,
) -> tuple:
    """Create the in-cluster ClusterIP Service for the MCP sidecar.

    Creates a ClusterIP K8s Service named ``{release_name}-mcp`` targeting the
    sidecar port, so the agent data-plane reaches the MCP sidecar via in-cluster
    DNS (``{release}-mcp.{ns}.svc.cluster.local``).

    #301: this deliberately does NOT create a public Cloudflare ``{subdomain}-mcp``
    route for the sidecar. That route was dead weight (Odoo drops the public URL,
    the agent data-plane uses in-cluster DNS) and an unprotected public attack
    surface (no CF Access; the #280 baseline NP allows cloudflared→sidecar; only
    the sidecar's bearer token guards it). The release-teardown path still deletes
    any residual ``{subdomain}-mcp`` route so pre-existing orphans converge. See
    decision doc ``agent-dataplane-in-cluster-no-cf-access-ip-bypass``.

    Service creation failure does NOT propagate - the sidecar patch is already done.

    Args:
        namespace: Kubernetes namespace
        release_name: Helm release name
        sidecar_port: Port the sidecar listens on

    Returns:
        Tuple of (mcp_service_name, mcp_internal_url, mcp_endpoint_url). By design
        (#301) mcp_endpoint_url is always None; mcp_internal_url is None if Service
        creation failed.
    """
    mcp_service_name = f"{release_name}-mcp"
    mcp_internal_url = None

    # Create the in-cluster ClusterIP Service for the sidecar
    service_manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": mcp_service_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/instance": release_name,
                "app.kubernetes.io/component": "mcp-sidecar",
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/instance": release_name,
            },
            "ports": [
                {
                    "port": sidecar_port,
                    "targetPort": sidecar_port,
                    "protocol": "TCP",
                    "name": "mcp",
                },
            ],
        },
    }

    try:
        k8s_service.apply_manifest(service_manifest)
        mcp_internal_url = (
            f"http://{mcp_service_name}.{namespace}.svc.cluster.local:{sidecar_port}"
        )
        logger.info(
            f"Created K8s Service {mcp_service_name} in {namespace} "
            f"targeting port {sidecar_port}"
        )
    except Exception as e:
        logger.error(
            f"Failed to create K8s Service for sidecar: {e}. "
            "Sidecar patch was successful but service is not exposed."
        )
        return mcp_service_name, None, None

    # #301: no public Cloudflare route for the sidecar (see docstring). The MCP
    # sidecar is reachable only in-cluster (agent data-plane) or via port-forward:
    #   kubectl port-forward -n {ns} svc/{release}-mcp {port}:{port}
    return mcp_service_name, mcp_internal_url, None


# Helper functions for Cloudflare integration


async def _create_cloudflare_route(
    namespace: str,
    release_name: str,
    expose_config,
) -> Optional[RouteInfo]:
    """Create a Cloudflare Tunnel route for a release.

    Args:
        namespace: Kubernetes namespace
        release_name: Helm release name
        expose_config: ExposeConfig from request

    Returns:
        RouteInfo if route was created, None otherwise
    """
    if not cloudflare_service.enabled:
        logger.info("Cloudflare integration disabled, skipping route creation")
        return None

    try:
        # Determine subdomain
        subdomain = expose_config.subdomain
        if not subdomain:
            subdomain = cloudflare_service.generate_subdomain(namespace, release_name)

        # Determine service name and port
        service_name = expose_config.service_name
        service_port = expose_config.service_port

        if not service_name or not service_port:
            # Auto-detect from K8s services
            # Wait a bit for services to be created
            await asyncio.sleep(3)

            services = k8s_service.get_services(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={release_name}",
            )

            if not services:
                logger.warning(f"No services found for release {release_name}")
                return None

            # Find first non-headless service with HTTP-like port
            for svc in services:
                # Skip headless services
                if svc.get("clusterIP") == "None":
                    continue

                # Use provided values or detect from service
                if not service_name:
                    service_name = svc["name"]

                if not service_port:
                    # Prefer common HTTP ports
                    http_ports = [80, 8080, 3000, 8000, 443, 8443]
                    for port_info in svc.get("ports", []):
                        port = port_info.get("port")
                        if port in http_ports:
                            service_port = port
                            break
                    # Fallback to first port
                    if not service_port and svc.get("ports"):
                        service_port = svc["ports"][0].get("port")

                if service_name and service_port:
                    break

        if not service_name or not service_port:
            logger.warning(
                f"Could not determine service to expose for release {release_name}"
            )
            return None

        # Generate internal service URL
        service_url = cloudflare_service.generate_service_url(
            namespace=namespace,
            service_name=service_name,
            port=service_port,
        )

        # Create the route
        await cloudflare_service.create_route(
            subdomain=subdomain,
            service_url=service_url,
        )

        hostname = f"{subdomain}.{settings.cloudflare_domain}"
        logger.info(f"Created Cloudflare route: {hostname} -> {service_url}")

        return RouteInfo(
            hostname=hostname,
            service_url=service_url,
            enabled=True,
        )

    except CloudflareException as e:
        logger.error(f"Failed to create Cloudflare route: {e.message}")
        # Don't fail the release creation, just log the error
        return None
    except Exception as e:
        logger.error(f"Unexpected error creating Cloudflare route: {e}")
        return None

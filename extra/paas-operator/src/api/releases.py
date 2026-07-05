"""API endpoints for Helm release management."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src.config import settings
from src.models.schemas import (
    ReleaseCreateRequest,
    ReleaseInfo,
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

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["releases"])

helm_service = HelmService()
k8s_service = KubernetesService()
cloudflare_service = CloudflareService()


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
        # Private-image charts (e.g. mcp-server pulling hermes-mcp-admin from
        # jcr-prod) need imagePullSecrets so the kubelet can authenticate; the
        # secret is provisioned into the ns by ensure_registry_secret. Mirrors
        # agent_runtimes.py (F-PILOT-4) — the cloud-service install path was the
        # missing half (same gap family as orm-vs-endpoint "half-applied" fixes).
        # setdefault lets a template's helm_default_values override; public-image
        # charts (bitnami n8n/odoo) ignore an unused top-level imagePullSecrets.
        values = dict(request.values or {})
        values.setdefault(
            "imagePullSecrets", [{"name": settings.registry_secret_name}]
        )
        release = helm_service.install(
            namespace=request.namespace,
            name=request.name,
            chart=request.chart,
            values=values,
            version=request.version,
            create_namespace=request.create_namespace,
        )
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
        release = helm_service.upgrade(
            namespace=namespace,
            name=name,
            chart=request.chart,
            values=request.values,
            version=request.version,
            reset_values=request.reset_values,
            reuse_values=request.reuse_values,
        )
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
def get_release_status(namespace: str, name: str):
    """Get detailed status including pod information.

    Args:
        namespace: Release namespace
        name: Release name

    Returns:
        Release and pod status information

    Raises:
        HTTPException: If status retrieval fails
    """
    try:
        # Get release info
        release = helm_service.get(namespace, name)

        # Get pods in namespace (filter by app.kubernetes.io/instance label)
        pods = []
        pod_retrieval_error = None
        try:
            pods = k8s_service.get_pods(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            )
        except Exception as e:
            logger.error(f"Failed to get pods for {namespace}/{name}: {e}")
            pod_retrieval_error = str(e)

        return ReleaseStatusResponse(
            release=release,
            pods=pods,
            pod_retrieval_error=pod_retrieval_error,
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
    """Scale all workloads (Deployments + StatefulSets) in a Helm release.

    Used for subscription suspend (replicas=0) and resume (replicas=1).
    Bitnami charts deploy their primary workload as a StatefulSet so we
    cover both kinds; releases that have neither are a 404.

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

        if not deployments and not statefulsets:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No deployments or statefulsets found for release {name} in namespace {namespace}",
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

        total = len(scaled_deps) + len(scaled_sts)
        return {
            "message": f"Scaled {total} workload(s) to {replicas} replicas",
            "deployments": ", ".join(scaled_deps),
            "statefulsets": ", ".join(scaled_sts),
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

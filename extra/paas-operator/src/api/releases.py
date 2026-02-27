"""API endpoints for Helm release management."""
import asyncio
import json
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, status

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
        release = helm_service.install(
            namespace=request.namespace,
            name=request.name,
            chart=request.chart,
            values=request.values,
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
async def get_release(namespace: str, name: str):
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
async def upgrade_release(
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
            mcp_subdomain = (subdomain or cloudflare_service.generate_subdomain(namespace, name)) + "-mcp"
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
async def rollback_release(
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
async def get_release_revisions(namespace: str, name: str):
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
async def get_release_status(namespace: str, name: str):
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
                await _create_sidecar_service_and_route(
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


async def _create_sidecar_service_and_route(
    namespace: str,
    release_name: str,
    sidecar_port: int,
) -> tuple:
    """Create a K8s ClusterIP Service and Cloudflare route for the MCP sidecar.

    This function:
    1. Creates a ClusterIP K8s Service named {release_name}-mcp targeting the sidecar port
    2. Creates a Cloudflare Tunnel route for external access

    Service creation failure does NOT propagate - the sidecar patch is already done.

    Args:
        namespace: Kubernetes namespace
        release_name: Helm release name
        sidecar_port: Port the sidecar listens on

    Returns:
        Tuple of (mcp_service_name, mcp_internal_url, mcp_endpoint_url)
        Any value may be None if the corresponding operation failed.
    """
    mcp_service_name = f"{release_name}-mcp"
    mcp_internal_url = None
    mcp_endpoint_url = None

    # Step 1: Create ClusterIP Service for the sidecar
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

    # Step 2: Create Cloudflare route for external access
    if cloudflare_service.enabled:
        try:
            # Generate subdomain: {original-subdomain}-mcp
            base_subdomain = cloudflare_service.generate_subdomain(namespace, release_name)
            mcp_subdomain = f"{base_subdomain}-mcp"

            service_url = cloudflare_service.generate_service_url(
                namespace=namespace,
                service_name=mcp_service_name,
                port=sidecar_port,
            )

            await cloudflare_service.create_route(
                subdomain=mcp_subdomain,
                service_url=service_url,
            )

            mcp_hostname = f"{mcp_subdomain}.{settings.cloudflare_domain}"
            mcp_endpoint_url = f"https://{mcp_hostname}/mcp"
            logger.info(
                f"Created Cloudflare route for MCP sidecar: {mcp_hostname} -> {service_url}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to create Cloudflare route for MCP sidecar: {e}. "
                "MCP sidecar is only reachable via internal K8s URL or port-forward."
            )
    else:
        logger.info(
            "Cloudflare disabled. MCP sidecar reachable via internal URL or port-forward: "
            f"kubectl port-forward -n {namespace} svc/{mcp_service_name} {sidecar_port}:{sidecar_port}"
        )

    return mcp_service_name, mcp_internal_url, mcp_endpoint_url


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

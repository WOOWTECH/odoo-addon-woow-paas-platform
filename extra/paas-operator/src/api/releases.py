"""API endpoints for Helm release management."""
import asyncio
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
)
from src.services.cloudflare import CloudflareException, CloudflareService
from src.services.helm import HelmException, HelmService, KubernetesService

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

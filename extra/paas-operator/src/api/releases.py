"""API endpoints for Helm release management."""
import logging
from typing import Dict, List

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    ReleaseCreateRequest,
    ReleaseInfo,
    ReleaseRevisionsResponse,
    ReleaseRollbackRequest,
    ReleaseStatusResponse,
    ReleaseUpgradeRequest,
)
from src.services.helm import HelmException, HelmService, KubernetesService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["releases"])

helm_service = HelmService()
k8s_service = KubernetesService()


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
        return release

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Helm install failed: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Helm installation failed: {e.message}",
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get release: {e.message}",
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
        logger.error(f"Helm upgrade failed: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Helm upgrade failed: {e.message}",
        )


@router.delete(
    "/{namespace}/{name}",
    response_model=Dict[str, str],
    summary="Uninstall a Helm release",
)
async def delete_release(namespace: str, name: str):
    """Uninstall a Helm release.

    Args:
        namespace: Release namespace
        name: Release name

    Returns:
        Deletion confirmation message

    Raises:
        HTTPException: If uninstallation fails
    """
    try:
        result = helm_service.uninstall(namespace, name)
        logger.info(f"Uninstalled release {name} from {namespace}")
        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Helm uninstall failed: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Helm uninstall failed: {e.message}",
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
        logger.error(f"Helm rollback failed: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Helm rollback failed: {e.message}",
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
        logger.error(f"Failed to get history: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get release history: {e.message}",
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
        try:
            pods = k8s_service.get_pods(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/instance={name}",
            )
        except Exception as e:
            logger.warning(f"Failed to get pods: {e}")
            pods = []

        return ReleaseStatusResponse(release=release, pods=pods)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except HelmException as e:
        logger.error(f"Failed to get status: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get release status: {e.message}",
        )

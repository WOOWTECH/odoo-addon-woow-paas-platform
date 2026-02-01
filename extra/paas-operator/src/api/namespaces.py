"""API endpoints for namespace management."""
import logging
from typing import Dict

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import NamespaceCreateRequest
from src.services.helm import KubernetesService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/namespaces", tags=["namespaces"])

k8s_service = KubernetesService()


@router.post(
    "",
    response_model=Dict[str, str],
    status_code=status.HTTP_201_CREATED,
    summary="Create a namespace with resource quota",
)
async def create_namespace(request: NamespaceCreateRequest):
    """Create a new Kubernetes namespace with resource quota.

    The namespace must start with 'paas-ws-' prefix for security.

    Args:
        request: Namespace creation parameters

    Returns:
        Creation confirmation message

    Raises:
        HTTPException: If namespace creation fails
    """
    try:
        result = k8s_service.create_namespace(
            name=request.name,
            cpu_limit=request.cpu_limit,
            memory_limit=request.memory_limit,
            storage_limit=request.storage_limit,
        )
        logger.info(f"Created namespace {request.name} with quota")
        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to create namespace: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create namespace: {str(e)}",
        )

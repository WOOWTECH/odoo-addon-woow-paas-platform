"""API endpoints for post-deploy initialization."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import N8nInitRequest, N8nInitResponse
from src.services.helm import KubernetesService
from src.services.n8n_init import N8nInitError, N8nInitService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["init"])

k8s_service = KubernetesService()
n8n_init_service = N8nInitService(k8s_service=k8s_service)


@router.post(
    "/{namespace}/{release_name}/init/n8n",
    response_model=N8nInitResponse,
    summary="Initialize n8n instance",
    description="Complete n8n post-deploy initialization: owner setup + API key generation",
)
async def init_n8n(
    namespace: str,
    release_name: str,
    request: N8nInitRequest,
):
    """Initialize an n8n instance after deployment.

    This endpoint:
    1. Waits for n8n API to be ready
    2. Creates owner user
    3. Generates API key
    4. Updates K8s Secret and sidecar env

    Args:
        namespace: K8s namespace
        release_name: Helm release name
        request: Owner credentials

    Returns:
        Initialization result with API key
    """
    try:
        result = n8n_init_service.initialize(
            namespace=namespace,
            release_name=release_name,
            owner_email=request.owner_email,
            owner_password=request.owner_password,
        )

        return N8nInitResponse(
            success=True,
            api_key=result["api_key"],
            owner_email=result["owner_email"],
            message="n8n initialization complete",
        )

    except N8nInitError as e:
        logger.error("n8n init failed at step '%s': %s", e.step, str(e))
        return N8nInitResponse(
            success=False,
            error=f"[{e.step}] {str(e)}",
            message=f"n8n initialization failed at step: {e.step}",
        )

    except Exception as e:
        logger.exception("Unexpected error during n8n initialization")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"n8n initialization failed: {str(e)}",
        )

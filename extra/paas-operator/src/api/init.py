"""API endpoints for post-deploy initialization."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import N8nInitRequest, N8nInitResponse, OdooInitResponse
from src.services.helm import KubernetesService
from src.services.n8n_init import N8nInitError, N8nInitService
from src.services.odoo_init import OdooInitError, OdooInitService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["init"])

k8s_service = KubernetesService()
n8n_init_service = N8nInitService(k8s_service=k8s_service)
odoo_init_service = OdooInitService(k8s_service=k8s_service)


def _unexpected_init_failure(message: str) -> HTTPException:
    """非預期例外的 500（#1263）：``detail`` 帶結構化分類，一律 unknown。

    ``error_step`` 為 None——例外不是從已知步驟丟出來的，不能假裝知道卡在哪。
    ``ErrorResponse.error_type`` 另由 exception handler 填例外類別名稱，兩者不同維度。
    """
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"message": message, "error_step": None, "error_kind": "unknown"},
    )


@router.post(
    "/{namespace}/{release_name}/init/n8n",
    response_model=N8nInitResponse,
    summary="Initialize n8n instance",
    description="Complete n8n post-deploy initialization: owner setup + API key generation",
)
def init_n8n(
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

        is_success = result.get("success", True)
        return N8nInitResponse(
            success=is_success,
            api_key=result["api_key"],
            owner_email=result["owner_email"],
            secret_patched=result.get("secret_patched", False),
            pod_restarted=result.get("pod_restarted", False),
            message="n8n initialization complete" if is_success else "n8n API key created but K8s Secret update failed",
            error_step=result.get("error_step"),
            error_kind=result.get("error_kind"),
        )

    except N8nInitError as e:
        logger.error("n8n init failed at step '%s': %s", e.step, str(e))
        return N8nInitResponse(
            success=False,
            error=f"[{e.step}] {str(e)}",
            message=f"n8n initialization failed at step: {e.step}",
            error_step=e.step,
            error_kind=e.kind,
        )

    except Exception as e:
        logger.exception("Unexpected error during n8n initialization")
        raise _unexpected_init_failure(f"n8n initialization failed: {str(e)}")


@router.post(
    "/{namespace}/{release_name}/init/odoo",
    response_model=OdooInitResponse,
    summary="Initialize Odoo module list",
    description="Trigger Odoo module list update to discover newly deployed addons",
)
def init_odoo(namespace: str, release_name: str):
    """Initialize Odoo module list after deployment.

    This endpoint:
    1. Finds the Odoo pod in the namespace
    2. Waits for Odoo HTTP service to be ready
    3. Executes ir.module.module.update_list() via direct ORM access

    Args:
        namespace: K8s namespace where Odoo is deployed
        release_name: Helm release name

    Returns:
        Initialization result with module counts
    """
    try:
        result = odoo_init_service.initialize(
            namespace=namespace,
            release_name=release_name,
        )

        return OdooInitResponse(
            success=result["success"],
            modules_added=result.get("modules_added", 0),
            modules_updated=result.get("modules_updated", 0),
            message=result.get("message", "Odoo module list updated"),
        )

    except OdooInitError as e:
        logger.error("Odoo init failed at step '%s': %s", e.step, str(e))
        return OdooInitResponse(
            success=False,
            error=f"[{e.step}] {str(e)}",
            message=f"Odoo initialization failed at step: {e.step}",
            error_step=e.step,
            error_kind=e.kind,
        )

    except Exception as e:
        logger.exception("Unexpected error during Odoo initialization")
        raise _unexpected_init_failure(f"Odoo initialization failed: {str(e)}")

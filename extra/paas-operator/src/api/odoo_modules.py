"""API endpoints for Odoo module state queries."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    OdooModuleStatesRequest,
    OdooModuleStatesResponse,
    OdooModuleStateInfo,
)
from src.services.helm import KubernetesService
from src.services.odoo_module import OdooModuleError, OdooModuleService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/odoo", tags=["odoo-modules"])

k8s_service = KubernetesService()
odoo_module_service = OdooModuleService(k8s_service=k8s_service)


@router.post(
    "/{namespace}/{release_name}/module-states",
    response_model=OdooModuleStatesResponse,
    summary="Query Odoo module states",
    description="Get installation states and reverse dependencies for specified modules",
)
async def get_module_states(
    namespace: str,
    release_name: str,
    request: OdooModuleStatesRequest,
):
    """Query Odoo module installation states and dependents.

    Args:
        namespace: K8s namespace where Odoo is deployed
        release_name: Helm release name
        request: List of module technical names to check

    Returns:
        Module states with dependency information
    """
    try:
        data = odoo_module_service.get_module_states(
            namespace=namespace,
            release_name=release_name,
            module_names=request.module_names,
        )

        modules = {
            name: OdooModuleStateInfo(
                state=info["state"],
                dependents=info.get("dependents", []),
            )
            for name, info in data.get("modules", {}).items()
        }

        return OdooModuleStatesResponse(success=True, modules=modules)

    except OdooModuleError as e:
        logger.error(
            "Module state query failed at step '%s': %s", e.step, str(e)
        )
        return OdooModuleStatesResponse(
            success=False,
            error=f"[{e.step}] {str(e)}",
        )

    except Exception as e:
        logger.exception("Unexpected error querying module states")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Module state query failed: {str(e)}",
        )

"""API endpoints for Odoo module state queries and uninstallation."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    OdooModuleStatesRequest,
    OdooModuleStatesResponse,
    OdooModuleStateInfo,
    OdooUninstallModulesRequest,
    OdooUninstallModulesResponse,
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


@router.post(
    "/{namespace}/{release_name}/uninstall-modules",
    response_model=OdooUninstallModulesResponse,
    summary="Uninstall Odoo modules",
    description="Uninstall specified Odoo modules via button_immediate_uninstall()",
)
async def uninstall_modules(
    namespace: str,
    release_name: str,
    request: OdooUninstallModulesRequest,
):
    """Uninstall Odoo modules from a running instance.

    Args:
        namespace: K8s namespace where Odoo is deployed
        release_name: Helm release name
        request: List of module technical names to uninstall

    Returns:
        Uninstallation result with list of uninstalled modules
    """
    try:
        uninstalled = odoo_module_service.uninstall_modules(
            namespace=namespace,
            release_name=release_name,
            module_names=request.module_names,
        )

        return OdooUninstallModulesResponse(
            success=True,
            uninstalled=uninstalled,
        )

    except OdooModuleError as e:
        logger.error(
            "Module uninstall failed at step '%s': %s", e.step, str(e)
        )
        return OdooUninstallModulesResponse(
            success=False,
            error=f"[{e.step}] {str(e)}",
        )

    except Exception as e:
        logger.exception("Unexpected error uninstalling modules")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Module uninstall failed: {str(e)}",
        )

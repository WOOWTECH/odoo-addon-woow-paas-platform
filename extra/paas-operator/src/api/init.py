"""API endpoints for post-deploy initialization."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    AppAdminPasswordResetRequest,
    AppAdminPasswordResetResponse,
    N8nInitRequest,
    N8nInitResponse,
    OdooInitResponse,
)
from src.services.app_admin_password import (
    AppAdminPasswordService,
    AppAdminResetError,
)
from src.services.helm import KubernetesService
from src.services.n8n_init import N8nInitError, N8nInitService
from src.services.odoo_init import OdooInitError, OdooInitService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/releases", tags=["init"])

k8s_service = KubernetesService()
n8n_init_service = N8nInitService(k8s_service=k8s_service)
odoo_init_service = OdooInitService(k8s_service=k8s_service)
app_admin_password_service = AppAdminPasswordService(k8s_service=k8s_service)


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
            localization=result.get("localization"),
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


# --- App 自管管理員密碼重設（#1370／#1186）----------------------------------
#
# 與 reset-config（helm upgrade／exec patch config.json）分開：Odoo／n8n 的密碼權威
# 在 app 自己的 DB，chart 動不到。業務失敗回 200＋success=false（與 init/* 一致，平台
# 照 error_kind 分類）；非預期例外回 500＋detail.error_kind=unknown。
# ⛔ 新密碼不得出現在任何回應、log、例外訊息裡。

_MIN_APP_ADMIN_PASSWORD = 12
_MAX_APP_ADMIN_PASSWORD = 128


def _app_admin_reset(app: str, namespace: str, release_name: str,
                     request: AppAdminPasswordResetRequest, reset) -> AppAdminPasswordResetResponse:
    new_password = request.new_password.get_secret_value()
    if not (_MIN_APP_ADMIN_PASSWORD <= len(new_password) <= _MAX_APP_ADMIN_PASSWORD):
        # 什麼都還沒碰 ⇒ not_executed；訊息不回顯密碼。
        return AppAdminPasswordResetResponse(
            success=False,
            error="new_password must be 12-128 characters",
            message=f"{app} admin password reset rejected",
            error_step="validate",
            error_code="invalid_password",
            error_kind="not_executed",
        )
    try:
        result = reset(namespace=namespace, release_name=release_name, new_password=new_password)
    except AppAdminResetError as e:
        logger.error("%s admin password reset failed at step '%s' (%s): %s", app, e.step, e.kind, str(e))
        return AppAdminPasswordResetResponse(
            success=False,
            error=f"[{e.step}] {str(e)}",
            message=f"{app} admin password reset failed at step: {e.step}",
            error_step=e.step,
            error_code=e.code,
            error_kind=e.kind,
        )
    except Exception as e:
        # 只記例外類別：traceback／訊息可能夾帶呼叫參數（含新密碼）。
        logger.error("Unexpected %s during %s admin password reset in %s/%s",
                     type(e).__name__, app, namespace, release_name)
        raise _unexpected_init_failure(f"{app} admin password reset failed: {type(e).__name__}")
    return AppAdminPasswordResetResponse(
        success=True,
        login=result.get("login"),
        second_factor_cleared=bool(result.get("second_factor_cleared")),
        message=f"{app} admin password reset",
    )


@router.post(
    "/{namespace}/{release_name}/odoo/admin-password",
    response_model=AppAdminPasswordResetResponse,
    summary="Reset the Odoo administrator password",
    description="Set a new password on base.user_admin (login unchanged) and clear its TOTP, "
                "directly in the tenant database. No helm upgrade, no restart.",
)
def reset_odoo_admin_password(namespace: str, release_name: str,
                              request: AppAdminPasswordResetRequest):
    return _app_admin_reset("Odoo", namespace, release_name, request,
                            app_admin_password_service.reset_odoo_admin)


@router.post(
    "/{namespace}/{release_name}/n8n/owner-password",
    response_model=AppAdminPasswordResetResponse,
    summary="Reset the n8n owner password",
    description="Set a new password on the n8n owner account (email unchanged) and turn off its MFA, "
                "directly in the n8n database. No helm upgrade, no restart; the API key is untouched.",
)
def reset_n8n_owner_password(namespace: str, release_name: str,
                             request: AppAdminPasswordResetRequest):
    return _app_admin_reset("n8n", namespace, release_name, request,
                            app_admin_password_service.reset_n8n_owner)


@router.post(
    "/{namespace}/{release_name}/omnigent/admin-password",
    response_model=AppAdminPasswordResetResponse,
    summary="Reset the Omnigent administrator password",
    description="Set a new password on the Omnigent admin account (username unchanged) directly in "
                "the Omnigent database, the same write as the in-app Members → Reset. No helm upgrade, "
                "no restart; signed-in sessions and paired hosts are left as they are.",
)
def reset_omnigent_admin_password(namespace: str, release_name: str,
                                  request: AppAdminPasswordResetRequest):
    return _app_admin_reset("Omnigent", namespace, release_name, request,
                            app_admin_password_service.reset_omnigent_admin)

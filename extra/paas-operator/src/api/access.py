"""API endpoints for Cloudflare Access (Zero Trust) on Security Access."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    AccessSyncRequest,
    AccessSyncResponse,
    IdentityProviderInfo,
    IdentityProvidersResponse,
)
from src.services.cloudflare import CloudflareException, CloudflareService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/access", tags=["access"])

cloudflare_service = CloudflareService()


def _require_credentials() -> None:
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for Access",
        )


@router.get(
    "/identity-providers",
    response_model=IdentityProvidersResponse,
    summary="List account Access identity providers",
)
async def list_identity_providers():
    """List IdPs so the caller can see available login methods.

    403 from Cloudflare here means the token lacks
    'Access: Organizations, Identity Providers and Groups (Read)'.
    """
    _require_credentials()
    try:
        idps = await cloudflare_service.list_identity_providers()
    except CloudflareException as e:
        logger.error("Failed to list identity providers: %s", e.message)
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list identity providers: {e.message}",
        )
    return IdentityProvidersResponse(identity_providers=[
        IdentityProviderInfo(id=i.get("id", ""), name=i.get("name", ""),
                             type=i.get("type", ""))
        for i in idps
    ])


@router.put(
    "/sync",
    response_model=AccessSyncResponse,
    summary="Declaratively sync one SA's Cloudflare Access config",
)
async def sync_access(request: AccessSyncRequest):
    """Reconcile the per-SA Access Application + reusable policies."""
    _require_credentials()
    try:
        result = await cloudflare_service.sync_access_app(
            canonical_name=request.canonical_name,
            app_name=request.app_name,
            primary_hostname=request.primary_hostname,
            protected_hostnames=request.protected_hostnames,
            session_duration=request.session_duration,
            login_idp_types=request.login_idp_types,
            policies=[p.model_dump() for p in request.policies],
            enabled=request.enabled,
            existing_app_id=getattr(request, "existing_app_id", None),
            existing_policy_ids=getattr(request, "existing_policy_ids", None),
        )
    except CloudflareException as e:
        logger.error("Failed to sync access: %s", e.message)
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync access: {e.message}",
        )
    return AccessSyncResponse(**result)

"""API endpoints for dedicated Cloudflare Tunnel lifecycle management."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.config import settings
from src.models.schemas import (
    TunnelCreateRequest,
    TunnelCreateResponse,
    TunnelStatusResponse,
    TunnelTokenResponse,
)
from src.services.cloudflare import CloudflareException, CloudflareService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tunnels", tags=["tunnels"])

cloudflare_service = CloudflareService()


@router.post(
    "",
    response_model=TunnelCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a dedicated Cloudflare Tunnel",
)
async def create_tunnel(request: TunnelCreateRequest):
    """Create a new dedicated Cloudflare Tunnel with ingress config and DNS.

    This performs the full tunnel provisioning flow:
    1. Create the tunnel via Cloudflare API
    2. Retrieve the tunnel token
    3. Configure ingress rules (hostname -> service_url + catch-all 404)
    4. Create a DNS CNAME record pointing to the tunnel

    Args:
        request: Tunnel creation parameters.

    Returns:
        Tunnel ID, name, token, hostname, and DNS record ID.

    Raises:
        HTTPException: If tunnel creation fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    try:
        # Step 1: Create the tunnel
        tunnel_info = await cloudflare_service.create_tunnel(name=request.name)
        tunnel_id = tunnel_info["tunnel_id"]
        tunnel_name = tunnel_info["tunnel_name"]

        # Step 2: Get the tunnel token
        tunnel_token = await cloudflare_service.get_tunnel_token(tunnel_id)

        # Step 3: Configure ingress rules
        await cloudflare_service.configure_tunnel(
            tunnel_id=tunnel_id,
            hostname=request.hostname,
            service_url=request.service_url,
        )

        # Step 4: Create DNS record
        dns_record_id = None
        if cloudflare_service.dns_enabled:
            # Extract subdomain from hostname
            domain = settings.cloudflare_domain
            if request.hostname.endswith(f".{domain}"):
                subdomain = request.hostname[: -(len(domain) + 1)]
            else:
                subdomain = request.hostname.split(".")[0]

            try:
                dns_record_id = await cloudflare_service.create_dns_record_for_tunnel(
                    subdomain=subdomain,
                    tunnel_id=tunnel_id,
                )
            except CloudflareException as e:
                logger.warning(
                    f"DNS record creation failed for {request.hostname}: {e.message}. "
                    "Tunnel is created but DNS must be configured manually."
                )

        logger.info(
            f"Tunnel provisioned: {tunnel_name} (ID: {tunnel_id}, "
            f"hostname: {request.hostname})"
        )

        return TunnelCreateResponse(
            tunnel_id=tunnel_id,
            tunnel_name=tunnel_name,
            tunnel_token=tunnel_token,
            hostname=request.hostname,
            dns_record_id=dns_record_id,
        )

    except CloudflareException as e:
        logger.error(f"Failed to create tunnel: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create tunnel: {e.message}",
        )


@router.get(
    "/{tunnel_id}",
    response_model=TunnelStatusResponse,
    summary="Get tunnel status",
)
async def get_tunnel_status(tunnel_id: str):
    """Get the status and connection information for a Cloudflare Tunnel.

    Args:
        tunnel_id: The Cloudflare Tunnel ID.

    Returns:
        Tunnel status, name, connections, and creation timestamp.

    Raises:
        HTTPException: If status retrieval fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    try:
        tunnel_status = await cloudflare_service.get_tunnel_status(tunnel_id)

        return TunnelStatusResponse(
            tunnel_id=tunnel_status["tunnel_id"],
            name=tunnel_status["name"],
            status=tunnel_status["status"],
            connections=tunnel_status["connections"],
            created_at=tunnel_status.get("created_at"),
        )

    except CloudflareException as e:
        if e.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tunnel {tunnel_id} not found",
            )
        logger.error(f"Failed to get tunnel status: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get tunnel status: {e.message}",
        )


@router.get(
    "/{tunnel_id}/token",
    response_model=TunnelTokenResponse,
    summary="Get tunnel token",
)
async def get_tunnel_token(tunnel_id: str):
    """Get the token for a Cloudflare Tunnel.

    The token is used by cloudflared to authenticate and connect to the tunnel.

    Args:
        tunnel_id: The Cloudflare Tunnel ID.

    Returns:
        Tunnel ID and token.

    Raises:
        HTTPException: If token retrieval fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    try:
        token = await cloudflare_service.get_tunnel_token(tunnel_id)

        return TunnelTokenResponse(
            tunnel_id=tunnel_id,
            token=token,
        )

    except CloudflareException as e:
        logger.error(f"Failed to get tunnel token: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get tunnel token: {e.message}",
        )


@router.delete(
    "/{tunnel_id}",
    summary="Delete a tunnel",
)
async def delete_tunnel(tunnel_id: str):
    """Delete a Cloudflare Tunnel and clean up associated resources.

    This will:
    1. Clean up DNS records pointing to the tunnel
    2. Delete the tunnel with cascade (removes connections)

    Args:
        tunnel_id: The Cloudflare Tunnel ID.

    Returns:
        Deletion confirmation message.

    Raises:
        HTTPException: If tunnel deletion fails.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    try:
        await cloudflare_service.delete_tunnel(tunnel_id)

        logger.info(f"Tunnel {tunnel_id} deleted successfully")
        return {"message": f"Tunnel {tunnel_id} deleted successfully"}

    except CloudflareException as e:
        logger.error(f"Failed to delete tunnel: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete tunnel: {e.message}",
        )

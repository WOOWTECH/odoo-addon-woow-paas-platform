"""API endpoints for Cloudflare Tunnel route management."""
import logging
from typing import List

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from src.config import settings
from src.services.cloudflare import CloudflareException, CloudflareService, TunnelRoute

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/routes", tags=["routes"])

cloudflare_service = CloudflareService()


class RouteCreateRequest(BaseModel):
    """Request to create a new tunnel route."""

    subdomain: str = Field(..., description="Subdomain (e.g., 'myapp' for myapp.domain.com)")
    service_url: str = Field(
        ...,
        description="Internal service URL (e.g., 'http://svc.ns.svc.cluster.local:8080')",
    )
    path: str | None = Field(None, description="Optional path prefix to match")


class RouteResponse(BaseModel):
    """Response for route operations."""

    hostname: str
    service: str
    path: str | None = None


class RouteListResponse(BaseModel):
    """Response containing list of routes."""

    routes: List[TunnelRoute]
    enabled: bool = Field(..., description="Whether Cloudflare integration is enabled")
    domain: str = Field(..., description="Base domain for routes")


@router.get(
    "",
    response_model=RouteListResponse,
    summary="List all tunnel routes",
)
async def list_routes():
    """List all Cloudflare Tunnel routes.

    Returns:
        List of configured routes
    """
    try:
        routes = await cloudflare_service.list_routes()
        return RouteListResponse(
            routes=routes,
            enabled=cloudflare_service.enabled,
            domain=settings.cloudflare_domain or "",
        )
    except CloudflareException as e:
        logger.error(f"Failed to list routes: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list routes. Check operator logs for details.",
        )


@router.post(
    "",
    response_model=RouteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tunnel route",
)
async def create_route(request: RouteCreateRequest):
    """Create a new Cloudflare Tunnel route.

    Args:
        request: Route creation parameters

    Returns:
        Created route information

    Raises:
        HTTPException: If route creation fails
    """
    if not cloudflare_service.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare integration is not enabled",
        )

    try:
        await cloudflare_service.create_route(
            subdomain=request.subdomain,
            service_url=request.service_url,
            path=request.path,
        )

        hostname = f"{request.subdomain}.{settings.cloudflare_domain}"
        logger.info(f"Created route: {hostname} -> {request.service_url}")

        return RouteResponse(
            hostname=hostname,
            service=request.service_url,
            path=request.path,
        )

    except CloudflareException as e:
        logger.error(f"Failed to create route: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create route. Check operator logs for details.",
        )


@router.delete(
    "/{subdomain}",
    response_model=dict,
    summary="Delete a tunnel route",
)
async def delete_route(subdomain: str):
    """Delete a Cloudflare Tunnel route.

    Args:
        subdomain: Subdomain of the route to delete

    Returns:
        Deletion confirmation

    Raises:
        HTTPException: If route deletion fails
    """
    if not cloudflare_service.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare integration is not enabled",
        )

    try:
        await cloudflare_service.delete_route(subdomain)
        hostname = f"{subdomain}.{settings.cloudflare_domain}"
        logger.info(f"Deleted route: {hostname}")

        return {"message": f"Route {hostname} deleted successfully"}

    except CloudflareException as e:
        logger.error(f"Failed to delete route: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete route. Check operator logs for details.",
        )

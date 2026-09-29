"""Per-hostname traffic for Security Access routes (#1234)."""
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from src.config import settings
from src.services import cf_analytics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class TrafficRequest(BaseModel):
    hostnames: list[str] = Field(..., max_length=cf_analytics.MAX_HOSTNAMES)
    window_hours: int = Field(24, ge=1, le=cf_analytics.MAX_WINDOW_HOURS)


@router.post("/traffic", summary="Request counts and last request time per hostname")
async def hostname_traffic(request: TrafficRequest):
    """Always 200 with ``available``/``reason`` — a missing analytics permission
    is a configuration state for the UI to explain, not a server error.

    Only hostnames under the platform domain are accepted: this endpoint must
    not become a way to read analytics for arbitrary names in the zone.
    """
    domain = (settings.cloudflare_domain or "").lower().strip(".")
    hostnames = [h.lower().strip(".") for h in request.hostnames]
    foreign = [h for h in hostnames if not domain or not h.endswith("." + domain)]
    if foreign:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"hostnames outside the platform domain: {foreign[:5]}",
        )
    return await cf_analytics.get_hostname_traffic(hostnames, request.window_hours)

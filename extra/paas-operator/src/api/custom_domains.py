"""Authenticated, fail-closed API for Cloudflare for SaaS custom hostnames."""
from __future__ import annotations

import ipaddress
import asyncio
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, field_validator

from src.config import settings
from src.services.cloudflare import CloudflareException, CloudflareService, dns_record_points_at
from src.services.custom_domains import CustomDomainsClient
from src.services.custom_domain_gateway import (
    GatewayRouteError, gateway_configured, publish, route_loaded, unpublish,
)

router = APIRouter(prefix="/api/custom-domains", tags=["custom-domains"])

_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# The dedicated tunnel has one immutable catch-all origin. Its gateway route
# registry is a Kubernetes ConfigMap updated with resourceVersion CAS, so no
# tenant onboarding writes the shared tunnel's whole ingress document.
_GATEWAY_REASON = "dedicated_gateway_not_configured"
_EDGE_NOT_READY = "dedicated_gateway_not_ready"
_edge_cache: tuple[float, bool] = (0.0, False)


def normalize_hostname(value: str) -> str:
    """Return a strict lower-case ASCII DNS hostname or reject it."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("must be a DNS hostname")
    if "://" in value or "/" in value or ":" in value or value.startswith("*."):
        raise ValueError("must be a DNS hostname without URL, port, or wildcard")
    candidate = value[:-1] if value.endswith(".") else value
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not allowed")
    try:
        ascii_name = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid IDNA hostname") from exc
    if len(ascii_name) > 253 or not ascii_name or "." not in ascii_name:
        raise ValueError("must be a fully-qualified DNS hostname")
    for label in ascii_name.split("."):
        if (
            not 1 <= len(label) <= 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
        ):
            raise ValueError("invalid DNS hostname")
    return ascii_name


def _zone() -> str:
    try:
        return normalize_hostname(settings.cloudflare_domain)
    except ValueError:
        return ""


def _configured() -> bool:
    try:
        target = normalize_hostname(settings.custom_domains_target_hostname)
    except ValueError:
        return False
    zone = _zone()
    return bool(
        settings.custom_domains_enabled
        and settings.cloudflare_api_token
        and settings.cloudflare_zone_id
        and zone
        and target.endswith(f".{zone}")
        and target != zone
        and gateway_configured()
    )


async def _edge_ready() -> bool:
    """Confirm static routing, active SaaS fallback, gateway and token scope.

    This is read-only. The brief cache avoids four provider probes on every
    reconciliation tick; a stale positive cannot bypass the gateway's own
    exact-Host mapping or the per-route loaded check.
    """
    global _edge_cache
    if not _configured():
        return False
    now = time.monotonic()
    if now < _edge_cache[0]:
        return _edge_cache[1]
    cf = CloudflareService()
    if not cf.enabled or not cf.dns_enabled:
        return False
    cf.tunnel_id = settings.custom_domains_gateway_tunnel_id
    target = normalize_hostname(settings.custom_domains_target_hostname)

    async def gateway_health() -> bool:
        async with httpx.AsyncClient(timeout=4.0) as client:
            result = await client.get(f"{settings.custom_domains_gateway_url}/healthz")
            return result.status_code == 200

    try:
        config, dns, healthy, scoped, fallback = await asyncio.wait_for(asyncio.gather(
            cf.get_tunnel_config(),
            cf.get_dns_record(target, raise_on_error=True),
            gateway_health(),
            _client().can_manage(),
            _client().get_fallback_origin(),
        ), timeout=6.0)
        ingress = (config.get("config") or {}).get("ingress")
        expected_origin = settings.custom_domains_gateway_url
        tunnel_ready = (isinstance(ingress, list) and len(ingress) == 1
                        and isinstance(ingress[0], dict)
                        and not ingress[0].get("hostname")
                        and not ingress[0].get("path")
                        and ingress[0].get("service") == expected_origin)
        dns_ready = (isinstance(dns, dict) and dns.get("proxied") is True
                     and dns_record_points_at(
                         dns, f"{settings.custom_domains_gateway_tunnel_id}.cfargotunnel.com"))
        # Cloudflare requires an active zone fallback, but each resource we
        # create explicitly selects this gateway as custom_origin_server.
        # Do not force an existing shared fallback to move to our gateway.
        fallback_origin = normalize_hostname(str((fallback or {}).get("origin") or ""))
        fallback_ready = (isinstance(fallback, dict)
                          and fallback.get("status") == "active"
                          and fallback_origin.endswith(f".{_zone()}"))
        ready = bool(tunnel_ready and dns_ready and healthy and scoped and fallback_ready)
    except Exception:  # provider/health failure must close onboarding
        ready = False
    _edge_cache = (time.monotonic() + 15.0, ready)
    return ready


async def _require_enabled() -> None:
    if not settings.custom_domains_enabled:
        raise HTTPException(status_code=404, detail="Custom domains are disabled")
    if not _configured():
        raise HTTPException(status_code=503, detail="Custom domains are not configured")
    if not await _edge_ready():
        raise HTTPException(status_code=503, detail="Dedicated gateway is not ready")


def _require_cleanup_configured() -> None:
    """The rollout kill switch must not strand existing provider objects."""
    if not ((settings.custom_domains_api_token or settings.cloudflare_api_token)
            and settings.cloudflare_zone_id and _zone()):
        raise HTTPException(status_code=503, detail="Custom domain cleanup is not configured")


def _validate_identity(hostname: str, origin_hostname: str) -> tuple[str, str]:
    customer = normalize_hostname(hostname)
    origin = normalize_hostname(origin_hostname)
    zone = _zone()
    if customer == zone or customer.endswith(f".{zone}"):
        raise ValueError("customer hostname must not be provider-owned")
    if not (origin == zone or origin.endswith(f".{zone}")):
        raise ValueError("origin hostname is outside the trusted provider zone")
    return customer, origin


class CustomDomainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hostname: str
    origin_hostname: str
    namespace: str
    service_name: str

    @field_validator("hostname", "origin_hostname")
    @classmethod
    def strict_hostname(cls, value: str) -> str:
        return normalize_hostname(value)


class GatewayRouteRequest(CustomDomainRequest):
    pass


class ValidationRecord(BaseModel):
    type: str
    name: str
    value: str


class CustomDomainResponse(BaseModel):
    provider_id: str
    hostname: str
    hostname_status: str
    ssl_status: str
    validation_records: list[ValidationRecord]
    origin_hostname: str
    route_ready: bool = False


class CustomDomainCapabilities(BaseModel):
    enabled: bool
    target_hostname: str | None
    supported: bool
    reason: str | None


def _dto(resource: dict[str, Any], logical_origin: str) -> CustomDomainResponse:
    # Provider traffic always goes to the dedicated gateway. The service's
    # canonical hostname is a separately verified logical ownership anchor,
    # not the Cloudflare custom origin (which would bypass the gateway).
    try:
        provider_origin = normalize_hostname(str(resource.get("custom_origin_server") or ""))
        gateway_origin = normalize_hostname(settings.custom_domains_target_hostname)
    except ValueError as exc:
        raise CloudflareException("Custom hostname origin is missing") from exc
    if provider_origin != gateway_origin:
        raise CloudflareException("Custom hostname points outside the gateway")
    ssl = resource.get("ssl") if isinstance(resource.get("ssl"), dict) else {}
    raw_records: list[Any] = []
    ownership = resource.get("ownership_verification")
    if isinstance(ownership, dict):
        raw_records.append(ownership)
    if isinstance(ssl.get("validation_records"), list):
        raw_records.extend(ssl["validation_records"])
    records: list[ValidationRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        # Cloudflare ownership verification uses {type,name,value}, whereas
        # certificate DCV records use {txt_name,txt_value} or
        # {cname,cname_target}. Never pass provider-only fields to tenants.
        candidates = []
        if all(item.get(k) for k in ("type", "name", "value")):
            candidates.append((str(item["type"]).upper(), str(item["name"]), str(item["value"])))
        if item.get("txt_name") and item.get("txt_value"):
            candidates.append(("TXT", str(item["txt_name"]), str(item["txt_value"])))
        if item.get("cname") and item.get("cname_target"):
            candidates.append(("CNAME", str(item["cname"]), str(item["cname_target"])))
        for triple in candidates:
            if triple[0] in ("TXT", "CNAME") and triple not in seen:
                seen.add(triple)
                records.append(ValidationRecord(type=triple[0], name=triple[1], value=triple[2]))
    try:
        return CustomDomainResponse(
            provider_id=str(resource["id"]),
            hostname=normalize_hostname(str(resource["hostname"])),
            hostname_status=str(resource.get("status") or "unknown"),
            ssl_status=str(ssl.get("status") or "unknown"),
            validation_records=records,
            origin_hostname=normalize_hostname(logical_origin),
        )
    except (KeyError, ValueError) as exc:
        raise CloudflareException("Cloudflare returned an incomplete custom hostname") from exc


def _client() -> CustomDomainsClient:
    return CustomDomainsClient(
        zone_id=settings.cloudflare_zone_id,
        api_token=settings.custom_domains_api_token or settings.cloudflare_api_token,
    )


def _provider_id(value: str) -> str:
    if _PROVIDER_ID_RE.fullmatch(value) is None:
        raise HTTPException(status_code=422, detail="Invalid provider ID")
    return value


async def _known_resource(provider_id: str, hostname: str, origin_hostname: str) -> dict[str, Any] | None:
    expected_hostname, expected_origin = _validate_identity(hostname, origin_hostname)
    resource = await _client().get(_provider_id(provider_id))
    if resource is None:
        return None
    dto = _dto(resource, expected_origin)
    if dto.hostname != expected_hostname or dto.origin_hostname != expected_origin:
        raise HTTPException(status_code=409, detail="Custom domain identity mismatch")
    return resource


@router.get("/capabilities", response_model=CustomDomainCapabilities)
async def capabilities():
    target = None
    try:
        candidate = normalize_hostname(settings.custom_domains_target_hostname)
        if candidate.endswith(f".{_zone()}"):
            target = candidate
    except ValueError:
        pass
    configured = _configured()
    ready = configured and await _edge_ready()
    return CustomDomainCapabilities(
        # enabled means end-to-end usable, not merely that the rollout flag was
        # flipped. This prevents Odoo from presenting a path which can create a
        # provider object but cannot route traffic through the owned tunnel.
        enabled=ready,
        target_hostname=target,
        supported=gateway_configured(),
        reason=None if ready else (_EDGE_NOT_READY if configured else _GATEWAY_REASON),
    )


@router.post("/routes", status_code=status.HTTP_204_NO_CONTENT)
async def publish_gateway_route(request: GatewayRouteRequest):
    await _require_enabled()
    try:
        hostname, origin = _validate_identity(request.hostname, request.origin_hostname)
        await publish(hostname, origin, request.namespace, request.service_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GatewayRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except CloudflareException as exc:
        raise HTTPException(status_code=502, detail="Canonical route check failed") from exc
    return None


@router.get("/routes/ready")
async def gateway_route_ready(
    hostname: str = Query(...),
    origin_hostname: str = Query(...),
    namespace: str = Query(...),
    service_name: str = Query(...),
):
    await _require_enabled()
    try:
        hostname, _ = _validate_identity(hostname, origin_hostname)
        return {"ready": await route_loaded(hostname, namespace, service_name,
                                             present=True)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GatewayRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.delete("/routes", status_code=status.HTTP_204_NO_CONTENT)
async def unpublish_gateway_route(request: GatewayRouteRequest):
    # Cleanup continues even after the onboarding feature flag is disabled.
    try:
        hostname, _ = _validate_identity(request.hostname, request.origin_hostname)
        await unpublish(hostname, request.namespace, request.service_name)
        if not await route_loaded(hostname, request.namespace, request.service_name,
                                  present=False):
            raise GatewayRouteError("Gateway has not observed route removal yet")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GatewayRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return None


@router.post("", response_model=CustomDomainResponse, status_code=status.HTTP_201_CREATED)
async def create_custom_domain(request: CustomDomainRequest):
    await _require_enabled()
    try:
        hostname, origin = _validate_identity(request.hostname, request.origin_hostname)
        if not await route_loaded(hostname, request.namespace, request.service_name,
                                  present=True):
            raise HTTPException(status_code=409, detail="Gateway route is not ready")
        dto = _dto(await _client().create(
            hostname, normalize_hostname(settings.custom_domains_target_hostname)), origin)
        if dto.hostname != hostname or dto.origin_hostname != origin:
            # POST may have succeeded, so the caller must quarantine this
            # ambiguous outcome rather than create/adopt by hostname.
            raise CloudflareException("Cloudflare create identity mismatch")
        return dto
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CloudflareException as exc:
        raise HTTPException(status_code=502, detail="Custom domain provider request failed") from exc
    except GatewayRouteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{provider_id}", response_model=CustomDomainResponse)
async def get_custom_domain(
    provider_id: str,
    hostname: str = Query(...),
    origin_hostname: str = Query(...),
    namespace: str = Query(...),
    service_name: str = Query(...),
):
    _require_cleanup_configured()
    try:
        resource = await _known_resource(provider_id, hostname, origin_hostname)
        if resource is None:
            raise HTTPException(status_code=404, detail="Custom domain not found")
        dto = _dto(resource, origin_hostname)
        try:
            dto.route_ready = await route_loaded(
                dto.hostname, namespace, service_name, present=True)
        except GatewayRouteError:
            dto.route_ready = False
        return dto
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CloudflareException as exc:
        raise HTTPException(status_code=502, detail="Custom domain provider request failed") from exc


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_domain(
    provider_id: str,
    hostname: str = Query(...),
    origin_hostname: str = Query(...),
):
    _require_cleanup_configured()
    try:
        resource = await _known_resource(provider_id, hostname, origin_hostname)
        if resource is not None:
            await _client().delete(_provider_id(provider_id))
        return None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CloudflareException as exc:
        raise HTTPException(status_code=502, detail="Custom domain provider request failed") from exc

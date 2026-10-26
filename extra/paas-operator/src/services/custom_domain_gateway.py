"""CAS-backed route registry for the dedicated customer-domain gateway.

The registry is an operator-owned ConfigMap in the cloudflared namespace.  A
mounted copy is read by the gateway.  No customer hostname is ever added to the
shared service tunnel configuration, whose whole-document writes have no CAS.
"""
from __future__ import annotations

import json
import re
import uuid

import httpx

from src.config import settings
from src.services.cloudflare import CloudflareService
from src.services.helm import KubernetesService, KubectlException, _cloudflared_namespace, validate_namespace


CONFIGMAP_NAME = "paas-custom-domain-routes"
ROUTES_KEY = "routes.json"
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_ROUTES_BYTES = 900_000  # leave headroom below the Kubernetes ConfigMap limit


class GatewayRouteError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        self.status_code = status_code
        super().__init__(message)


def gateway_namespace() -> str:
    namespace = _cloudflared_namespace()
    if namespace not in ("paas-cloudflare-system", "paas-stg-cloudflare-system"):
        raise GatewayRouteError("Dedicated gateway namespace is not configured")
    return namespace


def gateway_configured() -> bool:
    try:
        namespace = gateway_namespace()
    except GatewayRouteError:
        return False
    expected_environment = {
        "prod": ("paas-cloudflare-system", "paas-ws-"),
        "stg": ("paas-stg-cloudflare-system", "paas-ws-stg-"),
    }.get(settings.paas_env)
    if expected_environment != (namespace, settings.namespace_prefix):
        return False
    # The address is not supplied by tenants and must name this environment's
    # fixed in-cluster Service, not an arbitrary URL/SSRF target.
    expected = f"http://paas-custom-domain-gateway.{namespace}.svc.cluster.local:8080"
    try:
        gateway_tunnel = str(uuid.UUID(settings.custom_domains_gateway_tunnel_id))
        shared_tunnel = str(uuid.UUID(settings.cloudflare_tunnel_id))
    except (ValueError, AttributeError):
        return False
    return bool(settings.custom_domains_gateway_url == expected
                and settings.custom_domains_gateway_token
                and gateway_tunnel != shared_tunnel)


def _target(namespace: str, service_name: str) -> dict[str, object]:
    try:
        validate_namespace(namespace)
    except ValueError as exc:
        raise GatewayRouteError("Service namespace is outside this environment", 422) from exc
    if not _LABEL.fullmatch(namespace) or not _LABEL.fullmatch(service_name):
        raise GatewayRouteError("Invalid service identity", 422)
    return {"namespace": namespace, "service": service_name, "port": 80}


def _read(k8s: KubernetesService) -> tuple[dict, dict]:
    namespace = gateway_namespace()
    try:
        raw = k8s._run_command([
            "get", "configmap", CONFIGMAP_NAME, "--namespace", namespace,
            "--output", "json",
        ]).stdout
        cm = json.loads(raw)
        # The chart intentionally owns only ConfigMap metadata. The operator
        # adds this data key, so a Helm upgrade cannot reset live mappings.
        routes = json.loads((cm.get("data") or {}).get(ROUTES_KEY, "{}"))
    except (KubectlException, KeyError, ValueError, TypeError) as exc:
        raise GatewayRouteError("Gateway route registry is unavailable") from exc
    if not isinstance(routes, dict):
        raise GatewayRouteError("Gateway route registry is invalid")
    return cm, routes


def _verified_service(k8s: KubernetesService, namespace: str, service_name: str) -> None:
    # The request cannot choose a port or arbitrary address.  The service must
    # belong to the named Helm release and expose the known Odoo nginx port.
    try:
        services = k8s.get_services(
            namespace, label_selector=f"app.kubernetes.io/instance={service_name}"
        )
    except (KubectlException, ValueError) as exc:
        raise GatewayRouteError("Cannot verify the target service") from exc
    matched = [svc for svc in services if svc.get("name") == service_name
               and svc.get("clusterIP") not in (None, "", "None")
               and any(p.get("port") == 80 and p.get("protocol") == "TCP"
                       for p in svc.get("ports", []))]
    if len(matched) != 1:
        raise GatewayRouteError("Target Odoo service is not ready", 409)


async def _verified_public_route(origin_hostname: str, target: dict) -> None:
    cf = CloudflareService()
    if not cf.enabled:
        raise GatewayRouteError("Canonical tunnel routing is unavailable")
    expected = cf.generate_service_url(
        namespace=target["namespace"], service_name=target["service"], port=80
    )
    routes = await cf.list_routes()
    if not any(route.hostname == origin_hostname and route.service == expected
               and not route.path for route in routes):
        raise GatewayRouteError("Canonical public route does not own this service", 409)


def _replace(k8s: KubernetesService, cm: dict, routes: dict) -> None:
    encoded = json.dumps(routes, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > _MAX_ROUTES_BYTES:
        raise GatewayRouteError("Gateway route registry is full", 409)
    cm.setdefault("data", {})[ROUTES_KEY] = encoded
    k8s._run_command(
        ["replace", "--filename", "-", "--namespace", gateway_namespace()],
        input_data=json.dumps(cm),
    )


def _same(route: object, target: dict) -> bool:
    return isinstance(route, dict) and route == target


async def publish(hostname: str, origin_hostname: str, namespace: str, service_name: str) -> None:
    target = _target(namespace, service_name)
    k8s = KubernetesService()
    _verified_service(k8s, namespace, service_name)
    await _verified_public_route(origin_hostname, target)
    for _ in range(5):
        cm, routes = _read(k8s)
        existing = routes.get(hostname)
        if _same(existing, target):
            return
        if existing is not None:
            raise GatewayRouteError("Hostname already belongs to another service", 409)
        routes[hostname] = target
        try:
            _replace(k8s, cm, routes)
            return
        except KubectlException as exc:
            if "Conflict" not in exc.stderr and "the object has been modified" not in exc.stderr:
                raise GatewayRouteError("Gateway route update failed") from exc
    raise GatewayRouteError("Concurrent gateway route update; retry")


async def unpublish(hostname: str, namespace: str, service_name: str) -> None:
    target = _target(namespace, service_name)
    k8s = KubernetesService()
    for _ in range(5):
        cm, routes = _read(k8s)
        existing = routes.get(hostname)
        if existing is None:
            return
        if not _same(existing, target):
            raise GatewayRouteError("Hostname belongs to another service", 409)
        del routes[hostname]
        try:
            _replace(k8s, cm, routes)
            return
        except KubectlException as exc:
            if "Conflict" not in exc.stderr and "the object has been modified" not in exc.stderr:
                raise GatewayRouteError("Gateway route update failed") from exc
    raise GatewayRouteError("Concurrent gateway route update; retry")


async def route_loaded(hostname: str, namespace: str, service_name: str, *, present: bool) -> bool:
    target = _target(namespace, service_name)
    _, routes = _read(KubernetesService())
    entry = routes.get(hostname)
    if present and not _same(entry, target):
        return False
    if not present and entry is not None:
        return False
    if not gateway_configured():
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.custom_domains_gateway_url}/_internal/route",
                params={"hostname": hostname},
                headers={"Authorization": f"Bearer {settings.custom_domains_gateway_token}"},
            )
        if response.status_code != 200:
            return False
        return response.json().get("loaded") is present
    except (httpx.HTTPError, ValueError):
        return False

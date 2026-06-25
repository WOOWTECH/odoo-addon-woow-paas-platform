"""mcp.json ConfigMap invalidation for an agent-runtime project (design §5.4).

When a project's MCP binding changes, the operator patches the project pod's
``agent-pa-{hash}-mcp`` ConfigMap with the fresh snapshot (the pod hot-reloads
via WS push primary + inotify fallback).
"""
import json
import logging

from src.config import settings
from src.services.helm import KubernetesService

logger = logging.getLogger(__name__)

k8s_service = KubernetesService()


def _namespace(project_hash: str) -> str:
    # Mirror agent_runtimes._names: derive from the operator's allowed prefix so
    # the ConfigMap patch targets the SAME namespace the pod runs in (e.g.
    # 'paas-ws-stg-pa-{hash}' when env-scoped, 'paas-ws-pa-{hash}' by default).
    return f"{settings.namespace_prefix}pa-{project_hash}"


def _configmap_name(project_hash: str) -> str:
    return f"agent-pa-{project_hash}-mcp"


def invalidate(project_hash, mcp_snapshot):
    """Merge-patch the mcp.json ConfigMap for the project namespace.

    Uses a ``data``-only merge patch (NOT ``kubectl apply``) so the chart-created
    ConfigMap's Helm ownership metadata survives — else the next ``helm upgrade``
    hits an ownership conflict.
    """
    namespace = _namespace(project_hash)
    result = k8s_service.patch_configmap(
        namespace=namespace,
        configmap_name=_configmap_name(project_hash),
        data={"mcp.json": json.dumps(mcp_snapshot)},
    )
    logger.info("Invalidated mcp.json ConfigMap for %s", namespace)
    return result

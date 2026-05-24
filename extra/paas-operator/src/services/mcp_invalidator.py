"""mcp.json ConfigMap invalidation for an agent-runtime project (design §5.4).

When a project's MCP binding changes, the operator patches the project pod's
``agent-pa-{hash}-mcp`` ConfigMap with the fresh snapshot (the pod hot-reloads
via WS push primary + inotify fallback).
"""
import json
import logging

from src.services.helm import KubernetesService

logger = logging.getLogger(__name__)

k8s_service = KubernetesService()


def _namespace(project_hash: str) -> str:
    return f"paas-ws-pa-{project_hash}"


def _configmap_name(project_hash: str) -> str:
    return f"agent-pa-{project_hash}-mcp"


def invalidate(project_hash, mcp_snapshot):
    """Patch/replace the mcp.json ConfigMap for the project namespace."""
    namespace = _namespace(project_hash)
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _configmap_name(project_hash),
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/instance": f"agent-pa-{project_hash}",
                "app.kubernetes.io/component": "agent-runtime-mcp",
            },
        },
        "data": {"mcp.json": json.dumps(mcp_snapshot)},
    }
    result = k8s_service.apply_manifest(manifest)
    logger.info("Invalidated mcp.json ConfigMap for %s", namespace)
    return result

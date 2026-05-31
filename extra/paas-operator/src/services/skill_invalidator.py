"""skills.json ConfigMap invalidation for an agent-runtime project (design §5.4).

Symmetric with :mod:`src.services.mcp_invalidator`. When a project's published
skills change (or to push the current snapshot to an already-running pod), the
operator patches the project pod's ``agent-pa-{hash}-skills`` ConfigMap with the
fresh snapshot — the pod hot-reloads via WS push primary (``skill_invalidate``
envelope re-reads the file) + inotify fallback. This closes the Layer 3 gap: a
skill published AFTER cold-start never reached a running pod because spawn-or-
attach only renders skills.json on cold install (attach is a no-op).
"""
import json
import logging

from src.config import settings
from src.services.helm import KubernetesService

logger = logging.getLogger(__name__)

k8s_service = KubernetesService()


def _namespace(project_hash: str) -> str:
    # Mirror agent_runtimes._names / mcp_invalidator: derive from the operator's
    # allowed prefix so the ConfigMap patch targets the SAME namespace the pod
    # runs in ('paas-ws-stg-pa-{hash}' when env-scoped, 'paas-ws-pa-{hash}' default).
    return f"{settings.namespace_prefix}pa-{project_hash}"


def _configmap_name(project_hash: str) -> str:
    return f"agent-pa-{project_hash}-skills"


def invalidate(project_hash, skill_snapshot):
    """Patch/replace the skills.json ConfigMap for the project namespace.

    ``skill_snapshot`` is the list[{name, version, sha256, description}] the
    Odoo controller builds (``_build_skill_snapshot``); the pod's
    ``_snapshot_skill_entries`` tolerates a bare list.
    """
    namespace = _namespace(project_hash)
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _configmap_name(project_hash),
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/instance": f"agent-pa-{project_hash}",
                "app.kubernetes.io/component": "agent-runtime-skills",
            },
        },
        "data": {"skills.json": json.dumps(skill_snapshot)},
    }
    result = k8s_service.apply_manifest(manifest)
    logger.info("Invalidated skills.json ConfigMap for %s", namespace)
    return result

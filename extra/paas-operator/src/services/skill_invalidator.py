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
    """Merge-patch the skills.json ConfigMap for the project namespace.

    ``skill_snapshot`` is the list[{name, version, sha256, description}] the
    Odoo controller builds (``_build_skill_snapshot``); the pod's
    ``_snapshot_skill_entries`` tolerates a bare list.

    Uses a ``data``-only merge patch (NOT ``kubectl apply``) so the chart-created
    ConfigMap's Helm ownership metadata survives — else the next ``helm upgrade``
    hits an ownership conflict.
    """
    namespace = _namespace(project_hash)
    result = k8s_service.patch_configmap(
        namespace=namespace,
        configmap_name=_configmap_name(project_hash),
        data={"skills.json": json.dumps(skill_snapshot)},
    )
    logger.info("Invalidated skills.json ConfigMap for %s", namespace)
    return result

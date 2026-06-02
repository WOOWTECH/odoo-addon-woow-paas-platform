"""Odoo → paas-operator HTTP client for agent-runtime ops (design §2.11 / §5.6).

Mirrors ``services.paas_operator.get_paas_operator_client`` — reads
``operator_url`` + ``operator_api_key`` from ir.config_parameter (existing Odoo
settings; these are the outbound caller credential, distinct from the inbound
shared signing secret which lives in the injected env).
"""
import json
import logging
import time

import requests
from requests import exceptions as _req_exc

_logger = logging.getLogger(__name__)

_PREFIX = '/api/agent-runtimes'
_TIMEOUT = 30
_SPAWN_TIMEOUT = 90      # cold-start helm install + image pull headroom (#123)
_INVALIDATE_TIMEOUT = 10  # best-effort on-publish push, don't hang (#123/#111)

_SPAWN_MAX_RETRIES = 2          # total attempts = 1 + 2 (#123 cold-start)
_SPAWN_RETRY_BACKOFF = 2.0      # seconds, linear between attempts

# LLM model-config defaults — mirror ``ai_client.AIClient.from_assistant``
# (models/ai_client.py:121-125) so the pod's ChatOpenAI sees the same values the
# in-Odoo AI path would use.
_LLM_DEFAULT_MODEL = 'gpt-4o-mini'
_LLM_DEFAULT_API_BASE = 'https://api.openai.com/v1'
_LLM_DEFAULT_MAX_TOKENS = 4096
_LLM_DEFAULT_TEMPERATURE = 0.7


def _build_llm_credential(agent_template):
    """Resolve (api_key, llm_model_config) from the template's ai.config.

    F-PILOT-8 / design §8.2: the operator never queries the Odoo DB, so the
    tenant's LLM credential rides the spawn request; the operator provisions it
    as a per-release Secret consumed by the pod (LLM_API_KEY / LLM_MODEL_CONFIG).

    Reads ``agent_template.model_config_id`` (an ``ai.config``) the same way
    ``ai_client.from_assistant`` does (model / api_base_url / temperature /
    max_tokens), then merges ``agent_template.model_overrides`` (JSON) on top.

    Returns ``(None, None)`` when no config is bound or it carries no api_key —
    the operator then provisions no Secret and the pod falls back to echo
    (拍板: key absent = fallback echo). The model-config field name is
    ``llm_model_config`` (``model_config`` is pydantic-reserved operator-side).
    """
    config = agent_template.model_config_id if agent_template else None
    if not config:
        return None, None
    api_key = config.api_key or None
    if not api_key:
        return None, None
    model_config = {
        'model': config.model or _LLM_DEFAULT_MODEL,
        'api_base': config.api_base_url or _LLM_DEFAULT_API_BASE,
        'temperature': (
            config.temperature if config.temperature is not None
            else _LLM_DEFAULT_TEMPERATURE
        ),
        'max_tokens': config.max_tokens or _LLM_DEFAULT_MAX_TOKENS,
    }
    if agent_template.model_overrides:
        try:
            overrides = json.loads(agent_template.model_overrides)
        except (ValueError, TypeError):
            overrides = None
        if isinstance(overrides, dict):
            model_config.update(overrides)
    return api_key, model_config


class AgentRuntimeOperatorError(Exception):
    """Raised when the paas-operator returns a 4xx/5xx for an agent-runtime op.

    Carries the operator's ``detail`` so the SSE bridge surfaces the real cause
    instead of swallowing the error dict / leaking a cryptic JSONDecodeError
    (N-1 / F-PC-5).
    """


class AgentRuntimeClient:
    """Thin HTTP wrapper around the paas-operator agent-runtime endpoints."""

    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key

    def _request(self, method, path, json_body=None, timeout=None):
        url = self.base_url + _PREFIX + path
        resp = requests.request(
            method, url,
            json=json_body,
            headers={'X-API-Key': self.api_key, 'Content-Type': 'application/json'},
            timeout=timeout if timeout is not None else _TIMEOUT,
        )
        if not resp.ok:
            raise AgentRuntimeOperatorError(
                'paas-operator %s %s → %s: %s'
                % (method, path, resp.status_code, self._error_detail(resp))
            )
        return resp.json()

    @staticmethod
    def _error_detail(resp):
        """Best-effort extraction of the operator error ``detail``."""
        try:
            body = resp.json()
        except Exception:
            return resp.text
        if isinstance(body, dict):
            return body.get('detail') or body
        return body

    def spawn_or_attach(self, project, channel_id, invoker_user_id,
                        invoker_role, agent_template, mcp_snapshot, skill_snapshot):
        """POST /spawn-or-attach → {pod_ws_url, auth_token, pod_state, spawn_age_ms}."""
        body = {
            'project_hash': self._project_hash(project),
            'project_id': project.id,
            'channel_id': channel_id,
            'invoker_user_id': invoker_user_id,
            'invoker_role': invoker_role,
            'project_workspace_ids': project.workspace_ids.ids,
            'agent_template_snapshot': {'id': agent_template.id},
            'mcp_snapshot': mcp_snapshot or {},
            'skill_snapshot': skill_snapshot or [],
        }
        # F-PILOT-8: ride the tenant LLM credential on the spawn request so the
        # operator can provision the per-release Secret. Omitted entirely when
        # absent → operator skips the Secret → pod echo fallback.
        api_key, llm_model_config = _build_llm_credential(agent_template)
        if api_key:
            body['api_key'] = api_key
        if llm_model_config:
            body['llm_model_config'] = llm_model_config
        # F-ARL-1 / #123: cold-start spawn can exceed even the 90s headroom or
        # hit a transient connection error while helm install + image pull are
        # in flight. spawn-or-attach is IDEMPOTENT (operator _release_exists →
        # attach), so retrying transient timeout / connection errors is safe.
        # 4xx/5xx (AgentRuntimeOperatorError) are NOT retried — surface them.
        last_exc = None
        for attempt in range(_SPAWN_MAX_RETRIES + 1):
            try:
                return self._request('POST', '/spawn-or-attach', body,
                                     timeout=_SPAWN_TIMEOUT)
            except (_req_exc.Timeout, _req_exc.ConnectionError) as exc:
                last_exc = exc
                if attempt < _SPAWN_MAX_RETRIES:
                    _logger.warning(
                        'spawn-or-attach transient error (attempt %s/%s): %s',
                        attempt + 1, _SPAWN_MAX_RETRIES + 1, exc)
                    time.sleep(_SPAWN_RETRY_BACKOFF)
                    continue
                raise
        raise last_exc  # unreachable, defensive

    def reap(self, project_hash):
        return self._request('POST', f'/{project_hash}/reap')

    def resume(self, project_hash):
        return self._request('POST', f'/{project_hash}/resume')

    def invalidate_mcp(self, project_hash, mcp_snapshot=None):
        return self._request(
            'POST', f'/{project_hash}/invalidate-mcp',
            {'mcp_snapshot': mcp_snapshot or {}},
            timeout=_INVALIDATE_TIMEOUT,
        )

    def sync_workspaces(self, project_hash, workspace_ids):
        return self._request(
            'POST', f'/{project_hash}/sync-workspaces',
            {'project_workspace_ids': list(workspace_ids)},
        )

    def delete(self, project_hash):
        return self._request('DELETE', f'/{project_hash}')

    def get_status(self, project_hash):
        return self._request('GET', f'/{project_hash}/status')

    @staticmethod
    def _project_hash(project):
        """Resolve (creating if needed) the project's agent_session hash."""
        Session = project.env['woow_paas_platform.agent_session'].sudo()
        session = Session.search([('project_id', '=', project.id)], limit=1)
        if not session:
            session = Session.create({'project_id': project.id})
        return session.project_namespace_hash


def get_agent_runtime_client(env):
    """Return a configured client, or None when the operator is unconfigured."""
    ICP = env['ir.config_parameter'].sudo()
    base_url = ICP.get_param('woow_paas_platform.operator_url', '')
    api_key = ICP.get_param('woow_paas_platform.operator_api_key', '')
    if not base_url or not api_key:
        return None
    return AgentRuntimeClient(base_url, api_key)

"""Odoo → paas-operator HTTP client for agent-runtime ops (design §2.11 / §5.6).

Mirrors ``services.paas_operator.get_paas_operator_client`` — reads
``operator_url`` + ``operator_api_key`` from ir.config_parameter (existing Odoo
settings; these are the outbound caller credential, distinct from the inbound
shared signing secret which lives in the injected env).
"""
import logging

import requests

_logger = logging.getLogger(__name__)

_PREFIX = '/api/agent-runtimes'
_TIMEOUT = 30


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

    def _request(self, method, path, json_body=None):
        url = self.base_url + _PREFIX + path
        resp = requests.request(
            method, url,
            json=json_body,
            headers={'X-API-Key': self.api_key, 'Content-Type': 'application/json'},
            timeout=_TIMEOUT,
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
        return self._request('POST', '/spawn-or-attach', body)

    def reap(self, project_hash):
        return self._request('POST', f'/{project_hash}/reap')

    def resume(self, project_hash):
        return self._request('POST', f'/{project_hash}/resume')

    def invalidate_mcp(self, project_hash, mcp_snapshot=None):
        return self._request(
            'POST', f'/{project_hash}/invalidate-mcp',
            {'mcp_snapshot': mcp_snapshot or {}},
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

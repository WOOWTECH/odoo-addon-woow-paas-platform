"""Portal page extension: agent status card + AI chat on native pages.

Extends the native ``ProjectCustomerPortal`` view-value hooks. Pages are
reachable only after the native ``_document_check_access`` (ir.rule or
access_token), so values injected here add NO new visibility — the chat /
status data endpoints enforce their own portal legs (ai_assistant.py).
"""
import logging

from odoo.http import request

from odoo.addons.project.controllers.portal import ProjectCustomerPortal

_logger = logging.getLogger(__name__)


class PaasProjectCustomerPortal(ProjectCustomerPortal):

    def _task_get_page_view_values(self, task, access_token, **kwargs):
        values = super()._task_get_page_view_values(
            task, access_token, **kwargs)
        values.update(self._paas_agent_values_for_task(task))
        return values

    def _project_get_page_view_values(self, project, access_token, page=1,
                                      date_begin=None, date_end=None,
                                      sortby=None, search=None,
                                      search_in='content', groupby=None,
                                      **kwargs):
        values = super()._project_get_page_view_values(
            project, access_token, page=page, date_begin=date_begin,
            date_end=date_end, sortby=sortby, search=search,
            search_in=search_in, groupby=groupby, **kwargs)
        values['paas_agent_enabled'] = bool(project.sudo().use_agent_runtime)
        values['paas_project_id'] = project.id
        return values

    def _paas_agent_values_for_task(self, task):
        """Chat/status view values + lazy channel provisioning.

        - Channel ensure reuses ``task._create_chat_channel()`` (idempotent).
        - The current (non-public) visitor is auto-joined to the channel —
          legitimate because this method only runs after the page passed
          ``_document_check_access``; revocation is enforced per-request by
          the controller portal legs, so stale membership carries no access.
        - Public (token-only, unauthenticated) visitors get no channel_id —
          the template renders a login hint instead (chat API is auth='user').
        """
        task = task.sudo()
        project = task.project_id
        values = {
            'paas_agent_enabled': bool(project and project.use_agent_runtime),
            'paas_project_id': project.id if project else False,
            'paas_channel_id': False,
        }
        if not values['paas_agent_enabled']:
            return values
        user = request.env.user
        if user._is_public():
            return values
        channel = task._create_chat_channel()
        partner = user.partner_id
        if partner not in channel.channel_partner_ids:
            channel.sudo().write({
                'channel_member_ids': [(0, 0, {'partner_id': partner.id})]})
        values['paas_channel_id'] = channel.id
        return values

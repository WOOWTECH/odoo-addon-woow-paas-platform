"""Native portal: project agent cards/chat and D19 invoice privacy.

Extends the native ``ProjectCustomerPortal`` view-value hooks. Pages are
reachable only after the native ``_document_check_access`` (ir.rule or
access_token), so values injected here add NO new visibility — the chat /
status data endpoints enforce their own portal legs (ai_assistant.py).
"""
import logging

from odoo import _
from odoo.exceptions import MissingError, ValidationError
from odoo.http import request, route

from odoo.addons.account_payment.controllers.payment import PaymentPortal
from odoo.addons.account_payment.controllers.portal import PortalAccount
from odoo.addons.project.controllers.portal import ProjectCustomerPortal

_logger = logging.getLogger(__name__)


def _check_credit_document_privacy(model_name, document):
    """Token-authorized sudo documents still obey D19 for tenant visitors."""
    if (model_name == 'account.move' and request.env.user.share
            and document._llm_is_internal_credit_move()):
        raise MissingError('This document does not exist.')
    return document


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
        user = request.env.user
        agent_enabled = bool(project and project.use_agent_runtime)
        # UX gate (defense in depth): a share user reaching this page via an
        # access_token to a NON-portal project would see the chat/status UI,
        # but every chat/status API call gates on
        # ``_portal_partner_can_access`` which requires
        # ``privacy_visibility == 'portal'`` → all calls are rejected. Suppress
        # the dead control for share users. Internal visitors are unaffected.
        if user.share and project and project.privacy_visibility != 'portal':
            agent_enabled = False
        values = {
            'paas_agent_enabled': agent_enabled,
            'paas_project_id': project.id if project else False,
            'paas_channel_id': False,
        }
        if not values['paas_agent_enabled']:
            return values
        if user._is_public():
            return values
        channel = task._create_chat_channel()
        partner = user.partner_id
        if partner not in channel.channel_partner_ids:
            channel.sudo().write({
                'channel_member_ids': [(0, 0, {'partner_id': partner.id})]})
        values['paas_channel_id'] = channel.id
        return values


class PaasAccountCustomerPortal(PortalAccount):
    """D19: private conversion vouchers never enter the tenant invoice portal.

    Odoo 18 account.portal lists/home counters use the global read rule.
    Keep its caller-scoped search domain unchanged: the private marker is
    system-only and _field_to_sql checks field permissions. Rule expressions
    are compiled with sudo by _apply_ir_rules, so they can safely use it.
    Detail, report_type and legal-document download call _document_check_access
    first, but that helper accepts access tokens and returns a sudo document.
    The explicit share-user gate closes that token bypass. Direct /report
    rendering keeps the caller's environment and is covered by the global
    account.move read rule; internal ERP/accounting users remain unaffected.
    """

    def _document_check_access(self, model_name, document_id, access_token=None):
        document = super()._document_check_access(model_name, document_id, access_token)
        return _check_credit_document_privacy(model_name, document)


class PaasInvoicePaymentPortal(PaymentPortal):
    """Cover native transaction and payment-link routes as well as invoice UI.

    account_payment has a separate payment controller: invoice_transaction
    accepts a document token, while payment_pay uses a context payment token
    and directly browses the invoice under sudo. Neither grants tenants the
    right to pay internal vouchers. Internal accounting users keep native ERP
    behavior and ordinary invoices continue through the upstream flow.
    """

    def _document_check_access(self, model_name, document_id, access_token=None):
        document = super()._document_check_access(model_name, document_id, access_token)
        return _check_credit_document_privacy(model_name, document)

    @route()
    def payment_pay(self, *args, amount=None, invoice_id=None, access_token=None, **kwargs):
        document_id = self._cast_as_int(invoice_id)
        if request.env.user.share and document_id:
            # exists() resolves only this supplied id; the private model helper
            # reads just the marker. This lookup can only deny, never return an
            # invoice or relax the upstream payment-token/ownership checks.
            document = request.env['account.move'].browse(document_id).exists()
            if document and document._llm_is_internal_credit_move():
                raise ValidationError(_('The provided parameters are invalid.'))
        return super().payment_pay(
            *args, amount=amount, invoice_id=invoice_id, access_token=access_token, **kwargs)

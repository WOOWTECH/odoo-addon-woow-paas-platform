"""Authenticated invitation APIs; server-owned identity, role and nonce."""
import logging

from psycopg2 import OperationalError
from odoo import _
from odoo.http import Controller, request, route

from ..models.workspace_invitation import InvitationRejected

_logger = logging.getLogger(__name__)


def _invitation_failure(error):
    if isinstance(error, InvitationRejected):
        return {'success': False, 'code': error.code, 'error': str(error)}
    # Never log a submitted nonce, rendered invitation body or exception text.
    _logger.error('Workspace invitation operation failed (%s)', type(error).__name__)
    return {'success': False, 'code': 'invite_operation_failed', 'error': _('The invitation could not be processed')}


class WorkspaceInvitationController(Controller):
    @route('/api/workspace-invitations/accept', auth='user', methods=['POST'], type='json')
    def api_workspace_invitation_accept(self, token=None, **kwargs):
        try:
            data = request.env['woow_paas_platform.workspace_invitation']._accept(token)
            return {'success': True, 'data': data}
        except OperationalError:
            # PostgreSQL serialization failures must reach Odoo's retry loop.
            raise
        except Exception as error:
            return _invitation_failure(error)

    @route('/api/workspaces/<int:workspace_id>/invitations', auth='user', methods=['POST'], type='json')
    def api_workspace_invitations(self, workspace_id, action='list', invitation_id=None, **kwargs):
        ledger = request.env['woow_paas_platform.workspace_invitation']
        try:
            if action == 'list':
                return {'success': True, 'data': ledger._list_for_workspace(workspace_id)}
            if action == 'revoke':
                return {'success': True, 'data': ledger._revoke(workspace_id, invitation_id)}
            return {'success': False, 'code': 'invite_action_invalid', 'error': _('Unknown invitation action')}
        except OperationalError:
            raise
        except Exception as error:
            return _invitation_failure(error)

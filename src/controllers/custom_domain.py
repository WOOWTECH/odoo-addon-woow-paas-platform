"""Narrow JSON-RPC API for one already-authorized PaaS service."""
import logging

from psycopg2 import IntegrityError, OperationalError

from odoo import _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Controller, request, route

from ..models.workspace_access import ROLE_OWNER, ROLE_ADMIN
from ..services.paas_capability import _PaaSCapability

_logger = logging.getLogger(__name__)
_MODEL = 'woow_paas_platform.custom_domain'


class CustomDomainController(Controller):
    @route('/api/services/<int:service_id>/domains',
           auth='user', methods=['POST'], type='json')
    def api_service_domains(self, service_id, action='list', hostname=None,
                            domain_id=None, **kwargs):
        """Only explicit DTOs leave this endpoint; remote ids never do."""
        if kwargs or action not in ('list', 'create', 'check', 'remove'):
            return {'success': False, 'error': _('Invalid domain request.')}
        capability = _PaaSCapability(request.env)
        service = capability.cloud_service(service_id)
        if not service.exists() or service.template_id.conceal_instances:
            return {'success': False, 'error': _('Service not found.')}
        access = capability.access(service.workspace_id)
        if not access:
            return {'success': False, 'error': _('Service not found.')}
        can_manage = access.role in (ROLE_OWNER, ROLE_ADMIN)
        Domain = request.env[_MODEL]
        eligibility = Domain._capability(service)
        eligibility['supported'] = eligibility['supported'] and can_manage
        if not can_manage:
            eligibility['management_available'] = False
            eligibility['reason'] = _('Only workspace owners and admins can manage domains.')

        # Membership and the concrete service were established before the
        # privileged read.  Return only rows anchored on this service and only
        # the whitelisted fields from _public_dict (no provider ids or secrets).
        def payload():
            return {'success': True, 'data': {
                'domains': Domain._list_for_service(service),
                'capability': eligibility,
            }}

        if action == 'list':
            return payload()
        if not can_manage:
            return {'success': False, 'error': _('Insufficient permissions.')}
        if action == 'create':
            if domain_id is not None or not isinstance(hostname, str):
                return {'success': False, 'error': _('Invalid domain request.')}
            try:
                service._check_operation_allowed(operation='write')
                with request.env.cr.savepoint():
                    Domain._create_claim(service, hostname)
            except OperationalError:
                raise
            except IntegrityError:
                return {'success': False,
                        'error': _('This domain is already assigned to a service.')}
            except (ValidationError, UserError) as exc:
                return {'success': False, 'error': str(exc)}
            except AccessError:
                return {'success': False, 'error': _('Insufficient permissions.')}
            except Exception:
                _logger.exception('Custom domain request failed for service %s', service.id)
                return {'success': False, 'error': _('Domain provisioning is unavailable.')}
            return payload()

        if hostname is not None or type(domain_id) is not int:
            return {'success': False, 'error': _('Invalid domain request.')}
        domain = Domain._get_for_service(service, domain_id)
        if not domain:
            return {'success': False, 'error': _('Domain not found.')}
        if action == 'check':
            Domain._trigger_reconcile()
        else:
            if not domain._lock_claim() or domain.state == 'removed':
                return payload()
            if domain.state == 'needs_review' or domain.state == 'creating':
                return {'success': False,
                        'error': _('This domain needs operator review before removal.')}
            domain.write({'state': 'removing', 'last_error': False})
            Domain._trigger_reconcile()
        return payload()

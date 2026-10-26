"""Tenant domain claims and a durable, background Cloudflare onboarding journal.

The public API never accepts an origin.  A service's canonical public hostname is
the only origin and the operator independently validates it.  Unknown outcomes
of a remote create are quarantined: creating a second custom hostname would be
unsafe even if the first response was lost.
"""
import json
import logging
import re
import secrets
from urllib.parse import urlparse

import requests
from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.paas_operator import get_paas_operator_client

_logger = logging.getLogger(__name__)
_NAME = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
_PROVIDER_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_ACTIVE_STATES = ('pending_dns', 'creating', 'verifying', 'issuing',
                  'configuring', 'waiting_cname', 'active', 'error',
                  'needs_review', 'removing')
_DOH_URL = 'https://cloudflare-dns.com/dns-query'


def normalize_customer_hostname(value):
    """Only complete subdomains; root domains need a separate apex product."""
    if not isinstance(value, str):
        raise ValidationError(_('Enter a valid subdomain.'))
    value = value.strip().rstrip('.').lower()
    if not value or len(value) > 253 or '://' in value or '*' in value:
        raise ValidationError(_('Enter a valid subdomain.'))
    try:
        value = value.encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise ValidationError(_('Enter a valid subdomain.')) from exc
    labels = value.split('.')
    if len(labels) < 3:
        raise ValidationError(_('Root domains are not supported yet. Enter a subdomain such as www.example.com.'))
    if (any(not _NAME.fullmatch(label) for label in labels)
            or len(labels[-1]) < 2 or labels[-1].isdigit()
            or value.endswith(('.woowtech.io', '.cfargotunnel.com'))):
        raise ValidationError(_('Enter a subdomain outside the platform domain.'))
    return value


class CustomDomain(models.Model):
    _name = 'woow_paas_platform.custom_domain'
    _description = 'PaaS Custom Domain'
    _order = 'id desc'
    _sql_constraints = [
        ('claim_hostname_unique', 'unique(claim_hostname)',
         'This hostname is already assigned to a service.'),
    ]

    service_id = fields.Many2one('woow_paas_platform.cloud_service',
                                 required=True, index=True, ondelete='restrict')
    hostname = fields.Char(required=True, index=True, readonly=True)
    # Cleared only after the remote resource is confirmed absent.  A normal SQL
    # unique constraint then enforces a global one-service claim, including when
    # two workspaces submit the same hostname concurrently.
    claim_hostname = fields.Char(index=True, readonly=True)
    verification_token = fields.Char(required=True, readonly=True,
                                     groups='base.group_system')
    provider_id = fields.Char(readonly=True, copy=False,
                              groups='base.group_system')
    origin_hostname = fields.Char(required=True, readonly=True)
    cname_target = fields.Char(required=True, readonly=True)
    provider_validation_json = fields.Text(readonly=True, copy=False,
                                           groups='base.group_system')
    state = fields.Selection([
        ('pending_dns', 'Waiting for DNS'),
        ('creating', 'Creating'),
        ('verifying', 'Verifying'),
        ('issuing', 'Issuing Certificate'),
        ('configuring', 'Configuring Route'),
        ('waiting_cname', 'Ready for DNS Cutover'),
        ('active', 'Active'),
        ('error', 'Error'),
        ('needs_review', 'Needs Review'),
        ('removing', 'Removing'),
        ('removed', 'Removed'),
    ], default='pending_dns', required=True, index=True, readonly=True)
    last_error = fields.Char(readonly=True, copy=False)
    last_checked_at = fields.Datetime(readonly=True, copy=False)

    @api.constrains('hostname', 'claim_hostname')
    def _check_name(self):
        for rec in self:
            if rec.hostname != normalize_customer_hostname(rec.hostname):
                raise ValidationError(_('Invalid domain name.'))
            if rec.claim_hostname and rec.claim_hostname != rec.hostname:
                raise ValidationError(_('The domain claim must match its name.'))

    @api.model
    def _capability(self, service):
        enabled = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.custom_domains_enabled', 'False') == 'True'
        supported = bool(enabled and service and service.state == 'running'
                         and service.template_id.ingress_enabled
                         and service.subdomain and not service.is_protected
                         and service.helm_namespace and service.helm_release_name
                         and service.template_id._is_woow_odoo_chart())
        reason = '' if supported else _('This service is not yet enabled for custom domains.')
        existing = bool(service and self.sudo().search_count([
            ('service_id', '=', service.id), ('state', '!=', 'removed'),
        ]))
        return {'supported': supported, 'management_available': enabled or existing,
                'reason': reason, 'max_domains': 3}

    @api.model
    def _list_for_service(self, service):
        """Controller has proved membership; keep the privileged read anchored.

        This private method is not callable through ``/web/dataset/call_kw``.
        The returned DTO omits provider identifiers and other internal fields.
        """
        service.ensure_one()
        rows = self.sudo().search([
            ('service_id', '=', service.id), ('state', '!=', 'removed'),
        ], limit=20)
        return [row._public_dict() for row in rows]

    @api.model
    def _get_for_service(self, service, domain_id):
        service.ensure_one()
        return self.sudo().search([
            ('id', '=', domain_id), ('service_id', '=', service.id),
            ('state', '!=', 'removed'),
        ], limit=1)

    @api.model
    def _assert_service_can_be_deleted(self, service):
        """Never lose a provider claim when its service is uninstalled."""
        if self.sudo().search_count([
                ('service_id', 'in', service.ids), ('state', '!=', 'removed')]):
            raise UserError(_(
                'Remove this service\'s custom domains before deleting the service.'))

    @api.model
    def _purge_removed_for_service(self, service):
        """Remove audit rows only after provider absence was confirmed."""
        self._assert_service_can_be_deleted(service)
        self.sudo().search([
            ('service_id', 'in', service.ids), ('state', '=', 'removed'),
        ]).unlink()

    @api.model
    def _trigger_reconcile(self):
        self.env.ref('woow_paas_platform.ir_cron_reconcile_custom_domains').sudo()._trigger()

    @api.model
    def _create_claim(self, service, hostname):
        service.ensure_one()
        # Serialize against service teardown. Otherwise a claim could be
        # inserted after delete checked for live domains but before uninstall.
        if not service._try_lock_lifecycle_rows():
            raise UserError(_('Another service operation is in progress.'))
        capability = self._capability(service)
        if not capability['supported']:
            raise UserError(capability['reason'])
        hostname = normalize_customer_hostname(hostname)
        live_count = self.sudo().search_count([
            ('service_id', '=', service.id), ('state', 'in', _ACTIVE_STATES),
        ])
        if live_count >= capability['max_domains']:
            raise UserError(_('The domain limit for this service has been reached.'))
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(_('Domain provisioning is unavailable.'))
        try:
            advertised = client.custom_domain_capabilities()
        finally:
            client._session.close()
        target = advertised.get('target_hostname') if advertised.get('enabled') is True else None
        if not isinstance(target, str) or not target.endswith('.woowtech.io'):
            raise UserError(_('Domain provisioning is unavailable.'))
        origin = urlparse(service._public_base_url()).hostname
        if not origin or not origin.endswith('.woowtech.io'):
            raise UserError(_('This service has no valid public hostname.'))
        validation = []
        dcv_suffix = advertised.get('dcv_delegation_hostname')
        if dcv_suffix:
            if not isinstance(dcv_suffix, str) or not re.fullmatch(
                    r'[a-z0-9-]{1,63}\.dcv\.cloudflare\.com', dcv_suffix):
                raise UserError(_('Domain provisioning is unavailable.'))
            validation.append({'type': 'CNAME', 'name': '_acme-challenge.' + hostname,
                               'value': hostname + '.' + dcv_suffix})
        # The only server-created fields here are based on a checked service,
        # a fixed operator target and cryptographically random proof.
        return self.sudo().create({
            'service_id': service.id,
            'hostname': hostname,
            'claim_hostname': hostname,
            'origin_hostname': origin,
            'cname_target': target.lower().rstrip('.'),
            'verification_token': secrets.token_urlsafe(24),
            'provider_validation_json': json.dumps(validation),
            'state': 'pending_dns',
        })

    def _public_dict(self):
        self.ensure_one()
        proof_name = '_woow-verify.' + self.hostname
        records = [{'type': 'TXT', 'name': proof_name,
                    'value': self.verification_token, 'purpose': 'verify'}]
        try:
            validation = json.loads(self.provider_validation_json or '[]')
        except (TypeError, ValueError):
            validation = []
        for item in validation:
            if (isinstance(item, dict) and item.get('type') in ('TXT', 'CNAME')
                    and isinstance(item.get('name'), str)
                    and isinstance(item.get('value'), str)):
                records.append({**{key: item[key] for key in ('type', 'name', 'value')},
                                'purpose': 'verify'})
        records.append({'type': 'CNAME', 'name': self.hostname,
                        'value': self.cname_target, 'purpose': 'route'})
        return {
            'id': self.id, 'hostname': self.hostname, 'state': self.state,
            'last_error': self.last_error or '', 'dns_records': records,
            'is_primary': False,
        }

    def _dns_txt_owned(self):
        """Query a fixed resolver URL, never a customer-supplied address."""
        self.ensure_one()
        name = '_woow-verify.' + self.hostname
        answers = self._dns_answers(name, 'TXT')
        for answer in answers:
            if (isinstance(answer, dict) and answer.get('type') == 16
                    and answer.get('name', '').rstrip('.').lower() == name):
                # DNS wire TXT strings are represented with quotes by DoH.
                if answer.get('data', '').strip('"') == self.verification_token:
                    return True
        return False

    def _dns_cname_points_to_target(self):
        self.ensure_one()
        for answer in self._dns_answers(self.hostname, 'CNAME'):
            if (isinstance(answer, dict) and answer.get('type') == 5
                    and answer.get('name', '').rstrip('.').lower() == self.hostname
                    and answer.get('data', '').rstrip('.').lower() == self.cname_target):
                return True
        return False

    @staticmethod
    def _dns_answers(name, record_type):
        try:
            response = requests.get(_DOH_URL, params={'name': name, 'type': record_type},
                                    headers={'accept': 'application/dns-json'},
                                    timeout=(3, 6))
            response.raise_for_status()
            answers = response.json().get('Answer', [])
        except (requests.RequestException, ValueError, AttributeError):
            return []
        return answers if isinstance(answers, list) else []

    def _lock_claim(self):
        """Serialize removal with polling; refresh state after taking the lock."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            'SELECT id FROM woow_paas_platform_custom_domain '
            'WHERE id = %s FOR UPDATE', (self.id,))
        found = bool(self.env.cr.fetchone())
        self.invalidate_recordset()
        return found

    def _reconcile_one(self, client):
        self.ensure_one()
        if not self._lock_claim():
            return
        if self.state == 'pending_dns':
            if self.service_id.state != 'running' or self.service_id.is_protected:
                self.write({'last_checked_at': fields.Datetime.now()})
                return
            if not self._dns_txt_owned():
                self.write({'last_checked_at': fields.Datetime.now()})
                return
            # Idempotent CAS update. Wait for the mounted gateway copy to load
            # before making a non-idempotent Cloudflare Custom Hostname POST.
            self._publish_route(client)
            if not self._route_ready(client):
                self.write({'last_checked_at': fields.Datetime.now()})
                return
            # Commit a durable attempt marker *before* the external create.
            # A crashed worker leaves this row in creating/needs_review; no
            # automatic path sends a second create request.
            self.write({'state': 'creating', 'last_error': False})
            self.env.cr.commit()
            try:
                result = client.create_custom_domain(
                    self.hostname, self.origin_hostname,
                    self.service_id.helm_namespace,
                    self.service_id.helm_release_name)
                provider_id = result.get('provider_id')
                if (not isinstance(provider_id, str)
                        or not _PROVIDER_ID.fullmatch(provider_id)
                        or result.get('hostname') != self.hostname
                        or result.get('origin_hostname') != self.origin_hostname):
                    # A non-matching or incomplete POST response is ambiguous:
                    # never persist an ID for another tenant, and never retry.
                    raise ValueError('provider create identity mismatch')
                self.write({
                    'provider_id': provider_id,
                    'provider_validation_json': json.dumps(
                        result.get('validation_records') or []),
                    'state': 'verifying',
                })
                self.env.cr.commit()
            except OperationalError:
                raise
            except Exception:
                _logger.exception('Custom domain create outcome unknown for claim %s', self.id)
                try:
                    self._unpublish_route(client)
                except Exception:
                    _logger.exception('Could not close ambiguous custom domain route %s', self.id)
                self.write({'state': 'needs_review',
                            'last_error': _('Creation outcome needs operator review.')})
                return
        if self.state == 'creating' or self.state == 'needs_review':
            return
        if self.state == 'removing':
            self._unpublish_route(client)
            if self.provider_id:
                client.delete_custom_domain(self.provider_id, self.hostname,
                                            self.origin_hostname)
            self.write({'state': 'removed', 'claim_hostname': False,
                        'last_error': False})
            return
        if self.state not in ('verifying', 'issuing', 'configuring',
                              'waiting_cname', 'active', 'error'):
            return
        if self.service_id.state != 'running' or self.service_id.is_protected:
            self._unpublish_route(client)
            self.write({'state': 'configuring',
                        'last_error': _('The service is not publicly available.')})
            return
        if not self.provider_id:
            self.write({'state': 'needs_review',
                        'last_error': _('Provider identity is missing.')})
            return
        if not self._route_ready(client):
            self._publish_route(client)
        result = client.get_custom_domain(self.provider_id, self.hostname,
                                          self.origin_hostname,
                                          self.service_id.helm_namespace,
                                          self.service_id.helm_release_name)
        if (result.get('provider_id') != self.provider_id
                or result.get('hostname') != self.hostname
                or result.get('origin_hostname') != self.origin_hostname):
            self.write({'state': 'needs_review',
                        'last_error': _('Provider identity needs operator review.')})
            return
        validation = result.get('validation_records') or []
        vals = {'provider_validation_json': json.dumps(validation),
                'last_checked_at': fields.Datetime.now()}
        if (result.get('hostname_status') == 'active'
                and result.get('ssl_status') == 'active'
                and result.get('route_ready') is True):
            vals.update({'state': ('active' if self._dns_cname_points_to_target()
                                   else 'waiting_cname'), 'last_error': False})
        elif result.get('hostname_status') != 'active':
            vals['state'] = 'verifying'
        elif result.get('ssl_status') != 'active':
            vals['state'] = 'issuing'
        else:
            vals['state'] = 'configuring'
        self.write(vals)

    def _publish_route(self, client):
        self.ensure_one()
        client.publish_custom_domain_route(
            self.hostname, self.origin_hostname,
            self.service_id.helm_namespace, self.service_id.helm_release_name)

    def _unpublish_route(self, client):
        self.ensure_one()
        client.unpublish_custom_domain_route(
            self.hostname, self.origin_hostname,
            self.service_id.helm_namespace, self.service_id.helm_release_name)

    def _route_ready(self, client):
        self.ensure_one()
        # The route publication endpoint is idempotent, but its mounted copy
        # takes time to propagate. An explicit check avoids a premature provider
        # POST and leaves a retryable pending_dns claim.
        return client.custom_domain_gateway_route_ready(
            self.hostname, self.origin_hostname,
            self.service_id.helm_namespace, self.service_id.helm_release_name)

    @api.model
    def _cron_reconcile_custom_domains(self):
        enabled = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.custom_domains_enabled', 'False') == 'True'
        # Disabling onboarding must not strand an existing hostname. Continue
        # processing explicit removals; never create or poll otherwise.
        states = (('pending_dns', 'verifying', 'issuing', 'configuring',
                   'waiting_cname',
                   'active', 'error', 'removing') if enabled else ('removing',))
        if not self.sudo().search_count([('state', 'in', states)]):
            return
        client = get_paas_operator_client(self.env)
        if not client:
            return
        try:
            # New claims first, then least recently checked. A fixed id-desc
            # batch would permanently starve older claims once ten are active.
            batch = self.sudo().search([
                ('state', 'in', states), ('last_checked_at', '=', False),
            ], order='id', limit=10)
            if len(batch) < 10:
                batch |= self.sudo().search([
                    ('state', 'in', states), ('last_checked_at', '!=', False),
                ], order='last_checked_at, id', limit=10 - len(batch))
            for rec in batch:
                try:
                    if rec.state == 'pending_dns':
                        rec._reconcile_one(client)
                    else:
                        with self.env.cr.savepoint():
                            rec._reconcile_one(client)
                except OperationalError:
                    raise
                except Exception:
                    _logger.exception('Custom domain reconciliation failed id=%s', rec.id)
                    if rec.state != 'removing':
                        # A failed route/DNS check before provider POST is safe
                        # to retry; it must not become an ambiguous creation.
                        rec.write({'state': 'pending_dns' if rec.state == 'pending_dns' else 'error',
                                   'last_error': _('Checking the domain failed. Please retry.')})
                rec.write({'last_checked_at': fields.Datetime.now()})
        finally:
            client._session.close()

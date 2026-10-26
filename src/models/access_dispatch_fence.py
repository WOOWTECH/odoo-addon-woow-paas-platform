"""Durable, fail-closed fence for Access application creation.

Cloudflare does not expose an idempotency key for this create operation.  A
process death around the provider call is therefore an uncertain result, not
an automatically retryable failure.  This ledger prefers a stuck publication
and explicit review over a duplicate Access application.
"""
import logging

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError, UserError, ValidationError


_logger = logging.getLogger(__name__)
_MODEL = 'woow_paas_platform.access_dispatch_fence'
_ROUTE_MODEL = 'woow_paas_platform.smart_home_route'
_RETRY_CONFIRMATION = 'I VERIFIED THE PROVIDER HAS NO APP FOR THIS IDENTITY'


class AccessDispatchFence(models.Model):
    _name = _MODEL
    _description = 'Cloudflare Access create uncertainty fence'
    _order = 'id desc'

    anchor_model = fields.Char(required=True, readonly=True, index=True)
    anchor_id = fields.Integer(required=True, readonly=True, index=True)
    app_name = fields.Char(required=True, readonly=True)
    primary_hostname = fields.Char(required=True, readonly=True)
    state = fields.Selection([
        ('prepared', 'Prepared; no send claim committed'),
        ('dispatching', 'Send claimed; provider result unknown'),
        ('retry_authorized', 'One retry manually authorized'),
        ('provider_adopted', 'Provider app manually identified'),
        ('resolved', 'Resolved'),
        ('abandoned', 'Old identity abandoned'),
    ], required=True, readonly=True, default='prepared', index=True)
    attempt = fields.Integer(required=True, readonly=True, default=0)
    provider_app_id = fields.Char(readonly=True)
    claimed_at = fields.Datetime(readonly=True)
    resolved_at = fields.Datetime(readonly=True)
    reviewed_at = fields.Datetime(readonly=True)
    reviewed_by = fields.Many2one('res.users', readonly=True)
    review_reason = fields.Text(readonly=True)

    _sql_constraints = [(
        'identity_unique',
        'UNIQUE(anchor_model, anchor_id, app_name, primary_hostname)',
        'Only one unresolved dispatch fence is allowed per anchor identity.',
    )]

    @api.model
    def _identity(self, anchor, payload):
        return {
            'anchor_model': anchor._name,
            'anchor_id': anchor.id,
            'app_name': payload.get('app_name') or '',
            'primary_hostname': payload.get('primary_hostname') or '',
        }

    @api.model
    def _ensure_prepared(self, anchor, payload):
        identity = self._identity(anchor, payload)
        if not identity['app_name'] or not identity['primary_hostname']:
            raise ValidationError(_('Access create identity is incomplete.'))
        with self.env.registry.cursor() as cr:
            cr.execute(
                'INSERT INTO woow_paas_platform_access_dispatch_fence '
                '(anchor_model, anchor_id, app_name, primary_hostname, state, '
                'attempt, create_uid, write_uid, create_date, write_date) '
                "VALUES (%s,%s,%s,%s,'prepared',0,%s,%s,"
                "now() at time zone 'UTC',now() at time zone 'UTC') "
                'ON CONFLICT (anchor_model,anchor_id,app_name,primary_hostname) '
                'DO NOTHING RETURNING id',
                (identity['anchor_model'], identity['anchor_id'],
                 identity['app_name'], identity['primary_hostname'],
                 SUPERUSER_ID, SUPERUSER_ID))
            row = cr.fetchone()
            if not row:
                cr.execute(
                    'SELECT id FROM woow_paas_platform_access_dispatch_fence '
                    'WHERE anchor_model=%s AND anchor_id=%s AND app_name=%s '
                    'AND primary_hostname=%s',
                    (identity['anchor_model'], identity['anchor_id'],
                     identity['app_name'], identity['primary_hostname']))
                row = cr.fetchone()
            cr.commit()
            return row[0]

    @api.model
    def _claim_send(self, anchor, payload):
        identity = self._identity(anchor, payload)
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT id, state, provider_app_id, attempt '
                'FROM woow_paas_platform_access_dispatch_fence '
                'WHERE anchor_model=%s AND anchor_id=%s AND app_name=%s '
                'AND primary_hostname=%s FOR UPDATE',
                (identity['anchor_model'], identity['anchor_id'],
                 identity['app_name'], identity['primary_hostname']))
            row = cr.fetchone()
            if not row:
                raise UserError(_(
                    'Access create was refused because its durable fence is missing.'))
            fence_id, state, provider_app_id, attempt = row
            if state in ('prepared', 'retry_authorized'):
                cr.execute(
                    'UPDATE woow_paas_platform_access_dispatch_fence '
                    "SET state='dispatching', attempt=%s, claimed_at=now() at time zone 'UTC' "
                    'WHERE id=%s', (attempt + 1, fence_id))
                cr.commit()
                return {'mode': 'create', 'fence_id': fence_id,
                        'provider_app_id': provider_app_id}
            cr.commit()
            return {'mode': 'adopt_only', 'fence_id': fence_id,
                    'provider_app_id': provider_app_id}

    @api.model
    def _record_provider_identity(self, fence_id, app_id):
        if not app_id:
            return
        with self.env.registry.cursor() as cr:
            cr.execute(
                'UPDATE woow_paas_platform_access_dispatch_fence '
                'SET provider_app_id=%s WHERE id=%s AND state != %s',
                (app_id, fence_id, 'abandoned'))
            cr.commit()

    @api.model
    def _resolve_after_commit(self, fence_id, anchor_model, anchor_id, app_id):
        def settle():
            try:
                with self.env.registry.cursor() as cr:
                    cr.execute(
                        'SELECT access_app_id FROM woow_paas_platform_smart_home_route '
                        'WHERE id=%s', (anchor_id,))
                    row = cr.fetchone()
                    if (anchor_model == _ROUTE_MODEL and row and row[0] == app_id):
                        cr.execute(
                            'UPDATE woow_paas_platform_access_dispatch_fence '
                            "SET state='resolved', resolved_at=now() at time zone 'UTC' "
                            'WHERE id=%s AND state != %s', (fence_id, 'abandoned'))
                        cr.commit()
            except Exception:
                _logger.exception(
                    'Access dispatch fence post-commit settlement failed id=%s',
                    fence_id)
        self.env.cr.postcommit.add(settle)

    @api.model
    def _blocking_for(self, anchor, payload):
        identity = self._identity(anchor, payload)
        return self.sudo().search([
            (key, '=', value) for key, value in identity.items()
        ] + [('state', 'not in', ('resolved', 'abandoned'))], limit=1)

    def _require_system_review(self, reason):
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_(
                'Only Settings administrators may review a dispatch fence.'))
        if not reason or len(reason.strip()) < 12:
            raise ValidationError(_(
                'A specific review reason of at least 12 characters is required.'))

    def action_authorize_single_retry(self, confirmation, reason):
        self._require_system_review(reason)
        if confirmation != _RETRY_CONFIRMATION:
            raise ValidationError(_(
                'The provider-absence confirmation text is not exact.'))
        if self.state != 'dispatching':
            raise UserError(_(
                'Only an unresolved dispatching fence can be retried.'))
        self.sudo().write({
            'state': 'retry_authorized',
            'reviewed_at': fields.Datetime.now(),
            'reviewed_by': self.env.user.id,
            'review_reason': reason.strip(),
        })
        return True

    def action_record_verified_app(self, provider_app_id, reason):
        self._require_system_review(reason)
        if self.state not in ('dispatching', 'prepared'):
            raise UserError(_('This fence is not awaiting provider review.'))
        if not provider_app_id or len(provider_app_id.strip()) < 8:
            raise ValidationError(_(
                'A complete provider application ID is required.'))
        self.sudo().write({
            'state': 'provider_adopted',
            'provider_app_id': provider_app_id.strip(),
            'reviewed_at': fields.Datetime.now(),
            'reviewed_by': self.env.user.id,
            'review_reason': reason.strip(),
        })
        return True

    def action_abandon_after_identity_change(self, reason):
        self._require_system_review(reason)
        route = self.env[self.anchor_model].sudo().browse(
            self.anchor_id).exists()
        if route and route._name == _ROUTE_MODEL:
            current = (route._access_app_name(), route.hostname or '')
            if (current[0] == self.app_name or
                    current[1] == self.primary_hostname):
                raise UserError(_(
                    'Change both app name and hostname before abandoning this fence.'))
        self.sudo().write({
            'state': 'abandoned',
            'resolved_at': fields.Datetime.now(),
            'reviewed_at': fields.Datetime.now(),
            'reviewed_by': self.env.user.id,
            'review_reason': reason.strip(),
        })
        return True

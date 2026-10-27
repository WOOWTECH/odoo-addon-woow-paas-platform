"""Durable, fail-closed fence for Access application creation.

Cloudflare does not expose an idempotency key for this create operation.  A
process death around the provider call is therefore an uncertain result, not
an automatically retryable failure.  This ledger prefers a stuck publication
and explicit review over a duplicate Access application.
"""
from datetime import timedelta
import logging
from contextlib import contextmanager

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.paas_operator import (
    PaaSOperatorConnectionError,
    PaaSOperatorError,
    PaaSOperatorTimeoutError,
)


_logger = logging.getLogger(__name__)
_MODEL = 'woow_paas_platform.access_dispatch_fence'
_ROUTE_MODEL = 'woow_paas_platform.smart_home_route'
_RETRY_CONFIRMATION = 'I VERIFIED THE PROVIDER HAS NO APP FOR THIS IDENTITY'

#: HTTP statuses that prove the provider refused the create before any app
#: existed (validation, auth, not found, rate limit).  409 means "already
#: exists" and every 5xx / transport failure is uncertain, so they are not here.
_DEFINITIVE_REJECTION_STATUS = frozenset({400, 401, 403, 404, 405, 415, 422, 429})

#: An administrator may authorize ONE more create for an uncertain send only
#: after the provider had this long to make the app visible; absence from the
#: provider list right after the send proves nothing (no read-after-write).
_RETRY_SETTLE_MINUTES = 15


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
    # Version 0 is deliberately never auto-recovered: rows predating this
    # protocol cannot prove whether an effect-start marker should exist.
    effect_boundary_version = fields.Integer(
        required=True, readonly=True, default=0)
    provider_app_id = fields.Char(readonly=True)
    claimed_at = fields.Datetime(readonly=True)
    effect_started_at = fields.Datetime(readonly=True)
    effect_finished_at = fields.Datetime(readonly=True)
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
                'attempt, effect_boundary_version, create_uid, write_uid, '
                'create_date, write_date) '
                "VALUES (%s,%s,%s,%s,'prepared',0,1,%s,%s,"
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
        """Compatibility probe without automatic recovery.

        New create callers must use :meth:`_dispatch_guard`: only its live PG
        session lock can prove that a previous pre-effect owner is gone.
        """
        identity = self._identity(anchor, payload)
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT id, state, provider_app_id, attempt, '
                'effect_started_at, effect_boundary_version '
                'FROM woow_paas_platform_access_dispatch_fence '
                'WHERE anchor_model=%s AND anchor_id=%s AND app_name=%s '
                'AND primary_hostname=%s FOR UPDATE',
                (identity['anchor_model'], identity['anchor_id'],
                 identity['app_name'], identity['primary_hostname']))
            row = cr.fetchone()
            if not row:
                raise UserError(_(
                    'Access create was refused because its durable fence is missing.'))
            (fence_id, state, provider_app_id, attempt,
             effect_started_at, boundary_version) = row
            if (state in ('prepared', 'retry_authorized')
                    and boundary_version == 1 and not effect_started_at):
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
    def _advisory_identity(self, identity):
        return '\x1f'.join((
            identity['anchor_model'], str(identity['anchor_id']),
            identity['app_name'], identity['primary_hostname']))

    @api.model
    @contextmanager
    def _dispatch_guard(self, anchor, payload):
        """Serialize one identity and safely recover a pre-effect dead owner.

        The dedicated PostgreSQL session remains alive for the whole provider
        create boundary.  Session advisory locks are released by PostgreSQL if
        SIGKILL closes the worker connection.  Consequently a successor which
        acquires the same lock may retry only when ``effect_started_at`` is
        still NULL.  Once that marker is durable, provider outcome is
        uncertain and automatic POST is forbidden.
        """
        identity = self._identity(anchor, payload)
        lock_identity = self._advisory_identity(identity)
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT pg_advisory_lock(hashtextextended(%s, 0))',
                (lock_identity,))
            try:
                cr.execute(
                    'SELECT id, state, provider_app_id, attempt, '
                    'effect_started_at, effect_boundary_version FROM '
                    'woow_paas_platform_access_dispatch_fence '
                    'WHERE anchor_model=%s AND anchor_id=%s AND app_name=%s '
                    'AND primary_hostname=%s FOR UPDATE',
                    (identity['anchor_model'], identity['anchor_id'],
                     identity['app_name'], identity['primary_hostname']))
                row = cr.fetchone()
                if not row:
                    raise UserError(_(
                        'Access create was refused because its durable fence is missing.'))
                (fence_id, state, provider_app_id, attempt,
                 effect_started_at, boundary_version) = row
                recoverable = (
                    state == 'dispatching' and boundary_version == 1
                    and not effect_started_at)
                first_send = (
                    state in ('prepared', 'retry_authorized')
                    and boundary_version == 1 and not effect_started_at)
                # A completed earlier cycle (the app was created and later torn
                # down) must not pin the identity forever: callers only create
                # when no app id is stored, so the stale ``provider_app_id`` of a
                # ``resolved`` row names an app that no longer exists.  If it
                # does still exist the provider refuses the duplicate domain
                # (409), which stays uncertain and goes to review.
                renewable = state == 'resolved' and boundary_version == 1
                if first_send or recoverable or renewable:
                    cr.execute(
                        'UPDATE woow_paas_platform_access_dispatch_fence '
                        "SET state='dispatching', attempt=%s, "
                        "claimed_at=now() at time zone 'UTC', "
                        'effect_started_at=NULL, effect_finished_at=NULL, '
                        'provider_app_id=NULL, resolved_at=NULL '
                        'WHERE id=%s', (attempt + 1, fence_id))
                    claim = {
                        'mode': 'create', 'fence_id': fence_id,
                        'provider_app_id': None,
                        'automatic_recovery': recoverable,
                        '_guard_cursor': cr,
                    }
                else:
                    claim = {
                        'mode': 'adopt_only', 'fence_id': fence_id,
                        'provider_app_id': provider_app_id,
                        'automatic_recovery': False,
                        '_guard_cursor': cr,
                    }
                cr.commit()
                yield claim
            finally:
                # A provider exception does not abort this cursor (the marker
                # was already committed), but an unexpected SQL exception can.
                # Roll back only the dedicated guard cursor before unlocking;
                # closing the session is the final release guarantee.
                try:
                    cr.rollback()
                    cr.execute(
                        'SELECT pg_advisory_unlock(hashtextextended(%s, 0))',
                        (lock_identity,))
                except Exception:
                    _logger.exception(
                        'Access dispatch advisory unlock failed; cursor close '
                        'will release it for identity=%s', lock_identity)

    @api.model
    def _mark_effect_started(self, claim):
        """Durably close the automatic-retry window before provider I/O."""
        cr = claim.get('_guard_cursor')
        if not cr or claim.get('mode') != 'create':
            raise UserError(_('Access create has no active dispatch guard.'))
        cr.execute(
            'UPDATE woow_paas_platform_access_dispatch_fence '
            "SET effect_started_at=now() at time zone 'UTC' "
            'WHERE id=%s AND state=%s',
            (claim['fence_id'], 'dispatching'))
        if cr.rowcount != 1:
            raise UserError(_('Access create lost its durable dispatch fence.'))
        cr.commit()

    @api.model
    def _is_definitive_rejection(self, error):
        """True only when ``error`` proves the provider refused the create.

        A transport failure or a 5xx may still have created the app, and 409
        means it already exists: those stay uncertain (adopt-only, reviewed).
        """
        return (isinstance(error, PaaSOperatorError)
                and not isinstance(error, (PaaSOperatorConnectionError,
                                           PaaSOperatorTimeoutError))
                and error.status_code in _DEFINITIVE_REJECTION_STATUS)

    @api.model
    def _clear_effect_started(self, claim):
        """Reopen the retry window after a *definitive* provider refusal.

        Without this a refused create (an unavailable login method, a 4xx, a
        rate limit) would keep the effect-start marker and fence the route for
        good: the fence has no administrator exit for the "no app exists" case.
        """
        cr = claim.get('_guard_cursor')
        if not cr or claim.get('mode') != 'create':
            return False
        cr.execute(
            'UPDATE woow_paas_platform_access_dispatch_fence '
            "SET state='prepared', effect_started_at=NULL, "
            'effect_finished_at=NULL '
            "WHERE id=%s AND state='dispatching' AND provider_app_id IS NULL",
            (claim['fence_id'],))
        reopened = cr.rowcount == 1
        cr.commit()
        return reopened

    @api.model
    def _record_effect_finished(self, claim, app_id):
        """Persist the provider receipt before releasing dispatch ownership."""
        cr = claim.get('_guard_cursor')
        if not cr or not app_id:
            return
        cr.execute(
            'UPDATE woow_paas_platform_access_dispatch_fence '
            'SET provider_app_id=%s, '
            "effect_finished_at=now() at time zone 'UTC' "
            'WHERE id=%s AND state != %s',
            (app_id, claim['fence_id'], 'abandoned'))
        cr.commit()

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
        if self.effect_boundary_version != 1:
            raise UserError(_(
                'This fence predates the effect-start protocol and cannot be '
                'retried automatically.'))
        if self.effect_started_at:
            age = fields.Datetime.now() - self.effect_started_at
            if age < timedelta(minutes=_RETRY_SETTLE_MINUTES):
                raise UserError(_(
                    'The provider send started less than %(minutes)d minutes '
                    'ago. Absence from the provider list right after a send '
                    'proves nothing: wait, then verify there is no app for this '
                    'identity (or review and adopt the exact existing app).',
                    minutes=_RETRY_SETTLE_MINUTES))
        self.sudo().write({
            'state': 'retry_authorized',
            # The administrator's attestation reopens exactly one create.
            'effect_started_at': False,
            'effect_finished_at': False,
            'reviewed_at': fields.Datetime.now(),
            'reviewed_by': self.env.user.id,
            'review_reason': reason.strip(),
        })
        return True

    def action_record_verified_app(self, provider_app_id, reason):
        """Reject unverified ids; adoption must use provider exact readback."""
        self._require_system_review(reason)
        raise UserError(_(
            'Manual provider IDs are not accepted. Use exact provider '
            'readback review so name and hostname are verified.'))

    def action_review_and_adopt_provider_app(self, reason):
        """Adopt only a unique exact provider readback after admin review.

        Absence is not retry authority because Cloudflare does not document
        read-after-write consistency or POST idempotency. Multiple exact matches
        also leave the fence closed for explicit cleanup.
        """
        self._require_system_review(reason)
        if self.state not in ('dispatching', 'prepared'):
            raise UserError(_('This fence is not awaiting provider review.'))
        result = self._provider_review_client().review_access_app(
            self.app_name, self.primary_hostname)
        if (result.get('status') != 'unique'
                or result.get('match_count') != 1
                or not result.get('app_id')):
            raise UserError(_(
                'Provider review did not return exactly one matching app; '
                'the dispatch fence remains closed.'))
        self.sudo().write({
            'state': 'provider_adopted',
            'provider_app_id': result['app_id'],
            'reviewed_at': fields.Datetime.now(),
            'reviewed_by': self.env.user.id,
            'review_reason': reason.strip(),
        })
        return True

    def _provider_review_client(self):
        """Resolve the owning route's operator client; split for native tests."""
        self.ensure_one()
        route = self.env[self.anchor_model].sudo().browse(self.anchor_id).exists()
        if not route or route._name != _ROUTE_MODEL:
            raise UserError(_('The fenced route no longer exists.'))
        return route.smart_home_id._get_operator_client()

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

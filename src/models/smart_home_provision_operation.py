"""Off-default durable provision prototype.

A normal request only enqueues. Private worker stages own independent registry
cursors; no HTTP cursor is committed here. Unknown first-create results are
quarantined and never deleted/compensated. With the separate, also off-default
reconcile flag, a quarantined unknown outcome is resolved only from an
authoritative provider inventory read: exactly one live tunnel carrying the
intent's unique name is adopted; a twice-confirmed absence is re-queued at most
once; anything else stays a manual state. This is a prototype, not a provider
exactly-once or full commercial recovery implementation.
"""
from datetime import timedelta
import logging
import uuid

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.paas_operator import get_paas_operator_client
from .smart_home import _internal_identity

_logger = logging.getLogger(__name__)
_MODEL = 'woow_paas_platform.smart_home_provision_operation'
_FLAG = 'woow_paas_platform.shc_durable_provision_prototype_enabled'
_RECONCILE_FLAG = 'woow_paas_platform.shc_durable_provision_reconcile_enabled'
_RETENTION_KEY = 'woow_paas_platform.shc_durable_provision_retention_days'
_DEFAULT_RETENTION_DAYS = 30
_CAP_KEY = 'woow_shc_provision_operation_capability'
_CAPABILITY = object()
_PHASES = (
    ('queued', 'Queued'),
    ('create_unknown', 'Creation may have started'),
    ('anchored', 'Tunnel anchored'),
    ('converging', 'Configuring existing tunnel'),
    ('recovery_required', 'Confirmation required'),
    ('completed', 'Completed'),
)
# Only these quarantines mean "a create POST may have reached the provider".
# Pre-create refusals and local anchor mismatches stay manual.
_UNKNOWN_OUTCOME_CODES = (
    'create_outcome_unknown', 'create_worker_interrupted', 'anchor_commit_failed',
)
_CLAIM_LEASE = timedelta(minutes=5)
# Absence must be observed twice, this far apart. It exceeds every in-flight
# window of one create: 5 min worker lease, 120 s client connect/read timeout
# (HELM_OPERATION_TIMEOUT) and the operator's 30 s Cloudflare timeout.
_ABSENCE_SETTLE = timedelta(minutes=15)
_RECONCILE_BACKOFF_BASE = timedelta(minutes=5)
_WORKER_BACKOFF_BASE = timedelta(minutes=1)
_BACKOFF_CAP = timedelta(hours=6)
_RECONCILE_MAX_OBSERVATIONS = 8
_MAX_CREATE_ATTEMPTS = 2
_PURGE_BATCH = 100


def _backoff(base, count):
    """Exponential delay for the ``count``-th consecutive failure, capped."""
    return min(base * (2 ** min(max(count - 1, 0), 16)), _BACKOFF_CAP)


def _parse_observation(binding_before, inventory, binding_after, operator_url):
    """Normalize one provider read. Never raises; malformed means not ok.

    The inventory carries no account id, so the provider binding is read
    before and after it. A changed or malformed binding is never treated as
    evidence about any intent.
    """
    accounts = []
    for binding in (binding_before, binding_after):
        account = binding.get('provider_account_id') if isinstance(binding, dict) else None
        if not isinstance(account, str) or not account:
            return {'ok': False, 'reason': 'binding_malformed'}
        accounts.append(account)
    if not isinstance(inventory, dict):
        return {'ok': False, 'reason': 'inventory_malformed'}
    resources = inventory.get('resources')
    partial = inventory.get('partial')
    failed_kinds = inventory.get('failed_kinds', [])
    paas_env = inventory.get('paas_env')
    if (
        not isinstance(resources, list) or not isinstance(partial, bool)
        or not isinstance(failed_kinds, list) or not isinstance(paas_env, str)
    ):
        return {'ok': False, 'reason': 'inventory_malformed'}
    if partial or failed_kinds:
        # A partial list cannot prove absence or uniqueness.
        return {'ok': False, 'reason': 'inventory_partial'}
    tunnels = {}
    for resource in resources:
        if not isinstance(resource, dict):
            return {'ok': False, 'reason': 'inventory_malformed'}
        if resource.get('kind') != 'tunnel':
            continue
        tunnel_id, name = resource.get('id'), resource.get('name')
        if not isinstance(tunnel_id, str) or not isinstance(name, str):
            return {'ok': False, 'reason': 'inventory_malformed'}
        tunnels.setdefault(name, []).append(tunnel_id)
    return {
        'ok': True,
        'binding_stable': accounts[0] == accounts[1],
        'account': accounts[0],
        'paas_env': paas_env,
        'operator_url': operator_url,
        'tunnels_by_name': tunnels,
    }


def _reconcile_decision(state, observation, now):
    """Pure decision for one unknown-outcome intent (no I/O, no writes).

    Returns ``{'action': adopt|requeue|absent_wait|backoff|manual, ...}``.
    Authority is re-validated by the caller before adopt/requeue.
    """
    def backoff(reason):
        count = state['retry_count'] + 1
        if count >= _RECONCILE_MAX_OBSERVATIONS:
            return {'action': 'manual', 'code': 'reconcile_exhausted', 'reason': reason}
        return {
            'action': 'backoff', 'reason': reason, 'retry_count': count,
            'next_attempt_at': now + _backoff(_RECONCILE_BACKOFF_BASE, count),
        }

    def manual(code):
        return {'action': 'manual', 'code': code}

    if not observation.get('ok'):
        return backoff(observation.get('reason') or 'observation_failed')
    if (
        not observation['binding_stable']
        or observation['account'] != state['provider_account_id']
        or observation['operator_url'] != state['operator_url']
    ):
        return manual('reconcile_binding_changed')
    if observation['paas_env'] != (state['deploy_env'] or 'prod'):
        return backoff('environment_mismatch')
    matches = observation['tunnels_by_name'].get(state['tunnel_name'], [])
    known = [value for value in (state['remote_tunnel_id'], state['home_tunnel_id']) if value]
    if len(matches) > 1:
        return manual('reconcile_duplicate')
    if len(matches) == 1:
        try:
            tunnel_id = str(uuid.UUID(matches[0]))
        except (TypeError, ValueError, AttributeError):
            return manual('reconcile_identity_conflict')
        if any(value != tunnel_id for value in known):
            return manual('reconcile_identity_conflict')
        return {'action': 'adopt', 'tunnel_id': tunnel_id}
    if known:
        # A provider id was received once; its disappearance is not absence of
        # a create and never authorizes another one.
        return manual('reconcile_anchor_missing')
    absent_since = state['absent_since']
    if not absent_since or now < absent_since + _ABSENCE_SETTLE:
        first = absent_since or now
        return {
            'action': 'absent_wait', 'absent_since': first,
            'next_attempt_at': first + _ABSENCE_SETTLE,
        }
    if max(state['create_attempts'], 1) >= _MAX_CREATE_ATTEMPTS:
        return manual('reconcile_absent_after_retry')
    return {'action': 'requeue'}


def _internal_operation(records):
    return records.sudo().with_context(**{_CAP_KEY: _CAPABILITY})


def _require_internal(env):
    if env.context.get(_CAP_KEY) is not _CAPABILITY:
        raise AccessError(_('Provision operation fields are managed by the platform.'))


class SmartHomeProvisionOperation(models.Model):
    _name = 'woow_paas_platform.smart_home_provision_operation'
    _description = 'Experimental durable Smart Home provision intent'
    _order = 'id desc'

    operation_uuid = fields.Char(required=True, readonly=True, index=True, copy=False)
    home_id = fields.Many2one(
        'woow_paas_platform.smart_home', required=True, ondelete='restrict',
        readonly=True, index=True,
    )
    workspace_id = fields.Many2one(
        'woow_paas_platform.workspace', required=True, ondelete='restrict', readonly=True,
    )
    requester_id = fields.Many2one('res.users', required=True, ondelete='restrict', readonly=True)
    subscription_id = fields.Many2one(
        'sh.subscription.subscription', required=True, ondelete='restrict', readonly=True,
    )
    operator_url = fields.Char(required=True, readonly=True)
    provider_account_id = fields.Char(required=True, readonly=True)
    paas_domain = fields.Char(required=True, readonly=True)
    deploy_env = fields.Char(readonly=True)
    tunnel_name = fields.Char(required=True, readonly=True)
    subdomain = fields.Char(required=True, readonly=True)
    service_port = fields.Integer(required=True, readonly=True)
    phase = fields.Selection(_PHASES, required=True, default='queued', readonly=True, index=True)
    generation = fields.Char(readonly=True)
    lease_until = fields.Datetime(readonly=True)
    remote_tunnel_id = fields.Char(readonly=True, index=True)
    error_code = fields.Char(readonly=True)
    # Scheduling/reconcile bookkeeping; never part of the immutable intent.
    retry_count = fields.Integer(readonly=True, default=0)
    next_attempt_at = fields.Datetime(readonly=True, index=True)
    create_attempts = fields.Integer(readonly=True, default=0)
    reconcile_generation = fields.Char(readonly=True)
    absent_since = fields.Datetime(readonly=True)

    _sql_constraints = [
        ('operation_uuid_unique', 'unique(operation_uuid)', 'Provision operation must be unique.'),
    ]

    def init(self):
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS shc_provision_unfinished_home_unique
            ON woow_paas_platform_smart_home_provision_operation(home_id)
            WHERE phase <> 'completed'
        """)

    @api.model_create_multi
    def create(self, vals_list):
        _require_internal(self.env)
        return super().create(vals_list)

    def write(self, vals):
        _require_internal(self.env)
        immutable = {
            'operation_uuid', 'home_id', 'workspace_id', 'requester_id',
            'subscription_id', 'operator_url', 'provider_account_id',
            'paas_domain', 'deploy_env', 'tunnel_name', 'subdomain', 'service_port',
        }
        if immutable.intersection(vals):
            raise AccessError(_('Provision intent cannot be changed after enqueue.'))
        return super().write(vals)

    def unlink(self):
        # Retention: only the platform may remove, and only terminal completed
        # rows. Unfinished and manual-review rows are kept indefinitely.
        if self.env.context.get(_CAP_KEY) is not _CAPABILITY or any(
            operation.phase != 'completed' for operation in self
        ):
            raise AccessError(_('Provision recovery records are retained by the platform.'))
        return super().unlink()

    @api.model
    def _enabled(self):
        return self.env['ir.config_parameter'].sudo().get_param(_FLAG, 'False') == 'True'

    @api.model
    def _reconcile_enabled(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            _RECONCILE_FLAG, 'False') == 'True'

    @api.model
    def _retention_days(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(
            _RETENTION_KEY, str(_DEFAULT_RETENTION_DAYS))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_RETENTION_DAYS

    @api.model
    def _for_home(self, home):
        return self.sudo().search([('home_id', '=', home.id)], limit=1)

    def _public_status(self):
        self.ensure_one()
        return {
            'id': self.operation_uuid,
            'phase': self.phase,
            'retry_allowed': self.phase == 'anchored',
            'error_code': self.error_code or '',
        }

    def _accepted(self):
        self.ensure_one()
        if self.phase in ('create_unknown', 'recovery_required'):
            return {
                'success': False,
                'accepted': False,
                'error_code': 'provision_confirmation_required',
                'error': _('Setup needs confirmation before it can be retried.'),
                'operation': self._public_status(),
            }
        return {
            'success': False,
            'accepted': self.phase != 'completed',
            'operation': self._public_status(),
        }

    def _queue_existing_retry(self):
        """Reuse the committed intent with the worker's operation→home lock order."""
        self.ensure_one()
        if not self._lock():
            raise UserError(_('Setup is already being processed. Refresh before retrying.'))
        home = self.home_id
        self._validate_home(home, operation=self)
        # _lock invalidates both records. A worker may have completed between
        # the caller's cached phase read and this fresh, locked observation.
        if self.phase == 'completed':
            return {
                'success': home.state == 'active', 'accepted': False,
                'operation': self._public_status(),
            }
        if self.phase == 'anchored':
            if not self._enabled():
                return {
                    'success': False, 'accepted': False,
                    'error_code': 'provision_worker_disabled',
                    'error': _('Setup is temporarily paused. Retry when the worker is enabled.'),
                    'operation': self._public_status(),
                }
            if not self.remote_tunnel_id or home.tunnel_id != self.remote_tunnel_id:
                raise UserError(_('Setup needs confirmation before it can be retried.'))
            # This ordinary request transaction records the queued retry. No
            # provider call, journal identity change, or manual HTTP commit.
            home.write({'state': 'provisioning', 'error_message': False})
            # An explicit retry is due immediately, ahead of backed-off rows.
            _internal_operation(self).write({'next_attempt_at': False})
        return self._accepted()

    @api.model
    def _validate_home(self, home, requester=None, operation=None):
        if home.workspace_id.state != 'active':
            raise UserError(_('Workspace is not active.'))
        if not home.subscription_id or not home._is_subscription_active():
            raise UserError(_('An active subscription is required for queued setup.'))
        requester = requester or self.env.user
        if not requester.active:
            raise AccessError(_('Setup requester is no longer active.'))
        if not requester.has_group('base.group_system'):
            access = self.env['woow_paas_platform.workspace_access'].sudo().search([
                ('workspace_id', '=', home.workspace_id.id),
                ('user_id', '=', requester.id),
                ('role', 'in', ['owner', 'admin']),
            ], limit=1)
            if not access:
                raise AccessError(_('Workspace setup permission is required.'))
        if operation:
            params = self.env['ir.config_parameter'].sudo()
            if (
                home.workspace_id != operation.workspace_id
                or home.subscription_id != operation.subscription_id
                or home.ha_port != operation.service_port
                or params.get_param('woow_paas_platform.operator_url', '').rstrip('/')
                != operation.operator_url
                or home._get_paas_domain() != operation.paas_domain
                or params.get_param('woow_paas_platform.deploy_env', 'prod')
                != (operation.deploy_env or 'prod')
            ):
                raise UserError(_('Provision intent binding changed; confirmation is required.'))
        return home._get_operator_client()

    @api.model
    def _enqueue(self, home):
        home.ensure_one()
        home.check_field_access_rights('write', ['tunnel_token'])
        home.flush_recordset()
        self.env.cr.execute(
            'SELECT id FROM woow_paas_platform_smart_home WHERE id = %s FOR UPDATE',
            [home.id],
        )
        home.invalidate_recordset()
        client = self._validate_home(home)
        existing = self._for_home(home)
        if existing and existing.phase != 'completed':
            # New-intent enqueue holds only the home lock. Never acquire an
            # operation lock here in reverse order to the background worker.
            if existing.phase == 'anchored':
                raise UserError(_('Setup state changed. Refresh before retrying.'))
            return existing._accepted()
        if home.tunnel_id:
            return None
        if home.state != 'pending':
            raise UserError(_('Previous setup needs confirmation before a new tunnel can be created.'))
        binding = client.get_tunnel_provider_binding()
        account_id = binding.get('provider_account_id')
        if not isinstance(account_id, str) or not account_id:
            raise UserError(_('Provider binding is unavailable.'))
        operation_uuid = str(uuid.uuid4())
        # Marker is traceability only, never authorization or a uniqueness claim.
        name = home._generate_tunnel_name() + '-op-' + uuid.UUID(operation_uuid).hex
        if len(name) > 100:
            raise UserError(_('Internal tunnel name is too long.'))
        params = self.env['ir.config_parameter'].sudo()
        operation = _internal_operation(self).create({
            'operation_uuid': operation_uuid,
            'home_id': home.id,
            'workspace_id': home.workspace_id.id,
            'requester_id': self.env.uid,
            'subscription_id': home.subscription_id.id,
            'operator_url': client.base_url,
            'provider_account_id': account_id,
            'paas_domain': home._get_paas_domain(),
            'deploy_env': params.get_param('woow_paas_platform.deploy_env', 'prod'),
            'tunnel_name': name,
            'subdomain': home._generate_subdomain(),
            'service_port': home.ha_port,
        })
        home.write({'state': 'provisioning', 'error_message': False})
        return operation._accepted()

    def _lock(self):
        self.ensure_one()
        self.env.cr.execute("""
            SELECT id FROM woow_paas_platform_smart_home_provision_operation
            WHERE id = %s FOR UPDATE SKIP LOCKED
        """, [self.id])
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset()
        self.env.cr.execute(
            'SELECT id FROM woow_paas_platform_smart_home WHERE id = %s FOR UPDATE',
            [self.home_id.id],
        )
        self.home_id.invalidate_recordset()
        return True

    @api.model
    def _failure(self, operation_id, generation, code, unknown=False):
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            op = _internal_operation(env[_MODEL].browse(operation_id))
            if not op.exists() or not op._lock() or op.generation != generation:
                return
            if unknown:
                schedule = {'retry_count': 0, 'next_attempt_at': False, 'absent_since': False}
            else:
                count = op.retry_count + 1
                schedule = {
                    'retry_count': count,
                    'next_attempt_at': fields.Datetime.now() + _backoff(_WORKER_BACKOFF_BASE, count),
                }
            op.write({
                'phase': 'recovery_required' if unknown else 'anchored',
                'error_code': code, 'lease_until': False, **schedule,
            })
            op.home_id.write({
                'state': 'error',
                'error_message': _(
                    'Setup needs confirmation. Reference: %(operation)s',
                    operation=op.operation_uuid,
                ) if unknown else _('Setup could not finish. The existing tunnel is retained.'),
            })

    @api.model
    def _process_one(self, operation_id):
        """Private worker entry; stage cursors are independent of its caller."""
        if not self._enabled():
            return 'disabled'
        generation = str(uuid.uuid4())
        create = False
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            op = _internal_operation(env[_MODEL].browse(operation_id))
            if not op.exists() or not op._lock():
                return 'busy'
            now = fields.Datetime.now()
            if op.phase in ('completed', 'recovery_required'):
                return op.phase
            if op.phase == 'create_unknown':
                if op.lease_until and op.lease_until > now:
                    return 'busy'
                op.write({
                    'phase': 'recovery_required', 'error_code': 'create_worker_interrupted',
                    'retry_count': 0, 'next_attempt_at': False, 'absent_since': False,
                })
                op.home_id.write({'state': 'error', 'error_message': _('Setup needs confirmation before retry.')})
                return 'recovery_required'
            if op.phase == 'converging' and op.lease_until and op.lease_until > now:
                return 'busy'
            try:
                client = op._validate_home(op.home_id, op.requester_id, op)
                binding = client.get_tunnel_provider_binding()
                if binding.get('provider_account_id') != op.provider_account_id:
                    raise UserError(_('Provider binding changed.'))
            except (AccessError, UserError):
                # This intent is still before provider create. Known local
                # authority/binding invalidity is quarantined without a POST.
                op.write({
                    'phase': 'recovery_required', 'error_code': 'intent_preflight_invalid',
                    'lease_until': False,
                })
                op.home_id.write({
                    'state': 'error',
                    'error_message': _('Setup permission or binding changed; confirmation is required.'),
                })
                return 'recovery_required'
            create = op.phase == 'queued'
            if not create and (
                not op.remote_tunnel_id or op.home_id.tunnel_id != op.remote_tunnel_id
            ):
                op.write({'phase': 'recovery_required', 'error_code': 'anchor_mismatch'})
                return 'recovery_required'
            op.write({
                'phase': 'create_unknown' if create else 'converging',
                'generation': generation, 'lease_until': now + timedelta(minutes=5),
                'error_code': False,
                'create_attempts': op.create_attempts + (1 if create else 0),
            })
            snapshot = {
                'name': op.tunnel_name, 'account': op.provider_account_id,
                'subdomain': op.subdomain, 'domain': op.paas_domain,
            }
        # The committed marker exists before ANY provider create.
        if create:
            try:
                result = client.create_bare_tunnel(snapshot['name'], snapshot['account'])
                tunnel_id = str(uuid.UUID(result['tunnel_id']))
                if (
                    result['tunnel_name'] != snapshot['name']
                    or result['provider_account_id'] != snapshot['account']
                ):
                    raise ValueError('provider identity mismatch')
            except Exception:
                self._failure(operation_id, generation, 'create_outcome_unknown', unknown=True)
                return 'recovery_required'
            try:
                # Save verified response evidence separately from home binding.
                # Revocation/drift after POST must not discard a received ID;
                # it still never authorizes adoption or further provider writes.
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    op = _internal_operation(env[_MODEL].browse(operation_id))
                    if (
                        not op._lock() or op.generation != generation
                        or op.phase not in ('create_unknown', 'recovery_required')
                    ):
                        return 'stale'
                    if op.remote_tunnel_id and op.remote_tunnel_id != tunnel_id:
                        raise UserError(_('Received tunnel identity does not match.'))
                    op.write({'remote_tunnel_id': tunnel_id})
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    op = _internal_operation(env[_MODEL].browse(operation_id))
                    if not op._lock() or op.generation != generation or op.phase != 'create_unknown':
                        return 'stale'
                    op._validate_home(op.home_id, op.requester_id, op)
                    if op.home_id.tunnel_id and op.home_id.tunnel_id != tunnel_id:
                        raise UserError(_('Existing tunnel identity does not match.'))
                    _internal_identity(op.home_id).write({
                        'tunnel_id': tunnel_id, 'tunnel_name': snapshot['name'],
                        'subdomain': snapshot['subdomain'],
                        'tunnel_route': 'https://' + snapshot['subdomain'] + '.' + snapshot['domain'],
                        'state': 'provisioning',
                    })
                    op.write({'remote_tunnel_id': tunnel_id, 'phase': 'converging'})
            except Exception:
                self._failure(operation_id, generation, 'anchor_commit_failed', unknown=True)
                return 'recovery_required'
        try:
            # Token/ingress errors cannot roll back the already committed ID.
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                op = _internal_operation(env[_MODEL].browse(operation_id))
                if not op._lock() or op.generation != generation or op.phase != 'converging':
                    return 'stale'
                client = op._validate_home(op.home_id, op.requester_id, op)
                if client.get_tunnel_provider_binding().get('provider_account_id') != op.provider_account_id:
                    raise UserError(_('Provider binding changed.'))
                token = client.get_tunnel_token(op.remote_tunnel_id).get('token')
                if not isinstance(token, str) or not token:
                    raise UserError(_('Tunnel token is unavailable.'))
                _internal_identity(op.home_id).write({'tunnel_token': token})
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                op = _internal_operation(env[_MODEL].browse(operation_id))
                if not op._lock() or op.generation != generation or op.phase != 'converging':
                    return 'stale'
                op._validate_home(op.home_id, op.requester_id, op)
                op.home_id._converge_existing_tunnel()
                done = op.home_id.state == 'active'
                count = 0 if done else op.retry_count + 1
                op.write({
                    'phase': 'completed' if done else 'anchored',
                    'error_code': False if done else 'configuration_incomplete',
                    'lease_until': False,
                    'retry_count': count,
                    'next_attempt_at': False if done else (
                        fields.Datetime.now() + _backoff(_WORKER_BACKOFF_BASE, count)),
                })
                return 'completed' if done else 'anchored'
        except Exception:
            self._failure(operation_id, generation, 'configuration_failed')
            return 'anchored'

    @api.model
    def _worker_candidates(self, limit=10):
        """Due rows, fairest first: never-failed/explicitly retried rows (no
        ``next_attempt_at``) precede backed-off failures, so a block of
        permanently failing anchored rows cannot starve new intents."""
        now = fields.Datetime.now()
        return self.sudo().search([
            '|', '|',
            ('phase', '=', 'queued'),
            '&', ('phase', '=', 'anchored'),
            '|', ('next_attempt_at', '=', False), ('next_attempt_at', '<=', now),
            '&', ('phase', 'in', ['create_unknown', 'converging']),
            '|', ('lease_until', '=', False), ('lease_until', '<=', now),
        ], limit=limit, order='next_attempt_at asc nulls first, id')

    @api.model
    def _cron_process(self, limit=10):
        if not self._enabled():
            return
        if self._reconcile_enabled():
            try:
                self._reconcile_batch(limit=limit)
            except Exception as exc:
                _logger.warning('SHC provision reconcile pass stopped: %s', type(exc).__name__)
        # Fresh snapshot: the cron cursor is REPEATABLE READ and would not see
        # rows the reconcile pass just committed through its own cursors.
        with self.env.registry.cursor() as cr:
            Operation = api.Environment(cr, SUPERUSER_ID, {})[_MODEL]
            due = [(op.id, op.operation_uuid) for op in Operation._worker_candidates(limit)]
        for operation_id, operation_uuid in due:
            try:
                self._process_one(operation_id)
            except Exception:
                # Sanitized classification only; known durable markers remain.
                _logger.warning('SHC provision worker stopped operation=%s', operation_uuid)
        try:
            self._purge_completed()
        except Exception as exc:
            _logger.warning('SHC provision retention pass stopped: %s', type(exc).__name__)

    # ------------------------------------------------------------------ #
    #  Retention                                                          #
    # ------------------------------------------------------------------ #

    @api.model
    def _purge_completed(self, limit=_PURGE_BATCH):
        """Drop completed rows older than the retention window (<=0 keeps all)."""
        days = self._retention_days()
        if days <= 0:
            return 0
        cutoff = fields.Datetime.now() - timedelta(days=days)
        operations = self.sudo().search([
            ('phase', '=', 'completed'), ('write_date', '<', cutoff),
        ], limit=limit, order='write_date, id')
        count = len(operations)
        if operations:
            _internal_operation(operations).unlink()
        return count

    # ------------------------------------------------------------------ #
    #  Authoritative reconcile of unknown create outcomes                 #
    # ------------------------------------------------------------------ #

    def _reconcile_due(self, now):
        self.ensure_one()
        return (
            self.phase == 'recovery_required'
            and self.error_code in _UNKNOWN_OUTCOME_CODES
            and (not self.next_attempt_at or self.next_attempt_at <= now)
        )

    @api.model
    def _reconcile_candidates(self, limit=10):
        now = fields.Datetime.now()
        return self.sudo().search([
            ('phase', '=', 'recovery_required'),
            ('error_code', 'in', list(_UNKNOWN_OUTCOME_CODES)),
            '|', ('next_attempt_at', '=', False), ('next_attempt_at', '<=', now),
        ], limit=limit, order='next_attempt_at asc nulls first, id')

    def _reconcile_state(self):
        self.ensure_one()
        return {
            'tunnel_name': self.tunnel_name,
            'provider_account_id': self.provider_account_id,
            'operator_url': self.operator_url,
            'deploy_env': self.deploy_env,
            'remote_tunnel_id': self.remote_tunnel_id or False,
            'home_tunnel_id': self.home_id.tunnel_id or False,
            'retry_count': self.retry_count,
            'create_attempts': self.create_attempts,
            'absent_since': self.absent_since,
        }

    @api.model
    def _reconcile_observe(self):
        client = get_paas_operator_client(self.env)
        if not client:
            return {'ok': False, 'reason': 'operator_unconfigured'}
        try:
            before = client.get_tunnel_provider_binding()
            inventory = client.get_reconcile_inventory()
            after = client.get_tunnel_provider_binding()
        except Exception:
            # Unavailable is never absence; classification only, no body.
            return {'ok': False, 'reason': 'provider_unavailable'}
        return _parse_observation(before, inventory, after, client.base_url)

    @api.model
    def _reconcile_batch(self, limit=10):
        """Claim due unknown-outcome rows, read the provider once, then apply.

        Claims commit a short lease before any provider read, so a killed
        reconciler only delays a row and two reconcilers never both observe it.
        Apply re-locks and requires the same claim token.
        """
        if not (self._enabled() and self._reconcile_enabled()):
            return {}
        claimed = []
        for operation_id in self._reconcile_candidates(limit).ids:
            token = str(uuid.uuid4())
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                op = _internal_operation(env[_MODEL].browse(operation_id))
                now = fields.Datetime.now()
                if not op.exists() or not op._lock() or not op._reconcile_due(now):
                    continue
                op.write({
                    'reconcile_generation': token,
                    'next_attempt_at': now + _CLAIM_LEASE,
                })
            claimed.append((operation_id, token))
        if not claimed:
            return {}
        observation = self._reconcile_observe()
        outcomes = {}
        for operation_id, token in claimed:
            try:
                outcomes[operation_id] = self._reconcile_apply(operation_id, token, observation)
            except Exception as exc:
                # The claim lease expires; the row is retried later.
                _logger.warning('SHC provision reconcile stopped operation_id=%s: %s',
                                operation_id, type(exc).__name__)
                outcomes[operation_id] = 'error'
        return outcomes

    @api.model
    def _reconcile_apply(self, operation_id, token, observation):
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            op = _internal_operation(env[_MODEL].browse(operation_id))
            if not op.exists() or not op._lock():
                return 'busy'
            if (
                op.reconcile_generation != token or op.phase != 'recovery_required'
                or op.error_code not in _UNKNOWN_OUTCOME_CODES
            ):
                return 'stale'
            now = fields.Datetime.now()
            home = op.home_id
            decision = _reconcile_decision(op._reconcile_state(), observation, now)
            action = decision['action']
            evidence = decision.get('tunnel_id')
            if action in ('adopt', 'requeue'):
                try:
                    op._validate_home(home, op.requester_id, op)
                except AccessError:
                    decision = {'action': 'manual', 'code': 'reconcile_authority_invalid'}
                except UserError:
                    decision = {'action': 'manual', 'code': 'reconcile_intent_invalid'}
                action = decision['action']
            values = {'reconcile_generation': False}
            if action == 'adopt':
                _internal_identity(home).write({
                    'tunnel_id': evidence, 'tunnel_name': op.tunnel_name,
                    'subdomain': op.subdomain,
                    'tunnel_route': 'https://' + op.subdomain + '.' + op.paas_domain,
                    'state': 'provisioning', 'error_message': False,
                })
                values.update({
                    'remote_tunnel_id': evidence, 'phase': 'anchored', 'error_code': False,
                    'retry_count': 0, 'next_attempt_at': False, 'absent_since': False,
                })
                outcome = 'adopted'
            elif action == 'requeue':
                home.write({'state': 'provisioning', 'error_message': False})
                values.update({
                    'phase': 'queued', 'error_code': False, 'retry_count': 0,
                    'next_attempt_at': False, 'absent_since': False,
                })
                outcome = 'requeued'
            elif action == 'absent_wait':
                values.update({
                    'absent_since': decision['absent_since'],
                    'next_attempt_at': decision['next_attempt_at'],
                })
                outcome = 'absent_wait'
            elif action == 'backoff':
                values.update({
                    'retry_count': decision['retry_count'],
                    'next_attempt_at': decision['next_attempt_at'],
                })
                outcome = 'backoff'
            else:
                if evidence and not op.remote_tunnel_id:
                    # Provider-confirmed id is kept as evidence only; the home
                    # stays unbound and no token/configuration is requested.
                    values['remote_tunnel_id'] = evidence
                values.update({
                    'error_code': decision['code'], 'next_attempt_at': False,
                    'absent_since': False,
                })
                home.write({
                    'state': 'error',
                    'error_message': _(
                        'Setup needs confirmation. Reference: %(operation)s',
                        operation=op.operation_uuid,
                    ),
                })
                outcome = decision['code']
            op.write(values)
            return outcome


class SmartHomeDurableProvisionPrototype(models.Model):
    _inherit = 'woow_paas_platform.smart_home'

    def action_provision(self):
        self.ensure_one()
        Operation = self.env[_MODEL]
        existing = Operation._for_home(self)
        if existing:
            self.check_field_access_rights('write', ['tunnel_token'])
            return existing._queue_existing_retry()
        if Operation._enabled() and not self.tunnel_id:
            return Operation._enqueue(self)
        return super().action_provision()

    def to_dict(self, include_details=False):
        data = super().to_dict(include_details=include_details)
        operation = self.env[_MODEL]._for_home(self)
        if operation:
            data['provision_operation'] = operation._public_status()
        return data

    def unlink(self):
        # Retention: unfinished or manual-review journals block deletion. A
        # home whose journals are all completed may be deleted; its journal
        # rows go in the same savepoint, so a failed teardown keeps both.
        operations = self.env[_MODEL].sudo().search([('home_id', 'in', self.ids)])
        if not operations:
            return super().unlink()
        if any(operation.phase != 'completed' for operation in operations):
            raise UserError(_('Setup recovery history must be reviewed before this home can be deleted.'))
        self._assert_unlink_authorized()
        with self.env.cr.savepoint():
            _internal_operation(operations).unlink()
            return super().unlink()

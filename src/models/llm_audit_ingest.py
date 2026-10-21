"""Dormant one-page proxy audit ingestion. No scheduler/classification/incidents.

Private caller must independently verify expected environment ownership. CRON
HMAC authenticates the request; HTTPS authenticates the endpoint. The upstream
contract has neither per-event response signatures nor a store generation ID.
"""
import time

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError

from .billing_incident import _is_odoo_module_uninstall
from .llm_common import LlmQuotaInteger, config_fingerprint, llm_unconfigured
from ..services import newapi_client as nac
from ..services.llm_audit_page import environment, normalize_page
from ..services.llm_sql_budget import deadline_cursor

STREAM = 'woow_paas_platform.llm_audit_cursor'
EVENT = 'woow_paas_platform.llm_audit_evidence'
_KEY = 'woow_llm_audit_ingest_capability'
_CAP = object()


class AuditIngestError(ValueError):
    def __init__(self):
        super().__init__('audit_ingest_unavailable')


def _cap(records):
    return records.with_context({}, **{_KEY: _CAP})


def _require(records):
    if records.env.context.get(_KEY) is not _CAP:
        raise AccessError(_('Proxy audit data can only be persisted by the ingestion service.'))


class LlmAuditCursor(models.Model):
    _name = STREAM
    _description = 'Proxy audit environment ingestion cursor'
    _rec_name = 'environment'

    environment = fields.Char(required=True, readonly=True, index=True)
    fingerprint = fields.Char(required=True, readonly=True)
    after_seq = LlmQuotaInteger(default=0, readonly=True)
    through_seq = LlmQuotaInteger(default=0, readonly=True)
    snapshot_open = fields.Boolean(default=False, readonly=True)
    covered_seq = LlmQuotaInteger(default=0, readonly=True)
    _sql_constraints = [
        ('environment_unique', 'UNIQUE(environment)', 'An audit environment cursor already exists.'),
        ('sequence_valid', 'CHECK(covered_seq >= 0 AND covered_seq <= after_seq AND '
         'through_seq >= after_seq AND after_seq >= 0)', 'Invalid audit sequence.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        _require(self)
        return super().create(vals_list).with_context(**{_KEY: False})

    def write(self, vals):
        _require(self)
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('Audit cursors cannot be deleted.'))

    @api.model
    def _ingest_tick(self, expected_env):
        """One page <=100; isolated transaction shares a <=30s absolute budget.

        No registration/automatic activation. Missing control store, invalid
        pages and DB errors roll back this page. Previously committed pages
        remain durable. Only a successful context exit commits this transaction.
        """
        if llm_unconfigured(self.env):
            return None
        if not self.env.su:
            raise AccessError(_('Proxy audit ingestion requires the platform service.'))
        environment(expected_env)
        deadline = time.monotonic() + 30
        with deadline_cursor(self.env.registry.cursor, deadline, lock_seconds=0.1) as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            return env[STREAM]._ingest_in_env(expected_env, deadline)

    @api.model
    def _ingest_in_env(self, expected_env, deadline):
        """Transaction core for owned-cursor tests; production uses _ingest_tick.

        Lock only this stream, never accounts/orders; one bounded HTTP read.
        First-stream races fail UNIQUE/serialization and retry next invocation.
        No exception is swallowed, no partial page is committed by this method.
        """
        if llm_unconfigured(self.env):
            return None
        if not self.env.su:
            raise AccessError(_('Proxy audit ingestion requires the platform service.'))
        environment(expected_env)
        if time.monotonic() >= deadline - 1:
            raise AuditIngestError()
        with self.env.cr.savepoint():
            fingerprint = config_fingerprint(self.env)
            stream = self.search([('environment', '=', expected_env)], limit=1)
            if not stream:
                stream = _cap(self).create({'environment': expected_env, 'fingerprint': fingerprint})
            stream.flush_recordset()
            self.env.cr.execute('SELECT id FROM woow_paas_platform_llm_audit_cursor '
                                'WHERE id=%s FOR NO KEY UPDATE NOWAIT', (stream.id,))
            stream.invalidate_recordset()
            if stream.fingerprint != fingerprint:
                raise AuditIngestError()
            after = stream.after_seq
            through = stream.through_seq if stream.snapshot_open else None
            client = self.env['woow_paas_platform.llm_account']._llm_client(nac.ROLE_CRON)
            result = client.control_audit(after, through, 100, deadline=deadline - 1)
            if not isinstance(result, nac.Ok):
                raise AuditIngestError()
            head, rows = normalize_page(result.data, expected_env, after, through)
            if time.monotonic() >= deadline - 0.5:
                raise AuditIngestError()
            Evidence = self.env[EVENT]
            for values in rows:
                existing = Evidence.search([('cursor_id', '=', stream.id), ('seq', '=', values['seq'])], limit=1)
                if existing:
                    # Exact replay is a no-op; conflicting persisted evidence
                    # cannot be overwritten to make a cursor appear complete.
                    if any(existing[key] != value for key, value in values.items()):
                        raise AuditIngestError()
                else:
                    _cap(Evidence).create(dict(values, cursor_id=stream.id))
            after += len(rows)
            done = after == head
            _cap(stream).write({'after_seq': after, 'through_seq': head,
                                'snapshot_open': not done,
                                'covered_seq': head if done else stream.covered_seq})
            self.env.flush_all()
            if time.monotonic() >= deadline - 0.1:
                raise AuditIngestError()
            return {'after': after, 'through': head, 'snapshot_complete': done}


class LlmAuditEvidence(models.Model):
    _name = EVENT
    _description = 'Immutable proxy audit event evidence'
    _order = 'cursor_id, seq'
    _rec_name = 'attempt_id'

    cursor_id = fields.Many2one(STREAM, required=True, readonly=True, ondelete='restrict', index=True)
    seq = LlmQuotaInteger(required=True, readonly=True)
    attempt_id = fields.Char(required=True, readonly=True, size=36, index=True)
    kind = fields.Selection([('intent', 'Intent'), ('outcome', 'Outcome')], required=True, readonly=True)
    # Odoo Datetime serialization can discard microseconds; preserve canonical text.
    at = fields.Char(required=True, readonly=True, size=32)
    target_user_id = fields.Integer(readonly=True)
    quota = LlmQuotaInteger(readonly=True)
    result = fields.Selection([(v, v) for v in
                               ('forwarded', 'connect', 'reset', 'timeout', 'cancelled', 'not_sent')], readonly=True)
    status = fields.Integer(readonly=True)
    request_id = fields.Char(readonly=True, size=128)
    _sql_constraints = [
        ('sequence_unique', 'UNIQUE(cursor_id, seq)', 'This audit sequence was already ingested.'),
        ('attempt_kind_unique', 'UNIQUE(cursor_id, attempt_id, kind)', 'This attempt event already exists.'),
        ('sequence_positive', 'CHECK(seq > 0)', 'Invalid audit sequence.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        _require(self)
        return super().create(vals_list).with_context(**{_KEY: False})

    def write(self, vals):
        raise AccessError(_('Proxy audit evidence is immutable.'))

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('Proxy audit evidence cannot be deleted.'))

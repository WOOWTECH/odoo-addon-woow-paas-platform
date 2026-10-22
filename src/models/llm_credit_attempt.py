"""Local attempt identity and irreversible worker-entry latch (I3 slice A).

No remote owner/epoch assertion, audit join, wire opt-in or garbage collection.
The event reference is scalar: the event's 400-day retention is independent.
"""
import uuid

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError

from .billing_incident import _is_odoo_module_uninstall
from .llm_common import LlmQuotaInteger

ATTEMPT_MODEL = 'woow_paas_platform.llm_credit_attempt'
ATTEMPT_TABLE = 'woow_paas_platform_llm_credit_attempt'
_ISSUE_CAPABILITY = object()


def _issue_cr(cr, row, event_id, capability):
    """Only the claim transaction may issue; no independent commit here."""
    if capability is not _ISSUE_CAPABILITY:
        raise AccessError(_('Credit attempt issuance requires the claim service.'))
    attempt_uuid = str(uuid.uuid4())
    cr.execute(
        'INSERT INTO ' + ATTEMPT_TABLE + ' (order_id, account_id, attempt_no, '
        'attempt_uuid, target_user_id, quota, reservation_event_id, '
        'create_uid, create_date, write_uid, write_date) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (row['id'], row['account_id'], row['attempt'], attempt_uuid,
         row['newapi_user_id'], row['quota'], event_id,
         SUPERUSER_ID, fields.Datetime.now(), SUPERUSER_ID, fields.Datetime.now()),
        log_exceptions=False)
    return attempt_uuid


class LlmCreditAttempt(models.Model):
    _name = ATTEMPT_MODEL
    _description = 'Immutable local LLM conversion attempt'
    _rec_name = 'attempt_uuid'
    _order = 'id'

    order_id = fields.Many2one('woow_paas_platform.llm_credit_order', required=True,
                              ondelete='restrict', readonly=True, index=True)
    account_id = fields.Many2one('woow_paas_platform.llm_account', required=True,
                                ondelete='restrict', readonly=True, index=True)
    attempt_no = fields.Integer(required=True, readonly=True)
    attempt_uuid = fields.Char(required=True, readonly=True, copy=False)
    target_user_id = LlmQuotaInteger(required=True, readonly=True)
    quota = LlmQuotaInteger(required=True, readonly=True)
    reservation_event_id = LlmQuotaInteger(required=True, readonly=True)
    dispatch_claimed_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        ('uuid_unique', 'UNIQUE(attempt_uuid)', 'Attempt UUID must be unique.'),
        ('order_attempt_unique', 'UNIQUE(order_id, attempt_no)', 'Attempt already exists.'),
        ('uuid_v4', "CHECK(attempt_uuid ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')",
         'Attempt UUID must be canonical UUIDv4.'),
        ('snapshot_positive', 'CHECK(attempt_no > 0 AND target_user_id > 0 AND '
         'quota > 0 AND reservation_event_id > 0)', 'Invalid attempt snapshot.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_('Credit attempts can only be issued by the claim service.'))

    def write(self, vals):
        raise AccessError(_('Credit attempts are immutable.'))

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('Credit attempts must be retained.'))

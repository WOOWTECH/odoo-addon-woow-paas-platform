"""Durable, source-relation-free billing incident ledger (#864 PR-D).

Incident business fields are deliberately scalar snapshots.  Their source rows
may exist only in an outer transaction which later rolls back, so adding a
source Many2one here would make the fail-open record fail exactly when it is
needed most.  ``mail.thread`` contributes only Odoo mail metadata so standard
``mail.activity`` create/done/unlink semantics work against an already committed
incident.  This global finance ledger is assigned to the Odoo role
`account.group_account_manager` (not a Workspace role): it can read all
incidents across all companies, and review activities go to all current
managers in that Odoo group.
"""

import html
import logging
import math
import re
import unicodedata
from datetime import date, datetime

from markupsafe import Markup, escape
from psycopg2 import sql

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.tools import config, html2plaintext, single_email_re
from odoo.tools.translate import get_translation


_logger = logging.getLogger(__name__)

# Durable policy (#986 comment 11112):
# Odoo role `account.group_account_manager` (not a Workspace role);
# all incidents across all companies; all current managers in that Odoo group;
# no company M2O/rule; base.group_system behavior is not narrowed.  That last
# clause is not an incident grant: a pure system-only user is not a manager.


class _BillingIncidentCapability:
    """Base for JSON-inexpressible, module-private lifecycle capabilities."""

    __slots__ = ('_name',)

    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return '<billing incident %s capability>' % self._name


_BILLING_INCIDENT_CREATE = _BillingIncidentCapability('test fixture create')
_BILLING_INCIDENT_RESOLUTION_TRANSITION = _BillingIncidentCapability(
    'resolution transition')
_BILLING_INCIDENT_CHATTER = _BillingIncidentCapability(
    'validated activity-done chatter')
_BILLING_ACTIVITY_SCHEDULER = _BillingIncidentCapability(
    'activity scheduler')
_BILLING_ACTIVITY_TERMINAL = _BillingIncidentCapability(
    'activity terminal')
_BILLING_ROSTER_MUTATION = _BillingIncidentCapability(
    'manager roster mutation')
_BILLING_ATTACHMENT_CLEANUP = _BillingIncidentCapability(
    'squatted attachment cleanup')
_BILLING_INCIDENT_CREATE_CTX_KEY = 'woow_billing_incident_create'
_BILLING_INCIDENT_RESOLUTION_CTX_KEY = (
    'woow_billing_incident_resolution_transition')
_BILLING_INCIDENT_CHATTER_CTX_KEY = 'woow_billing_incident_chatter'
_BILLING_ACTIVITY_SCHEDULER_CTX_KEY = 'woow_billing_activity_scheduler'
_BILLING_ACTIVITY_TERMINAL_CTX_KEY = 'woow_billing_activity_terminal'
_BILLING_ROSTER_MUTATION_CTX_KEY = 'woow_billing_roster_mutation'
_BILLING_ATTACHMENT_CLEANUP_CTX_KEY = 'woow_billing_attachment_cleanup'
_ODOO_MODULE_UNINSTALL_CTX_KEY = '_force_unlink'
_BILLING_INTERNAL_CONTEXT_ALLOWLIST = frozenset({
    'allowed_company_ids',
    'lang',
    'mail_notify_force_send',
    'tz',
})
_BILLING_ROSTER_LOCK_NAME = 'woow.billing.incident.account-manager-roster'


def _acquire_billing_roster_lock(cr):
    """Serialize effective finance-roster reads and mutations until commit."""
    cr.execute(
        'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
        (_BILLING_ROSTER_LOCK_NAME,),
    )


def _with_billing_incident_capability(records, key, capability):
    return records.with_context(**{key: capability})


def _is_billing_incident_create(env):
    return (env.context.get(_BILLING_INCIDENT_CREATE_CTX_KEY)
            is _BILLING_INCIDENT_CREATE)


def _resolution_transition(records):
    """Attach the module-private first-resolution capability to ``records``."""
    return _with_billing_incident_capability(
        records,
        _BILLING_INCIDENT_RESOLUTION_CTX_KEY,
        _BILLING_INCIDENT_RESOLUTION_TRANSITION,
    )


def _is_resolution_transition(env):
    return (env.context.get(_BILLING_INCIDENT_RESOLUTION_CTX_KEY)
            is _BILLING_INCIDENT_RESOLUTION_TRANSITION)


def _incident_chatter(records):
    return _with_billing_incident_capability(
        records,
        _BILLING_INCIDENT_CHATTER_CTX_KEY,
        _BILLING_INCIDENT_CHATTER,
    )


def _is_incident_chatter(env):
    return (env.context.get(_BILLING_INCIDENT_CHATTER_CTX_KEY)
            is _BILLING_INCIDENT_CHATTER)


def _with_clean_billing_internal_context(records, **capabilities):
    """Discard caller context except the small operational allowlist."""
    context = {
        key: value
        for key, value in records.env.context.items()
        if key in _BILLING_INTERNAL_CONTEXT_ALLOWLIST
    }
    context.update(capabilities)
    return records.with_context(context)


def _activity_scheduler(records):
    # Enter capabilities only after rebuilding context from an allowlist.  In
    # particular, caller-controlled default_* keys must never become defaults
    # for the scheduler's ORM create.
    return _with_clean_billing_internal_context(
        records,
        **{
            _BILLING_ACTIVITY_SCHEDULER_CTX_KEY:
                _BILLING_ACTIVITY_SCHEDULER,
            # Odoo activity create/unlink may emit controlled messages.
            _BILLING_INCIDENT_CHATTER_CTX_KEY: _BILLING_INCIDENT_CHATTER,
        }
    )


def _is_activity_scheduler(env):
    return (env.context.get(_BILLING_ACTIVITY_SCHEDULER_CTX_KEY)
            is _BILLING_ACTIVITY_SCHEDULER)


def _activity_terminal(records):
    return _with_clean_billing_internal_context(
        records,
        **{
            _BILLING_ACTIVITY_TERMINAL_CTX_KEY: _BILLING_ACTIVITY_TERMINAL,
        }
    )


def _is_activity_terminal(env):
    return (env.context.get(_BILLING_ACTIVITY_TERMINAL_CTX_KEY)
            is _BILLING_ACTIVITY_TERMINAL)


def _roster_mutation(records):
    return _with_billing_incident_capability(
        records,
        _BILLING_ROSTER_MUTATION_CTX_KEY,
        _BILLING_ROSTER_MUTATION,
    )


def _is_roster_mutation(env):
    return (env.context.get(_BILLING_ROSTER_MUTATION_CTX_KEY)
            is _BILLING_ROSTER_MUTATION)


def _attachment_cleanup(records):
    return _with_clean_billing_internal_context(
        records,
        **{
            _BILLING_ATTACHMENT_CLEANUP_CTX_KEY:
                _BILLING_ATTACHMENT_CLEANUP,
        }
    )


def _is_attachment_cleanup(env):
    return (env.context.get(_BILLING_ATTACHMENT_CLEANUP_CTX_KEY)
            is _BILLING_ATTACHMENT_CLEANUP)


def _active_account_manager_roster_ids(env):
    """Read the effective active finance roster without active_test/cache drift."""
    # res.users.active may still be a pending ORM write (notably after core
    # reactivates the delegated partner first).  Flush before raw SQL or the
    # after-snapshot can repeat the old inactive state and skip convergence.
    env['res.users'].flush_model(['active', 'groups_id'])
    manager_group_id = env['ir.model.data']._xmlid_to_res_id(
        'account.group_account_manager', raise_if_not_found=False)
    if not manager_group_id:
        return frozenset()
    env.cr.execute(
        'SELECT u.id FROM res_users u '
        'JOIN res_groups_users_rel rel ON rel.uid = u.id '
        'WHERE rel.gid = %s AND u.active = true ORDER BY u.id',
        (manager_group_id,),
    )
    return frozenset(user_id for user_id, in env.cr.fetchall())


def _active_account_manager_partner_ids(env):
    user_ids = sorted(_active_account_manager_roster_ids(env))
    if not user_ids:
        return frozenset()
    env.cr.execute(
        'SELECT DISTINCT partner_id FROM res_users '
        'WHERE id = ANY(%s) AND partner_id IS NOT NULL',
        (user_ids,),
    )
    return frozenset(partner_id for partner_id, in env.cr.fetchall())


def _converge_billing_roster_if_changed(env, before):
    after = _active_account_manager_roster_ids(env)
    if after != before and not _is_odoo_module_uninstall(env):
        env['woow.billing.incident'].sudo() \
            ._converge_unresolved_review_activities_current_cursor()
    return after


def _is_odoo_module_uninstall(env):
    """Accept core's unlink hint only while this addon is actually uninstalling."""
    if env.context.get(_ODOO_MODULE_UNINSTALL_CTX_KEY) is not True:
        return False
    module = env['ir.module.module'].sudo().search([
        ('name', '=', 'woow_paas_platform'),
    ], limit=1)
    return module.state == 'to remove'


class BillingIncident(models.Model):
    _name = 'woow.billing.incident'
    _inherit = ['mail.thread']
    _description = 'Woow Billing Incident'
    # Never let generic relational labels copy the raw provider reference into
    # mail.activity.res_name or another model's SQL parameters.
    _rec_name = 'kind'
    _order = 'last_seen_at desc, id desc'

    _KIND_SELECTION = (
        ('intent', 'Payment intent'),
        ('charged_not_credited', 'Charged but not credited'),
        ('callback_tx_missing', 'Callback transaction missing'),
        ('invoice_half_settled', 'Invoice half settled'),
        ('bind_failed', 'Card binding failed'),
        ('stuck_pending', 'Stuck pending payment'),
        ('manual_invoice_stale', 'Manual invoice stale'),
    )
    _STATE_SELECTION = (
        ('open', 'Open'),
        ('resolved', 'Resolved'),
        ('orphan', 'Orphan'),
        ('needs_human', 'Needs human review'),
    )
    _MANUAL_RESULT_SELECTION = (
        ('ecpay_found', 'Found in ECPay'),
        ('ecpay_not_found', 'Not found in ECPay'),
    )

    external_ref = fields.Char(
        required=True,
        index=True,
        help=(
            'Payment incidents store the raw ECPay MerchantTradeNo. '
            'Non-payment callers must use a namespaced key such as '
            '`invoice:<stable-reference>`. Access belongs to the Odoo role '
            '`account.group_account_manager` (not a Workspace role), which '
            'reads all incidents across all companies; review activities go '
            'to all current managers in that Odoo group.'
        ),
    )
    kind = fields.Selection(
        selection=list(_KIND_SELECTION),
        required=True,
        index=True,
    )
    partner_ref = fields.Integer(index=True)
    partner_email = fields.Char()
    payment_token_ref = fields.Integer(index=True)
    amount = fields.Float()
    credits = fields.Float()
    bonus = fields.Float()
    currency = fields.Char()
    trade_no = fields.Char(index=True)
    rtn_code = fields.Char()
    failure_code = fields.Char()
    state = fields.Selection(
        selection=list(_STATE_SELECTION),
        required=True,
        default='open',
        index=True,
    )
    skip_reason = fields.Char()
    last_skipped_at = fields.Datetime()
    skip_count = fields.Integer(required=True, default=0)
    attempts = fields.Integer(required=True, default=0)
    next_query_at = fields.Datetime(index=True)
    query_attempts = fields.Integer(required=True, default=0)
    first_seen_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    last_seen_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    occurrences = fields.Integer(required=True, default=1)
    assessment_reason = fields.Char()
    detail = fields.Text()
    source_model = fields.Char(
        help='Scalar source model name only; this is not an ORM relation.',
    )
    source_ref = fields.Integer(
        help='Scalar source row id which may legitimately dangle after rollback.',
    )
    manual_check_result = fields.Selection(
        selection=list(_MANUAL_RESULT_SELECTION),
    )
    resolution_note = fields.Text()
    resolved_at = fields.Datetime()
    resolved_by_ref = fields.Integer()

    @api.depends('kind')
    def _compute_display_name(self):
        kind_labels = dict(
            self._fields['kind']._description_selection(self.env))
        for incident in self:
            incident.display_name = _(
                'Billing Incident #%(incident_id)s — %(kind)s',
                incident_id=incident.id,
                kind=kind_labels.get(incident.kind, incident.kind or '-'),
            )

    _sql_constraints = [
        (
            'external_ref_uniq',
            'UNIQUE(external_ref)',
            'Billing incident external reference must be unique.',
        ),
        (
            'counters_nonnegative',
            'CHECK(skip_count >= 0 AND attempts >= 0 AND query_attempts >= 0)',
            'Billing incident counters must be non-negative.',
        ),
        (
            'occurrences_positive',
            'CHECK(occurrences >= 1)',
            'Billing incident occurrences must be at least one.',
        ),
        (
            'seen_at_order',
            'CHECK(first_seen_at <= last_seen_at)',
            'Billing incident first seen time cannot be after last seen time.',
        ),
        (
            'resolved_at_required',
            "CHECK(state != 'resolved' OR resolved_at IS NOT NULL)",
            'Resolved billing incidents must have a resolution timestamp.',
        ),
    ]

    # Safety budget, not a performance knob.  The helper may be called while an
    # outer transaction still holds a lock; waiting without a bound would
    # self-deadlock the request thread.
    _LOCK_TIMEOUT = '1s'

    _KINDS = frozenset(key for key, _label in _KIND_SELECTION)
    _STATES = frozenset(key for key, _label in _STATE_SELECTION)
    _LOG_STATES = _STATES - {'resolved'}
    _MANUAL_RESULTS = frozenset(
        key for key, _label in _MANUAL_RESULT_SELECTION)
    _MANAGER_WRITABLE_FIELDS = frozenset({
        'manual_check_result',
        'resolution_note',
    })
    _RESOLUTION_TRANSITION_FIELDS = frozenset({
        'state',
        'resolved_at',
        'resolved_by_ref',
        'manual_check_result',
        'resolution_note',
    })

    _LOG_CHAR_FIELDS = frozenset({
        'external_ref', 'kind', 'partner_email', 'currency', 'trade_no',
        'rtn_code', 'failure_code', 'state', 'skip_reason',
        'assessment_reason', 'detail', 'source_model',
    })
    _LOG_INTEGER_FIELDS = frozenset({
        'partner_ref', 'payment_token_ref', 'skip_count', 'attempts',
        'query_attempts', 'source_ref',
    })
    _LOG_FLOAT_FIELDS = frozenset({'amount', 'credits', 'bonus'})
    _LOG_DATETIME_FIELDS = frozenset({'last_skipped_at', 'next_query_at'})
    _LOG_FREE_TEXT_FIELDS = frozenset({'detail'})
    _LOG_PROVIDER_REFERENCE_FIELDS = frozenset({'external_ref', 'trade_no'})
    _LOG_EMAIL_FIELDS = frozenset({'partner_email'})
    _LOG_SELECTION_FIELDS = frozenset({'kind', 'state'})
    _LOG_CODE_FIELDS = frozenset({
        'currency', 'rtn_code', 'failure_code', 'skip_reason',
        'assessment_reason', 'source_model',
    })
    _LOG_FIELDS = (
        _LOG_CHAR_FIELDS | _LOG_INTEGER_FIELDS | _LOG_FLOAT_FIELDS |
        _LOG_DATETIME_FIELDS
    )
    _NONNEGATIVE_INPUT_COUNTERS = frozenset({
        'skip_count', 'attempts', 'query_attempts',
    })
    _SENSITIVE_MARKER_RE = re.compile(
        r'(?<![A-Za-z0-9])(?:'
        r'result[\W_]*data|'
        r'card[\W_]*[46]?[\W_]*no|'
        r'card[\W_]*number|'
        r'c[\W_]*v[\W_]*[vc](?:[\W_]*2)?|'
        r'c[\W_]*i[\W_]*d|c[\W_]*s[\W_]*c|'
        r'(?:card[\W_]*)?security[\W_]*(?:code|number|no)|'
        r's[\W_]*a[\W_]*d|'
        r'sensitive[\W_]*authentication[\W_]*data|'
        r'p[\W_]*i[\W_]*n(?:[\W_]*block)?|'
        r'track[\W_]*[12](?:[\W_]*data)?|'
        r'magnetic[\W_]*stripe(?:[\W_]*data)?|'
        r'p[\W_]*a[\W_]*n'
        r')(?![A-Za-z0-9])',
        re.IGNORECASE,
    )
    # CJK text is normally written without word separators, so English-style
    # lookarounds would let phrases such as「看到卡片安全碼」bypass the gate.
    _SENSITIVE_CJK_MARKER_RE = re.compile(
        r'(?:信用卡|卡片|卡)?[\W_]*安全[\W_]*(?:碼|码)|'
        r'(?:安全[\W_]*)?(?:驗[\W_]*證|验[\W_]*证)'
        r'[\W_]*(?:碼|码|值)|'
        r'信用卡[\W_]*背[\W_]*面[\W_]*末[\W_]*(?:三|3)'
        r'[\W_]*(?:碼|码)|'
        r'(?:個[\W_]*人[\W_]*識[\W_]*別|个[\W_]*人[\W_]*识[\W_]*别)'
        r'[\W_]*(?:碼|码)|'
        r'p[\W_]*i[\W_]*n[\W_]*(?:碼|码|區[\W_]*塊|区[\W_]*块)|'
        r'磁[\W_]*(?:條|条|軌|轨)(?:[\W_]*(?:資料|数据))?|'
        r'第[\W_]*(?:一|二|1|2)[\W_]*(?:軌|轨)'
        r'(?:[\W_]*(?:資料|数据))?|'
        r'(?:軌|轨)[\W_]*道[\W_]*(?:資料|数据)|'
        r'卡[\W_]*背[\W_]*(?:三|3)[\W_]*(?:碼|码)|'
        r'授[\W_]*權[\W_]*(?:碼|码)',
        re.IGNORECASE,
    )
    _PAN_CANDIDATE_RE = re.compile(
        r'(?<![^\W_])(?:\d[.]*){13,19}(?![^\W_])')
    _RAW_MTN_RE = re.compile(r'[A-Za-z0-9_-]{1,20}\Z')
    _NAMESPACED_EXTERNAL_REF_RE = re.compile(
        r'[A-Za-z][A-Za-z0-9_-]{0,31}:'
        r'[A-Za-z0-9][A-Za-z0-9._/-]{0,94}\Z')
    _TRADE_NO_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}\Z')
    _SNAPSHOT_CODE_RE = re.compile(
        r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z')
    _SOURCE_MODEL_RE = re.compile(
        r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z')
    _EMAIL_MAX_LENGTH = 254
    _CONTROL_CHARACTER_RE = re.compile(r'[\x00-\x1f\x7f]')
    _HTML_ENTITY_RE = re.compile(
        r'&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]+);', re.IGNORECASE)
    _HTML_TAG_OR_COMMENT_RE = re.compile(
        r'<!--|-->|<![A-Za-z]|<\s*/?\s*[A-Za-z][^>]*>', re.IGNORECASE)
    _HTML_ATTRIBUTE_RE = re.compile(
        r'(?<![\w:-])[A-Za-z_:][\w:.-]*\s*=\s*(?:"[^"]*"|\'[^\']*\')')

    @api.model_create_multi
    def create(self, values_list):
        """Reject every generic ORM insert, including ``sudo().create()``."""
        if not _is_billing_incident_create(self.env):
            raise AccessError(_(
                'Billing incidents may be created only by the internal '
                'durable ledger writer.'
            ))
        guarded = _incident_chatter(self)
        created = super(BillingIncident, guarded).create(values_list)
        # Never return the chatter capability on a recordset the caller keeps.
        return self.browse(created.ids)

    @api.model
    def _create_test_fixture(self, values):
        """Private in-process fixture seam; unavailable outside test mode.

        Production ``_log`` deliberately remains a raw atomic upsert.  Tests
        that exercise ORM lifecycle or SQL constraints use this method rather
        than teaching callers that ``sudo().create()`` is a ledger authority.
        The sentinel itself is module-private and this underscore method is not
        callable through Odoo RPC.
        """
        if not config['test_enable']:
            raise AccessError(_(
                'Billing incident fixture creation is available only in tests.'
            ))
        return _with_billing_incident_capability(
            self.sudo(),
            _BILLING_INCIDENT_CREATE_CTX_KEY,
            _BILLING_INCIDENT_CREATE,
        ).create(values)

    def _lock_and_refresh_states(self, extra_fields=()):
        """Lock every row and reread lifecycle state from this cursor."""
        record_ids = sorted(set(self.ids))
        if not record_ids:
            return {}
        self.env.cr.execute(
            sql.SQL(
                'SELECT id FROM {table} WHERE id = ANY(%s) '
                'ORDER BY id FOR UPDATE'
            ).format(table=sql.Identifier(self._table)),
            (record_ids,),
        )
        locked_ids = [row[0] for row in self.env.cr.fetchall()]
        if locked_ids != record_ids:
            raise UserError(_('This billing incident no longer exists.'))
        field_names = ['state', *extra_fields]
        self.invalidate_recordset(field_names)
        return {
            row['id']: row['state']
            for row in self.read(['state', *extra_fields])
        }

    def write(self, values):
        """Pre-authorize generic writes before taking any incident row lock."""
        if _is_resolution_transition(self.env):
            if (set(values) != self._RESOLUTION_TRANSITION_FIELDS
                    or values.get('state') != 'resolved'
                    or not values.get('resolved_at')):
                raise AccessError(_(
                    'Billing incident resolution may update only the exact '
                    'first-resolution audit fields.'
                ))
            resolution_note = values.get('resolution_note')
            if (resolution_note not in (None, False)
                    and self._free_text_ingress_category(
                        'resolution_note', resolution_note)):
                raise UserError(_(
                    'Resolution notes must contain only safe, '
                    'non-sensitive summaries.'
                ))
            # The unforgeable transition capability still uses the same
            # controlled lock/re-read that makes first resolution immutable.
            states = self._lock_and_refresh_states()
            if any(state == 'resolved' for state in states.values()):
                raise AccessError(_(
                    'Resolved billing incidents are immutable.'
                ))
            return super().write(values)

        # Do not browse, lock, or otherwise distinguish caller-supplied ids
        # until the role and model ACL both pass.  ``sudo()`` is an ACL tool,
        # not a lifecycle capability.
        if (self.env.su or not self.env.user.has_group(
                'account.group_account_manager')):
            raise AccessError(_(
                'Only account managers may update billing incidents.'
            ))
        self.check_access('write')
        forbidden = set(values) - self._MANAGER_WRITABLE_FIELDS
        if forbidden:
            raise AccessError(_(
                'Account managers may update only the manual verification '
                'result and resolution note.'
            ))
        resolution_note = values.get('resolution_note')
        if (resolution_note not in (None, False)
                and self._free_text_ingress_category(
                    'resolution_note', resolution_note)):
            raise UserError(_(
                'Resolution notes must contain only safe, '
                'non-sensitive summaries.'
            ))

        states = self._lock_and_refresh_states()
        if any(state == 'resolved' for state in states.values()):
            raise AccessError(_(
                'Resolved billing incidents are immutable.'
            ))
        return super().write(values)

    def unlink(self):
        """Ledger rows are permanent; only core's real module uninstall may drop."""
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_(
            'Billing incidents are immutable ledger entries and cannot be '
            'deleted.'
        ))

    def message_subscribe(self, partner_ids=None, subtype_ids=None):
        """Reserve incident follower changes for roster-locked scheduling."""
        if not self or not partner_ids:
            return True
        if not _is_activity_scheduler(self.env):
            raise AccessError(_(
                'Only the internal billing scheduler may follow incidents.'
            ))
        roster_partner_ids = _active_account_manager_partner_ids(self.env)
        if not set(partner_ids).issubset(roster_partner_ids):
            raise AccessError(_(
                'Only current account managers may follow billing incidents.'
            ))
        return super().message_subscribe(
            partner_ids=partner_ids,
            subtype_ids=subtype_ids,
        )

    def message_post(self, **kwargs):
        """Disallow free chatter; only validated core activity-done may post."""
        if not _is_incident_chatter(self.env):
            raise AccessError(_(
                'Free-form chatter is disabled for billing incidents.'
            ))
        # Core renders controlled HTML only after _action_done feedback passed
        # the plain-text validator.  Do not reject that generated template as
        # user markup, and never expose this object capability to RPC.
        return super().message_post(**kwargs)

    def message_post_with_source(
            self, source_ref, render_values=None, **kwargs):
        """Permit only core's object-backed activity-done rendering path."""
        activity = (
            render_values.get('activity')
            if isinstance(render_values, dict)
            else None
        )
        is_core_activity_done = (
            source_ref == 'mail.message_activity_done'
            and isinstance(activity, models.BaseModel)
            and activity._name == 'mail.activity'
            and len(activity) == 1
            and len(self) == 1
            and activity.res_model == self._name
            and activity.res_id == self.id
            and kwargs.get('mail_activity_type_id')
            == activity.activity_type_id.id
            and kwargs.get('subtype_xmlid') == 'mail.mt_activities'
        )
        if not is_core_activity_done:
            raise AccessError(_(
                'Only the standard activity completion message is allowed '
                'for billing incidents.'
            ))
        has_activity_attachments = bool(
            activity.attachment_ids
            or self.env['ir.attachment'].sudo().search_count([
                ('res_model', '=', 'mail.activity'),
                ('res_id', '=', activity.id),
            ], limit=1)
        )
        if (kwargs.get('attachment_ids') or kwargs.get('attachments')
                or has_activity_attachments):
            raise UserError(_(
                'Attachments are not allowed on billing incident activities.'
            ))
        feedback = render_values.get('feedback')
        if (feedback not in (None, False)
                and self._free_text_ingress_category(
                    'activity_feedback', feedback)):
            raise UserError(_(
                'Billing incident activity feedback must contain only safe, '
                'non-sensitive summaries.'
            ))
        # The template/activity metadata is module/core controlled.  Only the
        # caller-owned feedback above is plain-validated; then an object-only
        # capability lets core render and persist its expected HTML.
        guarded = _incident_chatter(self)
        return super(BillingIncident, guarded).message_post_with_source(
            source_ref,
            render_values=render_values,
            **kwargs
        )

    @api.model
    def _canonicalize_pci_text(self, value):
        """Expose HTML-obscured and Unicode-obscured PCI candidates."""
        plaintext = html2plaintext(html.unescape(value))
        normalized = unicodedata.normalize('NFKC', plaintext)
        # Unicode letters/digits are the only boundaries that retain identity.
        # Every other category becomes one uniform separator, including Mc,
        # So/emoji, Sk, Cf, Mn, and Me.  A PAN split by any such character then
        # reaches the same 13–19 digit Luhn candidate scan.
        return ''.join(
            character
            if unicodedata.category(character)[0] in ('L', 'N')
            else '.'
            for character in normalized
        )

    @api.model
    def _raw_markup_category(self, value):
        """Fail closed on raw, entity-decoded, or visible markup syntax."""
        if self._HTML_ENTITY_RE.search(value):
            return 'html_markup'
        decoded = html.unescape(value)
        plaintext = html2plaintext(decoded)
        for candidate in (value, decoded, plaintext):
            if (self._HTML_TAG_OR_COMMENT_RE.search(candidate)
                    or self._HTML_ATTRIBUTE_RE.search(candidate)):
                return 'html_markup'
        return False

    @api.model
    def _sensitive_marker_category(self, value):
        canonical = self._canonicalize_pci_text(value)
        if (self._SENSITIVE_MARKER_RE.search(canonical)
                or self._SENSITIVE_CJK_MARKER_RE.search(canonical)):
            return 'sensitive_marker'
        return False

    @api.model
    def _sensitive_content_category(self, field_name, value):
        canonical = self._canonicalize_pci_text(value)
        marker_category = self._sensitive_marker_category(value)
        if marker_category:
            return marker_category
        for match in self._PAN_CANDIDATE_RE.finditer(canonical):
            digits = ''.join(character for character in match.group()
                             if character.isdigit())
            if not 13 <= len(digits) <= 19:
                continue
            checksum = 0
            for index, character in enumerate(reversed(digits)):
                number = int(character)
                if index % 2:
                    number *= 2
                    if number > 9:
                        number -= 9
                checksum += number
            if checksum % 10 == 0:
                return 'luhn_pan'
        return False

    @api.model
    def _free_text_ingress_category(self, field_name, value):
        """Require plain text, then canonicalize before marker/PAN checks."""
        if not isinstance(value, str):
            return 'invalid_type'
        markup_category = self._raw_markup_category(value)
        if markup_category:
            return markup_category
        return self._sensitive_content_category(field_name, value)

    @api.model
    def _provider_reference_category(self, field_name, value):
        """Allow numeric coincidences, but never marker-labelled secrets."""
        if not isinstance(value, str):
            return 'invalid_type'
        if self._CONTROL_CHARACTER_RE.search(value):
            return 'control_character'
        marker_category = self._sensitive_marker_category(value)
        if marker_category:
            return marker_category
        if field_name == 'external_ref':
            if (self._RAW_MTN_RE.fullmatch(value)
                    or self._NAMESPACED_EXTERNAL_REF_RE.fullmatch(value)):
                return False
            return 'invalid_provider_reference_shape'
        if field_name == 'trade_no':
            if self._TRADE_NO_RE.fullmatch(value):
                return False
            return 'invalid_provider_reference_shape'
        return 'unsupported_provider_reference_field'

    @api.model
    def _structured_snapshot_category(self, field_name, value):
        """Validate snapshot metadata with its own non-free-text shape."""
        if not isinstance(value, str):
            return 'invalid_type'
        if self._CONTROL_CHARACTER_RE.search(value):
            return 'control_character'
        if field_name in self._LOG_EMAIL_FIELDS:
            if len(value) > self._EMAIL_MAX_LENGTH:
                return 'invalid_email_length'
            if not single_email_re.fullmatch(value):
                return 'invalid_email_shape'
            return False
        if field_name == 'source_model':
            return (False if self._SOURCE_MODEL_RE.fullmatch(value)
                    else 'invalid_model_shape')
        if field_name in self._LOG_CODE_FIELDS:
            return (False if self._SNAPSHOT_CODE_RE.fullmatch(value)
                    else 'invalid_code_shape')
        return 'unsupported_structured_field'

    @api.model
    def _validate_log_snapshot(self, snapshot):
        """Return a SQL-ready scalar snapshot, or ``False`` fail-soft.

        Validation happens before the independent cursor.  Each structured
        scalar uses its own email/provider/code/enum shape.  Only actual
        free-text evidence is scanned for sensitive markers and Luhn-valid
        PAN-like content as defense in depth.
        """
        if not isinstance(snapshot, dict):
            _logger.error(
                '[BILLING_INCIDENT] _log rejected non-dict snapshot type=%s',
                type(snapshot).__name__,
            )
            return False
        if any(not isinstance(key, str) for key in snapshot):
            _logger.error(
                '[BILLING_INCIDENT] _log rejected snapshot with non-string key')
            return False
        unknown = set(snapshot) - self._LOG_FIELDS
        if unknown:
            _logger.error(
                '[BILLING_INCIDENT] _log rejected snapshot '
                'category=unknown_fields count=%s',
                len(unknown),
            )
            return False
        external_ref = snapshot.get('external_ref')
        if not isinstance(external_ref, str) or not external_ref.strip():
            _logger.error(
                '[BILLING_INCIDENT] _log rejected field=external_ref '
                'category=required type=%s',
                type(external_ref).__name__,
            )
            return False

        normalized = {}
        for name, value in snapshot.items():
            if name in self._LOG_CHAR_FIELDS:
                if value is None or value is False:
                    if name in ('external_ref', 'kind'):
                        _logger.error(
                            '[BILLING_INCIDENT] _log rejected field=%s '
                            'category=required type=%s',
                            name, type(value).__name__,
                        )
                        return False
                    normalized[name] = None
                elif isinstance(value, str):
                    normalized[name] = value
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected field=%s '
                        'category=invalid_type type=%s',
                        name, type(value).__name__,
                    )
                    return False
            elif name in self._LOG_INTEGER_FIELDS:
                if value is None or value is False:
                    normalized[name] = None
                elif isinstance(value, int) and not isinstance(value, bool):
                    normalized[name] = value
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected field=%s '
                        'category=invalid_type type=%s',
                        name, type(value).__name__,
                    )
                    return False
            elif name in self._LOG_FLOAT_FIELDS:
                if value is None or value is False:
                    normalized[name] = None
                elif (isinstance(value, (int, float))
                      and not isinstance(value, bool)
                      and math.isfinite(value)):
                    normalized[name] = float(value)
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected field=%s '
                        'category=invalid_number type=%s',
                        name, type(value).__name__,
                    )
                    return False
            elif name in self._LOG_DATETIME_FIELDS:
                if value is None or value is False:
                    normalized[name] = None
                elif isinstance(value, (str, date, datetime)):
                    try:
                        normalized[name] = fields.Datetime.to_datetime(value)
                    except (TypeError, ValueError):
                        _logger.error(
                            '[BILLING_INCIDENT] _log rejected field=%s '
                            'category=invalid_datetime type=%s',
                            name, type(value).__name__,
                        )
                        return False
                else:
                    _logger.error(
                        '[BILLING_INCIDENT] _log rejected field=%s '
                        'category=invalid_type type=%s',
                        name, type(value).__name__,
                    )
                    return False
        for name, value in normalized.items():
            if not isinstance(value, str):
                continue
            if name in self._LOG_PROVIDER_REFERENCE_FIELDS:
                category = self._provider_reference_category(name, value)
            elif name in self._LOG_FREE_TEXT_FIELDS:
                category = self._free_text_ingress_category(name, value)
            elif name in self._LOG_SELECTION_FIELDS:
                # Selection membership below is the complete shape.  PIN/PAN
                # substrings are irrelevant for enum codes.
                continue
            else:
                category = self._structured_snapshot_category(name, value)
            if category:
                _logger.error(
                    '[BILLING_INCIDENT] _log rejected field=%s '
                    'category=%s type=%s',
                    name, category, type(value).__name__,
                )
                return False
        kind = normalized.get('kind')
        if kind not in self._KINDS:
            _logger.error(
                '[BILLING_INCIDENT] _log rejected field=kind '
                'category=unsupported_selection type=%s',
                type(kind).__name__,
            )
            return False
        if ('state' in normalized
                and normalized['state'] not in self._LOG_STATES):
            _logger.error(
                '[BILLING_INCIDENT] _log rejected field=state '
                'category=unsupported_selection type=%s',
                type(normalized['state']).__name__,
            )
            return False
        for name in self._NONNEGATIVE_INPUT_COUNTERS:
            if name in normalized and (
                    normalized[name] is None or normalized[name] < 0):
                _logger.error(
                    '[BILLING_INCIDENT] _log rejected field=%s '
                    'category=negative_or_null type=%s',
                    name, type(normalized[name]).__name__,
                )
                return False
        return normalized

    @api.model
    def _log(self, snapshot):
        """Atomically insert/update one durable incident in a new transaction.

        Callers must freeze every needed ORM value into a flat scalar ``dict``
        *before* entering a payment/external-call failure region.  This method
        never browses or updates the outer topup, invoice, partner, or token.
        It returns only the integer incident id (or ``False``) so no recordset
        backed by a closed cursor can escape.
        """
        try:
            values = self._validate_log_snapshot(snapshot)
            if values is False:
                return False

            now = fields.Datetime.now()
            # Duplicate observations may refresh evidence, never lifecycle.
            # State changes belong to explicit transition methods.
            explicit_evidence = [
                name for name in values
                if name not in ('external_ref', 'kind', 'state')
            ]
            insert_values = dict(values)
            insert_values.setdefault('state', 'open')
            insert_values.setdefault('skip_count', 0)
            insert_values.setdefault('attempts', 0)
            insert_values.setdefault('query_attempts', 0)
            insert_values.update({
                'first_seen_at': now,
                'last_seen_at': now,
                'occurrences': 1,
                'create_uid': SUPERUSER_ID,
                'write_uid': SUPERUSER_ID,
                'create_date': now,
                'write_date': now,
            })
            columns = list(insert_values)
            updates = [
                sql.SQL('{field} = EXCLUDED.{field}').format(
                    field=sql.Identifier(name))
                for name in explicit_evidence
            ]
            updates.extend([
                sql.SQL('last_seen_at = EXCLUDED.last_seen_at'),
                sql.SQL('occurrences = {table}.occurrences + 1').format(
                    table=sql.Identifier(self._table)),
                sql.SQL('write_uid = EXCLUDED.write_uid'),
                sql.SQL('write_date = EXCLUDED.write_date'),
            ])
            query = sql.SQL(
                'INSERT INTO {table} ({columns}) VALUES ({placeholders}) '
                'ON CONFLICT (external_ref) DO UPDATE SET {updates} '
                'WHERE {table}.kind = EXCLUDED.kind '
                "AND {table}.state <> 'resolved' "
                'RETURNING id, state'
            ).format(
                table=sql.Identifier(self._table),
                columns=sql.SQL(', ').join(map(sql.Identifier, columns)),
                placeholders=sql.SQL(', ').join(
                    sql.Placeholder() for _name in columns),
                updates=sql.SQL(', ').join(updates),
            )

            with self.env.registry.cursor() as cr:
                # Must be the first statement: the independent connection can
                # otherwise wait on the caller's own outer transaction forever.
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._LOCK_TIMEOUT,))
                api.Environment(cr, SUPERUSER_ID, {})
                # Odoo's default SQL exception logger renders query params;
                # provider references must never be copied into an error log.
                cr.execute(
                    query,
                    [insert_values[name] for name in columns],
                    log_exceptions=False,
                )
                row = cr.fetchone()
                if not row:
                    cr.execute(
                        sql.SQL(
                            'SELECT id, kind, state FROM {table} '
                            'WHERE external_ref = %s'
                        ).format(table=sql.Identifier(self._table)),
                        (values['external_ref'],),
                        log_exceptions=False,
                    )
                    existing = cr.fetchone()
                    if not existing:
                        _logger.critical(
                            '[BILLING_INCIDENT] field=external_ref '
                            'category=upsert_missing_row'
                        )
                        cr.commit()
                        return False
                    incident_id, existing_kind, existing_state = existing
                    if existing_kind != values['kind']:
                        _logger.critical(
                            '[BILLING_INCIDENT] incident id=%s field=kind '
                            'category=collision; refusing update',
                            incident_id,
                        )
                        cr.commit()
                        return False
                    # A late duplicate is evidence, not permission to reopen a
                    # resolved incident.  Return its stable scalar id unchanged.
                    if existing_state == 'resolved':
                        cr.commit()
                        return incident_id
                    _logger.critical(
                        '[BILLING_INCIDENT] incident id=%s field=state '
                        'category=unresolved_upsert_missing_row',
                        incident_id,
                    )
                    cr.commit()
                    return False
                incident_id, incident_state = row
                cr.commit()
            if incident_state in ('orphan', 'needs_human'):
                try:
                    self._schedule_review_activities(incident_id)
                except Exception:
                    # The incident transaction is already committed.  Activity
                    # is a second best-effort transaction and must never change
                    # the caller-visible result or recurse through `_log`.
                    _logger.error(
                        '[BILLING_INCIDENT] incident id=%s '
                        'category=activity_schedule_failed; incident remains '
                        'durable',
                        incident_id,
                    )
            return incident_id
        except Exception:
            _logger.critical(
                '[BILLING_INCIDENT] field=external_ref '
                'category=persistence_failed; caller flow continues unchanged'
            )
            return False

    @api.model
    def _account_manager_users(self, env):
        """Return active finance-role members without active_test/cache drift."""
        # Reuse the SQL roster snapshot used by res.users/res.groups hooks.
        # Reading group.users then filtered('active') can retain an inactive
        # field value across a deactivate→reactivate sequence in one cursor.
        user_ids = sorted(_active_account_manager_roster_ids(env))
        return env['res.users'].with_context(active_test=False).browse(user_ids)

    @api.model
    def _acquire_review_activity_lock(self, cr, incident_id):
        cr.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
            ('woow.billing.incident.activity:%s' % incident_id,),
        )

    @api.model
    def _ensure_review_activities_current_cursor(self, incident_id):
        """Converge one active dedicated alert per manager on this cursor."""
        # Global roster lock must precede incident/activity locks everywhere.
        # Mutations hold it from before core membership writes through commit.
        _acquire_billing_roster_lock(self.env.cr)
        self._acquire_review_activity_lock(self.env.cr, incident_id)
        self.env.cr.execute(
            sql.SQL(
                'SELECT kind, state FROM {table} WHERE id = %s'
            ).format(table=sql.Identifier(self._table)),
            (incident_id,),
            log_exceptions=False,
        )
        incident = self.env.cr.fetchone()
        if not incident or incident[1] not in ('orphan', 'needs_human'):
            return False

        review_type = self.env.ref(
            'woow_paas_platform.'
            'mail_activity_type_billing_incident_review',
            raise_if_not_found=False,
        )
        model = self.env['ir.model']._get(self._name)
        if not (review_type and model):
            _logger.error(
                '[BILLING_INCIDENT] incident id=%s '
                'category=missing_activity_dependency', incident_id)
            return False

        users = self._account_manager_users(self.env)
        current_user_ids = set(users.ids)
        current_partner_ids = set(users.partner_id.ids)
        follower_domain = [
            ('res_model', '=', self._name),
            ('res_id', '=', incident_id),
        ]
        stale_follower_domain = list(follower_domain)
        if current_partner_ids:
            stale_follower_domain.append(
                ('partner_id', 'not in', sorted(current_partner_ids)))
        stale_followers = self.env['mail.followers'].sudo().search(
            stale_follower_domain)
        if stale_followers:
            stale_followers.unlink()
        Activity = _activity_scheduler(self.env['mail.activity'].sudo())
        activity_domain = [
            ('res_model', '=', self._name),
            ('res_id', '=', incident_id),
            ('activity_type_id', '=', review_type.id),
            ('active', '=', True),
        ]
        existing = Activity.search(activity_domain, order='id')
        kept_user_ids = set()
        obsolete = Activity
        for activity in existing:
            if (activity.user_id.id not in current_user_ids
                    or activity.user_id.id in kept_user_ids):
                obsolete |= activity
            else:
                kept_user_ids.add(activity.user_id.id)
        if obsolete:
            # Scheduler capability makes this a convergence cleanup, not a
            # user terminal action; MailActivity.unlink must not re-arm it.
            obsolete.unlink()
        if not users:
            _logger.error(
                '[BILLING_INCIDENT] incident id=%s '
                'category=no_review_assignee', incident_id)
            return False
        existing_user_ids = kept_user_ids
        kind, _state = incident
        activity_values = []
        for user in users:
            if user.id in existing_user_ids:
                continue
            user_env = self.env(context={
                **self.env.context,
                'lang': user.lang or 'en_US',
            })
            kind_labels = dict(
                user_env[self._name]._fields[
                    'kind']._description_selection(user_env))
            summary_template = get_translation(
                'woow_paas_platform',
                user_env.lang or 'en_US',
                'Billing Incident #%(incident_id)s: %(kind)s requires '
                'manual review',
                (),
            )
            summary = summary_template % {
                'incident_id': incident_id,
                'kind': kind_labels[kind],
            }
            note_template = Markup.escape(get_translation(
                'woow_paas_platform',
                user_env.lang or 'en_US',
                'Billing Incident #%(incident_id)s (%(kind)s) requires an '
                'ECPay verification and an explicit resolution note.',
                (),
            ))
            note = note_template % {
                'incident_id': incident_id,
                'kind': escape(kind_labels[kind]),
            }
            activity_values.append({
                # Explicitly own every security-sensitive stored input.  No
                # caller default_* value may alter target, assignee, lifecycle,
                # provenance, content, or attachments.
                'res_model_id': model.id,
                'res_id': incident_id,
                'activity_type_id': review_type.id,
                'user_id': user.id,
                'summary': summary,
                'note': str(note),
                'date_deadline': fields.Date.today(),
                'active': True,
                'attachment_ids': [(5, 0, 0)],
                'automated': False,
                'request_partner_id': False,
                'recommended_activity_type_id': False,
                'previous_activity_type_id': False,
            })
        if activity_values:
            Activity.create(activity_values)
        # Repair a missing follower even when its dedicated activity already
        # existed.  The object capability is required because this sudo caller
        # is infrastructure, not itself a finance-role member.
        _activity_scheduler(self.sudo().browse(incident_id)).message_subscribe(
            partner_ids=sorted(current_partner_ids),
        )
        return True

    @api.model
    def _converge_unresolved_review_activities_current_cursor(self):
        """Converge live followers and alerts after any roster mutation."""
        _acquire_billing_roster_lock(self.env.cr)
        current_partner_ids = _active_account_manager_partner_ids(self.env)
        stale_follower_domain = [('res_model', '=', self._name)]
        if current_partner_ids:
            stale_follower_domain.append(
                ('partner_id', 'not in', sorted(current_partner_ids)))
        stale_followers = self.env['mail.followers'].sudo().search(
            stale_follower_domain)
        if stale_followers:
            stale_followers.unlink()
        self.env.cr.execute(
            sql.SQL(
                "SELECT id FROM {table} WHERE state <> 'resolved' ORDER BY id"
            ).format(table=sql.Identifier(self._table)),
        )
        for incident_id, in self.env.cr.fetchall():
            self._ensure_review_activities_current_cursor(incident_id)
        return True

    @api.model
    def _schedule_review_activities(self, incident_id):
        """Best-effort transaction around current-cursor alert convergence."""
        # Real scheduler transactions bypass Registry.cursor's signaling
        # query so READ COMMITTED can be the first SQL statement.  Odoo's
        # TransactionCase/HttpCase cursor shim must remain on its shared test
        # transaction; the true raw-cursor contract is covered by integration.
        cursor_factory = (
            self.env.registry.cursor
            if config['test_enable']
            else self.env.registry._db.cursor
        )
        with cursor_factory() as cr:
            if not config['test_enable']:
                cr.execute(
                    'SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            cr.execute('SET LOCAL lock_timeout = %s',
                       (self._LOCK_TIMEOUT,))
            env = api.Environment(cr, SUPERUSER_ID, {})
            result = env[self._name] \
                ._ensure_review_activities_current_cursor(incident_id)
            cr.commit()
            return result

    @api.model
    def _clear_review_activities(self, incident_id):
        """Best-effort second transaction; delete only our review rows."""
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s',
                       (self._LOCK_TIMEOUT,))
            env = api.Environment(cr, SUPERUSER_ID, {})
            self._acquire_review_activity_lock(cr, incident_id)
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review',
                raise_if_not_found=False,
            )
            if review_type:
                env['mail.activity'].sudo().with_context(
                    active_test=False).search([
                        ('res_model', '=', self._name),
                        ('res_id', '=', incident_id),
                        ('activity_type_id', '=', review_type.id),
                    ]).unlink()
            cr.commit()
            return True

    def _transition_resolved_current_cursor(
            self, manual_check_result, resolution_note, resolved_by_ref):
        """Private UI transition; never open the independent `_resolve` cursor."""
        self.ensure_one()
        self._acquire_review_activity_lock(self.env.cr, self.id)
        states = self._lock_and_refresh_states()
        if states[self.id] == 'resolved':
            raise UserError(_('This billing incident is already resolved.'))
        _resolution_transition(self.sudo()).write({
            'state': 'resolved',
            'resolved_at': fields.Datetime.now(),
            'resolved_by_ref': resolved_by_ref,
            'manual_check_result': manual_check_result,
            'resolution_note': resolution_note,
        })
        # The UI transaction may already hold this incident row lock.  Delete
        # only this mechanism's review row on this cursor to avoid self-lock.
        review_type = self.env.ref(
            'woow_paas_platform.'
            'mail_activity_type_billing_incident_review',
            raise_if_not_found=False,
        )
        if review_type:
            self.env['mail.activity'].sudo().with_context(
                active_test=False).search([
                    ('res_model', '=', self._name),
                    ('res_id', '=', self.id),
                    ('activity_type_id', '=', review_type.id),
                ]).unlink()
        return True

    def action_mark_manually_resolved(self):
        """Record an ECPay verification and close; never credit/refund money."""
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_(
                'Only account managers may resolve billing incidents.'
            ))
        self.check_access('write')
        self._acquire_review_activity_lock(self.env.cr, self.id)
        states = self._lock_and_refresh_states(
            ('manual_check_result', 'resolution_note'))
        if states[self.id] == 'resolved':
            raise UserError(_('This billing incident is already resolved.'))
        manual_check_result = self.manual_check_result
        resolution_note = (self.resolution_note or '').strip()
        resolved_by_ref = self.env.user.id
        if manual_check_result not in self._MANUAL_RESULTS:
            raise UserError(_(
                'Choose whether the MerchantTradeNo was found in ECPay '
                'before resolving this incident.'
            ))
        if not resolution_note:
            raise UserError(_(
                'A resolution note describing the ECPay verification is '
                'required.'
            ))
        if self._free_text_ingress_category(
                'resolution_note', resolution_note):
            raise UserError(_(
                'Resolution notes must contain only safe, non-sensitive '
                'summaries.'
            ))
        return self.sudo()._transition_resolved_current_cursor(
            manual_check_result,
            resolution_note,
            resolved_by_ref,
        )

    @api.model
    def _resolve(self, external_ref, resolution_note=None,
                 resolved_by_ref=None, manual_check_result=None):
        """Resolve by external key in an independent, fail-soft transaction.

        Missing keys are not created.  Repeated calls preserve the first audit
        values, retry dedicated activity cleanup, and return the same integer
        id.
        """
        try:
            ref_category = self._provider_reference_category(
                'external_ref', external_ref)
            if ref_category:
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected field=external_ref '
                    'category=%s type=%s',
                    ref_category, type(external_ref).__name__,
                )
                return False
            if resolution_note is not None:
                category = self._free_text_ingress_category(
                    'resolution_note', resolution_note)
                if category:
                    _logger.error(
                        '[BILLING_INCIDENT] _resolve rejected '
                        'field=resolution_note category=%s type=%s',
                        category, type(resolution_note).__name__,
                    )
                    return False
            if (resolved_by_ref is not None
                    and (not isinstance(resolved_by_ref, int)
                         or isinstance(resolved_by_ref, bool))):
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected '
                    'field=resolved_by_ref category=invalid_type type=%s',
                    type(resolved_by_ref).__name__,
                )
                return False
            if (manual_check_result is not None
                    and manual_check_result not in self._MANUAL_RESULTS):
                _logger.error(
                    '[BILLING_INCIDENT] _resolve rejected '
                    'field=manual_check_result '
                    'category=unsupported_selection type=%s',
                    type(manual_check_result).__name__,
                )
                return False

            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._LOCK_TIMEOUT,))
                env = api.Environment(cr, SUPERUSER_ID, {})
                try:
                    cr.execute(
                        sql.SQL(
                            'SELECT id FROM {table} '
                            'WHERE external_ref = %s LIMIT 1'
                        ).format(table=sql.Identifier(self._table)),
                        (external_ref,),
                        log_exceptions=False,
                    )
                except Exception:
                    _logger.critical(
                        '[BILLING_INCIDENT] field=external_ref '
                        'category=lookup_failed'
                    )
                    return False
                row = cr.fetchone()
                if not row:
                    cr.commit()
                    return False
                incident_id = row[0]
                incident = env[self._name].browse(incident_id)
                self._acquire_review_activity_lock(cr, incident_id)
                states = incident._lock_and_refresh_states((
                    'manual_check_result',
                    'resolution_note',
                    'resolved_by_ref',
                ))
                if states[incident_id] != 'resolved':
                    _resolution_transition(incident.sudo()).write({
                        'state': 'resolved',
                        'resolved_at': fields.Datetime.now(),
                        'resolved_by_ref': (
                            resolved_by_ref
                            if resolved_by_ref is not None
                            else incident.resolved_by_ref
                        ),
                        'manual_check_result': (
                            manual_check_result
                            if manual_check_result is not None
                            else incident.manual_check_result
                        ),
                        'resolution_note': (
                            resolution_note
                            if resolution_note is not None
                            else incident.resolution_note
                        ),
                    })
                cr.commit()
            try:
                self._clear_review_activities(incident_id)
            except Exception:
                _logger.error(
                    '[BILLING_INCIDENT] incident id=%s '
                    'category=activity_cleanup_failed; resolution remains '
                    'committed',
                    incident_id,
                )
            return incident_id
        except Exception:
            _logger.critical(
                '[BILLING_INCIDENT] field=external_ref '
                'category=resolve_bookkeeping_failed; caller flow continues '
                'unchanged'
            )
            return False


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.model
    def _may_change_billing_manager_roster(self, values):
        """Cover raw API fields and the reified fields used by the user form."""
        return bool(
            {'groups_id', 'active'} & set(values)
            or any(
                name.startswith(('in_group_', 'sel_groups_'))
                for name in values
            )
        )

    @api.model_create_multi
    def create(self, values_list):
        """Converge after create's final implied-group and active state."""
        if _is_roster_mutation(self.env):
            return super().create(values_list)
        _acquire_billing_roster_lock(self.env.cr)
        before = _active_account_manager_roster_ids(self.env)
        guarded = _roster_mutation(self)
        users = super(ResUsers, guarded).create(values_list)
        _converge_billing_roster_if_changed(guarded.env, before)
        # Do not leak the recursion capability to the caller's recordset.
        return self.browse(users.ids)

    def write(self, values):
        """Observe standard form/API membership and active changes atomically."""
        if (_is_roster_mutation(self.env)
                or not self._may_change_billing_manager_roster(values)):
            return super().write(values)
        _acquire_billing_roster_lock(self.env.cr)
        before = _active_account_manager_roster_ids(self.env)
        guarded = _roster_mutation(self)
        records = guarded.browse(self.ids)
        result = super(ResUsers, records).write(values)
        _converge_billing_roster_if_changed(guarded.env, before)
        return result

    def unlink(self):
        """Deleting an active manager removes stale alerts in this transaction."""
        if _is_roster_mutation(self.env):
            return super().unlink()
        _acquire_billing_roster_lock(self.env.cr)
        before = _active_account_manager_roster_ids(self.env)
        guarded = _roster_mutation(self)
        records = guarded.browse(self.ids)
        result = super(ResUsers, records).unlink()
        _converge_billing_roster_if_changed(guarded.env, before)
        return result


class ResGroups(models.Model):
    _inherit = 'res.groups'

    @api.model_create_multi
    def create(self, values_list):
        """Capture every effective default/copy carrier, not only raw values."""
        if _is_roster_mutation(self.env):
            return super().create(values_list)
        _acquire_billing_roster_lock(self.env.cr)
        before = _active_account_manager_roster_ids(self.env)
        guarded = _roster_mutation(self)
        groups = super(ResGroups, guarded).create(values_list)
        # GroupsImplied.create pops explicit ``users`` before generic default
        # expansion.  With context/ir.default carriers, users and implied_ids
        # therefore arrive only inside generic create and core misses its
        # propagation write.  Replay each final user set under the private
        # recursion guard so effective implied membership is materialized.
        for group in groups.filtered(lambda item: (
                item.users and item.implied_ids)):
            group.write({'users': [(6, 0, group.users.ids)]})
        _converge_billing_roster_if_changed(guarded.env, before)
        return self.browse(groups.ids)

    def copy(self, default=None):
        """Converge once after copy replays users/implied values via create."""
        if _is_roster_mutation(self.env):
            return super().copy(default)
        _acquire_billing_roster_lock(self.env.cr)
        before = _active_account_manager_roster_ids(self.env)
        guarded = _roster_mutation(self)
        groups = guarded.browse(self.ids)
        copied = super(ResGroups, groups).copy(default)
        _converge_billing_roster_if_changed(guarded.env, before)
        return self.browse(copied.ids)

    def write(self, values):
        """Retain users/implied-group paths and converge only on roster delta."""
        if (_is_roster_mutation(self.env)
                or not {'users', 'implied_ids'} & set(values)):
            return super().write(values)
        _acquire_billing_roster_lock(self.env.cr)
        before = _active_account_manager_roster_ids(self.env)
        guarded = _roster_mutation(self)
        groups = guarded.browse(self.ids)
        result = super(ResGroups, groups).write(values)
        _converge_billing_roster_if_changed(guarded.env, before)
        return result

    def unlink(self):
        """Catch implied-group deletion if it changes effective membership."""
        if _is_roster_mutation(self.env):
            return super().unlink()
        _acquire_billing_roster_lock(self.env.cr)
        before = _active_account_manager_roster_ids(self.env)
        guarded = _roster_mutation(self)
        groups = guarded.browse(self.ids)
        result = super(ResGroups, groups).unlink()
        _converge_billing_roster_if_changed(guarded.env, before)
        return result


class MailMessage(models.Model):
    _inherit = 'mail.message'

    @api.model
    def _has_billing_message_access_authority(self):
        return bool(
            self.env.user.has_group('account.group_account_manager')
            or _is_incident_chatter(self.env)
            or _is_odoo_module_uninstall(self.env)
        )

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        """Do not expose incident chatter through author/creator shortcuts."""
        if not self._has_billing_message_access_authority():
            domain = expression.AND([
                domain,
                [('model', '!=', 'woow.billing.incident')],
            ])
        return super()._search(domain, offset, limit, order)

    def _get_forbidden_access(self, operation):
        forbidden = super()._get_forbidden_access(operation)
        if not self._has_billing_message_access_authority():
            incident_ids = [
                message.id
                for message in self.sudo()
                if message.model == 'woow.billing.incident'
            ]
            forbidden |= self.browse(incident_ids)
        return forbidden

    @api.model
    def _targets_billing_incident_values(self, values):
        model_name = (
            values['model'] if 'model' in values
            else self.env.context.get('default_model')
        )
        return model_name == 'woow.billing.incident'

    @api.model_create_multi
    def create(self, values_list):
        """Only validated, attachment-free completion may mint chatter."""
        if (any(self._targets_billing_incident_values(values)
                for values in values_list)
                and not _is_incident_chatter(self.env)):
            raise AccessError(_(
                'Billing incident messages may be created only by the '
                'internal activity completion flow.'
            ))
        messages = super().create(values_list)
        incident_messages = messages.sudo().filtered(
            lambda message: message.model == 'woow.billing.incident')
        # Recheck stored model after defaults/conversion, then close future-id
        # squatting: a dangling attachment may have named this message id
        # before the protected message row existed.
        if incident_messages and not _is_incident_chatter(self.env):
            raise AccessError(_(
                'Billing incident messages may be created only by the '
                'internal activity completion flow.'
            ))
        if incident_messages:
            squatted = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'mail.message'),
                ('res_id', 'in', incident_messages.ids),
            ])
            if squatted:
                _attachment_cleanup(squatted).unlink()
        return messages

    def write(self, values):
        """Reject direct body edits and old/new target moves into chatter."""
        targets_billing = (
            self._targets_billing_incident_values(values)
            or any(message.model == 'woow.billing.incident'
                   for message in self.sudo())
        )
        if targets_billing:
            core_metadata_only = (
                _is_incident_chatter(self.env)
                and not {'body', 'model', 'res_id'} & set(values)
            )
            if not core_metadata_only:
                raise AccessError(_(
                    'Billing incident chatter is managed automatically by '
                    'the internal activity completion flow.'
                ))
        return super().write(values)

    def unlink(self):
        """Keep incident chatter immutable outside a real module uninstall."""
        if (not _is_odoo_module_uninstall(self.env)
                and any(message.model == 'woow.billing.incident'
                        for message in self.sudo())):
            raise AccessError(_(
                'Billing incident chatter is managed automatically by the '
                'internal activity completion flow.'
            ))
        return super().unlink()


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    _BILLING_TARGET_CARRIERS = frozenset({
        'woow.billing.incident',
        'mail.activity',
        'mail.message',
    })
    _CANONICAL_TARGET_RES_ID_RE = re.compile(r'[1-9][0-9]*\Z')

    @api.model
    def _canonical_billing_target_res_id(self, res_model, res_id):
        """Canonicalize target ids before any protected-carrier decision."""
        if res_model not in self._BILLING_TARGET_CARRIERS:
            return res_id
        if isinstance(res_id, bool):
            raise AccessError(_(
                'Attachments are not allowed on billing incident activities.'
            ))
        if isinstance(res_id, int) and res_id > 0:
            return res_id
        if (isinstance(res_id, str)
                and self._CANONICAL_TARGET_RES_ID_RE.fullmatch(res_id)):
            return int(res_id)
        # Floats, zero, signs, whitespace, leading zeroes, Unicode digits, and
        # every other ambiguous carrier id fail closed before ORM coercion.
        raise AccessError(_(
            'Attachments are not allowed on billing incident activities.'
        ))

    @api.model
    def _billing_owned_target(self, res_model, res_id):
        """Return whether an attachment target belongs to the incident graph."""
        # The ledger model name alone is authoritative.  Never make direct
        # incident payload denial contingent on a usable/existing row id.
        if res_model == 'woow.billing.incident':
            return True
        canonical_res_id = self._canonical_billing_target_res_id(
            res_model, res_id)
        if res_model == 'mail.activity':
            review_type = self.env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review',
                raise_if_not_found=False,
            )
            activity = self.env['mail.activity'].sudo().with_context(
                active_test=False).browse(canonical_res_id).exists()
            return bool(
                activity
                and (activity.res_model == 'woow.billing.incident'
                     or (review_type
                         and activity.activity_type_id == review_type))
            )
        if res_model == 'mail.message':
            message = self.env['mail.message'].sudo().browse(
                canonical_res_id).exists()
            return bool(message and message.model == 'woow.billing.incident')
        return False

    @api.model
    def _values_billing_owned_target(self, values):
        res_model = (
            values['res_model'] if 'res_model' in values
            else self.env.context.get('default_res_model')
        )
        res_id = (
            values['res_id'] if 'res_id' in values
            else self.env.context.get('default_res_id')
        )
        return self._billing_owned_target(res_model, res_id)

    @api.model_create_multi
    def create(self, values_list):
        """Reject payload before a protected attachment row reaches the DB."""
        if _is_attachment_cleanup(self.env):
            return super().create(values_list)
        if any(self._values_billing_owned_target(values)
               for values in values_list):
            raise AccessError(_(
                'Attachments are not allowed on billing incident activities.'
            ))
        attachments = super().create(values_list)
        # Recheck actual stored targets in case ORM defaults/conversion produced
        # a final target different from the preflight payload.
        if any(self._billing_owned_target(
                attachment.res_model, attachment.res_id)
                for attachment in attachments.sudo()):
            raise AccessError(_(
                'Attachments are not allowed on billing incident activities.'
            ))
        return attachments

    def write(self, values):
        """Reject protected old, prospective, and ORM-final targets."""
        if _is_attachment_cleanup(self.env):
            return super().write(values)
        for attachment in self.sudo():
            old_target = self._billing_owned_target(
                attachment.res_model, attachment.res_id)
            new_target = self._billing_owned_target(
                values.get('res_model', attachment.res_model),
                values.get('res_id', attachment.res_id),
            )
            if old_target or new_target:
                raise AccessError(_(
                    'Attachments are not allowed on billing incident '
                    'activities.'
                ))
        result = super().write(values)
        if any(self._billing_owned_target(
                attachment.res_model, attachment.res_id)
                for attachment in self.sudo()):
            raise AccessError(_(
                'Attachments are not allowed on billing incident activities.'
            ))
        return result


class MailActivity(models.Model):
    _inherit = 'mail.activity'

    _BILLING_ACTIVITY_TARGET_ID_FIELDS = (
        'res_model_id',
        'activity_type_id',
        'res_id',
    )
    _CANONICAL_ACTIVITY_TARGET_ID_RE = re.compile(r'[1-9][0-9]*\Z')

    @api.model
    def _has_billing_activity_access_authority(self):
        return bool(
            self.env.user.has_group('account.group_account_manager')
            or _is_activity_scheduler(self.env)
            or _is_activity_terminal(self.env)
            or _is_odoo_module_uninstall(self.env)
        )

    def _billing_incident_activities(self):
        """Return every activity stored on the ledger, regardless of type."""
        incident_ids = [
            activity.id
            for activity in self.sudo()
            if activity.res_model == 'woow.billing.incident'
        ]
        return self.browse(incident_ids)

    def _require_billing_activity_access_authority(self, operation):
        incident_activities = self._billing_incident_activities()
        if (incident_activities
                and not self._has_billing_activity_access_authority()):
            raise incident_activities._make_access_error(operation)

    def _check_access(self, operation):
        result = super()._check_access(operation)
        if self._has_billing_activity_access_authority():
            return result
        incident_activities = self._billing_incident_activities()
        if not incident_activities:
            return result
        existing = result[0] if result else self.browse()
        forbidden = existing | incident_activities
        return forbidden, lambda: forbidden._make_access_error(operation)

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        """Assigned-to-me never bypasses current finance-role membership."""
        if not self._has_billing_activity_access_authority():
            domain = expression.AND([
                domain,
                [('res_model', '!=', 'woow.billing.incident')],
            ])
        return super()._search(domain, offset, limit, order)

    @api.model
    def _canonical_activity_target_id(self, value):
        """Accept only positive ASCII integer ids or their canonical strings."""
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        if (isinstance(value, str)
                and self._CANONICAL_ACTIVITY_TARGET_ID_RE.fullmatch(value)):
            return int(value)
        # Bool/False, floats, Unicode digits, signs, whitespace, zero and
        # leading zeroes fail before a field converter can erase their shape.
        raise AccessError(_(
            'Billing incident activities may be created only by the '
            'internal scheduler.'
        ))

    @api.model
    def _canonical_activity_target_values(self, values):
        canonical = dict(values)
        for field_name in self._BILLING_ACTIVITY_TARGET_ID_FIELDS:
            if field_name in canonical:
                canonical[field_name] = self._canonical_activity_target_id(
                    canonical[field_name])
        return canonical

    @api.model
    def _with_canonical_activity_target_defaults(self, field_names=()):
        context = dict(self.env.context)
        for field_name in field_names:
            context_name = 'default_%s' % field_name
            if context_name in context:
                context[context_name] = self._canonical_activity_target_id(
                    context[context_name])
        return self.with_context(context)

    @api.model
    def _canonical_activity_target_candidate(self, value):
        """Return a canonical positive id, or ``False`` without rejecting."""
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        if (isinstance(value, str)
                and self._CANONICAL_ACTIVITY_TARGET_ID_RE.fullmatch(value)):
            return int(value)
        return False

    @api.model
    def _targets_billing_activity_values(self, values, include_defaults=True):
        billing_model = self.env['ir.model'].sudo()._get(
            'woow.billing.incident')
        review_type = self.env.ref(
            'woow_paas_platform.'
            'mail_activity_type_billing_incident_review',
            raise_if_not_found=False,
        )
        defaults = self.env.context if include_defaults else {}
        res_model_id = values.get(
            'res_model_id', defaults.get('default_res_model_id'))
        res_model = values.get('res_model')
        if 'res_model_id' not in values and not res_model:
            res_model = defaults.get('default_res_model')
        activity_type_id = values.get(
            'activity_type_id', defaults.get('default_activity_type_id'))
        canonical_model_id = self._canonical_activity_target_candidate(
            res_model_id)
        canonical_type_id = self._canonical_activity_target_candidate(
            activity_type_id)
        return bool(
            res_model == 'woow.billing.incident'
            or (billing_model and canonical_model_id == billing_model.id)
            or (review_type and canonical_type_id == review_type.id)
        )

    @api.model
    def _validate_stored_billing_activity_create(self, activities):
        """Validate final related model/type and close future-id squatting."""
        review_type = self.env.ref(
            'woow_paas_platform.'
            'mail_activity_type_billing_incident_review',
            raise_if_not_found=False,
        )
        protected = activities.sudo().filtered(
            lambda activity: (
                activity.res_model == 'woow.billing.incident'
                or (review_type and activity.activity_type_id == review_type)
            )
        )
        invalid = protected.filtered(
            lambda activity: (
                not _is_activity_scheduler(self.env)
                or activity.res_model != 'woow.billing.incident'
                or not review_type
                or activity.activity_type_id != review_type
            )
        )
        if invalid:
            raise AccessError(_(
                'Billing incident activities may be created only by the '
                'internal scheduler.'
            ))
        incident_activities = protected.filtered(
            lambda activity: activity.res_model == 'woow.billing.incident')
        if incident_activities:
            squatted = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'mail.activity'),
                ('res_id', 'in', incident_activities.ids),
            ])
            if squatted:
                _attachment_cleanup(squatted).unlink()
        return True

    @api.model
    def _validate_stored_billing_activity_write(self, activities):
        """Reject converter-created incident/dedicated targets after write."""
        review_type = self.env.ref(
            'woow_paas_platform.'
            'mail_activity_type_billing_incident_review',
            raise_if_not_found=False,
        )
        protected = activities.sudo().filtered(
            lambda activity: (
                activity.res_model == 'woow.billing.incident'
                or (review_type and activity.activity_type_id == review_type)
            )
        )
        if protected and not _is_activity_terminal(self.env):
            raise AccessError(_(
                'Billing incident activities are managed automatically. '
                'Use the billing incident form instead.'
            ))
        return True

    def _dedicated_billing_incident_ids(self):
        review_type = self.env.ref(
            'woow_paas_platform.'
            'mail_activity_type_billing_incident_review',
            raise_if_not_found=False,
        )
        if not review_type or _is_odoo_module_uninstall(self.env):
            return []
        return sorted({
            activity.res_id
            for activity in self.sudo()
            if (activity.res_model == 'woow.billing.incident'
                and activity.activity_type_id == review_type)
        })

    @api.model_create_multi
    def create(self, values_list):
        """Strictly canonicalize only possible billing targets before core."""
        billing_branches = [
            self._targets_billing_activity_values(values)
            for values in values_list
        ]
        strict_default_fields = {
            field_name
            for values, targets_billing in zip(values_list, billing_branches)
            if targets_billing
            for field_name in self._BILLING_ACTIVITY_TARGET_ID_FIELDS
            if (field_name not in values
                and 'default_%s' % field_name in self.env.context)
        }
        guarded = self._with_canonical_activity_target_defaults(
            strict_default_fields)
        canonical_values = [
            guarded._canonical_activity_target_values(values)
            if targets_billing else dict(values)
            for values, targets_billing in zip(values_list, billing_branches)
        ]
        if (not _is_activity_scheduler(guarded.env)
                and any(billing_branches)):
            raise AccessError(_(
                'Billing incident activities may be created only by the '
                'internal scheduler.'
            ))
        activities = super(MailActivity, guarded).create(canonical_values)
        guarded._validate_stored_billing_activity_create(activities)
        return self.browse(activities.ids)

    def write(self, values):
        """Keep core coercion for other models; postcheck every final target."""
        review_type = self.env.ref(
            'woow_paas_platform.'
            'mail_activity_type_billing_incident_review',
            raise_if_not_found=False,
        )
        targets_billing = (
            self._targets_billing_activity_values(
                values, include_defaults=False)
            or any(
                activity.res_model == 'woow.billing.incident'
                or (review_type and activity.activity_type_id == review_type)
                for activity in self.sudo()
            )
        )
        write_values = (
            self._canonical_activity_target_values(values)
            if targets_billing else dict(values)
        )
        if targets_billing and not _is_activity_terminal(self.env):
            raise AccessError(_(
                'Billing incident activities are managed automatically. '
                'Use the billing incident form instead.'
            ))
        result = super().write(write_values)
        self._validate_stored_billing_activity_write(self)
        return result

    def _action_done(self, feedback=False, attachment_ids=None):
        """Serialize the whole core terminal flow with schedule and resolve."""
        self._require_billing_activity_access_authority('write')
        incident_ids = self._dedicated_billing_incident_ids()
        Incident = self.env['woow.billing.incident'].sudo()
        if (incident_ids and feedback not in (None, False)
                and Incident._free_text_ingress_category(
                    'activity_feedback', feedback)):
            raise UserError(_(
                'Billing incident activity feedback must contain only safe, '
                'non-sensitive summaries.'
            ))
        if incident_ids:
            _acquire_billing_roster_lock(self.env.cr)
        for incident_id in incident_ids:
            Incident._acquire_review_activity_lock(self.env.cr, incident_id)
        guarded = _activity_terminal(self)
        return super(MailActivity, guarded)._action_done(
            feedback=feedback,
            attachment_ids=attachment_ids,
        )

    def unlink(self):
        """Re-arm only unresolved dedicated billing alerts after terminal use."""
        self._require_billing_activity_access_authority('unlink')
        if _is_activity_scheduler(self.env):
            return super().unlink()
        incident_ids = self._dedicated_billing_incident_ids()
        Incident = self.env['woow.billing.incident'].sudo()
        if incident_ids:
            _acquire_billing_roster_lock(self.env.cr)
        # Lock before core deletes/archives the terminal activity.  Taking the
        # same incident-scoped lock as schedule/resolve prevents the classic
        # inversion "resolve holds advisory → waits activity row" versus
        # "done holds activity row → waits advisory".
        for incident_id in incident_ids:
            Incident._acquire_review_activity_lock(
                self.env.cr, incident_id)
        result = super().unlink()
        for incident_id in incident_ids:
            Incident._ensure_review_activities_current_cursor(incident_id)
        return result

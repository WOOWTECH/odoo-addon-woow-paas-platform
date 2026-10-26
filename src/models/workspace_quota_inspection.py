"""Administrator-only, live read projection of the managed Workspace quota."""
from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..services.paas_operator import PaaSOperatorError, get_paas_operator_client
from ..services.workspace_quota import workspace_namespace

# Backend list defaults to five rows; larger RPC batches expose the remainder
# as unknown rather than issuing an unbounded number of synchronous HTTP reads.
_QUOTA_OBSERVATION_BATCH_LIMIT = 5

_QUOTA_RESOURCES = (
    ('cpu_requests', 'requests.cpu'),
    ('cpu_limits', 'limits.cpu'),
    ('memory_requests', 'requests.memory'),
    ('memory_limits', 'limits.memory'),
    ('storage_requests', 'requests.storage'),
)


_QUANTITY_RE = re.compile(
    r'^([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)'
    r'(?:Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE])?$')


def _known_quantity(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    match = _QUANTITY_RE.fullmatch(value)
    if not match:
        return False
    try:
        return Decimal(match[1]).is_finite()
    except ArithmeticError:
        return False


def _observation(report: Any, namespace: str) -> tuple[dict[str, Any], str | None, datetime | None]:
    observation = report.get('managed_quota') if isinstance(report, dict) else None
    if not isinstance(observation, dict):
        return {}, 'operator_unsupported', None
    if observation.get('namespace') != namespace or observation.get('name') != namespace + '-quota':
        return {}, 'managed_quota_identity_mismatch', None
    observed_at = None
    try:
        timestamp = datetime.fromisoformat(observation.get('observed_at'))
        if timestamp.tzinfo is not None:
            observed_at = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        pass
    reason = observation.get('reason')
    reason = reason if isinstance(reason, str) and reason else 'managed_quota_incomplete'
    if observation.get('available') is True:
        complete = observed_at and all(isinstance(observation.get(key), str) and
                                       observation[key].strip() for key in ('uid', 'resource_version'))
        complete = complete and all(
            isinstance(observation.get(key), dict) and
            all(resource in observation[key] for _, resource in _QUOTA_RESOURCES)
            for key in ('hard', 'used'))
        if not complete:
            reason = 'managed_quota_incomplete'
        elif not all(_known_quantity(observation[key][resource])
                     for key in ('hard', 'used') for _, resource in _QUOTA_RESOURCES):
            reason = 'managed_quota_invalid_quantity'
        else:
            reason = None
    return observation, reason, observed_at


def _quota_char(string: str) -> fields.Char:
    return fields.Char(string=string, compute='_compute_quota_inspection',
                       compute_sudo=False, readonly=True, groups='base.group_system')


class WorkspaceQuotaInspection(models.Model):
    _inherit = 'woow_paas_platform.workspace'

    quota_namespace = _quota_char('Quota Namespace')
    quota_name = _quota_char('ResourceQuota Name')
    quota_uid = _quota_char('ResourceQuota UID')
    quota_resource_version = _quota_char('ResourceQuota Version')
    quota_reason = _quota_char('Quota Availability Reason')
    quota_status = _quota_char('Quota Observation')
    quota_observed_at = fields.Datetime(
        string='Quota Observed At', compute='_compute_quota_inspection',
        compute_sudo=False, readonly=True, groups='base.group_system',
        help='Time of the read, not a guarantee of quota controller reconciliation.',
    )
    quota_available = fields.Boolean(
        string='Complete Quota Observation', compute='_compute_quota_inspection',
        compute_sudo=False, readonly=True, groups='base.group_system',
    )
    quota_available_decoration = fields.Boolean(
        string='Quota Observation Decoration', compute='_compute_quota_available_decoration',
        compute_sudo=False, readonly=True, groups='base.group_user',
        help='Technical list-decoration helper. It mirrors quota availability for system '
             'administrators and is always false for other internal users.',
    )
    quota_cpu_requests_hard = _quota_char('CPU Requests Hard')
    quota_cpu_requests_used = _quota_char('CPU Requests Used')
    quota_cpu_limits_hard = _quota_char('CPU Limits Hard')
    quota_cpu_limits_used = _quota_char('CPU Limits Used')
    quota_memory_requests_hard = _quota_char('Memory Requests Hard')
    quota_memory_requests_used = _quota_char('Memory Requests Used')
    quota_memory_limits_hard = _quota_char('Memory Limits Hard')
    quota_memory_limits_used = _quota_char('Memory Limits Used')
    quota_storage_requests_hard = _quota_char('Storage Requests Hard')
    quota_storage_requests_used = _quota_char('Storage Requests Used')

    def _reject_quota_inspection_write(self, values, *, defaults=False):
        projection = {name for name, field in self._fields.items()
                      if field.compute == '_compute_quota_inspection'} | {
                          'quota_available_decoration'}
        if projection.intersection(values) or (defaults and any(
                'default_' + name in self.env.context for name in projection)):
            raise AccessError(_('Quota observations are read-only operator data.'))

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            self._reject_quota_inspection_write(values, defaults=True)
        return super().create(vals_list)

    def write(self, values):
        self._reject_quota_inspection_write(values)
        return super().write(values)

    def action_refresh_quota(self):
        """Backend refresh does not write a snapshot or accept a namespace."""
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only system administrators may inspect Workspace quotas.'))
        self.check_access('read')
        self.invalidate_recordset([name for name, field in self._fields.items()
                                  if field.compute == '_compute_quota_inspection'])
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    @api.depends_context('uid')
    def _compute_quota_available_decoration(self):
        if not self.env.user.has_group('base.group_system'):
            for workspace in self:
                workspace.quota_available_decoration = False
            return
        availability = self.mapped('quota_available')
        for workspace, available in zip(self, availability):
            workspace.quota_available_decoration = available

    @api.depends_context('uid')
    def _compute_quota_inspection(self):
        # No su/context escape hatch: Portal capability sudo retains the caller
        # uid and must never acquire this system-administrator read capability.
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only system administrators may inspect Workspace quotas.'))
        client = get_paas_operator_client(self.env(context={}))
        for index, workspace in enumerate(self):
            namespace = workspace_namespace(self.env, workspace)
            observation = {}
            observed_at = False
            reason = 'invalid_namespace' if not namespace else 'operator_disabled'
            if index >= _QUOTA_OBSERVATION_BATCH_LIMIT:
                reason = 'observation_batch_limit'
            elif namespace and client:
                try:
                    report = client.get_namespace_quota(namespace, include_managed=True)
                    observation, reason, observed_at = _observation(report, namespace)
                except PaaSOperatorError as exc:
                    reason = 'operator_unsupported' if exc.status_code in (404, 405) else 'operator_error'
            workspace.quota_namespace = namespace or False
            workspace.quota_name = observation.get('name') or False
            workspace.quota_uid = observation.get('uid') or False
            workspace.quota_resource_version = observation.get('resource_version') or False
            workspace.quota_observed_at = observed_at
            workspace.quota_available = not reason
            workspace.quota_reason = reason
            workspace.quota_status = _('Unknown / unavailable') if reason else _('Observed')
            for field_suffix, resource in _QUOTA_RESOURCES:
                for map_name in ('hard', 'used'):
                    values = observation.get(map_name)
                    value = values.get(resource) if isinstance(values, dict) else None
                    workspace['quota_%s_%s' % (field_suffix, map_name)] = (
                        value if _known_quantity(value) else False)

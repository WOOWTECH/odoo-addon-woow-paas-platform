"""Known remote publication failures preserve server-owned Access identities."""
from datetime import timedelta
import logging

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class AccessProtectionError(UserError):
    """The operator returned no usable protection identity."""

class AccessPublicationError(UserError):
    """The origin publication API returned a known unsuccessful contract."""

def access_configuration_ready(anchor):
    return (not anchor.access_publish_pending and
            (not anchor.is_protected or
             bool(anchor.access_app_id and not anchor.access_protection_lost)))

def access_mutation_result(anchors, data):
    pending = any(anchor.access_publish_pending for anchor in anchors)
    ready = all(access_configuration_ready(anchor) for anchor in anchors)
    result = {'success': not pending, 'access_configuration_ready': ready, 'data': data}
    if pending:
        result['error'] = _(
            'Access settings were saved, but the origin publication result is '
            'not confirmed. Retry this operation.')
    return result


def _publication_receipt_has_failure(result):
    """Reject explicit failure even when the response echoes the request."""
    return (('success' in result and result['success'] is not True) or
            bool(result.get('error')) or
            (type(result.get('status')) is str and
             result['status'].strip().lower() in
             {'error', 'failed', 'failure', 'disabled', 'partial'}))


def require_access_sync_receipt(result, payload):
    """Validate the complete current ``/api/access/sync`` wire receipt.

    This confirms the operator's desired-state response only.  It is not an
    edge-readiness or propagation observation.
    """
    enabled = (type(payload) is dict and
               payload.get('enabled') is True and
               type(payload.get('protected_hostnames')) is list and
               bool(payload['protected_hostnames']))
    valid = (type(result) is dict and type(payload) is dict and
             type(payload.get('policies')) is list and
             type(payload.get('existing_policy_ids')) is list and
             type(payload.get('login_idp_types')) is list and
             not _publication_receipt_has_failure(result) and
             not bool(result.get('partial')))
    collections = {}
    if valid:
        for field in ('policy_ids', 'allowed_idps',
                      'unresolved_idp_types', 'orphaned_policy_ids'):
            value = result.get(field)
            if (field not in result or type(value) is not list or
                    any(type(item) is not str or not item.strip()
                        for item in value) or
                    len(value) != len(set(value))):
                valid = False
                break
            collections[field] = value
    if valid:
        existing = payload['existing_policy_ids']
        selected_idps = payload['login_idp_types']
        old_ids_valid = (
            all(type(item) is str and item.strip() for item in existing) and
            len(existing) == len(set(existing)))
        selected_idps_valid = (
            all(type(item) is str and item.strip() for item in selected_idps) and
            len(selected_idps) == len(set(selected_idps)))
        orphaned = collections['orphaned_policy_ids']
        valid = (old_ids_valid and selected_idps_valid and
                 not collections['unresolved_idp_types'] and
                 set(orphaned).issubset(set(existing)) and
                 not set(orphaned).intersection(collections['policy_ids']))
    if valid and enabled:
        valid = (type(result.get('app_id')) is str and
                 bool(result['app_id'].strip()) and
                 len(collections['policy_ids']) == len(payload['policies']) and
                 len(collections['allowed_idps']) == len(selected_idps))
    elif valid:
        valid = ('app_id' in result and result['app_id'] is None and
                 collections['policy_ids'] == [] and
                 collections['allowed_idps'] == [] and
                 collections['unresolved_idp_types'] == [])
    if not valid:
        if enabled:
            raise AccessProtectionError(_(
                'Access protection was not established because the Access '
                'sync result is not confirmed. The origin remains blocked; '
                'retry the operation.'))
        raise AccessPublicationError(_(
            'Access teardown result is not confirmed. The origin remains '
            'blocked; retry the operation.'))
    return result


def require_tunnel_config_receipt(result, hostname, service_url):
    """Confirm the exact request echoed by /tunnels/{id}/config.

    This API receipt confirms desired configuration, not edge propagation.
    """
    if (type(result) is not dict or
            _publication_receipt_has_failure(result) or
            type(result.get('message')) is not str or
            type(result.get('hostname')) is not str or
            result['hostname'] != hostname or
            type(result.get('service_url')) is not str or
            result['service_url'] != service_url):
        raise AccessPublicationError(_(
            'Origin publication result is not confirmed. Retry.'))


def require_tunnel_routes_receipt(result, tunnel_id, routes, domain):
    """Confirm the tunnel and complete route identity set before DNS stamping."""
    valid = (type(result) is dict and
             not _publication_receipt_has_failure(result) and
             type(result.get('tunnel_id')) is str and
             result['tunnel_id'] == tunnel_id and
             type(result.get('routes')) is list)
    expected = {row['subdomain'] for row in routes}
    received = set()
    if valid:
        for row in result['routes']:
            if (type(row) is not dict or
                    type(row.get('subdomain')) is not str or
                    row['subdomain'] not in expected or
                    row['subdomain'] in received or
                    type(row.get('hostname')) is not str or
                    row['hostname'] != row['subdomain'] + '.' + domain or
                    (row.get('dns_record_id') is not None and
                     (type(row['dns_record_id']) is not str or
                      not row['dns_record_id']))):
                valid = False
                break
            received.add(row['subdomain'])
    if not valid or received != expected:
        raise AccessPublicationError(_(
            'Origin publication result is not confirmed. Retry.'))


# ---------------------------------------------------------------------------
# Token-revocation hold (REV-01)
#
# Removing an allow policy closes the origin immediately (503) and asks
# Cloudflare to revoke the app's tokens.  The provider only *accepts* that
# request; it never proves that an old cookie is denied, and the platform has
# no cookie to probe with.  The origin therefore stays closed for a bounded
# hold that starts when the provider acknowledges the revocation, and is
# re-published only after the Access state was re-confirmed.  The hold is not
# proof of denial: the live same-cookie oracle still has to observe it.
# ---------------------------------------------------------------------------
ACCESS_REVOKE_HOLD_PARAM = 'woow_paas_platform.access_revoke_hold_seconds'
ACCESS_REVOKE_HOLD_DEFAULT = 120
ACCESS_REVOKE_HOLD_MIN = 30
ACCESS_REVOKE_HOLD_MAX = 3600
ACCESS_RESYNC_CRON_XMLID = 'woow_paas_platform.ir_cron_access_protection_resync'


def access_revoke_hold_seconds(env):
    """Configured hold after provider acceptance, clamped to a sane range.

    An unset or unparsable value falls back to the default instead of
    shortening the hold: a typo must not weaken the revocation barrier.
    """
    raw = env['ir.config_parameter'].sudo().get_param(ACCESS_REVOKE_HOLD_PARAM)
    # ``get_param`` answers ``False`` for an unset (or empty) parameter and
    # ``int(False)`` is 0, which would clamp to the *minimum* instead of the
    # default: an unset parameter must mean the default.
    if not raw:
        return ACCESS_REVOKE_HOLD_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return ACCESS_REVOKE_HOLD_DEFAULT
    return max(ACCESS_REVOKE_HOLD_MIN, min(ACCESS_REVOKE_HOLD_MAX, value))


def access_revoke_provider_accepted(result):
    """True only for the operator's explicit ``accepted`` acknowledgement."""
    return type(result) is dict and result.get('token_revocation') == 'accepted'


def access_revoke_operator_reports(result):
    """True when the operator's receipt carries a ``token_revocation`` field.

    An operator that predates the revocation contract silently ignores
    ``revoke_tokens`` and never reports the field, so an acknowledgement would
    never arrive.  Callers log that skew instead of waiting without a reason.
    """
    return type(result) is dict and 'token_revocation' in result


def access_revoke_state(revoke_pending, accepted_at, now, hold_seconds):
    """Classify one anchor's revocation hold.

    ``none``              no revocation outstanding
    ``await_acceptance``  the provider has not acknowledged the revocation yet
    ``held``              acknowledged; the hold has not elapsed
    ``releasable``        acknowledged and the hold elapsed; re-publish may proceed
    """
    if not revoke_pending:
        return 'none'
    if not accepted_at:
        return 'await_acceptance'
    if now < accepted_at + timedelta(seconds=hold_seconds):
        return 'held'
    return 'releasable'


def schedule_access_resync(env, at):
    """Best effort: wake the Access resync cron at ``at`` (naive UTC datetime).

    The periodic cron remains the safety net, so a failure here only delays
    recovery to the next scheduled run.
    """
    cron = env.ref(ACCESS_RESYNC_CRON_XMLID, raise_if_not_found=False)
    if not cron:
        return False
    try:
        with env.cr.savepoint():
            cron.sudo()._trigger(at=at)
    except Exception as error:  # noqa: BLE001 - never fail a sync over a wake-up
        _logger.warning('[ACCESS] could not schedule resync at %s: %s', at, error)
        return False
    return True

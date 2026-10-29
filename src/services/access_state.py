"""Known remote publication failures preserve server-owned Access identities."""
import contextlib
from datetime import timedelta
import logging
import time

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
    # The controller calls this only after its authorized mutation persisted:
    # ``saved`` keeps "the change is stored" apart from "the change is live".
    result = {'success': not pending, 'saved': True,
              'access_configuration_ready': ready, 'data': data}
    if pending and any(anchor.access_token_revoke_pending for anchor in anchors):
        # Retrying cannot shorten a revocation hold: say what is going on.
        result['error'] = _(
            'Access settings were saved. Existing sessions are being revoked '
            'and access resumes by itself once the revocation is confirmed. '
            'Refresh to check the status.')
    elif pending:
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
# The provider rejected the token revocation (for example a Cloudflare 5xx): the origin
# stays closed, so do not leave it to the periodic cron (15 minutes) to ask again.
ACCESS_REVOKE_RETRY_SECONDS = 60


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


# ---------------------------------------------------------------------------
# Authorization-change revocation (trigger scope of #1707)
#
# Removing a policy is not the only way to take access away: editing an allow
# list, a require/exclude rule, the login methods or the session duration also
# leaves every already-issued Access token valid until it expires.  Every
# persisted edit that changes the app's *effective authorization semantics*
# therefore starts the same revocation generation as a policy removal (closed
# origin, scoped token revocation, bounded hold, re-confirmed re-publication).
#
# The decision compares a fingerprint taken before and after the write.  The
# fingerprint ignores everything that is cosmetic (policy names, remote ids,
# order / case / spacing inside a selector) and keeps everything that is not
# (decision, precedence, the include / require / exclude sets, login methods,
# session duration, hostnames, whether the app is enforced at all).  An edit
# that does not change it never revokes.
#
# An edit that only *widens* the authorization revokes nothing either: no
# already-issued token can have lost access.  That is decided conservatively
# (``access_authorization_widens_only``): a new Allow policy, more addresses in
# an Allow policy's include group, one more login method, a longer session
# duration.  Everything else -- including anything this function cannot prove --
# revokes.  A typo in the switch below never turns the revocation off.
# ---------------------------------------------------------------------------
ACCESS_REVOKE_ON_CHANGE_PARAM = 'woow_paas_platform.access_revoke_on_authorization_change'
# Anchor fields (all three anchor kinds) whose edit changes the app's authorization semantics.
ACCESS_AUTHORIZATION_CONFIG_FIELDS = (
    'is_protected', 'login_google', 'login_email_otp', 'session_duration')
_ACCESS_REVOKE_ON_CHANGE_OFF = frozenset({'0', 'false', 'off', 'no'})


def access_revoke_on_authorization_change(env):
    """Break-glass switch for the edit triggers; policy *removal* ignores it.

    Only an explicit ``0`` / ``false`` / ``off`` / ``no`` turns the edit
    triggers off.  Unset, empty or any other text keeps them on.
    """
    raw = env['ir.config_parameter'].sudo().get_param(ACCESS_REVOKE_ON_CHANGE_PARAM)
    return not (raw and str(raw).strip().lower() in _ACCESS_REVOKE_ON_CHANGE_OFF)


def _normalized_access_rule(rule):
    """Canonicalize selectors whose order/case/spacing does not change auth."""
    rule_type = rule.get('rule_type') or ''
    value = (rule.get('rule_value') or '').strip()
    if rule_type == 'email_list':
        value = tuple(sorted({item.strip().lower() for item in value.split(',')
                              if item.strip()}))
    elif rule_type == 'email_domain':
        value = value.lstrip('@').lower()
    elif rule_type == 'country':
        value = tuple(sorted({item.strip().upper() for item in value.split(',')
                              if item.strip()}))
    elif rule_type == 'everyone':
        value = ''
    return rule_type, value


def access_authorization_fingerprint(payload):
    """Effective authorization semantics of one Access sync payload.

    Policy identity and display names are cosmetic; decision and precedence
    are not.  Rules inside one include / require / exclude group are a set.
    The first element is whether the app is enforced at all.
    """
    policies = []
    for policy in payload.get('policies') or []:
        groups = tuple(
            tuple(sorted({_normalized_access_rule(rule)
                          for rule in (policy.get(group) or [])}))
            for group in ('include', 'require', 'exclude'))
        policies.append((policy.get('decision'), policy.get('precedence'), groups))
    return (
        payload.get('enabled') is True,
        (payload.get('primary_hostname') or '').strip().lower(),
        tuple(sorted({hostname.strip().lower() for hostname in
                      (payload.get('protected_hostnames') or []) if hostname})),
        payload.get('session_duration') or '24h',
        tuple(sorted(set(payload.get('login_idp_types') or []))),
        tuple(sorted(set(policies), key=repr)),
    )


#: Seconds of every selectable session duration; an unknown value is never "longer".
_ACCESS_SESSION_SECONDS = {
    '30m': 1800, '1h': 3600, '8h': 28800, '24h': 86400, '168h': 604800}


def _rule_atoms(rules):
    """One group of normalized rules as a set of single selectors.

    An e-mail / country list is split into its members, so "one more address in
    the same rule" is a superset like "one more rule" is.
    """
    atoms = set()
    for rule_type, value in rules:
        if rule_type in ('email_list', 'country'):
            atoms.update((rule_type, item) for item in value)
        else:
            atoms.add((rule_type, value))
    return frozenset(atoms)


def _policy_covers(after, before):
    """True when ``after`` grants everything ``before`` granted.

    Same decision, precedence, require and exclude groups; the include group of
    an Allow policy may only have grown (its entries are alternatives).  A policy
    of any other decision has to be identical.
    """
    decision, precedence, (include, require, exclude) = before
    a_decision, a_precedence, (a_include, a_require, a_exclude) = after
    if (decision, precedence) != (a_decision, a_precedence):
        return False
    if (_rule_atoms(require) != _rule_atoms(a_require) or
            _rule_atoms(exclude) != _rule_atoms(a_exclude)):
        return False
    if decision == 'allow':
        return _rule_atoms(include) <= _rule_atoms(a_include)
    return _rule_atoms(include) == _rule_atoms(a_include)


def access_authorization_widens_only(before, after):
    """True only when no token valid under ``before`` can be refused under ``after``.

    Both arguments are ``access_authorization_fingerprint`` results.  The test is
    deliberately one-sided: it proves a pure widening and answers False for
    everything else, including what it cannot decide (an unknown session
    duration, a login-method list that was empty, a policy it cannot pair).  It
    does not depend on how the provider orders its policies: a new Allow policy,
    or a larger include group in an Allow policy, can only add principals.
    """
    if before == after:
        return True
    b_enabled, b_host, b_hosts, b_duration, b_idps, b_policies = before
    a_enabled, a_host, a_hosts, a_duration, a_idps, a_policies = after
    if not (b_enabled and a_enabled) or (b_host, b_hosts) != (a_host, a_hosts):
        return False
    if b_duration != a_duration:
        old = _ACCESS_SESSION_SECONDS.get(b_duration)
        new = _ACCESS_SESSION_SECONDS.get(a_duration)
        if old is None or new is None or new < old:
            return False
    if b_idps != a_idps and (not b_idps or not set(b_idps) <= set(a_idps)):
        # An empty list means "every provider": it must never be mistaken for the
        # smallest set, and a method may only be added.
        return False
    unmatched = list(a_policies)
    for policy in b_policies:
        match = next((other for other in unmatched
                      if _policy_covers(other, policy)), None)
        if match is None:
            return False
        unmatched.remove(match)
    return all(policy[0] == 'allow' for policy in unmatched)


def access_record_ids(values):
    """The real record ids among ``values``.

    ``create`` / ``copy`` vals carry ``False`` for an empty Many2one, and in
    Python ``False`` is an ``int``: ``browse([False])`` is an SQL error.
    """
    return [value for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value > 0]


def access_authorization_snapshot(anchors):
    """``{(model, id): fingerprint}`` for the anchors that have an Access app.

    An anchor without an app has no issued token to revoke, so it needs no
    "before" and the payload builder is not run for it.
    """
    return {(anchor._name, anchor.id): anchor._access_authorization_fingerprint()
            for anchor in anchors if anchor.exists() and anchor.access_app_id}


@contextlib.contextmanager
def access_revoke_savepoint(env, reached):
    """``env.cr.savepoint()`` that keeps the edge from staying closed on failure.

    A revoke-driven sync parks the origin *before* its provider call and a parked
    edge cannot be rolled back with the transaction.  When the block fails (the
    edit rolls back, the revocation mark with it) every anchor in ``reached``
    gets a durable closure marker instead, so the resync cron re-confirms Access
    and re-opens it.  Callers append an anchor right after marking it.  The
    marker is written after the savepoint has rolled back; callers that catch
    the error (the controllers do) commit it.
    """
    try:
        with env.cr.savepoint():
            yield
    except Exception:
        for anchor in reached:
            if anchor.exists():
                anchor._access_keep_parked_marker()
        raise


def access_mark_authorization_changes(env, anchors, before):
    """Start a revocation generation on every anchor whose authorization narrowed.

    ``before`` is the snapshot taken before the write; an anchor missing from it
    counts as changed.  An edit that did not change the semantics, or only
    widened them (``access_authorization_widens_only``), revokes nothing.  An
    anchor that is not enforced afterwards, or has no Access app yet, has nothing
    to revoke.  Returns the marked anchors.
    """
    if not access_revoke_on_authorization_change(env):
        return []
    marked = []
    for anchor in anchors:
        if not anchor.exists() or not anchor.access_app_id:
            continue
        after = anchor._access_authorization_fingerprint()
        if not after[0]:
            continue
        previous = before.get((anchor._name, anchor.id))
        if previous is not None and access_authorization_widens_only(previous, after):
            continue
        anchor._mark_access_token_revoke_pending()
        marked.append(anchor)
    return marked


def access_resync_wake_pending(env, by):
    """True when a resync wake-up is already queued for ``by`` (naive UTC) or earlier.

    Several rows can be rejected by the provider in one run; one queued wake-up is
    enough for all of them (the resync scans every due row), so callers skip queuing
    another. Unconsumed wake-ups that are already due count as queued.
    """
    cron = env.ref(ACCESS_RESYNC_CRON_XMLID, raise_if_not_found=False)
    if not cron:
        return False
    return bool(env['ir.cron.trigger'].sudo().search_count(
        [('cron_id', '=', cron.id), ('call_at', '<=', by)], limit=1))


# ---------------------------------------------------------------------------
# Create-or-adopt (RACE-06)
#
# Cloudflare keeps exactly ONE Access application per domain: a second POST for
# the same domain is refused with HTTP 409 / error 11010
# ``access.api.error.application_already_exists`` -- measured on the live account,
# including 8 simultaneous POSTs (one 201, seven 409), and the domain (not the
# name) is the key.  That is the idempotency primitive the create path needs, so a
# create whose outcome is unknown (worker killed around the provider call) can
# simply be repeated: either it creates the app, or the provider says it already
# exists and we adopt the one that matches our exact name and domain.
# ---------------------------------------------------------------------------
ACCESS_CREATE_SETTLE_PARAM = 'woow_paas_platform.access_create_settle_seconds'
ACCESS_CREATE_SETTLE_DEFAULT = 600
ACCESS_CREATE_SETTLE_MIN = 30
ACCESS_CREATE_SETTLE_MAX = 86400
_ADOPT_REVIEW_ATTEMPTS = 3
_ADOPT_REVIEW_PAUSE_SECONDS = 2


def access_create_settle_seconds(env):
    """How long an unknown create outcome is left alone before it is repeated.

    The Operator finishes (or gives up) an in-flight create within its own request
    timeouts even when the Odoo worker that asked is dead, so waiting this long
    only avoids racing that tail; correctness never depends on it because a
    duplicate create is refused by the provider.  Unset or unparsable means the
    default; the value is clamped.
    """
    raw = env['ir.config_parameter'].sudo().get_param(ACCESS_CREATE_SETTLE_PARAM)
    if not raw:
        return ACCESS_CREATE_SETTLE_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return ACCESS_CREATE_SETTLE_DEFAULT
    return max(ACCESS_CREATE_SETTLE_MIN, min(ACCESS_CREATE_SETTLE_MAX, value))


def _adoptable_review(review):
    return (type(review) is dict and review.get('status') == 'unique' and
            review.get('match_count') == 1 and
            type(review.get('app_id')) is str and bool(review['app_id'].strip()))


def sync_access_app_create_or_adopt(client, payload):
    """``client.sync_access_app(payload)``, adopting the existing app on a conflict.

    Returns ``(result, payload_used)``.  Only a *create* (enabled, no
    ``existing_app_id``) that the provider refused with 409 is adopted, and only
    when a read-only exact name+domain lookup finds exactly one app: a foreign app
    holding the domain, or several matches, is not ours to take over and the
    original error is raised.
    """
    from .paas_operator import PaaSOperatorAPIError  # lazy: avoid an import cycle
    try:
        return client.sync_access_app(payload), payload
    except PaaSOperatorAPIError as error:
        if not (payload.get('enabled') is True and not payload.get('existing_app_id')
                and error.status_code == 409):
            raise
        review = None
        for attempt in range(_ADOPT_REVIEW_ATTEMPTS):
            review = client.review_access_app(
                payload.get('app_name'), payload.get('primary_hostname'))
            if _adoptable_review(review):
                break
            if attempt + 1 < _ADOPT_REVIEW_ATTEMPTS:
                time.sleep(_ADOPT_REVIEW_PAUSE_SECONDS)  # read-after-write lag
        else:
            raise error
        _logger.warning(
            '[ACCESS] create for %s conflicted with an existing app that matches '
            'our exact name and domain; adopting it instead of creating',
            payload.get('primary_hostname'))
        adopted = dict(payload, existing_app_id=review['app_id'])
        return client.sync_access_app(adopted), adopted

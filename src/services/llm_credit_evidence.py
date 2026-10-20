"""Pure, conservative evidence checks for an uncertain new-api credit write.

rc.44 source contract: controller/user_quota.go::manageUserQuota writes type 1
through model/log.go::RecordLogWithAdminInfo. Log.quota is not the adjustment:
the committed delta lives in other.op.params. No Odoo, network or free-text
upstream content is exposed by this module.
"""
import json
import re
from dataclasses import dataclass

from . import llm_redact

INT8_MAX = 2 ** 63 - 1
INT8_MIN = -(2 ** 63)
QUIET_SECONDS = 300
_REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')


class EvidenceError(ValueError):
    """Fixed reason only. Never include upstream values in exceptions/logs."""


def _integer(value, minimum=INT8_MIN):
    if type(value) is not int or not minimum <= value <= INT8_MAX:
        raise EvidenceError('invalid_evidence_number')
    return value


def _request_id(value):
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise EvidenceError('invalid_evidence_request_id')
    # A malformed response must not smuggle a credential into audit storage.
    if llm_redact.redact(value) != value:
        raise EvidenceError('invalid_evidence_request_id')
    return value


@dataclass(frozen=True)
class QuotaSnapshot:
    quota: int
    used_quota: int
    request_count: int

    def __post_init__(self):
        _integer(self.quota)
        _integer(self.used_quota, 0)
        _integer(self.request_count, 0)


@dataclass(frozen=True)
class CreditLogProof:
    request_id: str
    target_user_id: int
    admin_id: int
    quota: int
    before: int
    after: int
    created_at: int


def credit_log_proof(log, *, target_user_id, quota, quota_before, sent_at,
                     observed_at, expected_request_id=None, service_admin_id=None,
                     claimed_request_ids=frozenset()):
    """Return a proof or None; malformed evidence raises and cannot mean 'absent'.

    An exact response request id is the strongest correlation. Without one,
    require the trusted configured service-admin id AND the exact before/delta
    operation in the attempt's time window. A log's admin id is never its own
    trust anchor. The caller must still atomically claim request_id uniquely.
    """
    _integer(target_user_id, 1)
    _integer(quota, 1)
    _integer(sent_at, 0)
    _integer(observed_at, sent_at)
    if expected_request_id is not None:
        _request_id(expected_request_id)
    elif quota_before is None:
        # The full-operation fallback relies on the durable pre-send sample.
        raise EvidenceError('invalid_evidence_number')
    if quota_before is not None:
        _integer(quota_before)
    if service_admin_id is not None:
        _integer(service_admin_id, 1)
    if not isinstance(log, dict) or type(log.get('type')) is not int:
        raise EvidenceError('invalid_credit_log')
    if log['type'] != 1:
        return None
    user_id = _integer(log.get('user_id'), 1)
    created_at = _integer(log.get('created_at'), 0)
    request_id = _request_id(log.get('request_id'))
    other = log.get('other')
    if isinstance(other, str):
        if len(other) > 65536:
            raise EvidenceError('invalid_credit_log')
        try:
            other = json.loads(other)
        except (ValueError, RecursionError):
            raise EvidenceError('invalid_credit_log') from None
    if not isinstance(other, dict):
        raise EvidenceError('invalid_credit_log')
    op, admin = other.get('op'), other.get('admin_info')
    if not isinstance(op, dict) or not isinstance(admin, dict):
        raise EvidenceError('invalid_credit_log')
    if op.get('action') != 'user.quota_add':
        return None
    params = op.get('params')
    if not isinstance(params, dict):
        raise EvidenceError('invalid_credit_log')
    admin_id = _integer(admin.get('admin_id'), 1)
    target = _integer(params.get('target_user_id'), 1)
    # ``manageUserQuota`` deliberately records the requested adjustment in
    # ``requested_quota``. ``Log.quota`` is a display/usage field and remains
    # zero for this administrative top-up, so it must never become evidence.
    amount = _integer(params.get('requested_quota'), 1)
    before = _integer(params.get('from'))
    after = _integer(params.get('to'))
    if (params.get('mode') != 'add' or user_id != target_user_id or target != target_user_id
            or amount != quota or after - before != quota
            or not sent_at <= created_at <= observed_at):
        return None
    if expected_request_id is not None:
        if request_id != expected_request_id:
            return None
    # A response-correlated request id is independently durable evidence.  The
    # configured service-admin is only the trust anchor for a looser complete
    # operation match; an admin rotation must not make an exact receipt look
    # like an unproven write.
    elif (service_admin_id is None or admin_id != service_admin_id or
          before != quota_before):
        return None
    if request_id in claimed_request_ids:
        raise EvidenceError('credit_log_already_claimed')
    return CreditLogProof(request_id, target, admin_id, amount, before, after, created_at)


def quiescent_result(before, first, second, *, quota, elapsed_seconds):
    """Classify two successful samples at least five minutes apart.

    Call only after the complete log query succeeded with no usable evidence,
    while retaining exclusive ownership of this account's in-flight order.
    Network failure, missing snapshots and pagination are caller-side blockers.
    """
    if not all(isinstance(value, QuotaSnapshot) for value in (before, first, second)):
        raise EvidenceError('invalid_quota_snapshot')
    _integer(quota, 1)
    _integer(elapsed_seconds, 0)
    if elapsed_seconds < QUIET_SECONDS:
        return 'wait'
    if (first != second or first.used_quota != before.used_quota
            or first.request_count != before.request_count):
        return 'needs_review'
    if first.quota == before.quota + quota:
        return 'credited'
    if first.quota == before.quota:
        return 'not_executed'
    return 'needs_review'

"""Pure I2 balance classification for new-api quota snapshots.

The caller owns snapshot collection, completeness, account locking, and durable
incident deduplication. Pass freshly recalculated ledger bounds and discrepancy
aggregate on every call. Rechecks intentionally apply those *current* values to
the candidate's original sample and the current sample. The engine verifies
only those two observed endpoints and their elapsed time; it makes no claim
about unobserved samples between them.

This module never performs ORM/network work and returns only closed result
codes. I3 evidence is trustworthy only when its caller has completed the full
required log coverage (including all service-admin type=1 logs and relevant
PaaS-prefix users); incomplete coverage remains pending and cannot mean zero.
"""
from dataclasses import dataclass

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
EPSILON = 15_625
RECHECK_SECONDS = 300

_OUTCOMES = frozenset({
    'clear', 'candidate_up', 'candidate_down', 'external_credit',
    'settlement_anomaly', 'evidence_pending', 'mismatch',
})
_DIRECTIONS = frozenset({'up', 'down'})


class ReconcileInputError(ValueError):
    """Invalid input, with a fixed safe message."""


def _bounded_int(value, minimum, maximum=INT64_MAX):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReconcileInputError('invalid_reconcile_input')
    return value


def _aggregate(value):
    # Aggregates are Python integers and deliberately have no int64 cap: summing
    # valid ledger rows may exceed both int64 and IEEE-754's exact range.
    if type(value) is not int:
        raise ReconcileInputError('invalid_reconcile_input')
    return value


@dataclass(frozen=True)
class BalanceSample:
    quota: int
    used_quota: int
    request_count: int
    sampled_at: int

    def __post_init__(self):
        _bounded_int(self.quota, INT64_MIN, INT64_MAX)
        _bounded_int(self.used_quota, 0)
        _bounded_int(self.request_count, 0)
        _bounded_int(self.sampled_at, 0)

    @property
    def total(self):
        """Exact new-api sum; may exceed int64 after addition."""
        return self.quota + self.used_quota


@dataclass(frozen=True)
class Candidate:
    """Persist the first crossing sample and pass it to each recheck."""

    direction: str
    sample: BalanceSample

    def __post_init__(self):
        if type(self.direction) is not str or self.direction not in _DIRECTIONS:
            raise ReconcileInputError('invalid_reconcile_input')
        if not isinstance(self.sample, BalanceSample):
            raise ReconcileInputError('invalid_reconcile_input')


@dataclass(frozen=True)
class I3Evidence:
    """Coverage result from the caller's complete I3 log scan."""

    coverage_complete: bool
    unclaimed_type1_count: int

    def __post_init__(self):
        if type(self.coverage_complete) is not bool:
            raise ReconcileInputError('invalid_reconcile_input')
        _bounded_int(self.unclaimed_type1_count, 0)


@dataclass(frozen=True)
class Decision:
    outcome: str
    severity: str
    candidate: object = None

    def __post_init__(self):
        if (type(self.outcome) is not str or self.outcome not in _OUTCOMES
                or type(self.severity) is not str
                or self.severity not in {'none', 'medium', 'high'}
                or (self.candidate is not None and not isinstance(self.candidate, Candidate))):
            raise ReconcileInputError('invalid_reconcile_input')


def reconcile(current, lower, upper, *, known_discrepancy=0,
              candidate=None, evidence=None):
    """Classify one account against exact, caller-trusted current aggregates.

    ``lower`` is the credited quota sum (sources user/manual/dr/recharge;
    exclude credit_lost). ``upper`` additionally includes sending/unknown/
    needs_review amounts. Both must be recalculated by the caller. The signed
    discrepancy aggregate is subtracted from each sample's ``quota +
    used_quota`` before comparison.

    First crossing returns a candidate containing that exact sample. At
    recheck, both the candidate sample and current sample must still cross the
    current bounds, and at least 300 seconds must have elapsed. A low result
    additionally requires unchanged request_count; a change starts a new
    candidate at the current sample. At the exact epsilon boundary the sample
    is within tolerance. Clock reversal and malformed candidate state are
    rejected with a fixed exception. Only these two observed endpoints are
    checked; this function does not claim unobserved continuous crossing.
    """
    if not isinstance(current, BalanceSample):
        raise ReconcileInputError('invalid_reconcile_input')
    lower = _aggregate(lower)
    upper = _aggregate(upper)
    known_discrepancy = _aggregate(known_discrepancy)
    if lower < 0 or upper < lower:
        raise ReconcileInputError('invalid_reconcile_input')
    if candidate is not None and not isinstance(candidate, Candidate):
        raise ReconcileInputError('invalid_reconcile_input')
    if evidence is not None and not isinstance(evidence, I3Evidence):
        raise ReconcileInputError('invalid_reconcile_input')
    if candidate is not None and candidate.sample.sampled_at > current.sampled_at:
        raise ReconcileInputError('invalid_reconcile_input')

    adjusted = current.total - known_discrepancy
    below = adjusted < lower - EPSILON
    above = adjusted > upper + EPSILON
    if not below and not above:
        return Decision('clear', 'none')

    direction = 'down' if below else 'up'
    fresh = Candidate(direction, current)
    if candidate is None:
        return Decision('candidate_' + direction, 'none', fresh)

    baseline = candidate.sample
    baseline_adjusted = baseline.total - known_discrepancy
    baseline_still_crosses = (
        baseline_adjusted < lower - EPSILON if direction == 'down'
        else baseline_adjusted > upper + EPSILON
    )
    quiet = current.sampled_at - baseline.sampled_at
    traffic_stable = baseline.request_count == current.request_count

    if candidate.direction != direction or not baseline_still_crosses:
        return Decision('candidate_' + direction, 'none', fresh)
    if direction == 'down' and not traffic_stable:
        return Decision('candidate_down', 'none', fresh)
    if quiet < RECHECK_SECONDS:
        return Decision('candidate_' + direction, 'none', candidate)

    if direction == 'down':
        return Decision('mismatch', 'medium')
    if evidence is None or not evidence.coverage_complete:
        return Decision('evidence_pending', 'none', candidate)
    if evidence.unclaimed_type1_count:
        return Decision('external_credit', 'high')
    return Decision('settlement_anomaly', 'high')

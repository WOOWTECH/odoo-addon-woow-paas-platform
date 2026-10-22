"""Pure, inactive I8 validator for an already decoded anonymous /api/status.

Contract evidence: QuantumNous/new-api rc.44, commit
633b4c5b190dc1af99a2814391354d86948e7943: controller/misc.go GetStatus,
common/constants.go and setting/operation_setting/general_setting.go.
Expected values: DESIGN sections 4.4 I8 and 7.1; deployment tooling
Woow_k3s_newapi b7752131ba5f000ab4e4cb397713bd0677e16e68,
scripts/newapi_admin.py STATUS_C22 / STATUS_RUNTIME (except version).

Eight C22 fields plus three runtime fields are required. Go float64 values
can decode as Python int or float; bool is never a number here. No tolerance,
coercion, defaults, or version pinning. Extra keys are not invariants: rc.44
returns many other fields, including optional OAuth/console content.

This module does not fetch, parse HTTP/JSON, schedule, persist, change guards,
or emit alerts. A future caller must validate transport/freshness separately
and consume ``ok`` explicitly before permitting guarded operations. Results
contain only fixed field names/reasons, never upstream values or messages.
"""
from dataclasses import dataclass


# Tuples prevent callers from mutating the contract in place.
_EXPECTED = (
    ('quota_per_unit', 500_000),
    ('password_login_enabled', True),
    ('turnstile_check', False),
    ('password_login_encryption_enabled', False),
    ('enable_batch_update', False),
    ('enable_data_export', True),
    ('register_enabled', False),
    ('self_use_mode_enabled', False),
    ('quota_display_type', 'CUSTOM'),
    ('custom_currency_exchange_rate', 32),
    ('checkin_enabled', False),
)


@dataclass(frozen=True)
class StatusValidation:
    """Empty violations means these status invariants alone were satisfied."""

    violations: tuple

    @property
    def ok(self):
        return not self.violations


def validate_status(payload):
    """Validate the decoded rc.44 response envelope; fail closed on bad input.

    Accept only ordinary JSON decoder output (built-in dict/scalar types).
    Violation pairs are (fixed field name, missing/type/mismatch); malformed
    envelopes instead yield one fixed (response, envelope) pair.
    """
    if (type(payload) is not dict or payload.get('success') is not True
            or type(payload.get('data')) is not dict):
        return StatusValidation((('response', 'envelope'),))
    data = payload['data']
    violations = []
    for field, expected in _EXPECTED:
        if field not in data:
            violations.append((field, 'missing'))
            continue
        value = data[field]
        if type(expected) is int:
            valid_type = type(value) in (int, float)
        else:
            valid_type = type(value) is type(expected)
        if not valid_type:
            violations.append((field, 'type'))
        elif value != expected:
            violations.append((field, 'mismatch'))
    return StatusValidation(tuple(violations))

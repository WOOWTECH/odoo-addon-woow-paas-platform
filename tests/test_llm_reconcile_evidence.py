"""Pure AP log-normalization tests; no Odoo or network is required."""
import copy
import importlib
import json
from pathlib import Path
import sys
import types
import unittest

PACKAGE = 'woow_llm_reconcile_evidence_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / 'src/services')]
sys.modules[PACKAGE] = package
evidence = importlib.import_module(PACKAGE + '.llm_reconcile_evidence')


def quota_add(log_id=10):
    return {
        'id': log_id, 'type': 1, 'user_id': 42, 'created_at': 1700000000,
        'request_id': 'request-123', 'quota': 0,
        'other': {
            'admin_info': {'admin_id': 7},
            'op': {'action': 'user.quota_add', 'params': {
                'target_user_id': 42, 'requested_quota': 1562500, 'quota': 1562500,
                'mode': 'add', 'from': 300, 'to': 1562800,
            }},
        },
    }


def page(items, *, p=1, page_size=2, total=None):
    return {'page': p, 'page_size': page_size,
            'total': len(items) if total is None else total, 'items': items}


class TestNormalizePage(unittest.TestCase):

    def test_normalizes_only_typed_fields_and_uses_requested_quota(self):
        row = quota_add()
        normalized = evidence.normalize_page(page([row]), requested_page=1,
                                             requested_page_size=2)
        self.assertEqual(len(normalized.items), 1)
        item = normalized.items[0]
        self.assertEqual((item.log_id, item.log_type, item.user_id, item.created_at,
                          item.request_id, item.action, item.admin_id, item.target_user_id,
                          item.requested_quota, item.quota_from, item.quota_to),
                         (10, 1, 42, 1700000000, 'request-123', 'user.quota_add', 7,
                          42, 1562500, 300, 1562800))
        self.assertEqual(item.classification, 'type1_quota_add')
        self.assertFalse(hasattr(item, 'quota'))

    def test_accepts_json_other_and_rejects_duplicate_keys_and_oversize(self):
        row = quota_add()
        row['other'] = json.dumps(row['other'])
        self.assertEqual(evidence.normalize_page(page([row]), requested_page=1,
                                                 requested_page_size=2).items[0].admin_id, 7)
        for raw in ('{"op":{},"op":{},"admin_info":{"admin_id":7}}',
                    ' ' + 'x' * (evidence.MAX_OTHER_BYTES + 1)):
            bad = quota_add()
            bad['other'] = raw
            with self.subTest(size=len(raw)), self.assertRaises(evidence.EvidenceError):
                evidence.normalize_page(page([bad]), requested_page=1, requested_page_size=2)

    def test_page_requires_echoed_shape_and_exact_complete_row_count(self):
        valid = page([quota_add(10), quota_add(11)], total=3)
        result = evidence.normalize_page(valid, requested_page=1, requested_page_size=2)
        self.assertTrue(result.has_more)
        second = page([quota_add(12)], p=2, total=3)
        self.assertFalse(evidence.normalize_page(second, requested_page=2,
                                                 requested_page_size=2).has_more)
        malformed = [
            (dict(valid, page=2), 1, 2),
            (dict(valid, page_size=1), 1, 2),
            (dict(valid, items=valid['items'][:1]), 1, 2),  # truncated
            (dict(valid, items=valid['items'] + [quota_add(12)]), 1, 2),  # extra
            ({'p': 1, 'page_size': 2, 'total': 2, 'items': valid['items']}, 1, 2),
            (dict(valid, total=True), 1, 2),
        ]
        for data, p, size in malformed:
            with self.subTest(data=data), self.assertRaises(evidence.EvidenceError):
                evidence.normalize_page(data, requested_page=p, requested_page_size=size)

    def test_overlapping_pages_are_detectable_by_normalized_id(self):
        first = evidence.normalize_page(page([quota_add(10)], page_size=1, total=2),
                                       requested_page=1, requested_page_size=1)
        overlap = evidence.normalize_page(page([quota_add(10)], p=2, page_size=1, total=2),
                                          requested_page=2, requested_page_size=1)
        self.assertEqual(evidence.duplicate_log_ids([first, overlap]), frozenset({10}))
        self.assertEqual(evidence.duplicate_log_ids([first]), frozenset())

    def test_unsupported_type1_and_type4_are_retained_as_fixed_classifications(self):
        unsupported = quota_add()
        unsupported['other']['op'] = {'action': 'user.some_future_action'}
        ordinary_topup = {
            'id': 12, 'type': 1, 'user_id': 8, 'created_at': 1700000001,
            'request_id': 'redemption-1', 'other': {'content_parts': [
                {'key': 'Topped up {{quota}} with a redemption code, code ID {{id}}',
                 'params': {'quota': '1,000', 'id': 19}},
            ]},
        }
        # rc.44 DoCheckin -> RecordLog(LogTypeSystem, Message); its amount is
        # display text in content/content_parts, not a typed quota field.
        system_log = {
            'id': 11, 'type': 4, 'user_id': 8, 'created_at': 1700000002,
            'request_id': 'system-1', 'quota': 0,
            'content': 'Daily check-in, received 1,000',
            'other': {'content_parts': [
                {'key': 'Daily check-in, received {{quota}}',
                 'params': {'quota': '1,000'}},
            ]},
        }
        result = evidence.normalize_page(page([unsupported, ordinary_topup, system_log],
                                             page_size=3),
                                         requested_page=1, requested_page_size=3)
        self.assertEqual([i.classification for i in result.items],
                         ['unsupported_type1_operation', 'unsupported_type1_operation',
                          'type4_system'])
        self.assertIsNone(result.items[0].action)
        self.assertEqual(result.items[0].admin_id, 7)
        self.assertIsNone(result.items[1].admin_id)
        self.assertEqual((result.items[2].log_id, result.items[2].user_id,
                          result.items[2].created_at, result.items[2].request_id),
                         (11, 8, 1700000002, 'system-1'))
        self.assertIsNone(result.items[2].admin_id)
        self.assertFalse(hasattr(result.items[2], 'content'))

    def test_boolean_float_negative_and_int64_overflow_values_reject(self):
        cases = [
            ('id', True), ('user_id', 42.0), ('created_at', 2 ** 63),
            ('id', -(2 ** 63) - 1), ('type', False),
        ]
        for key, value in cases:
            row = quota_add()
            row[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(evidence.EvidenceError):
                evidence.normalize_page(page([row]), requested_page=1, requested_page_size=2)
        for key, value in (('requested_quota', -1), ('requested_quota', 2 ** 63),
                           ('requested_quota', True), ('quota', 1), ('from', 1.0),
                           ('to', -(2 ** 63) - 1)):
            row = quota_add()
            row['other']['op']['params'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(evidence.EvidenceError):
                evidence.normalize_page(page([row]), requested_page=1, requested_page_size=2)

    def test_request_id_sanitizer_rejects_credential_shaped_values(self):
        for value in ('nap_abcdefghijklmnopqrst', 'sk-' + 'x' * 24,
                      'Bearer secret', 'x' * 129, 'request\nforged'):
            row = quota_add()
            row['request_id'] = value
            with self.subTest(value=value), self.assertRaises(evidence.EvidenceError):
                evidence.normalize_page(page([row]), requested_page=1, requested_page_size=2)

    def test_bad_type1_envelope_fails_closed(self):
        for mutate in (
                lambda r: r.update(other={'op': 'malformed'}),
                lambda r: r.update(other={'op': {'action': 'user.quota_add'},
                                          'admin_info': {'admin_id': 7}}),
                lambda r: r['other'].update(admin_info={'admin_id': True}),
                lambda r: r['other'].pop('admin_info'),
                lambda r: r['other']['op']['params'].update(mode='subtract'),
                lambda r: r['other']['op']['params'].update(to=1562799),
                lambda r: r.update(user_id=43),
        ):
            row = copy.deepcopy(quota_add())
            mutate(row)
            with self.assertRaises(evidence.EvidenceError):
                evidence.normalize_page(page([row]), requested_page=1, requested_page_size=2)

    def test_type1_without_admin_or_operation_is_safe_unsupported_metadata(self):
        rows = [
            {'id': 20, 'type': 1, 'user_id': 50, 'created_at': 1700000003,
             'request_id': 'payment-1', 'other': {'content_parts': []}},
            {'id': 21, 'type': 1, 'user_id': 51, 'created_at': 1700000004,
             'request_id': 'legacy-1', 'other': ''},
        ]
        result = evidence.normalize_page(page(rows), requested_page=1, requested_page_size=2)
        self.assertEqual([item.classification for item in result.items],
                         ['unsupported_type1_operation', 'unsupported_type1_operation'])
        self.assertEqual([item.admin_id for item in result.items], [None, None])

    def test_exact_large_quota_delta_above_float_integer_precision(self):
        amount = 2 ** 53 + 1
        row = quota_add(30)
        params = row['other']['op']['params']
        params.update(requested_quota=amount, quota=amount, **{'from': 19, 'to': 19 + amount})
        item = evidence.normalize_page(page([row]), requested_page=1,
                                       requested_page_size=2).items[0]
        self.assertEqual(item.requested_quota, amount)
        self.assertEqual(item.quota_to - item.quota_from, amount)


if __name__ == '__main__':
    unittest.main()

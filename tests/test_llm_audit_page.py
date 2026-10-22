"""Standalone strict page contract tests; transport/ORM are not mocked as proof."""
import copy
import importlib.util
from pathlib import Path
import unittest
import uuid

spec = importlib.util.spec_from_file_location('audit_page', Path(__file__).parents[1] / 'src/services/llm_audit_page.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def event(seq, *, kind='intent', attempt=None):
    base = {'seq': seq, 'attempt_id': attempt or str(uuid.uuid4()), 'kind': kind,
            'at': '2026-10-21T12:34:56.123456+08:00'}
    if kind == 'intent':
        return dict(base, target_user_id=12, quota=15625)
    return dict(base, result='forwarded', status=200, request_id='safe-request-1')


def page(events, head=None):
    return {'env': 'llmdev', 'highwater': head if head is not None else len(events), 'events': events}


class TestAuditPage(unittest.TestCase):
    def test_contiguous_fixed_highwater_resume_and_precise_timestamp(self):
        head, rows = audit.normalize_page(page([event(1), event(2)], 3), 'llmdev', 0, limit=2)
        self.assertEqual(head, 3)
        self.assertEqual(rows[0]['at'], '2026-10-21T04:34:56.123456Z')
        head2, rows2 = audit.normalize_page(page([event(3)], 3), 'llmdev', 2, through=head, limit=2)
        self.assertEqual(head2, head)
        self.assertEqual(rows2[0]['seq'], 3)

    def test_int64_sequences_never_pass_through_float(self):
        start = 2 ** 53 + 1
        head, rows = audit.normalize_page(page([event(start + 1)], start + 1), 'llmdev', start)
        self.assertEqual(head, start + 1)
        self.assertEqual(rows[0]['seq'], start + 1)

    def test_no_events_is_valid_only_at_confirmed_highwater(self):
        self.assertEqual(audit.normalize_page(page([], 7), 'llmdev', 7, 7), (7, []))
        for head in (8, 6):
            with self.assertRaises(audit.AuditPageError):
                audit.normalize_page(page([], head), 'llmdev', 7)

    def test_gap_duplicate_reorder_and_short_page_fail_closed(self):
        for events in ([event(2)], [event(1), event(1)], [event(2), event(1)], [event(1)]):
            with self.subTest(events=events), self.assertRaises(audit.AuditPageError):
                audit.normalize_page(page(events, 2), 'llmdev', 0)

    def test_highwater_must_stay_fixed_and_environment_cannot_drift(self):
        for value, env, through in ((page([event(1)], 2), 'llmdev', 1),
                                    (page([event(1)]), 'aimodels', None)):
            with self.assertRaises(audit.AuditPageError):
                audit.normalize_page(value, env, 0, through)

    def test_intent_and_outcome_are_two_evidence_events_not_two_credits(self):
        attempt = str(uuid.uuid4())
        _, rows = audit.normalize_page(page([event(1, attempt=attempt), event(2, kind='outcome', attempt=attempt)]),
                                       'llmdev', 0)
        self.assertEqual([row['kind'] for row in rows], ['intent', 'outcome'])
        self.assertEqual(rows[1]['quota'], 0)
        for result in audit.RESULTS - {'forwarded'}:
            value = event(1, kind='outcome')
            value.update(result=result)
            del value['status'], value['request_id']
            self.assertEqual(audit.normalize_page(page([value]), 'llmdev', 0)[1][0]['result'], result)

    def test_malformed_schema_boolean_float_ids_and_secret_fields_rejected(self):
        original = event(1)
        mutations = ({'seq': True}, {'seq': 1.0}, {'quota': True}, {'quota': 15625.0},
                     {'target_user_id': 0}, {'attempt_id': str(uuid.uuid1())},
                     {'at': '2026-10-21T12:34:56'}, {'at': '2026-10-21T12:34:56.1234567Z'}, {'at': 'invalid'}, {'token': 'secret'})
        for mutation in mutations:
            value = copy.deepcopy(original)
            value.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(audit.AuditPageError):
                audit.normalize_page(page([value]), 'llmdev', 0)
        for head in (True, 1.0, 2 ** 63):
            with self.assertRaises(audit.AuditPageError):
                audit.normalize_page(page([original], head), 'llmdev', 0)

    def test_credential_shaped_request_and_invalid_outcome_never_persist(self):
        for mutation in ({'request_id': 'sk-secret'}, {'request_id': 'a.b.c'},
                         {'request_id': 'a' * 64}, {'status': True}, {'result': 'success'}):
            value = event(1, kind='outcome')
            value.update(mutation)
            with self.assertRaises(audit.AuditPageError):
                audit.normalize_page(page([value]), 'llmdev', 0)

    def test_duplicate_attempt_kind_is_rejected_and_input_is_immutable(self):
        attempt = str(uuid.uuid4())
        value = page([event(1, attempt=attempt), event(2, attempt=attempt)])
        before = copy.deepcopy(value)
        with self.assertRaises(audit.AuditPageError):
            audit.normalize_page(value, 'llmdev', 0)
        self.assertEqual(value, before)

    def test_bounds_unknown_payloads_and_invalid_environment(self):
        for payload in (None, {}, dict(page([], 0), extra=True), page([], '0')):
            with self.assertRaises(audit.AuditPageError):
                audit.normalize_page(payload, 'llmdev', 0)
        for env in ('', '../llmdev', 'x' * 65, None):
            with self.assertRaises(audit.AuditPageError):
                audit.normalize_page(page([], 0), env, 0)
        for after, limit in ((True, 100), (-1, 100), (0, 0), (0, 101)):
            with self.assertRaises(audit.AuditPageError):
                audit.normalize_page(page([], 0), 'llmdev', after, limit=limit)


if __name__ == '__main__':
    unittest.main()

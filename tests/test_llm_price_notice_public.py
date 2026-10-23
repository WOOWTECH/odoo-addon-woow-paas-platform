"""Public price-notice projection never returns publication or recipient metadata."""
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'llm_price_notice_public', ROOT / 'src/services/llm_price_notice_public.py')
notice_public = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = notice_public
SPEC.loader.exec_module(notice_public)


def notice():
    return {
        'id': 'pc_' + 'a' * 32, 'version': 'stg-v1', 'title': 'AI 模型價格調整公告',
        'body_text': 'Synthetic notice only', 'not_before': 1800000000,
        'changes': [{'model': 'vendor/model', 'mode': 'ratio',
                     'before': {'input': '1', 'output': '2', 'cache_read': '0.1', 'cache_write': '0.2'},
                     'after': {'input': '1.3', 'output': '2.6', 'cache_read': '0.13', 'cache_write': '0.26'}}],
    }


class TestPublicNoticeProjection(unittest.TestCase):
    def test_valid_notice_projects_only_public_fields(self):
        result = notice_public.project_public_notices([notice()])
        self.assertEqual(set(result[0]), {'id', 'version', 'title', 'body_text', 'not_before', 'changes'})
        self.assertEqual(result[0]['changes'][0]['after']['input'], '1.3')

    def test_private_fields_fail_closed_even_when_public_fields_are_valid(self):
        for extra in ({'recipients': ['legacy:owner']}, {'legal_approval_ref': 'private'},
                      {'destination_snapshot': 'private@example.test'}):
            with self.subTest(extra=list(extra)):
                self.assertEqual(notice_public.project_public_notices([{**notice(), **extra}]), [])

    def test_malformed_collection_denies_all_notices(self):
        for rows in (None, {}, [notice()] * 11, [notice(), notice()], [notice(), None]):
            with self.subTest(rows=type(rows).__name__):
                self.assertEqual(notice_public.project_public_notices(rows), [])

    def test_invalid_public_fields_deny(self):
        for patch in ({'id': 'someone'}, {'title': ''}, {'body_text': 'x' * 65537},
                      {'not_before': True}, {'not_before': 4102444801}, {'changes': []},
                      {'changes': [{'model': 'bad<script>', 'mode': 'fixed',
                                    'before': {'per_call': '1'}, 'after': {'per_call': '2'}}]}):
            with self.subTest(patch=list(patch)):
                self.assertEqual(notice_public.project_public_notices([{**notice(), **patch}]), [])

    def test_price_components_must_be_complete_nonnegative_decimal_strings(self):
        for rate in (None, True, 1, '-1', 'NaN', '1e3', '1.2.3', '0' * 65):
            row = notice()
            row['changes'][0]['after']['input'] = rate
            with self.subTest(rate=rate):
                self.assertEqual(notice_public.project_public_notices([row]), [])
        fixed = notice()
        fixed['changes'] = [{'model': 'vendor/fixed', 'mode': 'fixed',
                             'before': {'per_call': '1'}, 'after': {'per_call': '2'}}]
        self.assertEqual(notice_public.project_public_notices([fixed])[0]['changes'][0]['mode'], 'fixed')


if __name__ == '__main__':
    unittest.main()

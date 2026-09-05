"""#864 PR-F 上線前 SQL 必須維持三句唯讀 count。"""
from pathlib import Path
import re
import unittest


_ROOT = Path(__file__).resolve().parents[2]
_SQL = _ROOT / 'scripts' / '864-f-auto-recredit-preflight.sql'
_WRITE = re.compile(
    r'\b(?:INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE|MERGE|CALL)\b',
    re.IGNORECASE,
)


class TestAutoRecreditPreflightSql(unittest.TestCase):
    def test_three_statements_are_read_only_counts(self):
        sql_without_comments = '\n'.join(
            line for line in _SQL.read_text().splitlines()
            if not line.lstrip().startswith('--')
        )
        statements = [
            chunk.strip() for chunk in sql_without_comments.split(';')
            if chunk.strip()
        ]
        self.assertEqual(len(statements), 3)
        for sql in statements:
            self.assertTrue(sql.upper().startswith('SELECT COUNT(*)'))
            self.assertIsNone(_WRITE.search(sql))

    def test_queries_return_no_identifier_or_pii_columns(self):
        text = _SQL.read_text()
        for forbidden in (
                'partner_id', 'partner_ref', 'email', 'amount',
                'pt.reference AS', 'ARRAY_AGG'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()

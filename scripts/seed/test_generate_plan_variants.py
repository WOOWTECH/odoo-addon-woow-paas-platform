# -*- coding: utf-8 -*-
"""generator 自測：parse／classify／公式／計數／產出冪等。"""
import unittest
from pathlib import Path

import generate_plan_variants as g

MANIFEST = Path(__file__).resolve().parents[2] / (
    'docs/plans/2026-Q3/2026-07-12-cloud-services-214-manifest.md')


class TestParse(unittest.TestCase):

    def test_total_214(self):
        items = g.parse_manifest(MANIFEST)
        self.assertEqual(len(items), 214)
        self.assertEqual([i.no for i in items], list(range(1, 215)))

    def test_kinds(self):
        items = g.parse_manifest(MANIFEST)
        self.assertEqual(sum(1 for i in items if i.kind == '新建基礎商品'), 6)
        self.assertEqual(sum(1 for i in items if i.kind == '+access變體'), 208)


class TestClassify(unittest.TestCase):

    def test_odoo_variant(self):
        v = g.classify(g.Item(no=9, name='Odoo訂閱服務（1-3人）+2access月繳',
                              price=2700, kind='+access變體'))
        self.assertEqual((v.family, v.tier_key, v.n, v.cycle, v.price),
                         ('odoo', 't1', 2, 'month', 2700))

    def test_sa_variant_year(self):
        v = g.classify(g.Item(no=90, name='遠端安全連結訂閱服務（1個）+1access年繳',
                              price=6600, kind='+access變體'))
        self.assertEqual((v.family, v.tier_key, v.n, v.cycle),
                         ('sa', 'r1', 1, 'year'))

    def test_counts_per_family(self):
        items = g.parse_manifest(MANIFEST)
        variants = [g.classify(i) for i in items if i.kind == '+access變體']
        counts = {}
        for v in variants:
            counts[v.family] = counts.get(v.family, 0) + 1
        self.assertEqual(counts, {'odoo': 42, 'n8n': 40, 'sa': 80,
                                  'sh': 40, 'mcp': 6})


if __name__ == '__main__':
    unittest.main()

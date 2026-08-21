# -*- coding: utf-8 -*-
"""generator 自測：parse／classify／公式／計數／產出冪等。"""
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import generate_plan_variants as g

MANIFEST = Path(__file__).resolve().parents[2] / (
    'docs/plans/2026-Q3/2026-07-19-cloud-services-220-manifest.md')

# 年繳倍率：預設 ×8，sa ×9，mcp_hermes ×10（#765 拍板時明列的家族異例——
# 其 base 年繳本來就是月×10，加購側仍統一 +4800/席）。
YEAR_RATIO = {'sa': 9, 'mcp_hermes': 10}

# 哪些 template 會收到席數變體（集合等值＝同時是正向與負向對照）。
# 不在此集合內＝刻意不做加購：cloud_app_haos（#423 Q5）、cloud_app_ha_paas（免費）、
# cloud_app_postgresql／cloud_app_redis（未指定）。少一個或多一個都要紅。
EXPECTED_VARIANT_TEMPLATES = {
    'cloud_app_odoo', 'cloud_app_n8n', 'cloud_app_mcp_odoo',
    'cloud_app_mcp_odoo_manage', 'cloud_app_mcp_n8n', 'cloud_app_mcp_k8s',
    'cloud_app_mcp_vk', 'cloud_app_mcp_opendesign', 'cloud_app_opendesign',
    'cloud_app_hermes_agent', 'cloud_app_ha_full', 'cloud_app_mcp_hermes',
    'smart_home_home_assistant', 'security_access_default',
}

# #765 拍板的席數結構（每 tier 開到第幾席）。月／年兩軌各自要湊滿同一組。
EXPECTED_SEAT_LADDERS_765 = {
    ('hermes_agent', 'app'): [1],
    ('ha_full', 'app'): [1, 2, 3],
    ('mcp_hermes', 't1'): [1, 2, 3],
    ('mcp_hermes', 't2'): list(range(1, 7)),
    ('mcp_hermes', 't3'): list(range(1, 11)),
}


class TestParse(unittest.TestCase):

    def test_manifest_rows_are_contiguous(self):
        """manifest 編號 1..N 連續無跳號（#433：取代 assertEqual(len, 222)）。

        比原本的總數斷言更嚴：regex 漏 parse 中間任一列都會讓編號跳號而紅，
        總數斷言只在「剛好漏掉尾列」時才紅；且 manifest 合法成長時不必回頭改。
        """
        items = g.parse_manifest(MANIFEST)
        self.assertTrue(items, '前置：manifest 應解析出項目（防 regex 壞掉變空集合）')
        self.assertEqual(
            [i.no for i in items], list(range(1, len(items) + 1)),
            'manifest 編號應為 1..N 連續無跳號（跳號＝有列沒被 parse 到）')

    def test_kinds_partition_all_items(self):
        """已知的兩種 kind 恰好分割全部項目（#433：守分類無漏網，不守個別數量）。

        原本硬編 6／216 兩個計數；真正要守的是「沒有項目落在已知分類之外」——
        manifest 新增一列而 kind 欄打錯字時，原斷言會紅但訊息只說「數字不對」，
        本斷言直接把未知 kind 列出來。
        """
        items = g.parse_manifest(MANIFEST)
        bases = sum(1 for i in items if i.kind == '新建基礎商品')
        variants = sum(1 for i in items if i.kind == '+access變體')
        self.assertTrue(bases and variants, '前置：兩種 kind 都應有項目')
        self.assertEqual(
            bases + variants, len(items),
            '有項目的 kind 不屬已知兩類（分類漏網）：%s'
            % sorted({i.kind for i in items} - {'新建基礎商品', '+access變體'}))


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

    def test_mcp_hermes_variant(self):
        """#765：家族 prefix 帶結尾空格，marker 是「T1（1–3 人）」這種 tier 標。"""
        v = g.classify(g.Item(no=234, name='Hermes MCP Server T2（3–6 人）+4access年繳',
                              price=64200, kind='+access變體'))
        self.assertEqual((v.family, v.tier_key, v.n, v.cycle, v.price),
                         ('mcp_hermes', 't2', 4, 'year', 64200))

    def test_hermes_agent_is_not_mcp_hermes(self):
        """兩個 Hermes 商品的 prefix 不互相吃掉（#424 撞名風險的 generator 側）。"""
        agent = g.classify(g.Item(no=223, name='Hermes Agent 雲端訂閱服務+1access月繳',
                                  price=5100, kind='+access變體'))
        self.assertEqual((agent.family, agent.tier_key), ('hermes_agent', 'app'))
        self.assertEqual(g._template_xmlid(agent), 'cloud_app_hermes_agent')

    def test_counts_per_family(self):
        items = g.parse_manifest(MANIFEST)
        variants = [g.classify(i) for i in items if i.kind == '+access變體']
        counts = {}
        for v in variants:
            counts[v.family] = counts.get(v.family, 0) + 1
        self.assertEqual(counts, {'odoo': 42, 'n8n': 40, 'sa': 80,
                                  'sh': 40, 'mcp': 12, 'opendesign': 2,
                                  # #765：1 席 ×2 週期／3 席 ×2／(3+6+10) 席 ×2
                                  'hermes_agent': 2, 'ha_full': 6,
                                  'mcp_hermes': 38})


class TestEmit(unittest.TestCase):

    def setUp(self):
        items = g.parse_manifest(MANIFEST)
        self.variants = [g.classify(i) for i in items
                         if i.kind == '+access變體']

    def test_xml_record_count(self):
        # #433：一律綁 len(self.variants)（同 scope 就有），不留字面量。
        # 守線因此變嚴：輸入變體數改變時，本斷言仍要求「一變體恰一 plan 一 product」。
        xml = g.emit_variants_xml(self.variants)
        self.assertTrue(self.variants, '前置：應解析出變體')
        self.assertEqual(
            xml.count('model="sh.subscription.plan"'), len(self.variants))
        self.assertEqual(
            xml.count('model="product.product"'), len(self.variants))
        self.assertIn('GENERATED by scripts/seed/generate_plan_variants.py', xml)
        self.assertIn('noupdate="1"', xml)

    def test_xmlid_rule(self):
        xml = g.emit_variants_xml(self.variants)
        self.assertIn('id="plan_odoo_t2_ac3_monthly"', xml)
        self.assertIn('id="plan_security_access_r1_ac10_yearly"', xml)
        self.assertIn('id="product_smarthome_mcp_ac2_monthly"', xml)

    def test_all_variants_hidden_with_seats(self):
        xml = g.emit_variants_xml(self.variants)
        self.assertEqual(
            xml.count('name="storefront_hidden" eval="True"'),
            len(self.variants))
        self.assertIn('<field name="ac_max_access_seats">11</field>', xml)

    def test_links_append_not_replace(self):
        links = g.emit_links_xml(self.variants)
        self.assertNotIn('(6, 0', links, 'M2M 必須 (4,) append，嚴禁 (6,0) 覆寫 base 集')
        self.assertEqual(links.count('(4, ref('), len(self.variants))

    def test_links_file_noupdate_zero(self):
        """鐵律：links 檔必 noupdate=0（升級時隨 base (6,0) 重設後重新 append）；
        誤設 noupdate=1 → 變體連結在每次 -u 後靜默消失。"""
        links = g.emit_links_xml(self.variants)
        self.assertIn('noupdate="0"', links)
        self.assertNotIn('noupdate="1"', links)

    def test_variants_file_noupdate_one(self):
        """對照：plans/products 檔必 noupdate=1（record 欄位不被升級覆寫）。"""
        xml = g.emit_variants_xml(self.variants)
        self.assertIn('noupdate="1"', xml)

    def test_idempotent(self):
        self.assertEqual(g.emit_variants_xml(self.variants),
                         g.emit_variants_xml(self.variants))

    def test_year_ratio_formula(self):
        """機械公式：AC 單價 月 600／年 4800；年繳 base ×8（SA ×9、mcp_hermes ×10）。

        倍率例外收在 YEAR_RATIO 一張表，而不是散在條件式裡：家族異例只有寫成
        「這一格跟別人不同」才看得見；#765 加 mcp_hermes 時就是照這張表比對，
        沒有第二處要同步。
        """
        by_key = {(v.family, v.tier_key, v.n, v.cycle): v.price
                  for v in self.variants}
        for (fam, tier, n, cyc), price in by_key.items():
            if cyc == 'month':
                y = by_key.get((fam, tier, n, 'year'))
                self.assertIsNotNone(y, (fam, tier, n))
                base_m = price - n * 600
                base_y = y - n * 4800
                ratio = YEAR_RATIO.get(fam, 8)
                self.assertEqual(base_y, base_m * ratio, (fam, tier, n))

    def test_seat_ladders_765(self):
        """#765 拍板席數：Hermes Agent 固定 1／HA 完整版 1-3／MCP 隨 tier 放大。"""
        seats = {}
        for v in self.variants:
            key = (v.family, v.tier_key)
            if key in EXPECTED_SEAT_LADDERS_765:
                seats.setdefault((key, v.cycle), []).append(v.n)
        actual = {key + (cyc,): sorted(ns) for (key, cyc), ns in seats.items()}
        expected = {key + (cyc,): ladder
                    for key, ladder in EXPECTED_SEAT_LADDERS_765.items()
                    for cyc in ('month', 'year')}
        self.assertEqual(actual, expected)

    def test_variant_templates_exact_set(self):
        """哪些 template 收到變體＝集合等值（正向＋負向對照一次到位）。

        負向那半才是本條的重點：HAOS／HA PaaS／PostgreSQL／Redis 是**刻意**不做
        加購的，只斷言「三個新的有」會讓「順手把別人也掛上去」全綠。
        """
        actual = {g._template_xmlid(v) for v in self.variants}
        self.assertEqual(actual, EXPECTED_VARIANT_TEMPLATES)

    def test_hermes_families_never_cross_mount(self):
        """檔案層面：兩個 Hermes 商品的變體 xmlid 不得掛到對方的 template。

        對應 src/tests/test_hermes_agent_seed_424.py 的
        test_distinct_from_mcp_hermes_plans（DB 層），此處在 seed 產出檔上直接驗，
        不需要 Odoo 環境。
        """
        root = ET.fromstring(g.emit_links_xml(self.variants))
        mounted = {}
        for rec in root.findall('.//record'):
            field = rec.find("field[@name='subscription_plan_ids']")
            mounted[rec.get('id')] = [
                ref.split("'")[1]
                for ref in (field.get('eval') or '').split('ref(')[1:]]

        agent = mounted.get('cloud_app_hermes_agent') or []
        mcp = mounted.get('cloud_app_mcp_hermes') or []
        self.assertTrue(agent and mcp, '前置：兩個 Hermes template 都應掛到變體')
        self.assertTrue(
            all(x.startswith('plan_hermes_agent_') for x in agent),
            'cloud_app_hermes_agent 掛到非 Hermes Agent 的 plan：%s' % agent)
        self.assertTrue(
            all(x.startswith('plan_mcp_hermes_') for x in mcp),
            'cloud_app_mcp_hermes 掛到非 Hermes MCP 的 plan：%s' % mcp)
        # 反向：任一 Hermes 家族的 plan 都不得出現在其他 template 上。
        for tpl, plan_ids in mounted.items():
            for plan_id in plan_ids:
                if plan_id.startswith('plan_hermes_agent_'):
                    self.assertEqual(tpl, 'cloud_app_hermes_agent', plan_id)
                elif plan_id.startswith('plan_mcp_hermes_'):
                    self.assertEqual(tpl, 'cloud_app_mcp_hermes', plan_id)

    def test_unregistered_template_is_fail_loud(self):
        """漏登記 _TEMPLATE_ORDER 會 raise，不是靜默漏掉那一族的 (4,) append。"""
        model = 'woow_paas_platform.cloud_app_template'
        with self.assertRaises(ValueError):
            g._assert_templates_registered(
                {'cloud_app_not_in_order': (model, ['plan_x_ac1_monthly'])})
        # 陽性對照：已登記的 template 不得誤爆（否則上面那條 raise 沒有鑑別力）。
        g._assert_templates_registered(
            {'cloud_app_odoo': (model, ['plan_odoo_ac1_monthly'])})


class TestEmitTestsPy(unittest.TestCase):
    """emit_tests_py 產出的生成測試檔基本面（強化，非 plan 最小集）。"""

    def setUp(self):
        items = g.parse_manifest(MANIFEST)
        self.variants = [g.classify(i) for i in items
                         if i.kind == '+access變體']

    def test_valid_python_and_counts(self):
        src = g.emit_tests_py(self.variants)
        compile(src, 'test_plan_variants_generated.py', 'exec')  # 語法必合法
        self.assertIn('GENERATED by scripts/seed/generate_plan_variants.py', src)
        # #433：原為 assertIn('267', src)——那個 267（base 51＋變體 216）寫死在
        # _TESTS_TEMPLATE 裡，而 51 根本在 generator 的輸入域外，撞紅時重跑
        # generator 是 no-op。現在反過來守「樣板的計數斷言必須綁 len(VARIANTS)」，
        # 讓字面量無法回流。
        self.assertIn(
            'len(VARIANTS)', src,
            '產出檔的計數斷言必須綁 len(VARIANTS)，不得留計數字面量（#433）')
        self.assertEqual(src.count("'xmlid':"), len(self.variants))

    def test_idempotent(self):
        self.assertEqual(g.emit_tests_py(self.variants),
                         g.emit_tests_py(self.variants))


if __name__ == '__main__':
    unittest.main()

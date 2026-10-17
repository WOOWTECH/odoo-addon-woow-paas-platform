"""X-LC-001-1／-2／-5：應用市集搜尋把 % _ 當萬用字元、標籤搜不到、空分類頁籤。

M.1  純函式：% _ \\ " 只代表自己；比對名稱、說明、每個標籤；不分大小寫。
M.2  端點：搜 % 與 _ 不再回全部範本（只回真的含該字元的，種子資料裡是 0 筆）。
M.3  端點：卡片上看得到、但名稱／說明裡沒有的標籤，搜得到那張卡。
M.4  端點：回應帶 ``categories``＝目前有公開範本的分類（不受 category／search 篩選影響），
     前端據此產生分類頁籤，不再列出點進去是空的「資料庫」「數據分析」。
"""

import json

from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..services.marketplace_search import (
    CATEGORY_ORDER, ordered_categories, parse_tags, template_matches_search,
)


class TestMarketplaceSearchHelpers(TransactionCase):

    def test_wildcards_are_literal(self):
        for needle in ('%', '_', 'a%d', 'o_o', '\\', '"', '[', ','):
            with self.subTest(needle=needle):
                self.assertFalse(template_matches_search(
                    'Odoo ERP', 'All-in-one business suite', ['odoo', 'erp'], needle))
        self.assertTrue(template_matches_search('100% uptime', '', [], '%'))
        self.assertTrue(template_matches_search('snake_case', '', [], '_'))

    def test_matches_name_description_and_each_tag(self):
        tags = ['code-server', 'vscode', 'ide']
        self.assertTrue(template_matches_search('Code Server', 'Browser IDE', tags, 'vscode'))
        self.assertTrue(template_matches_search('Code Server', 'x', tags, 'IDE'))
        self.assertTrue(template_matches_search('Code Server', 'x', tags, ' server '))
        self.assertTrue(template_matches_search('Code Server', 'browser ide', [], 'Browser'))
        self.assertFalse(template_matches_search('Code Server', 'x', tags, 'workflow'))
        self.assertTrue(template_matches_search('Code Server', 'x', tags, ''), '空字串＝不篩選')
        self.assertTrue(template_matches_search('Code Server', 'x', tags, None))

    def test_parse_tags(self):
        self.assertEqual(parse_tags('["a", "b"]'), ['a', 'b'])
        self.assertEqual(parse_tags(''), [])
        self.assertEqual(parse_tags(False), [])
        self.assertEqual(parse_tags('not json'), [])
        self.assertEqual(parse_tags('{"a": 1}'), [])

    def test_ordered_categories(self):
        self.assertEqual(ordered_categories(['mcp', 'ai_llm', 'mcp', False, 'zzz', 'web']),
                         ['ai_llm', 'web', 'mcp', 'zzz'])

    def test_category_order_matches_model_selection(self):
        selection = [key for key, _label in
                     self.env['woow_paas_platform.cloud_app_template']._fields['category'].selection]
        self.assertEqual(list(CATEGORY_ORDER), selection,
                         'CATEGORY_ORDER 要與 cloud_app_template.category 的 selection 同步')


@tagged('post_install', '-at_install')
class TestMarketplaceSearchEndpoint(HttpCase):

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.visible = self.env['woow_paas_platform.cloud_app_template'].search(
            [('is_active', '=', True), ('is_hidden', '=', False)])
        self.assertTrue(self.visible, '前提：市集至少有一個公開範本')

    def _list(self, **params):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
        resp = self.url_open('/api/cloud/templates', data=body,
                             headers={'Content-Type': 'application/json'})
        result = resp.json()['result']
        self.assertTrue(result.get('success'), result)
        return result

    def _expected_ids(self, needle):
        return {t.id for t in self.visible
                if template_matches_search(t.name, t.description, parse_tags(t.tags), needle)}

    def test_M2_wildcards_do_not_list_everything(self):
        total = len(self._list()['data'])
        self.assertEqual(total, len(self.visible))
        for needle in ('%', '_', '%%', '\\'):
            with self.subTest(needle=needle):
                ids = {item['id'] for item in self._list(search=needle)['data']}
                self.assertEqual(ids, self._expected_ids(needle))
                self.assertLess(len(ids), total, f'搜 {needle!r} 不可列出全部範本')

    def test_M3_visible_tag_is_searchable(self):
        probe = None
        for tmpl in self.visible:
            for tag in parse_tags(tmpl.tags):
                text = f'{tmpl.name} {tmpl.description or ""}'.casefold()
                if tag and tag.casefold() not in text:
                    probe = (tmpl, tag)
                    break
            if probe:
                break
        if not probe:
            self.skipTest('種子資料沒有「只出現在標籤」的字')
        tmpl, tag = probe
        ids = {item['id'] for item in self._list(search=tag)['data']}
        self.assertIn(tmpl.id, ids, f'{tmpl.name} 的標籤 {tag!r} 搜不到自己')
        self.assertEqual(ids, self._expected_ids(tag))

    def test_M4_categories_only_lists_categories_with_public_templates(self):
        expected = ordered_categories(self.visible.mapped('category'))
        result = self._list()
        self.assertEqual(result['categories'], expected)
        # 篩選不影響分類頁籤（否則點了一個分類，其他頁籤就消失）。
        some = expected[0]
        self.assertEqual(self._list(category=some)['categories'], expected)
        self.assertEqual(self._list(search='zzz-no-such-template')['categories'], expected)
        empty = set(CATEGORY_ORDER) - set(expected)
        self.assertFalse(empty & set(result['categories']))

# -*- coding: utf-8 -*-
"""#368 PR-1：後端 additive-only（group_key + specs）。

- TestGroupKeys368：對「可見且掛 template」的 base 全量斷言 _group_keys()——
  每個 group_key 恰有 month+year 兩筆，且與 base xmlid 的 cycle 尾綴交叉一致
  （防手改 seed XML 打錯 cycle）。
- TestDisplaySpecs368：_get_display_specs 依 service_type 分流，含 CS
  cs_vcpu=0 時 fallback template.min_vcpu 的守線（postgresql base）。
- TestEndpointOrderUnchanged368：neutrality guard——controller 加了
  group_key/specs 兩鍵後，可見 base 的回傳順序仍與原生 recordset filtered
  順序（未經任何 .sorted()）完全一致。

設計：本 PR 的 brief（scratchpad/pr1-brief.md）＋ #368 選擇器重設計母案。
Refs #368 #433
"""
import os
import re
import xml.etree.ElementTree as ET

from odoo.tests.common import HttpCase, TransactionCase, tagged

MOD = 'woow_paas_platform'

_LINKS_XML = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data',
    'subscription_template_links.xml')

_CYCLE_SUFFIX_RE = re.compile(r'_(monthly|yearly)$')

# 刻意留空：曾有 6 筆 hermes base 在此（#336 Wave A 隱藏 → #431 解封）。
# 未來若要再隱藏「已掛 template 的 base」，必須顯式加進這裡並在 PR 說明理由——
# 讓「刻意隱藏」變成可 review 的動作，而不是靠一個浮動計數默默表達。
_INTENTIONALLY_HIDDEN = frozenset()


def _seeded_link_refs():
    """直接解析 subscription_template_links.xml，回「seed 掛上 template 的
    base plan xmlid」集合。#433：取代硬編計數。

    ⚠️ 這招只對 links.xml 成立，**絕不可照抄到 subscription_products*.xml**：
      - links.xml 是 ``<data noupdate="0">`` ＋ 全部 ``eval="[(6, 0, [...])]"``
        全量 reset，每次 ``-u`` 都把 DB 的 M2M 重寫成檔案內容 ⇒ DB 保證鏡射檔案，
        fresh install 與升級兩軌都成立。
      - subscription_products*.xml 是 ``noupdate="1"``，升級軌上「從 XML 刪掉的
        record 仍留在 DB」是正常的 ⇒ XML 數 ≠ DB 數，照抄會在 ``-u`` 軌假紅。
        該側的正確寫法見 test_subscription_tax_clear_366.py 的
        TestSeedProductCorpusIntact（斷言「零丟棄」而非數量）。

    比對的另一半（DB 的 `storefront_hidden` / `ac_max_access_seats`）確實來自
    `noupdate="1"` 的 products.xml，升級軌不會被 `-u` 覆寫——本 repo 的既有慣例是
    「改這些語意欄位就補 migration」（#336 的 `18.0.1.0.54`、#431 的
    `18.0.1.0.79`）。因此本斷言在升級軌上**順帶守住了那條慣例**：改了 seed 的
    可見性卻忘了補 migration，fresh 綠而 `-u` 紅——那正是該紅的（兩軌行為分歧）。
    """
    root = ET.parse(_LINKS_XML).getroot()
    refs = set()
    for rec in root.findall('.//record'):
        for field in rec.findall('field'):
            if field.get('name') == 'subscription_plan_ids':
                refs |= set(
                    re.findall(r"ref\('([^']+)'\)", field.get('eval') or ''))
    return refs


@tagged('post_install', '-at_install')
class TestGroupKeys368(TransactionCase):
    """可見且掛 template 的 base 兩兩配對（month+year），group_key 與 xmlid 一致。"""

    def _visible_templated_bases(self):
        Plan = self.env['sh.subscription.plan']
        bases = self.env['woow_paas_platform.cloud_app_template'].search(
            []).subscription_plan_ids
        bases |= self.env[
            'woow_paas_platform.smart_home_template'].search(
                []).subscription_plan_ids
        bases |= self.env[
            'woow_paas_platform.security_access_template'].search(
                []).subscription_plan_ids
        return bases.filtered(
            lambda p: p.ac_max_access_seats == 0 and not p.storefront_hidden)

    def test_group_key_pairs_month_and_year(self):
        bases = self._visible_templated_bases()
        # #433：不再硬編計數（原為 34，8 天內漂到 48）。改成對 seed 檔做集合等值——
        # 新增 template／增減 tier 完全不必回頭改本測試，而「link 被清空、base 被
        # 誤 hide、ac_max_access_seats 誤設、migration 誤刪」立刻紅；這些正是任何
        # 數值斷言（不論 == 還是 >=）全部漏掉的 regression。
        # 刻意不用 >=：seed 並非只增不減，d13e4602（#336）就在主線上零 record
        # 增刪、純翻 storefront_hidden 讓可見 base 從 32 掉到 26。
        expected = _seeded_link_refs() - _INTENTIONALLY_HIDDEN
        self.assertTrue(
            expected, '前置：links XML 應解析出 base plan（防解析器壞掉變空集合）')
        self.assertTrue(
            bases, '前置：可見 base 不得為空（否則以下斷言全部 vacuously pass）')

        group_keys = bases._group_keys()
        self.assertEqual(
            set(group_keys.keys()), set(bases.ids),
            '每個可見 base 都應有 group_key（零略過）')

        # 獨立反查 xmlid（不透過 _group_keys 內部邏輯），交叉驗證 cycle 一致，
        # 防止手改 seed XML 把 monthly plan 的 sh_unit 打成 year 之類的錯誤。
        imd = self.env['ir.model.data'].sudo()
        rows = imd.search([
            ('model', '=', 'sh.subscription.plan'),
            ('module', '=', MOD),
            ('res_id', 'in', bases.ids),
        ])
        xmlid_by_res = {row.res_id: row.name for row in rows}

        missing = sorted(b.id for b in bases if b.id not in xmlid_by_res)
        self.assertFalse(missing, 'base 缺 xmlid：%s' % missing)
        actual = {xmlid_by_res[base.id] for base in bases}
        self.assertEqual(
            actual, expected,
            'seed 掛載的可見 base 與 subscription_template_links.xml 不一致')

        by_group = {}
        for base in bases:
            xmlid = xmlid_by_res.get(base.id)
            self.assertTrue(xmlid, 'base id=%s 缺 xmlid' % base.id)
            if xmlid.endswith('_monthly'):
                cycle, expected_unit = 'monthly', 'month'
            elif xmlid.endswith('_yearly'):
                cycle, expected_unit = 'yearly', 'year'
            else:
                self.fail('base xmlid %s 無 _monthly/_yearly 尾綴' % xmlid)
            self.assertEqual(
                base.sh_unit, expected_unit,
                'xmlid %s 尾綴 cycle 與 sh_unit=%s 不一致' % (xmlid, base.sh_unit))
            by_group.setdefault(group_keys[base.id], set()).add(cycle)

        # #433：原為 assertEqual(len(by_group), 17)。改成結構關係後守線反而更嚴：
        #   by_group 每個 base 恰 setdefault 一次 ⇒ Σ|group| == len(bases)；
        #   下方迴圈斷言每 group 的 cycles == {'monthly','yearly'} ⇒ 每 group ≥ 2。
        #   「平均恰 2」∧「每個 ≥ 2」⇒「每個恰 2」。
        # 原本的 == 17 反而放行「某 group 3 筆（2 monthly + 1 yearly）」這種畸形
        # ——cycles 是 set，重複的 monthly 會被靜默吃掉。
        expected_groups = {_CYCLE_SUFFIX_RE.sub('', ref) for ref in expected}
        self.assertEqual(
            set(by_group), expected_groups,
            'group_key 集合應等於 seed link 的 tier 集合')
        self.assertEqual(
            len(by_group) * 2, len(bases), '每個 tier 恰 month+year 兩筆')
        for gk, cycles in by_group.items():
            self.assertEqual(
                cycles, {'monthly', 'yearly'},
                'group_key %s 應恰有 month+year 兩筆，實際 %s' % (gk, cycles))


@tagged('post_install', '-at_install')
class TestDisplaySpecs368(TransactionCase):
    """_get_display_specs 依 service_type 分流，含 CS fallback 守線。"""

    def test_display_specs_by_service_type(self):
        # (a) odoo base：CS，cs_vcpu 有設定值 → specs 含 vCPU
        odoo_tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        odoo_plan = self.env.ref('%s.plan_odoo_monthly' % MOD)
        odoo_specs = odoo_plan._get_display_specs(odoo_tpl)
        self.assertTrue(
            any('vCPU' in s['label'] for s in odoo_specs),
            'odoo base 應顯示 vCPU 規格：%s' % odoo_specs)

        # (b) postgresql base：cs_vcpu=0（seed 未設）→ 必須 fallback
        # template.min_vcpu，否則卡片空白（本 PR 的關鍵守線）。
        pg_tpl = self.env.ref('%s.cloud_app_postgresql' % MOD)
        pg_plan = self.env.ref('%s.plan_postgresql_monthly' % MOD)
        self.assertEqual(
            pg_plan.cs_vcpu, 0, '前置：postgresql base cs_vcpu 未設＝0')
        pg = pg_plan._get_display_specs(pg_tpl)
        self.assertTrue(
            any('vCPU' in s['label'] for s in pg),
            'postgresql base cs_vcpu=0 應 fallback template.min_vcpu，'
            '非空白：%s' % pg)

        # (c) SA r1 base：security_access → specs 含 routes
        sa_plan = self.env.ref('%s.plan_security_access_r1_monthly' % MOD)
        sa_specs = sa_plan._get_display_specs()
        self.assertTrue(
            any('route' in s['label'] for s in sa_specs),
            'SA r1 base 應顯示 routes 規格：%s' % sa_specs)

        # (d) SH p2（mcp_integration 非空）→ 含 MCP；p1（無 mcp）→ []
        sh_p2 = self.env.ref('%s.plan_smarthome_mcp_monthly' % MOD)
        sh_p2_specs = sh_p2._get_display_specs()
        self.assertTrue(
            any('MCP' in s['label'] for s in sh_p2_specs),
            'SH p2（mcp）base 應顯示 MCP 規格：%s' % sh_p2_specs)

        sh_p1 = self.env.ref('%s.plan_smarthome_monthly' % MOD)
        sh_p1_specs = sh_p1._get_display_specs()
        self.assertEqual(sh_p1_specs, [], 'SH p1（無 mcp）base 規格應為 []')


@tagged('post_install', '-at_install')
class TestEndpointOrderUnchanged368(HttpCase):
    """Neutrality guard：加 group_key/specs 後，controller 回傳序不得被重排。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')

    def _result(self, url, params='{}'):
        resp = self.url_open(
            url,
            data='{"jsonrpc":"2.0","method":"call","params":' + params + '}',
            headers={'Content-Type': 'application/json'})
        body = resp.json()
        self.assertIn('result', body, 'resp: %s' % body)
        return body['result']

    def test_endpoint_order_and_keys_unchanged(self):
        tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        r = self._result('/api/cloud-services/templates/%s/plans' % tpl.id)
        self.assertTrue(r['success'], r)

        returned_ids = [row['id'] for row in r['data']]
        # 原生 recordset 序（未經任何 sort）＝ neutrality 基準。刻意不比較
        # sorted('id')：M2M 序未必＝id 升序，比對 sorted 值證不到 controller
        # 真的沒重排，只有比對「原生 filtered 序」才鎖得住 controller 沒
        # 誤加 .sorted()（例如誤用 group_key 字串排序）。
        native_ids = tpl.sudo().subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden).ids
        self.assertGreater(
            len(native_ids), 1, '前置：odoo template 應有多個可見 base')
        self.assertEqual(
            returned_ids, native_ids,
            'controller 回傳序應與原生 recordset filtered 序完全一致'
            '（不得 .sorted()）')

        for row in r['data']:
            self.assertIn(
                'group_key', row, 'base id=%s 缺 group_key 鍵' % row['id'])
            self.assertIn(
                'specs', row, 'base id=%s 缺 specs 鍵' % row['id'])

# -*- coding: utf-8 -*-
"""#552 層 1 守線：seed template 的 helm_chart_version 不得留空。

## 守的是什麼

`CloudAppTemplate.helm_chart_version` 留空時，整條 helm 管線不會帶 helm 的 version
旗標（閘門在 src/services/paas_operator.py:419-420、
extra/paas-operator/src/services/helm.py:681 與 :838），helm 會解析成 registry
當下的 latest ⇒ 版本決定權交給上游、帳本記空、漂移偵測（cloud_service.py:630 的
target_chart_version 比較要求兩邊都有值）結構上看不到。

開單當時 postgresql / n8n / redis / odoo 四個 seed 是空字串（不是 NULL），且四個
全部是第三方公開 registry（bitnamicharts、8gears）——有釘版號的 11 個全是自家
jcr-prod。相關性 100%。

## 為什麼寫成「掃全部 seed」而不是「檢查那四個」

寫成硬編四個 slug 的清單，日後新增一個沒填版本的 template 照樣全綠——迴圈只是
少跑一輪。這裡改成從 ir.model.data 反查**本模組 seed 出來的全部** template，
未來新增的自動納入，無法靠改清單繞過。

配套的 floor 斷言（>= 15 筆、且四個已知 slug 都在集合內）是這條檢查自己的陽性
對照：查詢條件寫錯而回空集合時，迴圈跑 0 輪會假綠，floor 會先變紅。

## 版本值的出處

見 src/data/cloud_app_templates.xml 各 record 上方註解（2026-08-03 以
`helm show chart <oci-ref>` 實測 latest ＋ `helm list` 實測既存服務交叉決定；
helm 3.13.3〔operator Dockerfile 釘的同一顆〕與系統 helm 4.2.3 量到同值）。

⚠ 下方 EXPECTED_PINS 是**釘版契約**，不是「當下 latest 的快照」。上游發新版時
本測試理應維持綠——若要升版，那是一次獨立的、要對照既存服務評估的變更，屆時
同一次 commit 改 seed 與本表。**不得**為了讓 CI 綠而放寬成「只檢查非空」。
"""
from odoo.tests.common import TransactionCase, tagged


#: 本模組 seed 出來的 cloud_app_template 數量下限（2026-08-03 實為 15）。
#: 純粹是本檔查詢的陽性對照——查詢寫錯回空集合時先讓它變紅，而不是靜默跑 0 輪。
SEED_TEMPLATE_FLOOR = 15

#: #552 本次補上的四個釘版契約（xmlid -> 版本）。
#: #1130：cloud_app_odoo 從 Bitnami odoo 28.2.10 換成自家 woow-odoo chart 0.2.3
#: （woow-paas/woow-paas-charts#81）；2026-10-04 升 0.2.7（備份 Job 起跑等 PG，woow-paas-charts#136）。
EXPECTED_PINS = {
    'cloud_app_postgresql': '18.8.5',
    'cloud_app_n8n': '2.0.1',
    'cloud_app_redis': '27.0.18',
    'cloud_app_odoo': '0.2.7',
}

#: 仍在第三方 registry 的那幾個（「版本決定權在上游」的釘版理由只適用它們）。
#: #1130 起 cloud_app_odoo 搬到自家 jcr-prod，移出本集合。
THIRD_PARTY_PINS = ('cloud_app_postgresql', 'cloud_app_n8n', 'cloud_app_redis')


@tagged('post_install', '-at_install')
class TestSeedChartVersionPin552(TransactionCase):

    def _seeded_templates(self):
        """本模組 seed 出來的全部 cloud_app_template，回 {xmlid_name: record}。

        用 ir.model.data 反查而非 search([])，才不會把其他測試 create 出來的
        暫時性 template 一起算進來（那些沒有 xmlid、也不是本守線的對象）。
        """
        data = self.env['ir.model.data'].sudo().search([
            ('module', '=', 'woow_paas_platform'),
            ('model', '=', 'woow_paas_platform.cloud_app_template'),
        ])
        return {
            d.name: self.env['woow_paas_platform.cloud_app_template'].browse(d.res_id)
            for d in data
        }

    def test_seed_query_reaches_the_whole_set(self):
        """陽性對照：確認上面那個查詢真的撈得到 seed，否則後面兩條都零鑑別力。"""
        seeded = self._seeded_templates()
        self.assertGreaterEqual(
            len(seeded), SEED_TEMPLATE_FLOOR,
            f'只查到 {len(seeded)} 筆 seed template（下限 {SEED_TEMPLATE_FLOOR}）——'
            '查詢條件壞了，本檔其餘斷言會退化成恆真')
        missing = set(EXPECTED_PINS) - set(seeded)
        self.assertFalse(
            missing, f'查詢沒撈到本單直接處理的 seed：{sorted(missing)}')

    def test_no_seeded_template_leaves_chart_version_blank(self):
        """#552 的不變式：沒有任何 seed template 可以留空 helm_chart_version。

        留空 ⇒ helm 不帶 version 旗標 ⇒ 抓 registry 當下 latest。新增 template
        時忘了填也會在這裡變紅。
        """
        seeded = self._seeded_templates()
        self.assertGreaterEqual(len(seeded), SEED_TEMPLATE_FLOOR)  # floor 折進同一條
        blank = sorted(
            name for name, tpl in seeded.items()
            if not (tpl.helm_chart_version or '').strip()
        )
        self.assertFalse(
            blank,
            'seed template 的 helm_chart_version 留空 ⇒ helm 管線不帶 version 旗標、'
            f'部署時抓 registry 當下 latest（Refs #552）：{blank}')

    def test_pinned_versions_match_measured_values(self):
        """四個釘版契約值。改版時本表與 seed 必須同一次 commit 一起改。"""
        seeded = self._seeded_templates()
        actual = {
            name: (seeded[name].helm_chart_version or '').strip()
            for name in EXPECTED_PINS if name in seeded
        }
        self.assertEqual(
            actual, EXPECTED_PINS,
            '釘版值與 src/data/cloud_app_templates.xml 不一致——若是刻意升版，'
            '請同一次 commit 更新本表並在 PR 說明依據（實測指令＋日期）')

    def test_third_party_charts_are_the_ones_being_pinned(self):
        """前提斷言：這三個確實是第三方 registry 的 chart。

        本單的嚴重性來自「版本決定權在上游」。若哪天它們搬到自家 jcr-prod，
        上面的釘版理由就換了一套，應該回來重讀而不是照抄（cloud_app_odoo 已於 #1130
        搬走，見下一條）。
        """
        seeded = self._seeded_templates()
        self.assertLessEqual(set(THIRD_PARTY_PINS), set(EXPECTED_PINS))
        for name in THIRD_PARTY_PINS:
            chart = seeded[name].helm_chart_name or ''
            self.assertNotIn(
                'jcr-prod.woowtech.io', chart,
                f'{name} 已搬到自家 registry，本檔的釘版理由需重新評估（Refs #552）')
            self.assertTrue(
                chart.startswith('oci://'),
                f'{name} 的 helm_chart_name 不是 OCI ref：{chart!r}')

    def test_odoo_moved_to_the_in_house_woow_odoo_chart(self):
        """#1130：cloud_app_odoo 釘的是自家 woow-odoo chart，不再是 Bitnami。

        換 chart 而沒換版號（或反過來）會讓 helm 拿 Bitnami 的版號去拉 woow-odoo、
        或用 woow-odoo 版號去拉 Bitnami——兩者都在 install 當下才炸。座標與版號鎖在同
        一條。
        """
        odoo = self._seeded_templates()['cloud_app_odoo']
        self.assertEqual(
            odoo.helm_chart_name,
            'oci://jcr-prod.woowtech.io/woow-paas-docker-local/woow-odoo')
        self.assertTrue(odoo._is_woow_odoo_chart())

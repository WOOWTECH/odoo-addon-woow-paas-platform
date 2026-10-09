"""#1612：範本 ``helm_value_specs`` 的欄位名稱（label）一律繁體中文。

2026-10-08 正式站重測 #1561 時發現：Odoo 範本配置頁的「Administrator Email」「Load Demo
Data」欄位名稱還是英文（旁邊的說明已是中文），reconfigure 硬送時的拒絕訊息也帶英文名稱。

spec label 不經翻譯檔：/woow 建立表單（HelmValueForm）、配置頁（ConfigurationTab）與後端
拒絕訊息（``PaasController._drop_create_only_values``）都直接讀 seed 的 label。所以作法
比照 #1584 與其他較新的範本——seed 直接寫中文，改一處、三處一致（拒絕訊息的斷言在
``test_create_only_settings_1561``）。

守線：本模組 seed 的每個範本，spec label 都要含中文；產品名／協定名（``KEEP_ENGLISH``）
刻意保留，新增例外時寫明理由。
"""
import re

from odoo.tests.common import TransactionCase, tagged

_CJK = re.compile(r'[\u4e00-\u9fff]')

# 刻意保留英文的 label：產品、功能或協定名稱，翻了反而對不上外部文件與後台。
KEEP_ENGLISH = {
    'Git Token',                    # code-server；同範本另有「Git Token 適用主機」，沿用同一詞
    'ngrok Authtoken',              # ngrok 後台的欄位名稱，照抄方便租戶對照
    'MagicDNS',                     # Tailscale／Headscale 的功能名稱
    'MQTT over WebSocket（8083）',  # 協定名稱
}


def _seeded_templates(env):
    imd = env['ir.model.data'].sudo().search([
        ('module', '=', 'woow_paas_platform'),
        ('model', '=', 'woow_paas_platform.cloud_app_template'),
    ])
    return env['woow_paas_platform.cloud_app_template'].sudo().browse(
        imd.mapped('res_id')).exists()


def _specs(template):
    specs = template._parse_helm_value_specs()
    return [s for section in ('required', 'optional')
            for s in (specs.get(section) or []) if isinstance(s, dict)]


@tagged('post_install', '-at_install')
class TestSpecLabelsZh1612(TransactionCase):

    def test_odoo_create_only_labels_are_chinese(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        specs = {s['key']: s for s in _specs(tpl)}
        self.assertEqual(specs['conf.adminEmail']['label'], '管理員 Email')
        self.assertEqual(specs['loadDemoData']['label'], '載入示範資料')
        self.assertRegex(specs['conf.adminEmail']['description'], _CJK)
        # 配置頁唯讀列出、拒絕訊息引用的就是這兩個 label。
        self.assertEqual(
            {k: s['label'] for k, s in tpl._create_only_value_specs().items()},
            {'conf.adminEmail': '管理員 Email', 'loadDemoData': '載入示範資料'})

    def test_seeded_spec_labels_are_chinese(self):
        templates = _seeded_templates(self.env)
        self.assertTrue(templates, '範本 seed 未載入')
        offenders = []
        for tpl in templates:
            for spec in _specs(tpl):
                label = spec.get('label') or ''
                if label in KEEP_ENGLISH or _CJK.search(label):
                    continue
                offenders.append(f'{tpl.slug}: {spec.get("key")} → {label!r}')
        self.assertFalse(
            offenders,
            'spec label 還是英文（租戶在 /woow 會看到）——seed 直接改中文；'
            '產品／協定名才加進 KEEP_ENGLISH：\n' + '\n'.join(offenders))

    def test_keep_english_entries_are_still_in_use(self):
        """例外清單不留過期項目：每一條都還對得到某個 seed label。"""
        labels = {spec.get('label') for tpl in _seeded_templates(self.env)
                  for spec in _specs(tpl)}
        self.assertFalse(KEEP_ENGLISH - labels,
                         f'KEEP_ENGLISH 有用不到的項目：{sorted(KEEP_ENGLISH - labels)}')

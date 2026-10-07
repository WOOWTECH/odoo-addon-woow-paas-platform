"""#1535：「自訂網域」按鈕與 CNAME 教學先對租戶隱藏（功能從未實作）。

正式站 .210 實測（uid 205）：服務頁按「自訂網域」→ 填網域 → 存檔一律回
「未授權的配置鍵：custom_domain」。原因不只是 #1489 把 helm values 白名單改成
fail-closed——``cloud_service.custom_domain`` 全 repo 沒有任何寫入，operator 與
Cloudflare tunnel 也都沒讀它，#1489 之前「成功」也只是把垃圾鍵寫進 helm_values。

本檔以靜態讀檔守前端契約（沿用 ``test_admin_credentials_tenant_set`` 的前端原始碼
契約慣例）：

* 服務頁的「Custom Domain」按鈕與 EditDomainModal 的掛載都掛在
  ``customDomainSupported`` 之後；
* 該 getter 目前固定回 ``false``；
* ``showDomainModal`` 本身也先檢查它（深層呼叫不會繞過）；
* EditDomainModal 元件保留（程式不刪，日後實作時沿用）。

後端 ``_update_service`` 照舊拒絕 ``custom_domain``（``test_helm_values_fail_closed``
系列已鎖住），本檔不重複。
"""
import os
import re

from odoo.tests.common import TransactionCase, tagged

_PAAS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'src', 'paas')


def _read(*parts):
    path = os.path.join(_PAAS, *parts)
    with open(path, encoding='utf-8') as handle:
        return handle.read()


@tagged('post_install', '-at_install')
class TestCustomDomainHidden1535(TransactionCase):

    def setUp(self):
        super().setUp()
        self.page_js = _read('pages', 'service', 'ServiceDetailPage.js')
        self.page_xml = _read('pages', 'service', 'ServiceDetailPage.xml')

    def _button_tag(self, handler):
        """回傳 onClick.bind 為 ``handler`` 的那個 ``<WoowButton ...>`` 開頭標籤。"""
        for match in re.finditer(r'<WoowButton\b[^>]*>', self.page_xml, re.S):
            if 'onClick.bind="%s"' % handler in match.group(0):
                return match.group(0)
        self.fail('找不到 onClick.bind="%s" 的按鈕（改名了？同步更新本測試）' % handler)

    def test_custom_domain_button_is_gated(self):
        tag = self._button_tag('showDomainModal')
        self.assertIn(
            't-if="customDomainSupported"', tag,
            '自訂網域按鈕必須掛在 customDomainSupported 之後（#1535：功能未實作，不對租戶顯示）')

    def test_modal_mount_is_gated(self):
        match = re.search(r'<EditDomainModal\b[^>]*>', self.page_xml, re.S)
        self.assertTrue(match, '找不到 EditDomainModal 的掛載點')
        self.assertIn('customDomainSupported and', match.group(0))

    def test_capability_getter_is_off(self):
        match = re.search(
            r'get customDomainSupported\(\)\s*\{(.*?)\n    \}', self.page_js, re.S)
        self.assertTrue(match, '找不到 customDomainSupported getter')
        self.assertRegex(
            match.group(1), r'^\s*return false;\s*$',
            '功能實作前 getter 必須固定回 false；實作時改讀後端 payload 的能力欄位')

    def test_open_handler_refuses_when_unsupported(self):
        match = re.search(r'\n    showDomainModal\(\)\s*\{(.*?)\n    \}', self.page_js, re.S)
        self.assertTrue(match, '找不到 showDomainModal handler')
        self.assertIn('if (!this.customDomainSupported)', match.group(1))

    def test_modal_component_is_kept(self):
        """程式保留不刪：日後實作沿用同一個元件與 CNAME 教學。"""
        for name in ('EditDomainModal.js', 'EditDomainModal.xml'):
            with self.subTest(file=name):
                self.assertTrue(
                    os.path.exists(os.path.join(_PAAS, 'components', 'modal', name)))
        self.assertIn('import { EditDomainModal }', self.page_js)

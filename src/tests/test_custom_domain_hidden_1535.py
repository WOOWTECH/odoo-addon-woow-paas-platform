"""The domain UI is gated by the server's per-service capability.

The old hardcoded-false guard was correct while the feature was unimplemented.
The new controller issues a capability only for an eligible, authorized service;
the frontend may not infer availability from an old custom_domain text field.
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
        self.assertIn('t-if="customDomainManagementAvailable"', tag)

    def test_modal_mount_is_gated(self):
        match = re.search(r'<EditDomainModal\b[^>]*>', self.page_xml, re.S)
        self.assertTrue(match, '找不到 EditDomainModal 的掛載點')
        self.assertIn('customDomainManagementAvailable and', match.group(0))

    def test_capability_getter_uses_server_field(self):
        match = re.search(
            r'get customDomainSupported\(\)\s*\{(.*?)\n    \}', self.page_js, re.S)
        self.assertTrue(match, '找不到 customDomainSupported getter')
        self.assertIn('custom_domain_capability', match.group(1))
        self.assertNotIn('return true', match.group(1))

    def test_open_handler_refuses_when_management_is_unavailable(self):
        match = re.search(r'\n    showDomainModal\(\)\s*\{(.*?)\n    \}', self.page_js, re.S)
        self.assertTrue(match, '找不到 showDomainModal handler')
        self.assertIn('if (!this.customDomainManagementAvailable)', match.group(1))

    def test_modal_component_is_kept(self):
        """程式保留不刪：日後實作沿用同一個元件與 CNAME 教學。"""
        for name in ('EditDomainModal.js', 'EditDomainModal.xml'):
            with self.subTest(file=name):
                self.assertTrue(
                    os.path.exists(os.path.join(_PAAS, 'components', 'modal', name)))
        self.assertIn('import { EditDomainModal }', self.page_js)

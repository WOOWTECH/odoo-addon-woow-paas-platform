# -*- coding: utf-8 -*-
"""AI-HER-007／AI-HER-010（商用驗收 2026-10-16）：Hermes 一次性憑證視窗文案與回復版本說明。

* 一次性視窗（hermes-agent 分支）原本無條件叫租戶「填進 MCP 連線設定」。沒有買 Hermes MCP
  服務的租戶看不到那個設定頁，等於指到不存在的地方。文案改成條件式（「若你使用 Hermes MCP
  服務…」），並先說明這個值本身是什麼。
* 回復版本對話框：平台回復寫入的 helm 說明是「Rollback to N (credentials preserved)」，
  前端只認「Rollback to N」，整句掉回英文原文（AI-HER-007-2）。

前端部分沿用 ``test_service_role_ux_1560`` 的靜態讀檔契約慣例。
"""
import os
import re

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODAL = os.path.join(_SRC, 'static', 'src', 'paas', 'components', 'modal')


def _read(name):
    with open(os.path.join(_MODAL, name), encoding='utf-8') as fh:
        return fh.read()


@tagged('post_install', '-at_install')
class TestHermesCopyHer3(TransactionCase):

    def test_hermes_hints_are_conditional_on_the_mcp_server(self):
        js = _read('OneTimeCredentialsModal.js')
        self.assertNotIn('of the MCP connection settings', js,
                         '不可再無條件指向「MCP connection settings」')
        self.assertEqual(js.count("If you use the Hermes MCP server"), 3)
        self.assertEqual(js.count("If you also use the Hermes MCP server"), 1)

    def test_new_hint_strings_are_translated(self):
        po = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        js = _read('OneTimeCredentialsModal.js')
        hints = re.findall(r'_t\(\s*"((?:[^"\\]|\\.)*Hermes MCP server(?:[^"\\]|\\.)*)"', js)
        self.assertEqual(len(hints), 4, hints)
        for src in hints:
            with self.subTest(src=src[:50]):
                self.assertTrue(code.get(src), '缺 zh_TW 譯文')
                self.assertNotIn('MCP connection settings', src)
        for stale in code:
            self.assertNotIn('of the MCP connection settings', stale,
                             '舊 msgid 要從 po 移除')

    def test_rollback_description_accepts_credentials_preserved_suffix(self):
        js = _read('RollbackModal.js')
        self.assertIn(
            r'/^Rollback to (\d+)(?: \(credentials preserved\))?$/', js)

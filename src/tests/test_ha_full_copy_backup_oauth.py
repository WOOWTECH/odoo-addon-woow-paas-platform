# -*- coding: utf-8 -*-
"""ha-full 商品說明：內建備份與 OAuth 回呼網址要寫實況（商用驗收 X-BK-005-2、INFRA-DOC-002-1）。

- X-BK-005-2：說明原本寫「② 沒有內建備份」，但 HA 241（Core 2026.7.2，Container 版）實測可用
  原生 UI 建立、下載、還原備份。備份只存在同一顆資料磁碟上，沒有異地副本，所以說明要寫
  「有內建備份、存在同一顆磁碟、請下載另存」，不能寫成沒有，也不能寫成像 HAOS 那樣的完整
  備份方案。
- INFRA-DOC-002-1（#1787）：映像刻意刪掉 ``my`` 元件（ha-rebrand Dockerfile Step 1a，
  去品牌的移除清單），所以 Home Assistant 的 OAuth redirect_uri 不是 my.home-assistant.io，
  而是「客戶自己的服務網址＋/auth/external/callback」（core
  ``config_entry_oauth2_flow.async_get_redirect_uri``）。照原廠提示去 Google／Withings 等
  開發者後台填 my.home-assistant.io，授權後會被導到 404。商品說明要寫明正確填法。
"""
from odoo.tests.common import TransactionCase, tagged


def _ha_full(env):
    return env.ref('woow_paas_platform.cloud_app_ha_full')


@tagged('post_install', '-at_install')
class TestHaFullCopyBackupOAuth(TransactionCase):

    def _text(self):
        return _ha_full(self.env).full_description or ''

    def test_backup_is_described_as_present_on_same_disk(self):
        """X-BK-005-2：寫「有內建備份」、存同一顆磁碟、要下載另存；不再寫「沒有內建備份」。"""
        text = self._text()
        self.assertNotIn('沒有內建備份', text)
        self.assertIn('內建備份', text)
        self.assertIn('同一顆', text)
        self.assertIn('下載另存', text)

    def test_missing_features_list_is_store_and_update_only(self):
        """本版沒有的只剩 Add-on 商店與一鍵更新；不再說要備份就改選 HAOS。"""
        text = self._text()
        self.assertIn('沒有 Add-on 商店', text)
        self.assertIn('沒有一鍵更新', text)
        self.assertNotIn('三項功能', text)
        self.assertNotIn('備份快照與系統一鍵更新', text)

    def test_oauth_redirect_uri_is_service_url_callback(self):
        """INFRA-DOC-002-1：OAuth redirect URI 填「服務網址＋/auth/external/callback」。"""
        text = self._text()
        self.assertIn('/auth/external/callback', text)
        self.assertIn('服務網址', text)
        # 明講不要照原廠提示填 My Home Assistant 轉址
        self.assertIn('my.home-assistant.io', text)
        self.assertIn('不要填', text)

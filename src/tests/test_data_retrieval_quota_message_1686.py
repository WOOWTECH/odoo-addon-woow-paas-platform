"""#1686：資料取回因工作區配額不足而開不了入口時，租戶訊息的出口只有聯絡客服。

#1540 原本的句子叫租戶「停止工作區內的其他服務再試」。這個出口走不通：

* ``/woow`` 沒有讓租戶自行停止服務的操作，租戶實際能做的只有刪除服務；
* 刪除也換不回空間——工作區的 CPU／記憶體上限＝存活服務的方案規格加總 × 3
  （``_compute_ns_quota``，另有下限）：刪掉一台，上限少 3 份、用量只少約 1 份，剩餘空間
  通常變小而不是變大。

#1647 已定下同類文案的兩條原則（``test_workload_fault_1647.py`` 的
``test_quota_text_only_points_the_tenant_to_support``、
``test_quota_text_says_not_enough_left_rather_than_full``），本檔把它們套到這一句：

* 只請租戶聯絡客服，不建議停止／刪除服務或換成較小的方案；
* 說的是「剩餘配額不足以啟動」，不說配額已滿／用完——被擋下的常見形狀是「還有剩，但
  不夠啟動」，寫成用完會與工作區頁配額面板上的「還剩 N」對不起來。
"""
import os

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from ..models.data_retrieval import ERR_QUOTA_EXCEEDED
from ..services.paas_operator import PaaSOperatorAPIError

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# operator 的 detail 原文：租戶不該看到（平台只依錯誤碼換訊息）。
_OPERATOR_TEXT = 'operator-side-text-1686'

_ENGLISH_REMOVAL = (
    r'(?i)\b(delet\w*|remov\w*|stop\w*|paus\w*|suspend\w*|disabl\w*'
    r'|shut\w* down|turn\w* off|downgrad\w*|smaller|free\w* up|try again|retry)\b')
_ENGLISH_FULL = r'(?i)\b(full|used up|exhausted|ran out|run out|no\b.*\bleft)\b'
_CHINESE_REMOVAL = ('刪除', '移除', '停用', '停止', '暫停', '關閉', '降級', '較小', '釋放', '釋出',
                    '再試')
_CHINESE_FULL = ('已滿', '滿了', '用完', '用盡', '耗盡')


def _quota_error():
    return PaaSOperatorAPIError(
        'API error', status_code=409,
        detail={'error_code': ERR_QUOTA_EXCEEDED, 'message': _OPERATOR_TEXT})


@tagged('post_install', '-at_install')
class TestDataRetrievalQuotaMessage1686(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['res.lang']._activate_lang('zh_TW')
        cls.Retrieval = cls.env['woow_paas_platform.data_retrieval']
        cls.entries = [entry for entry in PoFileReader(os.path.join(_SRC, 'i18n', 'zh_TW.po'))
                       if entry.get('type') == 'code']

    def _message(self, lang):
        return self.Retrieval.with_context(lang=lang)._operator_error_text(_quota_error())

    def test_english_only_points_the_tenant_to_support(self):
        message = self._message('en_US')
        self.assertEqual(
            message,
            'This workspace does not have enough CPU or memory quota left to start the '
            'download service. Please contact support.')
        self.assertNotRegex(message, _ENGLISH_REMOVAL)
        self.assertNotRegex(message, _ENGLISH_FULL)
        self.assertNotIn(_OPERATOR_TEXT, message)

    def test_zh_tw_only_points_the_tenant_to_support(self):
        message = self._message('zh_TW')
        self.assertEqual(
            message, '這個工作區剩餘的 CPU 或記憶體配額不足以啟動下載服務。請聯絡客服。')
        for word in _CHINESE_REMOVAL + _CHINESE_FULL:
            self.assertNotIn(word, message)
        self.assertNotIn(_OPERATOR_TEXT, message)

    def test_po_has_no_leftover_of_the_old_sentence(self):
        """改英文原文時 msgid 要跟著改：舊 msgid 留著的話，畫面上是英文、po 裡卻還有舊譯文。"""
        english = self._message('en_US')
        translated = {entry['src']: entry['value'] for entry in self.entries}
        self.assertTrue(translated.get(english), 'zh_TW.po 缺譯文，或 msgid 與英文原文不一致')
        download = [entry for entry in self.entries
                    if 'download service' in entry['src'] and 'quota' in entry['src'].lower()]
        # 陽性對照：這個篩法至少抓得到現在這一句。
        self.assertIn(english, [entry['src'] for entry in download])
        for entry in download:
            with self.subTest(msgid=entry['src']):
                self.assertIn('contact support', entry['src'])
                self.assertNotRegex(entry['src'], _ENGLISH_REMOVAL)
                self.assertNotRegex(entry['src'], _ENGLISH_FULL)
                self.assertIn('聯絡客服', entry['value'])
                for word in _CHINESE_REMOVAL + _CHINESE_FULL:
                    self.assertNotIn(word, entry['value'])

"""#107：`/woow` 在地化的最後一哩——啟用 `zh_TW`，但先驗翻譯檔真的讀得動。

## 為什麼啟用邏輯放 model 而不是只放 migration

`odoo/modules/migration.py :: Migration.migrate_module` 對 `state == 'to install'`
**直接 return**（連腳本清單都不建）⇒ 只寫 migration 的話，**新裝的 DB 永遠不會啟用
`zh_TW`**。真正保證「新安裝」與「每次 `-u`」都執行的那一格是
`data/lang_zh_tw_activation.xml` 的 `<function>`；migration 只是給「這次升級」一個
可驗錨點。邏輯放在 model 上，三個呼叫點才共用同一份實作、也才能被單元測試直驅。

## 為什麼啟用之前要先驗 `.po`

啟用了但翻譯讀不到，畫面會變成「Odoo 原生中文 ＋ 我們的英文」混排——看起來像做壞
了，而且**沒有任何東西會叫**：UI 不報錯、`.pot` 對帳全綠。fail-safe 方向因此刻意是
**不啟用**（維持英文窗口）。

這道 gate 跟著產物一起旅行（`.po` 與本檔在同一個 addon 目錄裡）、正好落在使用點，
比跨 repo 的 CI gate 強且零協調成本。

## 判準用 Odoo 自己的 reader，不用 polib / msgfmt

`PoFileReader` **就是 runtime 真正會拿去讀這個檔的那個東西**
（`CodeTranslations._load_web_translations` → `/web/webclient/translations`）。
用第二把尺（polib 直接解析、或 `msgfmt -c`）只能證明「那把尺讀得動」，而它們與
Odoo 的分歧正是最危險的一格：`PoFileReader.__iter__` 在條目缺 `#. module:` 註解時
是 `re.match(...).groups()` 的 **AttributeError 整份硬崩**（`translate.py`），
而 polib 與 msgfmt 都認為那份檔完全合法。

⚠ 這道 gate **抓不到**的一格（誠實記錄）：occurrence 行格式不認得時
`PoFileReader.__iter__` 只 `_logger.error("malformed po file: ...")` 就跳過那一條，
不 raise ⇒ 「少了幾條翻譯」對本 gate 不可見。它擋的是「整份不可用」，不是逐條對帳。
"""
import logging
import os

from odoo import api, models
from odoo.exceptions import ValidationError
from odoo.tools.translate import PoFileReader

_logger = logging.getLogger(__name__)

_ZH_TW = 'zh_TW'

#: 相對本檔的位置。`models/` 的上一層就是 addon 根（repo 內叫 `src/`，image 內叫
#: `woow_paas_platform/`）⇒ 這條路徑跟著產物旅行，不依賴 addons_path 怎麼設。
_PO_RELPATH = ('i18n', 'zh_TW.po')

#: 三種「不啟用」的成因。訊息刻意寫成互不相同的字面值——「檔案不存在」與「檔案在
#: 但壞掉」若共用一句 log，事後從 log 分不出該去補檔還是該去修檔。
_PROBLEM_MISSING = 'missing'
_PROBLEM_UNREADABLE = 'unreadable'
_PROBLEM_EMPTY = 'empty'

_PROBLEM_MESSAGES = {
    _PROBLEM_MISSING: (
        "[107] zh_TW 未啟用：翻譯檔不存在（%s）。這通常表示 image build 把 "
        "i18n/ 排除掉了，或 addon 目錄不完整。維持英文介面，不啟用語系。"
    ),
    _PROBLEM_UNREADABLE: (
        "[107] zh_TW 未啟用：翻譯檔存在但 Odoo 讀不動（%s）。"
        "整份 .po 讀取會拋例外 ⇒ 啟用後 /web/webclient/translations 會 500，"
        "整個 webclient 掛掉。維持英文介面，不啟用語系。"
    ),
    _PROBLEM_EMPTY: (
        "[107] zh_TW 未啟用：翻譯檔解析成功但一條可用條目都沒有（%s）。"
        "啟用後畫面會是 Odoo 原生中文混本模組英文，比全英文更難判讀。"
        "維持英文介面，不啟用語系。"
    ),
}


class ResLang(models.Model):
    _inherit = 'res.lang'

    @api.model
    def _woow_zh_tw_po_path(self):
        """本模組出貨的 `zh_TW.po` 絕對路徑。"""
        addon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(addon_root, *_PO_RELPATH)

    @api.model
    def _woow_zh_tw_po_problem(self):
        """`None` ＝可用；否則回 `(kind, detail)`，`kind` 是三個 `_PROBLEM_*` 之一。

        回 tuple 而不是 bool，是為了讓呼叫端能印出**互相區分**的 CRITICAL log，
        也讓測試能斷言「擋下來的理由是哪一種」而不只是「有擋」。
        """
        path = self._woow_zh_tw_po_path()
        if not os.path.isfile(path):
            return (_PROBLEM_MISSING, path)

        try:
            # 真的把整份跑完——`PoFileReader.__iter__` 是 generator，只建構不迭代
            # 等於什麼都沒驗（AttributeError 那一格正是在迭代時才炸）。
            entry_count = sum(1 for _entry in PoFileReader(path))
        except Exception as exc:  # noqa: BLE001 — 任何例外都代表這份檔不能用
            return (_PROBLEM_UNREADABLE,
                    '%s — %s: %s' % (path, type(exc).__name__, exc))

        if not entry_count:
            return (_PROBLEM_EMPTY, path)

        return None

    @api.model
    def _ensure_zh_tw_lang_active(self):
        """啟用 `zh_TW`——`.po` 不可用時**不啟用**並留 CRITICAL log。

        冪等：`_activate_lang` 對已 active 的語言一次 write 都不發。
        回傳被啟用（或本來就 active）的 `res.lang` recordset；被 gate 擋下時回空
        recordset。
        """
        problem = self._woow_zh_tw_po_problem()
        if problem:
            kind, detail = problem
            _logger.critical(_PROBLEM_MESSAGES[kind], detail)
            return self.browse()

        # `_activate_lang` 內部自帶 active_test=False，這裡不必再包一層。
        lang = self._activate_lang(_ZH_TW)
        if not lang:
            # 空 recordset ＝ `res_lang` 裡根本沒有 zh_TW 這一列。base 的
            # res.lang.csv 一定會 seed 它，所以這不是「降級狀態」而是 base 壞掉；
            # 靜默 return 會讓整條啟用鏈在毫無輸出的情況下什麼都沒做。
            raise ValidationError(
                "[107] res.lang has no row with code=%s — cannot activate the "
                "Traditional Chinese UI. base/data/res.lang.csv seeds this row "
                "on every database, so its absence means the base module data "
                "is incomplete." % _ZH_TW
            )

        _logger.info("[107] zh_TW active (lang id=%s)", lang.id)
        return lang

"""#107：既有 DB 升級時啟用 `zh_TW`（`/woow` 在地化的最後一哩）。

## 這支不是保證執行的那一格

`odoo/modules/migration.py :: Migration.migrate_module` 對 `state == 'to install'`
**直接 return** ⇒ 新安裝根本不會跑到這裡。真正覆蓋「新安裝」與「每次 `-u`」的是
`data/lang_zh_tw_activation.xml` 的 `<function>`（effective noupdate=0）。

那為什麼還要有這支？兩個理由，都不是「怕 `<function>` 沒跑」：

1. **規則 B**：manifest 版號要 bump 就必須帶一個同號的新 migration 目錄
   （`scripts/ci/manifest-version-gate.py`）。
2. **可驗錨點**：`<function>` 每次 `-u` 都跑，所以它證明不了「**這一次**升級啟用了
   語系」；而 migration 只在 `installed < 18.0.1.0.176 <= manifest` 的視窗內跑一次，
   log 裡那一行就是「這次升級做了這件事」的唯一時間戳。

## 冪等

啟用邏輯在 `res.lang._ensure_zh_tw_lang_active()`，內部走 `_activate_lang`：
對已經 active 的語言一次 write 都不發。重跑不會有副作用，也不會多寫一列。

⚠ 這也正是本題最容易做出假綠測試的地方——`assertTrue(lang.active)` 在「本來就
active」的 DB 上恆綠。判別式必須先造出 inactive 的世界再跑（見
`src/tests/test_lang_zh_tw_activation_107.py`）。
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    lang = env['res.lang']._ensure_zh_tw_lang_active()
    if lang:
        _logger.info(
            "[107] post-migrate 18.0.1.0.176：zh_TW 已啟用（lang id=%s）", lang.id)
    else:
        # 不 raise：gate 擋下來時已經留了 CRITICAL log，而 fail-safe 的方向是
        # 「維持英文、升級照常完成」——為了語系翻掉整次升級是更糟的交換。
        _logger.error(
            "[107] post-migrate 18.0.1.0.176：zh_TW **未**啟用，"
            "原因見上一行 CRITICAL（res.lang._ensure_zh_tw_lang_active）")

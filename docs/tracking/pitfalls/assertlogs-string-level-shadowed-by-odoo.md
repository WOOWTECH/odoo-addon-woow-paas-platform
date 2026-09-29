# Pitfall: `assertLogs(..., level='INFO')` 在 Odoo 進程裡等於 setLevel(**25**) → 守 INFO log 的測試恆紅、`assertNoLogs` 恆綠

> 來源：#492（2026-07-31）。develop 全套常紅一顆 `TestMcpAdminPassword.test_chart_version_drift_is_logged`，訊息是 `no logs of level INFO or higher triggered`，讀起來像「被測 code 把 log 弄丟了」。實際上被測的 `_logger.info(...)` 好端端在 `src/models/cloud_service.py:581`，生產環境照印不誤——**壞的是斷言本身**：字串 `'INFO'` 在 Odoo 進程裡不是 20。
>
> 守線：`scripts/audit/repo-lint.py` 第 11 條 `check_assertlogs_numeric_level`（CLI + PostToolUse hook 雙模式）。

## Symptom

- 測試訊息：`AssertionError: no logs of level INFO or higher triggered on odoo.addons.<module>.models.<file>`；
- 被測 code 明明有 `_logger.info(...)`，且在 `--log-level=info` 下實跑真的印得出來；
- 該測試**從引入當下就紅**、不是某次改動打壞的——所以 `git bisect` 找「引入回歸的 commit」會撲空（本案在引入 commit `59e17eb2` 與當時的 develop tip `8acea757` 上跑，失敗訊息一字不差）；
- 鏡像版更陰險：`assertNoLogs(..., level='INFO')` 會**恆綠**（20–24 級的 log 一律漏放），零鑑別力卻長得像有守線。

## Why it happens — Odoo 覆寫了 stdlib 的名稱→數值表

`odoo/netsvc.py`（18.0）：

```python
logging.RUNBOT = 25
logging.addLevelName(logging.RUNBOT, "INFO")  # displayed as info in log
```

`logging.addLevelName(level, name)` 是**雙向**寫表的：

```python
_levelToName[25]     = "INFO"
_nameToLevel["INFO"] = 25      # ← 原本是 20，被覆寫
```

而 unittest 解析 `level` 參數時查的正是那張被改掉的表（`unittest/_log.py:37`）：

```python
if level:
    self.level = logging._nameToLevel.get(level, level)   # 'INFO' → 25（Odoo 進程內）
else:
    self.level = logging.INFO                             # 沒傳 level 反而拿到數值 20
```

於是 `assertLogs(logger, level='INFO')` 對該 logger 做 `setLevel(25)`：所有 `_logger.info()`（20）在 record 產生前就被門檻擋掉，context 內一條都沒捕捉到 → `__exit__` 判定失敗。

一句話：**在 Odoo 進程裡，字串 `'INFO'` 和 `logging.INFO` 不是同一個東西。**

只有 `'INFO'` 這個名字被覆寫；`'DEBUG'`（10）、`'WARNING'`（30）等字串仍與數值等價，既有用法不受影響（本 repo 另兩處 `assertLogs` 分別用 `'DEBUG'` 與 `'WARNING'`，都是綠的）。

## 為什麼沒有及早被抓到

這條紅**不是**「跑起來才知道」的偶發，而是一引入就必紅——它躲過來的原因是 review 與 PR 驗收看的是「這條測試在測什麼」，而它的名字、docstring、斷言字串（`carries a chart upgrade 0.1.4 -> 0.1.5`）三者都精準描述了要守的行為，**只有 level 那個參數形態是錯的**。加上訊息本身把讀者導向「code 掉了 log」，歸因很容易往被測 code 追而不是往斷言追。

## 修法

```python
import logging
...
with self.assertLogs('odoo.addons.woow_paas_platform.models.cloud_service',
                     level=logging.INFO) as logs:      # ← 數值，不經過名稱表
```

斷言強度不變（門檻仍是 INFO），只是不再走那張被覆寫的表。

**判定修法對不對的方式是鑑別力驗證，不是「變綠了」**：把被測 code 的 `_logger.info(...)` 停掉（`if False and ...`）再跑一次，該測試必須紅——本案實測確實紅（同一則訊息）。改 level 前後兩次紅的訊息一樣，正說明「訊息相同 ≠ 成因相同」，只能靠實驗分辨。

## 守線（機械化）

`scripts/audit/repo-lint.py` → `check_assertlogs_numeric_level`：AST 掃 `src/tests/**/*.py`，抓 `assertLogs` / `assertNoLogs` 的 `level` 為字串常值且落在 `ASSERTLOGS_SHADOWED_LEVELS`（目前只有 `'INFO'`）者，keyword 與 positional 兩種形態都認。未來若又有人 `addLevelName` 覆寫別的名字，往那個常數 append 即可。

## 相關

- 同家族（綠不代表對 / 紅不代表壞）：[tag-scoped-test-misses-full-u-failures](tag-scoped-test-misses-full-u-failures.md)、[sudo-getter-acl-cache-priming-false-green](sudo-getter-acl-cache-priming-false-green.md)、[sandbox-module-test-false-green](sandbox-module-test-false-green.md)
- 被守的行為本身（reset 時服務 chart 落後的可診斷性；F-461-2 已於 2026-09-29 改為釘版，log 改為「keeps chart … redeploy to upgrade」）：`src/models/cloud_service.py` `_reset_admin_password`

# Pitfall：`DateTime.fromSQL` 不吃 `T` 分隔，而 invalid DateTime 是 truthy ⇒ 畫面印出 `Invalid DateTime`

> **一句話**：Odoo 的 `deserializeDate` / `deserializeDateTime` 內部走 `DateTime.fromSQL`，
> 那支**只吃 SQL 空格格式**（`2026-08-26 12:34:56`），對後端 `.isoformat()` 產出的
> `2026-08-26T12:34:56.123456` 一律回 invalid；而 luxon 的 invalid DateTime 是 **truthy**、
> `formatDate()` 只在 `!value` 時早退 ⇒ **不是空字串、不是崩潰，是把字串 `Invalid DateTime`
> 顯示給使用者**——最難在 e2e 抽查中被發現的失效形狀。
>
> **來源**：#107 locale formatter 線的前提驗證（2026-08-26 實測，luxon 3.4.4）
>
> **發現場景**：把 `/woow` 的 `toLocale*` 收斂成 Odoo 語系 helper 時，第一直覺是
> 「字串 → `deserializeDateTime` → `formatDate`」。那條路對本 repo 用量最大的日期線是壞的。

## Symptom

- 日期欄位顯示成英文字串 **`Invalid DateTime`**（不是空白、不是 `-`、不是 `NaN`）。
- 沒有任何 console error、沒有例外。
- 只有部分欄位中招——同一頁其他日期正常 ⇒ 看起來像「某個欄位的資料壞了」，
  而不是「解析器選錯了」。

## Root Cause

### (a) `fromSQL` 與 `fromISO` 的接受集合**互斥**，不是包含關係

實測（Odoo 自帶 `web/static/lib/luxon/luxon.js`，VERSION `3.4.4`）：

| 輸入 | `fromSQL` | `fromISO` |
| --- | --- | --- |
| `2026-08-26 12:34:56`（SQL 空格） | ✅ | ❌ unparsable |
| `2026-08-26T12:34:56`（ISO `T`） | ❌ unparsable | ✅ |
| `2026-08-26T12:34:56.123456` | ❌ unparsable | ✅ |
| `2026-08-26T12:34:56Z` | ❌ unparsable | ✅ |
| `2026-08-26T12:34:56+08:00` | ❌ unparsable | ✅ |
| `2026-08-26`（只有日期） | ✅ | ✅ |

⇒ **沒有任何一支單獨吃得下所有形狀。** 而 Odoo 的 `deserializeDate` / `deserializeDateTime`
（`odoo-18.0/addons/web/static/src/core/l10n/dates.js`）內部固定用 `fromSQL`——它們的契約是
「反序列化 **Odoo ORM wire format**」，那個 format 就是 SQL 空格版。
後端自己呼叫 `.isoformat()` 產生的字串**不在**那個契約裡。

### (b) invalid DateTime 是 truthy ⇒ 早退條件接不住

```js
// odoo-18.0/addons/web/static/src/core/l10n/dates.js :: formatDate()
export function formatDate(value, options = {}) {
    if (!value) { return ""; }        // ← invalid DateTime 是 truthy，走不到這裡
    ...
    return value.toFormat(format);    // ← invalid.toFormat(...) === "Invalid DateTime"
}
```

實測：

```
truthy?                  true
!value ->                false        (formatDate 的 early-return 條件)
toFormat("dd/MM/yyyy") -> "Invalid DateTime"
```

⇒ 失效模式是**顯示一個看起來像資料的英文字串**，而不是空白或例外。
在中文介面裡它甚至長得像「還沒翻譯的字串」，很容易被歸類成 i18n 漏網而不是解析失敗。

### (c) `parseDateTime` 看起來能救場，但不能用

`dates.js :: parseDateTime()` 的 docstring 自稱會 fallback 到 ISO8601 與 SQL，但它是
**「解析使用者鍵入字串」**的工具，不是 wire-format deserializer：

- 它把所有 naive 字串一律當 local time（`zone: "default"`）。
- 它的成功分支**隨語系而異**——zh_TW 的 `%Y年%m月%d日` 是純數字格式，會讓
  shorthand 分支（`yyyyMMdd`）提前吃掉輸入，走不到 ISO/SQL fallback。

⇒ **用它寫出來的 bug 可能只在 zh_TW 下發生、en_US 下永遠測不出來。**

## 修法

不要挑一支 luxon parser，**寫一層按「字串形狀」分派的 adapter**，並且：

🔴 **解析失敗必須回 `null`，⛔ 絕不回 invalid DateTime。**
理由不是潔癖：呼叫端普遍寫的是 `if (!x) return fallback`，
而那個條件對 invalid DateTime **不成立** ⇒ 每一個呼叫端都會把它當成有效值往下傳。
回 `null` 讓「四種形狀都不吃」有定義的行為。

本 repo 的落點：`src/static/src/paas/utils/datetime_helpers.js`
（`parseBackendDate` / `formatUserDate` / `formatUserDateTime` / `formatUserTime`）。

## 守線

`src/tests/test_locale_formatting_guard_107.py`：

- `LOC.1` — `toLocale*` / `Intl.*` 呼叫站點歸零（那些跟隨瀏覽器語言，不跟隨 Odoo 語系）
- `LOC.2` — 硬編 BCP-47 語言標籤字面值歸零
- `LOC.3` — QWeb 不得裸 `t-esc` 後端日期欄位（判準用**資源側枚舉**，⛔ 不用欄位名 heuristic
  ——#107 盤點時那個做法漏掉了 `next_payment` 與 `last_sync` 兩格）
- `LOC.0` — 掃描器自檢（陽性對照）：先證明掃描器會動，上面三條的零命中才可採信

⚠ 守線守的是「call site 有沒有回流」，**守不住 helper 內部被改壞**
（例如 `parseBackendDate` 被改成回 invalid DateTime）。那一格沒有機械守線——
本 repo 沒有可執行 JS 的測試基礎設施（無 hoot/QUnit、`HttpCase.browser_js` 只會 SkipTest）。

## 相關

- #914 —— 時區偏移（同一行程式碼的另一個維度，刻意分開）
- #916 —— 後端日期序列化不一致
- #917 —— `last_release_sync_at` 漏 `.isoformat()`（本批已修）

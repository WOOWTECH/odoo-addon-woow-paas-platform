# Pattern: i18n 範圍 — 資料值不走 `.po`，程式碼字串與欄位 metadata 才走

> 適用：任何 i18n / 在地化批次要決定「這個中文字串該不該改成英文 source 並進翻譯流程」時。
> 定案者：Eugene，2026-08-26（#107 三縫裁決 ③）。**之後同類問題引用本通則，不再逐案上呈。**

## 通則

**Odoo record 的「資料值」維持單一語言原文，⛔ 不進 `.po` 翻譯流程。**
只有**程式碼字串**與**欄位 metadata** 走 `.po`。

## 三層區分（這是判準，不是分類學）

| 層 | 是什麼 | `.po` 裡的形態 | 該不該翻 |
| --- | --- | --- | --- |
| **程式碼字串** | `_()` / `_lt()` / `_t()` 包起來的字面 | `#: code:addons/...` | ✅ **要** |
| **欄位 metadata** | `string=` / `help=` / selection label / `_description` / view arch 的可翻屬性 | `#: model:ir.model.fields...`、`#: model_terms:ir.ui.view` | ✅ **要**（#107 段四／段五做的就是這個）|
| **資料值** | record 欄位的**內容**（商品名、方案名、seed 出來的業務資料） | `#: model:<business.model>,<field>`，或**根本不出現** | ⛔ **不要** |

## 🔴 最容易踩的那一格：「在不在 `.po` 裡」**不是判準**

兩個實例都被裁「不動」，但它們在 `.po` 裡的**狀態完全相反**：

| | 商品名 | 訂閱方案名 |
| --- | --- | --- |
| 載體 | `src/data/subscription_products.xml`（XML data record）| `src/models/sh_subscription_plan_reseed_b.py` 等（Python seed 字典）|
| model / 欄位 | `product.template.name` | `sh.subscription.plan.name` |
| **在 `.po` 裡** | ✅ **有**（`model:product.template,name`，msgid 絕大多數是中文）| ⛔ **零條** |
| 裁決 | 不動 | 不動（比照） |

⇒ **資料值出現在 `.po` 裡，不代表它該被翻。** `--i18n-export` 對 `model:` / `model_terms:` 兩類是
**從 DB 讀**的，只要欄位是可翻型態就會被匯出——那是抽取器的行為，不是「有人決定要翻它」。

⇒ 反過來也一樣：**資料值不在 `.po` 裡，不代表漏了。** Python seed 字典裡的字面本來就進不去
（除非有人去包 `_()`——⛔ 那正是本通則禁止的動作）。

⚠ 條數刻意不寫在本檔。三種口徑會給三個不同的數（msgid block 數／`#:` 註解行數／裁決當時的引用值），
寫下來就會漂。**要數字就現場枚舉**：`.po` 裡 `^#: model:<model>,<field>` 的 msgid block 數。

## 判準（可機械套用）

問一句：**「這個字串是某個 record 的欄位內容嗎？」**

- **是** ⇒ 資料值 ⇒ ⛔ 不包 `_()`、不改英文 source、不填 msgstr。
- **否**（它是原始碼裡的訊息、或欄位的標籤／說明）⇒ ✅ 走 `.po`。

⚠ 兩個容易混淆的邊界：

1. **欄位標籤 vs 欄位內容**——`fields.Char(string='方案名稱')` 的 `'方案名稱'` 是 metadata（**要翻**）；
   而某筆 record 的 `name` 值 `'Odoo 訂閱服務（1-3 人）'` 是資料值（**不翻**）。兩者在 `.po` 裡的
   comment 前綴不同（`model:ir.model.fields` vs `model:<business.model>`），但長得很像。
2. **被當成鍵在用的字面**——那是**第三類**，與本通則無關：它既不翻也不改，理由是
   「改了會壞」不是「它是資料」。見 `_UNCONFIRMED_WORKLOAD_MARKER_497`（#107 三縫裁決 ② 的明列排除項）。

## 不適用

- **需要多語呈現的業務資料**：若某天商品名真的要對外文租戶顯示多語，那走 Odoo 的
  **DB 層 record translation**（`translate=True` 欄位的多語值），**不是**把 source 改英文再用 `.po` 翻。
  ⇒ 那是產品決策，要重新上呈，⛔ 不能用本通則自行放行。
- **不是 record 欄位內容的中文**（錯誤訊息、按鈕文字、欄位標籤）——那些一律走 `.po`。

## 出處

- Eugene 2026-08-26 裁決 ③（`.scratch/ruling-107-scope-gaps.md`）：
  「`sh.subscription.plan.name` 訂閱方案名 ⛔ 比照商品名，不動」＋「把『資料值不走 `.po`』寫成通則落檔」
- 更早的商品名定案：「那是資料不是程式碼，本來就該中文」
- ⚠ 本通則的存在理由：2026-08-26 曾因為
  **Eugene 裁的是 `product.template/name`、而執行者問的是 `sh.subscription.plan.name`（不同 model）**，
  判定「字面上不涵蓋、不外推」而必須單獨上呈一次。通則落檔就是為了免掉下一次。

## 相關

- [`../../tracking/pitfalls/README.md`](../../tracking/pitfalls/README.md) — i18n 相關陷阱索引
- #107（`/woow` 介面在地化）的三縫裁決：① wizard 動、② `error_message` 動但 marker 排除、③ 本通則

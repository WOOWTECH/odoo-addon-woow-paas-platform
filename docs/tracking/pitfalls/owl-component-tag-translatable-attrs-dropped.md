# Pitfall: 那 9 個「可翻譯屬性」寫在 component tag 上，Odoo 抽取器 9 個全部抽不到——`.pot` 少一條，畫面永遠是原文

> 來源：#107 i18n 重構。W2（`--i18n-export` 端到端實跑）在沙盒 node 上放探針檔，把 9 個屬性各寫三種寫法後重跑 export 實測出來；W3（XML 層）把它寫成守線。
> 撞這條的成本不對稱：**加對了什麼都不會發生，加錯了也什麼都不會發生**——只是那句文案永遠不會被翻譯，而 diff 上兩者長得一模一樣。

## Symptom

一句 UI 文案寫在屬性上（`title="Edit policy"`、`placeholder="Search addons..."`、`label="Confirm"`），
語法完全合法、頁面正常顯示、`repo-lint` 綠、Python 測試綠、`--i18n-export` exit 0。

但那句話**不在 `.pot` 裡**，於是：

- 翻譯者看不到它 ⇒ `zh_TW.po` 不會有它 ⇒ 租戶畫面上它永遠是英文原文。
- 若走的是 #107 Q2＝(a) 的路線（原本寫死中文 → 改英文 source ＋ 中文搬進 `.po`），
  **這一句會從中文倒退成英文而且沒有任何訊號**：`.pot` 的中文 msgid 一樣歸零（因為它從頭到尾就沒進去過）、
  對帳全綠、UI 不報錯。成功與失敗長得一模一樣。

## Why it happens

`odoo/tools/translate.py :: _extract_translatable_qweb_terms`：

```python
is_component = el.tag[0].isupper() or "t-component" in el.attrib or "t-set-slot" in el.attrib
for attr in el.attrib:
    if (not is_component and attr in OWL_TRANSLATED_ATTRS) or (is_component and attr.endswith(".translate")):
        _push(callback, el.attrib[attr], el.sourceline)
```

兩條分支**互斥**：

| tag 形態 | 判準 | 那 9 個屬性 |
| --- | --- | --- |
| 普通小寫 tag（`<button>` / `<input>` / `<nav>`） | `attr in OWL_TRANSLATED_ATTRS` | **抽得到** |
| component tag（大寫開頭，或帶 `t-component` / `t-set-slot`） | `attr.endswith(".translate")` | **9 個全部抽不到** |

`OWL_TRANSLATED_ATTRS` 那 9 個：
`alt` / `aria-label` / `aria-placeholder` / `aria-roledescription` / `aria-valuetext` / `data-tooltip` / `label` / `placeholder` / `title`。

⚠ 注意 `is_component` 不只看大寫開頭——**帶 `t-component` 或 `t-set-slot` 的小寫 tag 也算 component**。
只用「首字母大寫」當心智模型會漏掉這兩種。

### W2 的實測（node 上的探針檔，repo 未動）

9 個屬性 × 三種寫法，一次 export 測完：

| 屬性 | (a) 普通小寫 tag | (b) component tag | (c) component ＋ `.translate` |
| --- | --- | --- | --- |
| `alt` / `aria-label` / `aria-placeholder` / `aria-roledescription` / `aria-valuetext` / `data-tooltip` / `placeholder` | 抽到 ✓ | **抽不到 ✗** | （未測，同一行程式碼） |
| `label` | 抽到 ✓ | **抽不到 ✗** | 抽到 ✓ |
| `title` | 抽到 ✓ | **抽不到 ✗** | 抽到 ✓ |

**探針自己的陽性對照**：同檔的純文字節點 `V3PROBEtextNODE` 抽到 ✓
⇒ 那 9 個 `抽不到 ✗` 是真的抽不到，不是「這個檔沒被掃到」。

**算術對照**：探針 `.pot` 比 baseline 多 12 條 ＝ 9（plain）＋ 2（`.translate`）＋ 1（文字節點）。分毫不差。

## ⚠ 真正危險的那一種寫法（不是你以為的那種）

component 的 prop **值本身就是 expression**（見 `owl.js :: formatProp` 的 else 分支
`value = this.captureExpression(value)`）。所以有兩種寫法，只有一種是靜默失敗：

| 寫法 | runtime | 抽取 | 危險度 |
| --- | --- | --- | --- |
| `<WoowButton label="Confirm change"/>` | **炸**（去 eval `Confirm change`，SyntaxError） | 抽不到 | 低——當場看得到 |
| `<WoowButton label="'Confirm change'"/>` | 正常顯示 | **抽不到** | 🔴 **就是這個**——成功與失敗長得一模一樣 |
| `<WoowButton label.translate="Confirm change"/>` | 正常顯示（且**會被翻譯**） | 抽得到 | 正解 |

`.translate` 是 OWL 原生支援的 prop 後綴，不是 Odoo 外掛的：

```js
// owl.js :: formatProp
if (name.endsWith(".translate")) {
    value = toStringExpression(this.translateFn(value, attrTranslationCtx));
} else {
    value = this.captureExpression(value);   // ← 沒有 .translate 就是 expression
}
...
switch (suffix) { case "bind": ... case "alike": case "translate": break;
                  default: throw new OwlError(`Invalid prop suffix: ${suffix}`); }
```

⇒ `.translate` 的值是**裸文案**（不要再包內層引號），OWL 會把它送進 `translateFn` 再轉成字串常量。

## 正解

```xml
<!-- ✗ 顯示正常、永遠不會被翻譯（內層引號＝OWL 字面字串 expression） -->
<WoowButton label="'Confirm change'"/>
<WoowCard title="'Access Policies'"/>

<!-- ✓ 裸文案，不加內層引號 -->
<WoowButton label.translate="Confirm change"/>
<WoowCard title.translate="Access Policies"/>
```

```xml
<!-- 普通小寫 tag 維持原樣即可，不要加 .translate（那反而抽不到） -->
<button title="Edit policy">…</button>
<input placeholder="Search addons..."/>
```

### 判斷順序

1. 這個 tag 是不是 component？（首字母大寫 **或** 帶 `t-component` / `t-set-slot`）
2. 是 → 屬性名加 `.translate`、值改成裸文案；否 → 保持原樣。
3. 屬性值是 **expression 而不是文案**（`title="record.name"` 這種）時**兩邊都不加**——
   那本來就不該進 `.pot`，值會在 runtime 求出來。

## 守線

- **機器 gate**：`scripts/audit/repo-lint.py :: check_owl_component_translatable_attr`
  （併在第 2 條規則 `owl-template` 底下）。掃 `src/static/src/**/*.xml`，對每個 component tag
  上出現的那 9 個屬性，**值是 OWL 字面字串**（內外層引號都在，即上表第二列那種）就報違規並指名正解寫法。
  - 判準刻意鎖在「字面字串」而不是「所有值」：`title="record.name"` 這種真 expression 是正常用法，
    抓它會製造大量假陽性，人就會學會忽略這條 gate。
  - 這條上線時 repo 內實例數為 **0**（W2 V3 盤點：9 個屬性在 component tag 上一個都沒有）
    ⇒ gate 是**預防性**的，不是清掃既有債。
  - ⚠ **覆蓋邊界**：
    - 只抓「屬性名在那 9 個之內」。有人在 component 上用**別的**屬性名傳文案
      （`text="'Confirm'"` / `caption="..."`），本 gate 一條都抓不到——那類文案在 OWL 裡本來也不在
      可翻譯屬性白名單內，正解是改用 slot 或改屬性名，不是加 `.translate`。
    - 只抓字面字串。文案由 expression 拼出來（`label="prefix + '中'"`）一樣抽不到，gate 看不見。
- **人工提示**：`.claude/hooks/pattern-guard-map.json` 的 `owl-template` class 已把本檔列為必讀件
  ⇒ 編輯任何 `src/static/src/**/*.xml` 時 hook 會列出來。

## 對照記憶

同一個 `is_component` 判準在另一條 pitfall 也出現過，方向相反：
[`owl-template-crash-class-syntax`](owl-template-crash-class-syntax.md) 講的是
**component prop 本身就是 expression，所以不能加 `t-att-`**。
兩條合起來看比較好記：

- component 的 prop **值**＝expression（所以 `t-att-` 多餘、會 crash）
- component 的 prop **名**要讓抽取器認得是文案，得加 `.translate`（否則靜默抽不到）

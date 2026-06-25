# Pitfall: OWL template 語法錯誤通過 XML well-formed + 全部 Python 測試，runtime render 直接摧毀整個 OWL root（整站白屏）

> 來源：2026-06-07 Security Access P1 Phase 5（`SecurityAccessDetailPage` 六分頁詳情頁）。同一頁連撞兩次 `OwlError`，兩次都是「Vue 的肌肉記憶寫進 OWL template」造成。兩次都通過了 XML well-formed 檢查與整套 Odoo Python 測試，**唯一抓到的網是 headed 瀏覽器實走 + console error**。第二次（按鍵 modifier）是修完第一次重驗時才現形——白屏不是疊加的，是「先炸的那個擋住了後面那個」。

## Symptom

頁面一進去整片白屏。不只該頁——因為 `/woow` 是純 hash-based 導航的 standalone OWL app，**root component 被毀後所有 hash route 都空白**，直到 `F5` full reload 才恢復（reload 又進同一頁 → 再白）。browser console 一行 `[Owl] Unhandled error. Destroying the root component`，往上才是真因：

- **撞 1**：`OwlError: t-att makes no sense on component: props are already treated as expressions`
- **撞 2**（修完撞 1 重驗才現形）：`OwlError: Unknown event modifier: 'enter'`

## Why it happens

### 撞 1 — `t-att-` 用在 component tag 上

```xml
<!-- 反例（SecurityAccessDetailPage.xml） -->
<WoowIcon t-att-name="tab.icon" size="16"/>
```

OWL component 的 **prop 本身就是 expression**——`name="tab.icon"` 已經是「求值 `tab.icon`」，不是字面字串。`t-att-name` 是 DOM attribute 的動態綁定語法，套在 component 上等於要求 OWL「對一個已經是 expression 的東西再做一次 attribute 動態綁定」，語義矛盾，OWL 編譯時直接拋 `OwlError`。正解是拿掉 `t-att-`：

```xml
<WoowIcon name="tab.icon" size="16"/>   <!-- 正解：component prop 直接寫 expression -->
```

> 對照記憶：傳「字面字串」給 component prop 才要加引號 `name="'block'"`（見 `AppCard.xml`、`AddonRemovalDialog.xml`）；傳「變數 / expression」就直接 `name="tab.icon"`。`t-att-name` 兩種都不是。

### 撞 2 — Vue 的按鍵 modifier `.enter`

```xml
<!-- 反例 -->
<input t-model="state.nameDraft" t-on-keyup.enter="saveName" t-on-blur="saveName"/>
```

`t-on-<event>.<modifier>` 的 modifier，OWL **只支援 synthetic modifier**：`.stop` / `.prevent` / `.self` / `.capture`（以及 `.synthetic`）。OWL **沒有 Vue 的按鍵 modifier**（`.enter` / `.esc` / `.tab` …）。按鍵判斷一律寫在 handler 內讀 `ev.key`：

```xml
<input t-model="state.nameDraft" t-on-keydown="onNameKeydown" t-on-blur="saveName"/>
```

```javascript
onNameKeydown(ev) {
  if (ev.key === "Enter") {
    ev.target.blur();   // blur → 委派給 t-on-blur=saveName，維持單一儲存路徑
  }
}
```

設計上讓 Enter 只觸發 `blur()`，由 `t-on-blur="saveName"` 收斂成**唯一一條儲存路徑**，避免 Enter 與失焦各存一次。

### 三層檢查為什麼全部沒抓到

| 層 | 為什麼漏 |
| --- | --- |
| `ET.parse`（Odoo 載入 XML 模板） | 只驗 **well-formed**（標籤閉合、屬性合法格式）；`t-att-name` / `t-on-keyup.enter` 都是合法 XML 屬性字串，parse 全過。 |
| Odoo Python 測試（`--test-tags`） | **不執行任何 JS**；OWL template 是前端 runtime 才編譯，後端測試碰不到。 |
| assets bundle build | 只做打包 / minify，**不做 OWL template compile**；template→render function 的編譯發生在瀏覽器載入 component 那一刻。 |

→ 三層全綠，但**只有真瀏覽器 render 會炸**。這就是兩個 bug 都活到 manual verify 才暴露的結構原因。

## 守則（何時適用、怎麼做）

1. **本 repo 沒有 JS 測試框架** → 任何 `src/static/` 前端改動的驗證底線就是 **headed 瀏覽器實走**（CLAUDE.md 的 `playwright-cli` UX 流程 / `agent-browser` 視覺驗證規範）+ **console error 零容忍**。「Python 測試綠 + 頁面 XML 載得進去」**不等於**前端沒炸——它只證明 well-formed，不證明 render 成功。
2. **OWL ≠ Vue**，寫 template 時兩條硬規則：
   - component tag（大寫開頭）上**禁用 `t-att-*`**——prop 已是 expression，直接 `prop="expr"`（變數）或 `prop="'lit'"`（字面字串）。
   - **按鍵判斷一律在 handler 內讀 `ev.key`**，不存在 `.enter` / `.esc` 之類 key modifier；`t-on-*.<mod>` 的 mod 只有 `.stop/.prevent/.self/.capture`。
3. **白屏 ≠ 該頁的 bug**：`/woow` 是 hash 導航 single root，`Destroying the root component` 後全站白到 full reload。debug 時先看 console **第一個** `OwlError`（不是 `Destroying the root` 那行），那才是真因；且修完一個要**重新 reload 再驗**，因為先炸的會遮住後面的（本案撞 2 就是這樣才現形）。

## 偵測 / 測試建議

grep 守線（CI / pre-commit 可掛）——抓「非 synthetic 的 event modifier」（會命中 `.enter` 之類 Vue 殘留）：

```bash
grep -rn "t-on-[a-z]*\." src/static | grep -v "\.stop\|\.prevent\|\.self\|\.capture\|\.synthetic"
# 期望：空輸出。任何 hit = 用了 OWL 不支援的 event modifier（八成是 Vue 按鍵 modifier）
```

`t-att-` 在 component 上的偵測較難純 grep（要分辨 tag 是 component 還是 DOM element），務實做法是**每次前端改動以 headed 瀏覽器走過受影響頁面 + 確認 console 無 `OwlError`** 當收尾門檻，不靠靜態規則。

## 證據

- 撞 1 修復：commit `ebe6ec1`（`fix: heal sticky noupdate template links and owl component prop syntax`）—— `src/static/src/paas/pages/security-access/SecurityAccessDetailPage.xml:49` `<WoowIcon t-att-name="tab.icon"/>` → `<WoowIcon name="tab.icon"/>`。
- 撞 2 修復：commit `ada231f`（`fix: replace unsupported owl enter key modifier with keydown handler`）—— `SecurityAccessDetailPage.xml:24` `t-on-keyup.enter="saveName"` → `t-on-keydown="onNameKeydown"`；handler 在 `SecurityAccessDetailPage.js:115` `onNameKeydown(ev)` 讀 `ev.key === "Enter"` → `ev.target.blur()` → 委派 `t-on-blur="saveName"`。
- 頁面初版：commit `844e5c2`（`feat: add security access detail page with six tabs`）—— 兩個語法錯誤的引入點。
- 守線 grep 現況：對 `src/static` 跑上述 grep 為空輸出（已無殘留）。

## 關聯

- [[sandbox-module-test-false-green]] / [[floor-env-relative-path-empty-ext-addons]] —— 同主題家族：「某層檢查綠 ≠ 真的好」。本篇是前端 render 維度的變體（XML parse 綠 + Python 測試綠 ≠ OWL render 成功）。
- [[orm-test-green-endpoint-leak-blindness]] —— 後端同構教訓（ORM test 綠 ≠ endpoint secure）；本篇是前端版「測試綠 ≠ runtime 安全」。
- CLAUDE.md「Browser Automation」段 —— headed 工具選擇規範（UX 流程走 `playwright-cli`、視覺驗證走 `agent-browser`），是本 pitfall 的唯一攔截網。

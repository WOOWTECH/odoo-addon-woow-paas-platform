---
title: "能力 gate 收斂到後端單一欄位（前端不得自建 slug 名單）"
status: accepted
date: 2026-07-29
deciders: [platform]
tags: [frontend, owl, capability-gate, repo-lint, false-green, drift]
supersedes: []
related:
  - docs/decisions/patterns/two-layer-acl-lockstep.md
  - docs/decisions/patterns/hidden-template-presentation-chain.md
  - docs/decisions/patterns/template-vs-plan-property-boundary.md
---

# 能力 gate 收斂到後端單一欄位（前端不得自建 slug 名單）

> 適用：**某個模板／方案「支不支援某能力」同時被後端授權與前端 UI 判斷**（Reset 密碼
> 按鈕、進階設定分頁、專屬操作入口…）。判準必須是**後端 model 的一個欄位**，經 payload
> 鏡射給前端；前端**不得**用 `slug ∈ 名單` 自己算一份。兩份判準不會同時錯，只會**無聲漂移**
> ——而且漂移的方向永遠是「後端放行、前端按不到」＝**修復對租戶不可達等於沒修**。

## Context

同一個能力被判了兩次、判準不同，代價已經量化過兩輪：

| 輪次 | 兩邊判準 | 後果 |
|---|---|---|
| #357 C01 | controller = slug allowlist；model（`create` / `_build_mcp_sensitive_values`）= `mcp_enabled` | controller 放行並生密碼、model 過濾成空集合不建 secret ⇒ **密碼永不生效** |
| #460 / F-449-4 | 後端 reset endpoint = `template.has_admin_gui`；前端按鈕 = 硬編碼三筆 slug | `has_admin_gui=True` 的 8 個模板有 **5 個按不到按鈕** |

#357 只修了後端半邊（controller 收斂到 `has_admin_gui`，該行上方留了 C01 註解說明為何不能用
slug allowlist），前端沒跟上。2026-07-29 stg live DB 實查的 5 個受害者：

- `mcp-k8s` / `mcp-vk` / `mcp-opendesign` —— 自 #375 三型 MCP 上架起就漏，**缺口早於 #449**
- `open-design` / `hermes-agent` —— #449／PR #455 才剛把載體修通、stg E2E 驗過，租戶卻按不到

三個月裡沒有任何測試變紅：後端測試驗後端判準、前端沒有能力測試、E2E 只走原白名單內的
`mcp-odoo`。**典型假綠家族**——綠不代表對，是剛好沒人走那條路。

## When to use

只要一個能力的「誰有／誰沒有」同時出現在兩個地方，就套本 pattern：

1. 後端 endpoint 用 `template.<flag>` 決定放行 / 400，前端要決定按鈕顯不顯示
2. 新模板上架（seed）時，有人問「這個模板要不要也加進前端那個名單」——這個問題本身就是味道
3. 前端出現 `["a-slug", "b-slug"].includes(x.slug)` 形態的任何判斷

**不適用**（別硬套）：

- **單值 identity 判準**：`template.slug === 'odoo'`（`isOdooService`）——那是「這個服務是不是 Odoo」
  的身分判斷，不是「有沒有某能力」的能力判斷。Odoo 專屬 UI（addon 選擇器）本來就綁死那一個模板，
  沒有第二個候選，不會漂移。
- **列表操作**：marketplace 篩選 / 排序 / 搜尋裡的 `filter` / `find` / `map`——那不是布林 gate。
- **能力本身還沒有欄位**：那就先在 model 加欄位、seed 補值，而不是在前端補名單（見下方 Anti-patterns）。

## The pattern（三段一線）

```
CloudAppTemplate.has_admin_gui          ← ① 能力的唯一事實源（model 欄位 + seed）
        │
        ├── controllers/paas.py:1293    ← ② 授權：if not service.template_id.has_admin_gui → 400
        │
        └── _format_service() 的 template 子物件
              'has_admin_gui': bool(...)  ← ③ payload 鏡射，**直接搬、不做任何推導**
                     │
                     └── ServiceDetailPage.hasAdminGui
                           return this.service?.template?.has_admin_gui === true;   ← ④ 前端讀
```

四個要點：

1. **payload 鏡射不推導**：`bool(service.template_id.has_admin_gui)`，不是 `slug in (...)`、
   也不是 `category == 'mcp'`。一旦 payload 端「聰明」一次，它就變成第三個判準。
2. **前端 fail-closed**：`=== true`。欄位缺失（舊快取、尚未更新的 payload、其他 endpoint 漏帶）
   一律視同 False——寧可不顯示，也不要長出一顆按了必然失敗的按鈕。
3. **凡回傳 template 子物件的 payload 都要帶**（含 `include_details` 路徑），否則同一顆按鈕
   在不同進入點行為不同。
4. **前端 getter 用能力語意命名**（`hasAdminGui`），不要用族群語意（`isMcpAdminService`）
   ——名字叫 MCP，下一個非 MCP 的模板（`open-design`）就會被漏掉，而且名字讓人以為那是對的。

## Why not slug allowlist

| 理由 | 具體代價 |
|---|---|
| 上架新模板要「想起」兩處 | #375 三型 MCP 上架時只有後端能力對，前端漏了三個月沒人發現 |
| 漂移無聲 | 沒有任何一層會因為兩邊不一致而報錯；只有租戶會看到「別人有我沒有」 |
| 漂移方向致命 | 後端放行 + 前端擋 ⇒ 修好的功能對租戶不存在；反向（前端放行 + 後端擋）⇒ 按了拿到錯誤訊息 |
| 前端根本無從得知 | 能力是 model 屬性，前端只拿得到 payload 給的東西——猜 slug 是拿不到資料時的代償行為，正解是補 payload |

## Gate（機械化落點）

`scripts/audit/repo-lint.py` 的 **capability-gate** check（#460 S2），CI（`.gitea/workflows/repo-lint.yml`）
與 PostToolUse hook 兩邊都跑。規則**方向相反**，刻意互補——一條禁**已知的壞形態**，其餘每條各鎖住
一個「唯一的對形態」錨點。**新能力上線就會多一個錨點，此處刻意不寫條數**（同 #605 判準：只寫規則、
不寫當下狀態——狀態會過期，規則不會）；權威清單是 `scripts/audit/repo-lint.py` 的
`REPO_LINT_RULES["capability-gate"]`，下表隨之增修：

| 規則 | 語意 | 擋掉的東西 |
|---|---|---|
| `check_slug_capability_gate` | 禁**已知的壞形態** | `src/static/src/**/*.{js,xml}` 出現以 slug 做集合成員判斷 |
| `check_admin_gui_gate_anchor` | 鎖**唯一的對形態** | Reset 按鈕的 `t-if` 鏈（含祖先、含 getter body）追不到 `has_admin_gui` |
| `check_gateway_copy_gate_anchor` | 同上，錨在**文案**而非按鈕 | 提到 "Gateway API Key" 的文字節點，其 `t-if` 鏈追不到 `has_gateway` |
| `check_access_tab_gate_anchor` | 同上，錨在**頁籤**（#746） | Cloud Service 的 Access 頁籤（`setTab('access')`）其 `t-if` 鏈追不到 `ac_max_access_seats` |

第二條是重點：它不管你把判準寫成什麼樣子、getter 改叫什麼名字——**只要追不到那個欄位就 fail**。
所以「第三個判準」不論長相（category、`mcp_enabled`、新名單）都會在 review 之前被擋下。

第三條（#530）示範**同一個 pattern 的另一種錨點**：要守的不是「按鈕在不在」而是「這句話會不會
對不該看到的人出現」，所以錨在文案字面。它揭露的漂移方向也和 #460 相反——#460 是**後端放行、
前端擋住**（功能不可達）；#530 是**前端宣告、後端不做**（承諾跳票）：Reset 提示語判
`hasAdminGui`，但真正的交付判準是 `service._gateway_public_url()` 非空，而 `has_admin_gui=True`
的 8 個模板裡只有 `hermes-agent` 有 gateway ⇒ 其餘 7 個的租戶被告知一件永遠不會發生的事。
兩個方向的共同點不變：**兩個判準不會同時錯，只會無聲漂移**。

> 複合 gate（`hasAdminGui and hasGateway`）是合法的：兩個都是後端鏡射欄位，各自代表一段語意
> （按鈕在不在 ／ 會不會真的交付）。本 pattern 禁的是**自己算一份**，不是「只准引用一個欄位」。

第四條（#746）把同一個 pattern 推到**方案（plan）屬性**上：Access 頁籤的顯示判準是
`subscription.ac_max_access_seats > 0`（設計拍板 6），而 `ac_` 家族是 capability-orthogonal 的
方案上限（OQ-C 拍板），**不是模板屬性**。誘惑在於「有 Access 的就那幾個模板」今天剛好成立
——下一個賣 AC 席位的方案上架就漏，漏的方向仍是 #460 那個：後端有設定、租戶看不到頁籤。
它也示範能力欄位可以住在 `template` 子物件以外的地方（這裡是 `subscription`），錨點規則不變：
**t-if 鏈追得到那個欄位**。

形態規則的 scope（抓什麼／不抓什麼，實測矩陣見 #463 的 PR 說明）：

```
✗ ["mcp-odoo"].includes(t.template.slug)      ✓ template.slug === 'odoo'（單值 identity）
✗ MCP_SLUGS.includes(slug)                    ✓ disabledStates.includes(service.state)
✗ SLUGS.indexOf(t.slug) !== -1                ✓ ['text','password'].includes(spec.type)
✗ slug.includes('mcp-')（prefix 猜能力）       ✓ templates.filter(t => t.slug !== cur)（列表操作）
                                              ✓ 註解／字串裡引述壞形態（先剝 noise 再比對）
```

**刻意不抓** `filter` / `find` / `map`：那是列表操作不是布林判準，抓了會誤殺 marketplace。
規則沒有 baseline 白名單——要豁免就得改規則本身，讓豁免在 diff 裡被看見。

改動這條 gate 時的紅燈自驗（比照 seed-topology / reuse-values）：把 slug 陣列 gate 塞回
`ServiceDetailPage.js`，`repo-lint.py --all` 必須同時報「形態」與「anchor」兩條，再還原。

## Anti-patterns（具體別做）

1. **在前端補名單把新模板加進去** —— 這是把 #460 再做一次，只是這次名單有 8 筆。缺欄位補
   payload，不缺欄位就讀欄位。
2. **payload 端「順手」推導**（`'has_admin_gui': tmpl.slug in MCP_SLUGS`）—— 守線會綠（前端確實讀了
   欄位），但事實源被搬到 controller，等於把漂移藏得更深。
3. **用 category / 族群欄位代替能力欄位** —— `category == 'mcp'` 今天剛好等價，明天上架一個
   非 MCP 但有 admin GUI 的模板（`open-design` 就是）就又漏。能力要有自己的欄位。
4. **只改前端不改後端 payload**（或反之）—— 半套修復比不修更難察覺，因為兩邊各自都「看起來對」。
5. **把 gate 從按鈕搬到外層 wrapper 以繞過 anchor** —— anchor 會沿祖先鏈找，繞不掉；真要改結構
   就讓 `t-if` 仍然引用能力欄位。

## See also

- [[two-layer-acl-lockstep]] —— 同一 invariant 被多層 enforcement 守時的 lock-step 紀律（本 pattern 是它在
  「能力呈現」而非「ACL」上的變體：那邊漂移＝安全洞，這邊漂移＝功能不可達）
- [[hidden-template-presentation-chain]] —— 呈現層過濾的 SSOT + DRY helper 全鏈（同樣是「一個 model 欄位
  要一路貫穿到 UI」的形狀）
- [[template-vs-plan-property-boundary]] —— 新能力該放 template 欄位還是 plan 屬性
- `src/controllers/paas.py`（`api_service_reset_admin_password` 的 #357 C01 註解、`_format_service` 的
  `has_admin_gui` 鏡射）、`src/static/src/paas/pages/service/ServiceDetailPage.js`（`hasAdminGui` getter）
- Issues：#357（C01 後端收斂）、#460（前端 8 選 5 不可達）、#462（S1 雙邊修復）、#463（S2 守線）

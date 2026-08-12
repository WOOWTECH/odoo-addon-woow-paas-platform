---
title: "憑證值落到上游預設時，功能測試永遠是綠的 —— 判準要換成「這個值是不是全世界都知道」"
status: accepted
date: 2026-08-12
deciders: [platform]
tags: [credential, seed, cloud-app-template, false-green, e2e, security, coverage-guard]
supersedes: []
related:
  - docs/decisions/patterns/capability-gate-single-source-field.md
  - docs/decisions/patterns/credential-claim-test-fixture-coupling.md
  - docs/decisions/patterns/seed-topology-invariants.md
  - docs/tracking/pitfalls/provision-e2e-green-on-chart-defaults.md
---

# 憑證值落到上游預設時，功能測試永遠是綠的 —— 判準要換成「這個值是不是全世界都知道」

> 適用：某個設定值來自**上游**（image 內建的 `_DEFAULT_CONFIG`、chart `values.yaml` 預設、
> 程式碼裡的硬編 fallback），而該值所在的欄位是**憑證性質**（password / token / key /
> secret / credential）。
>
> 此時**所有以「能不能動」為判準的驗證都會通過**：值存在、服務起得來、登入得了、流程走得通。
> 綠燈不是「沒查到問題」，綠燈**就是漏洞本人**——正因為那個值可用，功能軸才綠。
> 唯一能讓它變紅的是換一把尺：**這個值是不是全世界都知道。**

## When to use（兩個明確觸發點，不是「要注意」）

本 pattern 有兩個可判定的觸發時刻。看到條件成立就**強制執行下面的判準一次**，不論你手上原本的任務是什麼。

### T1 —— 上架／改動 template seed 時

**條件（AND）**：

1. 你在動 `src/data/cloud_app_templates.xml`（新增一筆 template，或改既有 record 的部署形態）；**且**
2. 那個 image／chart 有任何一個**帶身分的入口**：admin GUI、管理 API、DB 帳號、webhook secret、gateway API key。

→ **必須明確回答一題：這個入口的憑證由誰決定？** 只有兩個合法答案，而且兩個都要在 seed 上留下**字面痕跡**：

| 答案 | seed 上必須看得到什麼 |
|---|---|
| (a) **平台鑄造並注入** | 對應的能力欄位明寫為 True（本 repo ＝ `src/models/cloud_app_template.py:has_admin_gui`），且該 template 必須落在某條**家族級覆蓋率守線**的量化域內（見下方〈落地形態〉） |
| (b) **租戶自己在首次開機時設定** | 明寫為 False **並附多行 WHY 註解**（既有範本：`cloud_app_templates.xml` 裡 HAOS 與 HA 兩筆，各自寫明「管理者帳號由租戶在 onboarding 自訂」） |

🔴 **沒有第三個選項，但有第三個結果**：(a)、(b) 都沒做時，欄位落到 model 預設 `False`，平台從不鑄造，值靜默落到**上游預設**。它不是被選出來的，是**沒被選**的沉默結果——而它在 seed 的 diff 上長得跟 (b) 一模一樣（都是「沒有那一行」／「False」）。

⇒ **判準：seed record 上找不到「明寫的欄位 ＋ 理由」，現況就是上游預設。** 不要用「應該有人想過」補位。

### T2 —— 調查／驗收途中讀到某個值的來源時

**條件（AND）**：

1. 你在追一條鏈（debug、e2e 驗收、code review、架構盤點），讀到某個值來自上游的預設或 fallback——`_DEFAULT_CONFIG[...]`、chart `values.yaml` 的預設、`or "xxx"` 形態的硬編 fallback；**且**
2. 該值所在的欄位名含 `password` / `token` / `key` / `secret` / `credential`（或它語意上就是這些）。

→ **停一次，把這一格單獨用下面的判準判過，再回到你原本的任務。**

⚠ T2 的觸發條件刻意**不含**「你正在做安全審查」。理由見下方〈為什麼這不是粗心〉：#686 那一格正是在一次**功能**驗收裡被讀到的，而那次驗收的判準把它染成了綠色。

### 不適用

- **值不是憑證**（副本數、port、逾時、resource limit…）落到上游預設：那是正常的 chart 用法，本 pattern 不管。
- **平台值傳導鏈斷掉、上游預設碰巧能動**：那是另一條，見下方〈與相鄰條目的界線〉。

## 判準：換一把尺（三問）

功能軸問的是「能不能動」。憑證軸問的是「**這個值是不是全世界都知道**」。可以機械地問三題：

1. **這個值印得出來嗎？** 上游 repo / chart / image 公開或內部可讀 ⇒ 那個值是**公開常數**，不是密碼。查證方式是去上游把那一行讀出來，不是推測。
2. **它後面有沒有第二道門？** 這個入口實際掛在哪：服務的公開根 URL？authproxy sidecar 後面？只在叢集內？—— 「pod 是單一容器、沒有 authproxy」這種結構事實，決定了公開常數等不等於**任何人可用**。
3. **拿一把錯的憑證再打一次。** 只打對的那把、拿到 200，證明不了 200 的意思——fail-open 的服務對每一把鑰匙都回 200。**必須配陽性對照**：對的 → 200、錯的 → 401，才證明「200 是真的登入成功」。

三問任一命中就是紅燈，不需要湊齊。

## 為什麼功能測試永遠綠（機制，不是疏忽）

值存在 ⇒ 服務起得來 ⇒ 登得進去 ⇒ 端到端流程走得通。凡是以**可用性**為判準的驗證，這一格都會給綠燈，而且**綠得理直氣壯**。

所以這不是「測試沒寫夠」——**再加一條功能測試，加的還是功能測試**。要變紅只能換判準，不能加數量。

### 🔑 為什麼這不是粗心：判準決定了你看到什麼顏色

#686 最值得記下來的不是「漏了一個欄位」，是這一段：

負責該服務 e2e 驗收的 agent **一路挖到了** `_DEFAULT_CONFIG["admin_password"] = "admin"` 那一行，**並把它寫進了報告**——然後歸類為「不影響本次驗收結論」。

**那個歸類在它自己的判準下是對的。** 它的驗收判準是功能可用性；字面 `admin` 確實不影響「能不能登入」——它甚至正是「能登入」的原因。

⇒ 同一個觀察，在功能尺上是「符合預期」，在憑證尺上是 P1。**不是沒看到，是看到了、而判準把它染成了綠色。**

這一段決定了處方的形狀：處方**不能**是「調查要更仔細」（那次調查已經夠仔細了——該讀的那一行讀到了、也寫進報告了），只能是「**遇到 T2 的條件就強制切換一次判準**」。

## 反例：#686 全程（closed，PR #688）

### 時間軸（repo 可驗）

| 日期 | commit | 事件 |
|---|---|---|
| 2026-06-29 | `de5c07d9` | `cloud_app_mcp_hermes` 首建（pilot）。`has_admin_gui` **從未寫過**，落 model 預設 `False` |
| 2026-07-01 | `a4863518` | odoo / n8n MCP server 上架，commit message 自述「複製 hermes pilot」 |
| 2026-07-16 | `009e0388` | #357 接上 `has_admin_gui` 供給鏈，為 odoo / n8n / odoo-manage 三筆各加 `eval="True"`；**通篇沒提 hermes 一個字** |
| 2026-08-12 | PR #688 | 補 `has_admin_gui eval="True"`，並把家族覆蓋率守線補到 7/7 |

（不寫「活了 N 天」：那個數字會隨敘述漂移，日期不會。從 seed 首建到修好是 2026-06-29 → 2026-08-12。）

### 後果鏈

`cloud_app_mcp_hermes` 沒設 `has_admin_gui`
⇒ 平台從不鑄造 `admin_password`（鑄造點 `src/models/cloud_service.py:CloudService.create` 只對 `has_admin_gui` 為真的 record 走 `_generate_admin_password` ＋ `_set_admin_password_secret`）
⇒ 值落到 image 內建 `mcp-admin-core` 的 `_DEFAULT_CONFIG["admin_password"] = "admin"`
⇒ **admin GUI 掛在服務公開根 URL、密碼是字面 `admin`**（MCP pod 單一容器、無 authproxy sidecar）
⇒ 拿到這組密碼即可改寫 `connection` 設定並輪替讀取 `mcp_auth_token`＝該實例完整 MCP 存取權。

### 決定性驗證（stg `svc-b2e8a605`，port-forward 直打、唯讀）

```
POST /api/auth/login {"password":"admin"}      -> 200   ← 字面 admin
POST /api/auth/login {"password":"wrong-xyz"}  -> 401   ← 陽性對照，證明 200 不是 fail-open
GET  /                                         -> 200   ← 無 basic-auth，admin GUI 直接可達
```

第二行不是多餘的禮貌。**沒有它，第一行的 200 什麼都不證明。**

### 這一格為什麼躲過了既有的全稱守線

當時 repo 裡**已經有**一條對憑證載體的全稱斷言：
`src/tests/test_admin_password_carrier_449.py:test_every_admin_gui_template_declares_a_carrier`。
它甚至做對了取樣層陽性對照（`src/tests/test_admin_password_carrier_449.py:_templates_with_admin_gui` 保證非空）。

但它的量化域是 `search([('has_admin_gui', '=', True)])`。

⇒ **漏判的那一格在結構上就不在它的定義域內。** 這類守線再加幾條、寫得再嚴謹，都不會碰到 hermes——它們全都是對「已經被判為需要平台憑證的那些 record」量化的。

🔴 這是本條最可移植的一句：**「涵蓋哪些 record」要看可達性，不是屬性篩選。** 用被漏判的那個屬性去篩選，等於用漏洞當濾網。

## 落地形態（指向既有機制，刻意不複述）

兩件事必須成對，缺任一件都會讓下一個成員重演：

1. **平台側要有「鑄造並注入」的閘門**，且閘門是**單一欄位**——本例＝`src/models/cloud_app_template.py:has_admin_gui`（能力判準收斂到單一欄位的理由見 [[capability-gate-single-source-field]]）。
2. **該閘門必須被家族級覆蓋率守線納管**，而守線的量化域要取自**結構座標（chart）而非閘門欄位本身**——否則就重演上一節那個「用漏洞當濾網」的問題。

本 repo 的落地件：`src/tests/test_mcp_server_template_seed.py:TestMcpServerFamilyCoverage`（PR #688）。

⚠ **本文刻意不複述它的判準與斷言內容**——複述必漂。要知道它怎麼判、為什麼那樣判（判準取自 chart 座標、取樣層陽性對照、輸出「是哪一個」而非「差幾個」、反向守衛防隨便掛 TMPL 字串），**去讀那個類自己的 docstring**。

## 與相鄰條目的界線

| 相鄰條目 | 它管什麼 | 為什麼不是同一條 |
|---|---|---|
| `docs/tracking/pitfalls/provision-e2e-green-on-chart-defaults.md` | 平台值**傳導鏈斷**、chart 預設碰巧自足 ⇒ e2e 綠。處方＝斷言釘在「只有平台側值才會產生的差異點」 | 那個處方對本條**無效**：`has_admin_gui=False` 的模板（HAOS / HA）本來就不該有平台鑄造值，對它們斷言「平台獨有值存在」是誤報。本條要判的是**那個 boolean 該不該是 True**——分類判斷，不是傳導斷言 |
| [[capability-gate-single-source-field]] | 同一個能力別被判兩次（後端欄位 vs 前端 slug 名單） | 那條**假設欄位已經有值**，守的是兩份判準別漂移。本條守的是「這筆 record 上那個欄位到底有沒有被判過」 |
| [[credential-claim-test-fixture-coupling]] | **硬化既有** credential 路徑時，所有自建該 credential 的 fixture forward-coupled | 那條的前提是路徑已經接上。本條是「這條路徑從來沒被接上」 |
| [[seed-topology-invariants]] | seed 拓撲計數的非 monotonic 陷阱 | 同樣談 seed 守線的形狀，但那條管**計數**、本條管**單一 record 的分類是否被明確做過** |

## 本 pattern 沒有機械強制的部分（別把綠燈讀成沒事）

- **T1 的家族覆蓋率**：已機械化，但**只涵蓋 mcp-server chart 家族**。其他 chart 家族**沒有**等價守線。⇒ `repo-lint --all` 綠、seed 測試全綠，都**不代表**「沒有第二個 hermes」。
- 🔴 **本 doc 已登記進 pattern-guard 的 `seed-data` class，但編輯 T1 那個檔時實際上看不到它。**
  pattern-guard 有第二層收斂器（`scripts/audit/doc_citation_index.py`）：被編輯的檔若**逐字引用**過任何 pattern doc，就只推被引用的那幾份，其餘一律壓掉；零引用的檔才退回 class 推播。
  落檔當下實測 `src/data/*.xml` 共 29 檔，**只有 `cloud_app_templates.xml` 一檔有引用**（引用的是 `hidden-template-presentation-chain`）——而那正是 T1 唯一會動到的檔。
  ⇒ 其餘 28 檔會推到本 doc，**最相關的那一檔不會**。
  這一格本身就是本 pattern 的同構案例：**登記過 ≠ 會被送到人面前**，而兩者的 gate 輸出（repo-lint 全綠）一模一樣。
  **修法**：在 `cloud_app_templates.xml` 的檔頭註解裡逐字寫一次本 doc 的 slug（收斂器認裸 slug／完整路徑／`[[wiki-link]]` 三種形態）。本 doc 落檔的那次改動刻意不做——它的硬約束是不動 seed——所以這個缺口在有人補上那行註解之前一直存在。下一個動 `cloud_app_templates.xml` 的人請順手補；補完可用 `python3 scripts/audit/doc_citation_index.py --uncited` 複驗本 slug 已不在盲區清單裡。
- **T2（調查時換尺）完全無法機械化**：沒有任何 gate 能判斷「你剛剛讀到的那一行是不是憑證」。這一段只能靠人／agent 在觸發條件成立時自己執行——所以上面的觸發條件寫成了可判定的形式，而不是「要注意憑證安全」。
- 本條**不涵蓋**既有實例的補救。翻閘門欄位**不會**追溯改掉已部署實例的密碼（chart 的 seed 邏輯是 seed-if-absent，`/data/config.json` 已存在就早退），存量實例必須有人主動觸發一次 Reset——那是營運決策，不是本 pattern 能保證的事。

## 相關

- Issue #686（closed）／ PR #688 —— 本條的反例全程與守線落地
- `docs/tracking/pitfalls/provision-e2e-green-on-chart-defaults.md` —— 同家族但判準不同的假綠（見上表）
- [[capability-gate-single-source-field]]、[[credential-claim-test-fixture-coupling]]、[[seed-topology-invariants]]

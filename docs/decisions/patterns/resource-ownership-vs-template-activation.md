---
title: 資源歸屬 vs 配置啟用雙關聯模型
category: decisions
status: active
created: 2026-06-05T14:07:54Z
updated: 2026-06-05T14:35:00Z
related:
  - ../../plans/2026-Q2/agent-runtime-pod-design.md
  - ../../plans/2026-Q2/2026-05-30-skill-dual-scope-design.md
  - constraint-input-trust-expansion.md
  - scope-flip-write-guard-not-ir-rule.md
  - mcp-acl-6-rule-split.md
---

# Pattern: 資源歸屬 vs 配置啟用雙關聯模型（ownership vs activation）

> 適用：平台級 agent 資源（目前兩個 instance：`skill`、`mcp_server`）同時需要兩種語意 —
> 「**這個資源屬於誰**」（租戶邊界 / ACL / cascade）與「**哪個 agent 配置啟用它**」（工具
> 選擇 / least privilege）。正解是**兩條獨立關聯**：歸屬 = 資源端 `Many2one` FK
> （`project_id`，1:N）、配置 = template 端 `Many2many`（M:N），再以 **subset constraint**
> 黏合（配置 ⊆ 綁定 projects 的歸屬聯集 ∪ system scope）。看到「project 已有 One2many、
> template 又有 M2M，好像重複」時 — 不是重複，是兩層；**不要**試圖用單一關聯同時表達
> 兩種語意。

## Context

agent-runtime epic 的資料模型裡，同一個資源 model 出現「兩條看似重複的關聯」：

```python
# src/models/project_project.py:56 — 歸屬層（One2many，反向視圖）
skill_ids = fields.One2many('woow_paas_platform.skill', 'project_id')

# src/models/agent_template.py:34 — 配置層（Many2many，中介表）
skill_ids = fields.Many2many('woow_paas_platform.skill',
                             relation='agent_template_skill_rel', ...)
```

`mcp_server` 完全平行：`project_project.py:50`（O2M）+ `agent_template.py:41`（M2M）。
資源 model 自身帶 `scope`（system/user）維度，由 consistency constraint 強制
`scope='user'` ⟺ `project_id` 有值（`skill.py:118`，註解明言 mirrors mcp_server）。

第二個維度兩邊**只有欄位名相同、語意不同**（見下方不對稱節）：

- `skill.state`（`skill.py:73`）= **發佈生命週期**（draft/published/deprecated/archived），
  決定可否生效；
- `mcp_server.state`（`mcp_server.py:79`）= **連線健康狀態**（draft/connected/error，
  readonly、由 sync 回寫）— MCP 沒有發佈生命週期，啟用開關是 `active` boolean
  （`mcp_server.py:46`）。

兩條關聯回答的是**不同的問題**：

| 層 | 關聯 | 真實儲存 | 回答的問題 | 服務的機制 |
|---|---|---|---|---|
| **歸屬層**（ownership） | `skill.project_id` Many2one（1:N） | 資源表上的 FK column | 「這個資源是**誰的**」 | 租戶邊界、ir.rule walk、`ondelete='cascade'`、bundle 下載授權 leg（`agent_runtime_skill.py:118-127`）、後台 UI 內嵌列表（`project_views.xml:76`） |
| **配置層**（activation） | `template.skill_ids` Many2many（M:N） | 中介表 `agent_template_skill_rel` | 「這個 agent **開哪些工具**」 | least-privilege 工具選擇、跨 project / unbound template、版本釘選空間 |

> 類比：歸屬層是「這台機器上裝了哪些套件」，配置層是「某個 app 的
> `requirements.txt`」。裝了 ≠ 每個 app 都要 import。
>
> ORM 機制備忘：`One2many` 無自身儲存，是對面 `Many2one` FK 的反向查詢視圖；在
> project 端宣告它只為兩個消費者 — template subset constraint 的
> `t.project_ids.mapped('skill_ids')` 路徑（`agent_template.py:199`）與後台表單內嵌列表。
> FK 永遠在 `Many2one` 那端。

### 生效集合公式（事實規格）

Pod 實際載入的集合由 `_build_skill_snapshot`（`agent_session.py:299-315`）決定：

```
生效 skills = （resolved template 綁定 且 state='published' 的 skills）
            ∪ （scope='system' 且 state='published' 的 skills，同名被前者遮蔽）
```

- template 由 `_resolve_agent_template`（`agent_session.py:267-288`）解析：
  project-bound published → project-bound any → `project.agent_template_id` → 平台 default。
- **`project.skill_ids`（歸屬層）不直接進 snapshot** — project 名下的 skill 沒被 resolved
  template 綁定就不會載入。它只有兩個間接角色：subset constraint 的上界、bundle 下載
  授權的 project leg。
- `scope` 與 `state` 是正交過濾：`scope='system'` 但 `state='draft'` 的 skill 不進任何 pod。

**MCP 的三個不對稱**（如實記錄，避免誤推平行）— 「歸屬 + 配置 + subset constraint」
三件結構平行，但生效語意不平行，改 snapshot / filter 邏輯時兩邊分開查證：

1. **無 system 自動聯集**：`_build_mcp_snapshot`（`agent_session.py:291-296`）**只**
   序列化 template 綁定的 MCP servers；skill 是 bound ∪ system。
2. **無生效 filter**：skill snapshot 過濾 `state='published'`；MCP snapshot **連
   `active` / `state` 都不過濾** — template 綁了就進 pod 的 mcp.json（error / archived
   也進，pod 端自行面對連線失敗）。背景：MCP 的 `state` 是連線健康狀態而非發佈
   生命週期（見上），所以「published filter」在 MCP 上沒有對應物。
3. **存在第二條 legacy 消費路徑，語意是「歸屬即啟用」**：legacy chat 路徑
   `discuss_channel._get_user_mcp_tools`（`discuss_channel.py:240-242`）與前端 API
   （`paas.py:1340` / `:1550` / `:1668`）**直接吃 `project.mcp_server_ids`、不經
   template**，filter `scope='user' and active and state='connected'`（API 端僅
   `active`）。即 dual-mode rollout（`use_agent_runtime`）的兩面：**legacy 路徑 =
   單層（歸屬即啟用、有嚴格 filter）；pod 路徑 = 兩層（template 配置挑選、無
   filter）**。skill 先天只有兩層模型一條路，沒有這條歷史路徑。

## Options

### 方案 A — 單一歸屬關聯，agent 隱式全拿（被否決）

Template 不帶 skill 綁定，agent 直接載入 project 名下全部 skills。四個硬傷：

1. **無法挑子集**：project 會囤多版本、draft、實驗品（partial unique index 即
   `(project_id, name, version)`，`skill.py:103-115`）；每多掛一個 skill = system prompt
   變大 + attack surface 變大，least privilege 蕩然無存。
2. **推導鏈會斷**：`template.project_ids` 是 M2M（`agent_template.py:48`）— template 可綁
   多個 project，而**平台 default template 必須綁 0 個 project**（tenant-neutral
   invariant，`_check_not_platform_default_when_bound` 守著，見 CLAUDE.md Known patterns
   「Seeded 平台預設 agent_template」條）。隱式推導對綁多 project 的 template 要嘛跨租戶
   聯集（洩漏）、對 unbound template 則永遠推出空集。
3. **版本釘選沒地方放**：design doc 預留 binding 可選 pin to version / follow latest
   （`agent-runtime-pod-design.md:1615`），這語意只能長在 template→skill 綁定上。
4. **system 混合邏輯散落**：bound-shadows-system 的同名遮蔽需要一個明確的 bound 集合
   作為遮蔽方。

### 方案 B — 用 FK 表達配置（`skill.template_id`）（被否決）

Cardinality 數學上不通：`Many2one` 讓 1 個 skill 只能進 1 個 template，但同 project 可有
多份 template 各自引用同一 skill 的不同組合（`_resolve_agent_template` 的搜尋鏈即預設
一個 project 可能有多筆 project-bound template）。且歸屬與啟用耦合在同一 FK 上，刪
template 會牽連資源本體的歸屬語意。

### 方案 C — 雙關聯 + subset constraint（採用）

歸屬與配置各自獨立關聯，以 constraint 黏合。design doc 拍板紀錄：
`agent-runtime-pod-design.md:1685`（「agent_template 內 reference skill name（M:N 透過
Many2many）」）。

## Decision

**兩條關聯 + subset constraint 三件套**：

```
歸屬（FK）:   skill ──belongs to──▶ project          「東西放在誰家」
配置（M2M）:  template ──enables──▶ skills 子集       「哪個 agent 用哪幾件」
約束:         配置 ⊆ （template 綁定 projects 的歸屬聯集） ∪ scope='system'
```

Constraint 實體：`_check_skill_subset`（`agent_template.py:189-210`）與
`_check_mcp_subset`（`agent_template.py:166-187`）— 擋 template 引用「它沒綁定的
project」的 user-scope 資源（cross-tenant 防線）。「⊆」語意本身就是雙層存在的證明：
若配置永遠等於歸屬全集，這條 constraint 不需要存在。

## Consequences

- **新資源類型照抄 4 件套**：新增第三種 agent 資源（connector / tool / memory provider）
  時複製同構：① 資源 model 帶 `scope` + `project_id` + consistency constraint
  ② project 端 One2many ③ template 端 Many2many ④ template 上的 subset constraint。
  Snapshot 聯集規則（是否 system 自動聯集）按該資源的產品語意**另行決定**並寫明 —
  skill 與 MCP 已經不同。
- **Constraint 的輸入信任面**：subset constraint 用 attacker-writable M2M
  （`project_ids`）做 authority 推導 → 必須搭配 grantor authority check（HC-3，
  `_check_project_ids_grantor_authority`），詳見 [[constraint-input-trust-expansion]]。
- **窄→寬 flip 另有防線**：`scope` / `project_id` 的 user→system flip 不歸本 pattern 管，
  由 `write()` guard 擋（ir.rule 看 pre-write 狀態擋不住），見
  [[scope-flip-write-guard-not-ir-rule]]。
- **ACL 面**：資源 model 混 platform public-read + workspace-scoped 兩種 scope 的
  CRUD 開法，見 [[mcp-acl-6-rule-split]]。
- **平台 default template 永遠 unbound**：方案 A 否決理由 2 的反面 — 正因配置層獨立，
  unbound public template 才能只靠 system skills 服務全平台（tenant-neutral invariant）。

## Anti-pattern

1. **讓 template 隱式繼承 project 全部資源**（「反正都是這個 project 的，直接全載」）→
   回到方案 A 的四個硬傷；review 時看到「為什麼不直接用 `project.skill_ids`」的提案即
   此味道。
2. **在資源 model 上加 `template_id` FK 表達配置** → 方案 B 的 cardinality 錯誤。
3. **把 `project.skill_ids` 當生效集合算 snapshot / 顯示「agent 會用的工具」** → 歸屬 ≠
   生效；project 名下未被 template 綁定的 skill 不會進 pod。UI 或 API 若把歸屬列表標成
   「agent 可用」即是誤導。
4. **繞過 subset constraint 直接 `sudo()` 寫 template 綁定** → constraint 是 cross-tenant
   防線的一部分，sudo 路徑（seeding / migration）寫入前自行負責等價驗證。
5. **拿 skill 的 snapshot 規則類推 MCP（或反之）** → 兩者只有「三件套結構」平行：
   system 聯集（skill 有、MCP 無）、生效 filter（skill 過濾 published、MCP 無 filter）、
   `state` 語意（發佈生命週期 vs 連線健康）、消費路徑數（skill 一條、MCP 另有 legacy
   歸屬即啟用路徑）皆不同。

## 程式碼錨點

| 角色 | 位置 |
|---|---|
| 歸屬 FK（skill） | `src/models/skill.py:43` |
| scope / state 正交維度（skill） | `src/models/skill.py:33` / `src/models/skill.py:73` |
| scope-project consistency | `src/models/skill.py:118` |
| 歸屬反向視圖（project 端） | `src/models/project_project.py:56`（skill）、`:50`（mcp） |
| 配置 M2M（template 端） | `src/models/agent_template.py:34`（skill）、`:41`（mcp） |
| subset constraint | `src/models/agent_template.py:189`（skill）、`:166`（mcp） |
| template 解析鏈 | `src/models/agent_session.py:267` |
| skill snapshot（bound ∪ system） | `src/models/agent_session.py:299` |
| mcp snapshot（bound only，無 system 聯集、無 filter） | `src/models/agent_session.py:291` |
| mcp legacy 路徑（歸屬即啟用，不經 template） | `src/models/discuss_channel.py:240`、`src/controllers/paas.py:1340` |
| bundle 下載授權 legs | `src/controllers/agent_runtime_skill.py:118` |

## 相關

- Design 拍板：[agent-runtime-pod-design.md](../../plans/2026-Q2/agent-runtime-pod-design.md)（§ skill binding M:N：L1685；pin-to-version 預留：L1615）
- Dual-scope 設計：[2026-05-30-skill-dual-scope-design.md](../../plans/2026-Q2/2026-05-30-skill-dual-scope-design.md)
- 相鄰 pattern：[[constraint-input-trust-expansion]]、[[scope-flip-write-guard-not-ir-rule]]、[[mcp-acl-6-rule-split]]

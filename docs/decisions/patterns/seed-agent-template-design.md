---
title: "Design: Seed 平台預設 Agent Template（開箱即用初始 record）"
category: decisions
status: active
created: 2026-06-01
updated: 2026-07-19
source: docs/superpowers/specs/2026-06-01-seed-agent-template-design.md
promoted-at: 2026-07-19
---

# Design: Seed 平台預設 Agent Template（開箱即用初始 record）

> 狀態：design approved（brainstorming 2026-06-01），待 writing-plans。
> 前置：#112「平台預設 agent template（Settings fallback）」已落地**機制**
> （Settings 下拉 + resolver 第 4 條 fallback + 卸載清理 + D-1~D-6 測試）。
> 本設計補 #112 刻意列為 YAGNI／待評估的那一塊：**seed 一筆真正可當預設的
> system template，讓新環境開箱即用**。

## 背景

#112 把「平台預設 agent template」的機制做完，但**刻意不 seed 資料**：

- 既有 seed `agent_template_demo`（`data/agent_template_demo.xml`）是 `state='draft'`、
  inert placeholder，故意不合格（resolver 要求 `public + published + unbound`）。
- #112 設計決策 #5：「code 功能進 repo；資料（哪個 template/config）stg 手動設」。
  理由是安全性——平台預設會經 `.sudo()` 廣播給**所有沒有自己 template 的 project、
  跨所有 tenant**；當時沒有一筆「保證 tenant-neutral」的 template 可安全 seed。

結果：新環境（含 stg）裝好 module 後 Settings「Default Agent Template」下拉是空的，
要逐 project 手動綁，呼應 `agent-runtime-stg-enablement-gap-chain.md` gap #3 的痛點。

## 目標

新環境裝好 module 後即有一筆**合格的平台預設 agent template**
（`is_public=True + state='published' + project_ids 空`），`post_init_hook` 自動把
`ir.config_parameter['woow_paas_platform.default_agent_template_id']` 指向它（**未設才指、
不覆寫**），resolver 第 4 條 fallback 直接生效。admin 只需在既有 `ai_config_woowbot`
補真 api_key 即可 spawn agent-runtime pod。

## 安全前提（重新檢視 #112 決策 #5 的唯一依據）

seed 的 template 結構上必須 **tenant-neutral**：

- **不綁 project**（`project_ids` 空）——本就是 unbound default 的合格條件。
- **不綁任何 user-scope skill / mcp_server**——`.sudo()` 廣播時不外洩任何租戶的
  user-scope 綁定。system-scope skill 由 `_build_skill_snapshot` 自動載入，template
  不需自綁。

唯有滿足以上，「seed 哪個當預設」才從營運資料安全收回成 code-seed。**實作必須守住
這個前提**（測試 S-1 即在守它）。

## 非目標（YAGNI）

- 不動 `agent_template` model / migration / ir.rule / ir.model.access。
- 不動既有 `agent_template_demo`（draft placeholder）——避免擾動 stg 上它已被手動
  publish + 綁 project 2 的現況；它與新 seed 並存，各司其職。
- 不在 model 加 `is_default` 欄位 / write 互斥（沿用 #112 的 config_parameter 唯一性）。
- 不新建專用 ai.config——沿用既有 `ai_config_woowbot`，admin 只在一處補 api_key。
- 不在 hook 裡覆寫 admin 已選的 default（idempotent，不 clobber）。

## 設計決策（brainstorming 定案）

| # | 決策 | 選擇 |
|---|------|------|
| 1 | seed 範圍 | 完整鏈：template + 沿用既有 `ai_config_woowbot`（placeholder，正式環境換 api_key） |
| 2 | 預設指向 | `post_init_hook` 自動指向 seeded template，但**未設才指、不覆寫**（idempotent） |
| 3 | ai.config 來源 | 沿用既有 `ai_config_woowbot`（`template.model_config_id` → ref 它），seed surface 最小 |
| 4 | seed 落點 | 新增專用 record `agent_template_default`，既有 `agent_template_demo` 不動 |
| 5 | noupdate | 新 seed `noupdate="1"`——admin 後續策展（改 system_prompt）更新時不被覆寫 |

### 否決的替代路徑

- **把 `agent_template_demo` 改成 published**：✗ 它 `noupdate="1"`，既有環境（stg 已手動
  publish + 綁 project 2）不會被更新、反而仍不合格；且其註解明文「must stay draft」。
- **post_init 用程式碼建 record（非 XML）**：✗ 比宣告式 XML seed 複雜、xmlid 難管理，
  偏離既有 `ai_assistant_data.xml` pattern。

## 架構與資料流

```
新環境 install woow_paas_platform
  → data/ai_assistant_data.xml      seed ai_config_woowbot（既有，先載）
  → data/agent_template_default.xml seed agent_template_default（ref ai_config_woowbot）
  → post_init_hook → _ensure_default_agent_template(env)
        config_parameter 'default_agent_template_id' 未設？
          是 → set_param = agent_template_default.id
          否 → 不動（尊重 admin 已選）

project chat (use_agent_runtime=True)
  → _resolve_agent_template(project)        [src/controllers/ai_assistant.py:755]
      ① project-bound published → ② project-bound any → ③ project.agent_template_id
      ④ platform default ← config_parameter（resolver 重驗 public+published+unbound）
  → 找到 seeded default → spawn pod（api_key 由 ai_config_woowbot 提供，未填則 fail-loud）
```

## 元件與改動清單

| 檔案 | 改動 |
|------|------|
| `src/data/agent_template_default.xml` | **新檔**：seed `agent_template_default` |
| `src/hooks.py` | 加 `_ensure_default_agent_template(env)`，於 `post_init_hook` **最前面**呼叫（避開 early return）|
| `src/__manifest__.py` | data list 加新檔（置於 `agent_template_demo.xml` 後）；版號 `18.0.1.0.29 → 18.0.1.0.30` |
| `src/tests/test_default_agent_template.py` | 追加 S-1~S-4 |

`agent_template` model / ir.rule / ir.model.access / migration / `agent_template_demo` 皆不動。
`default_agent_template_id` 已在 `_CONFIG_PARAMETERS`（hooks.py），卸載清理已涵蓋；seed
records 為 module data，卸載由 ir.model.data 自動移除。

### 1. `src/data/agent_template_default.xml`（新檔）

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- SEED.2: the platform-default agent_template — a real, published,
         tenant-neutral system template that ships with the module so a fresh
         environment is usable out of the box.
         - is_public=True + state='published' + NO project_ids → meets the
           resolver's qualification (public + published + unbound).
         - NO skill_ids / mcp_server_ids → tenant-neutral: broadcasting it via
           .sudo() to every project that lacks its own template leaks no tenant
           user-scope binding (system-scope skills auto-load in the pod anyway).
         - model_config_id → ai_config_woowbot (shared placeholder ai.config;
           admin supplies the real api_key there, on group_system field).
         - noupdate="1": admin curation of system_prompt survives module update.
         - xmlid is a frozen contract: woow_paas_platform.agent_template_default.
         Distinct from agent_template_demo (the inert draft placeholder), which
         is left untouched. -->
    <data noupdate="1">
        <record id="agent_template_default" model="woow_paas_platform.agent_template">
            <field name="name">Default Agent Template</field>
            <field name="title">Woow Assistant</field>
            <field name="description">Platform-default agent-runtime template. Tenant-neutral (unbound, no user-scope skills/mcp) so it is safe as the cluster-wide fallback for any project without its own template. Admin supplies the real LLM api_key on the bound ai.config (WoowBot Config).</field>
            <field name="system_prompt">You are the Woow PaaS platform assistant, running inside a project's agent runtime. Help the user operate their workspace: answer questions about the platform, run the skills available to you, and assist with their tasks. Be concise, professional, and supportive. Act only within the scope of the current project. When you are unsure or lack a capability, say so honestly rather than guessing or fabricating an answer.</field>
            <field name="is_public" eval="True"/>
            <field name="state">published</field>
            <field name="model_config_id" ref="ai_config_woowbot"/>
        </record>
    </data>
</odoo>
```

**載入順序**：新檔 ref `ai_config_woowbot`（在 `data/ai_assistant_data.xml`），manifest 中
新檔須排在 `ai_assistant_data.xml` 之後。置於既有 `agent_template_demo.xml`（行 50）後即可。

**約束載入時全過**：`is_public=True` → `_check_visibility_consistency` 跳過（該約束只擋
`is_public=False` 的 unbound 非 draft）；superuser 載入 → `_check_project_ids_grantor_authority`
跳過；無 mcp/skill → `_check_mcp_subset` / `_check_skill_subset` 跳過。

### 2. `src/hooks.py` — auto-point helper

```python
def _ensure_default_agent_template(env):
    """Point the platform-default agent template at the seeded system template
    on first install, WITHOUT clobbering an admin's existing choice.

    Idempotent: only sets the config_parameter when it is currently empty, so
    re-running -u (post_init fires on every update) never overrides a value the
    admin picked in Settings. The seeded template is tenant-neutral (public,
    published, unbound, no user-scope skills/mcp) so broadcasting it as the
    cluster-wide default is safe.
    """
    ICP = env['ir.config_parameter'].sudo()
    key = 'woow_paas_platform.default_agent_template_id'
    if ICP.get_param(key):
        return  # admin already chose (or a prior install set it) — don't clobber
    template = env.ref(
        'woow_paas_platform.agent_template_default', raise_if_not_found=False,
    )
    if template:
        ICP.set_param(key, str(template.id))
        _logger.info(
            "Auto-pointed platform default agent template -> %s (id=%s)",
            template.name, template.id,
        )
```

在既有 `post_init_hook(env)` 的**最前面**（docstring 之後、`company = env.ref(...)` 之前）插入
`_ensure_default_agent_template(env)`。**必須放最前面、不可加在末尾**——`post_init_hook` 有兩個
early `return`（無 company / journal 已設），加在末尾在既有環境（journal 常態已設）會被略過。
兩者關注點獨立，把 agent-template 呼叫排在最前面即保證它必執行。抽成獨立函式，是為了 S-2/S-3
可直接單測 set/不覆寫兩支。

### 3. `src/__manifest__.py`

- data list：在 `"data/agent_template_demo.xml",` 後加 `"data/agent_template_default.xml",`。
- `version`：`18.0.1.0.29` → `18.0.1.0.30`。

### 4. `src/tests/test_default_agent_template.py` — 追加 S-1~S-4

| TC | 場景 | 期望 |
|----|------|------|
| S-1 | 讀 `env.ref('woow_paas_platform.agent_template_default')` | 存在；`is_public is True`、`state == 'published'`、`not project_ids`、`model_config_id == env.ref('ai_config_woowbot')`（= resolver 合格條件 + tenant-neutral 守線） |
| S-2 | 清空 param → 呼叫 `_ensure_default_agent_template(env)` | param == `str(template.id)` |
| S-3 | param 先設成別的值（如另一 template id）→ 呼叫 helper | param **不變**（不覆寫） |
| S-4 | unbound project + param 指 seeded template → `_resolve_agent_template(project)` | 回該 seeded template（seed → resolver 端到端） |

- 測試 setUp/tearDown save & restore `default_agent_template_id`，避免跨測試污染（既有
  D-1/D-3/D-4 也會動同一 param）。
- helper 為純函式，S-2/S-3 直接 import 呼叫，不依賴重跑 post_init。

## 錯誤處理 / 邊界

| 情境 | 行為 |
|------|------|
| admin 已手動選 default | `get_param` 非空 → hook 跳過，尊重 admin |
| admin 清空後再 `-u` | 視同未設 → hook 重新指向（= 設計的「未設才指」） |
| seeded template 被 admin archive/deprecate | resolver 第 4 條重驗 `published+public+unbound`，不合格 → fail-loud `no_template` |
| `ai_config_woowbot` api_key 未填 | `_build_llm_credential` 回 `(None, None)` → 不傳 key → pod LLM 呼叫 fail-loud（預期，補 key 即解） |

## 改動範圍總結

| 檔案 | 改動 |
|------|------|
| `src/data/agent_template_default.xml` | 新檔（1 筆 seed） |
| `src/hooks.py` | +`_ensure_default_agent_template`，post_init 呼叫 |
| `src/__manifest__.py` | data list +1、版號 +1 |
| `src/tests/test_default_agent_template.py` | +S-1~S-4 |

`agent_template` model / migration / ir.rule / ir.model.access / `agent_template_demo` 皆不動。

## Related

- 前置機制：#112 設計 `docs/archived/superpowers/specs/2026-06-01-default-agent-template-design.md`。
- 觸發痛點：`docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md`（gap #3）。
- resolver 落點：`src/controllers/ai_assistant.py:755` `_resolve_agent_template`（第 4 條 fallback）。
- spawn 取 api_key：`src/services/agent_runtime_client.py` `_build_llm_credential`。
- 既有 seed pattern：`src/data/ai_assistant_data.xml`（ai_config_woowbot）、`src/data/agent_template_demo.xml`。
- ACL 背景：`src/security/ir_rules.xml` agent_template 4 rules（`is_public` carve-out = default 全員可讀依據）。

# Design: Platform Default Agent Template

> 狀態：design approved（brainstorming 2026-06-01），待 writing-plans。
> 來源脈絡：stg agent-runtime 端到端 debug（見 `docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md`）。其中 gap #3「project 無 agent_template → spawn 前 `No agent template configured`」目前只能逐 project 手動綁 template。本設計提供一個**平台級預設 template**，讓沒有自己 template 的 project 自動 fallback，免去逐一手動綁。

## 目標

新增一個「平台預設 agent template」：當某個 project 沒有自己綁定的 agent_template（也沒有 `project.agent_template_id`）時，agent-runtime chat 自動 fallback 用平台預設，而不是報 `No agent template configured`。

平台預設由 admin 在 **Settings → Woow PaaS → AI Assistant Configuration** 區塊以單選下拉指定，與既有的「Default AI Configuration」「Default AI Assistant」並排，沿用同一 pattern（`res.config.settings` Many2one + `config_parameter`）。

## 非目標（YAGNI）

- 不在 `agent_template` model 加 `is_default` boolean 欄位 / write 互斥 / DB constraint（改用 config_parameter，唯一性天然保證）。
- 不改 seed（`agent_template_demo.xml`）——「哪個 template 當預設、綁哪個 ai.config」屬營運資料，於各環境（stg）手動設定。新環境開箱即用留待日後單獨評估（牽涉 `ai.config` 的 seed 化）。
- 不加 settings onchange 警告（resolution 端已 fail-loud）。
- 不動既有 4 條 agent_template ir.rule。

## 設計決策（brainstorming 定案）

| # | 決策 | 選擇 |
|---|------|------|
| 1 | 預設的承載方式 | **Settings Many2one + config_parameter**（非 model boolean）——與既有 default AI config/assistant 一致；唯一性由單一 config_parameter 天然保證 |
| 2 | 唯一性 | 天然唯一（單一 config_parameter 只存一個 id），無需 write 互斥 |
| 3 | 合格條件把關 | config_parameter 無 DB constraint → 把關移到 **resolution 端**：必須 `is_public=True AND state='published'`，否則視同沒設（fail-loud 經 `no_template`） |
| 4 | fallback 順序 | **平台預設為最後 fallback**，project 自己的綁定永遠優先（「預設」= 沒指定時才用） |
| 5 | seed 範圍 | code 功能進 repo；資料（哪個 template/config）stg 手動設 |

## 架構與資料流

```
project chat (use_agent_runtime=True)
  → _resolve_agent_template(project)        [src/controllers/ai_assistant.py]
      ① project-bound published template
      ② project-bound any template
      ③ project.agent_template_id
      ④ platform default ← ir.config_parameter['woow_paas_platform.default_agent_template_id']
           （resolution 端重新驗 is_public + published；不合格 = 視同沒設）
  → 找到 → spawn pod；找不到 → SSE error 'no_template'
```

平台預設的 id 存於 `ir.config_parameter`，由 admin 在 Settings 設定；resolution 端讀取 + 防呆 + 合格驗證。

## 元件與改動清單

### 1. `src/models/res_config_settings.py` — 新增 Many2one 欄位

於 AI Assistant Configuration 群組（緊接 `woow_ai_assistant_id` 後）新增：

```python
woow_default_agent_template_id = fields.Many2one(
    'woow_paas_platform.agent_template',
    string='Default Agent Template',
    config_parameter='woow_paas_platform.default_agent_template_id',
    help='Platform-wide fallback template used by any project that has no '
         'template of its own (agent-runtime pod chat).',
)
```

存入 `ir.config_parameter` key `woow_paas_platform.default_agent_template_id`（值為 template id 字串）。

### 2. `src/views/res_config_settings_views.xml` — 新增下拉 row

在 `block name="ai_assistant"` 內、`woow_ai_assistant_id` row 之後、Test Connection button 之前：

```xml
<div class="row mt8">
    <label for="woow_default_agent_template_id" class="col-lg-3 o_light_label"/>
    <field name="woow_default_agent_template_id" class="col-lg-6"/>
</div>
```

### 3. `src/controllers/ai_assistant.py` — `_resolve_agent_template` 加第 4 條 fallback

於既有 3 條 fallback 之後（method 開頭已 `.sudo()`）：

```python
if not template:
    # ④ platform default from Settings (config_parameter). Last fallback so
    # a project's own binding wins. Re-validate public+published here since
    # config_parameter has no DB constraint — a stale/unpublished/non-public
    # id is treated as "no default" (fail-loud via the no_template branch).
    default_id = request.env['ir.config_parameter'].sudo().get_param(
        'woow_paas_platform.default_agent_template_id'
    )
    if default_id:
        candidate = AgentTemplate.browse(int(default_id)).exists()
        if candidate and candidate.is_public and candidate.state == 'published':
            template = candidate
return template or None
```

**安全性說明**：第 4 條沿用 method 開頭的 `.sudo()`。default template 經把關必為 `is_public`（既有 ir.rule `agent_template_rule_public_readonly` 本就讓全體 internal user 可讀），sudo 僅做 read，內容（system_prompt / model_config / skills）全由 admin 策展 → 不構成越權讀取。三條件（`is_default 概念` 由 config_parameter 承載 + `is_public` + `published`）防禦性全列，避免未來把關漂移造成 silent 漏洞。

**防呆**：`int(default_id)` 可能因髒值 raise；`.exists()` 處理 template 已刪。實作時 `int()` 包在合理防護下（值非數字 → 視同沒設，不 crash chat）。

### 4. `src/hooks.py` — 卸載清理

`_CONFIG_PARAMETERS` list 內（緊接 `default_ai_assistant_id` 後）新增：

```python
'woow_paas_platform.default_agent_template_id',
```

（CLAUDE.md 明文：`config_parameter` 卸載不自動清，必須同步此 list。）

### 5. `src/tests/test_agent_template_default_fallback.py` — 新測試（守 fallback + fail-loud）

| TC | 場景 | 期望 |
|----|------|------|
| D-1 | project 無綁定 + 設合格 default（public+published）| `_resolve_agent_template` 回 default |
| D-2 | project 有自綁 published template + 也設 default | 回 project 自己的（順序 A：project 優先） |
| D-3 | default 指向未 published template | 視同沒設 → 回 None |
| D-4 | default 指向非 public template | 視同沒設 → 回 None |
| D-5 | config_parameter 髒值（template 已刪 / 非數字字串）| `.exists()`/`int` 防呆，不 crash，回 None |

## 錯誤處理

- **default 未設**：`get_param` 回空 → 跳過第 4 條 → 既有 `no_template` 行為不變。
- **default 不合格（未 published / 非 public / 已刪 / 髒值）**：resolution 端把關 → 視同沒設 → `no_template`（fail-loud，不靜默誤用）。這是設計刻意——呼應 stg debug 的核心教訓：「設了卻靜默不生效」最難 debug。

## 改動範圍總結

| 檔案 | 改動 |
|------|------|
| `src/models/res_config_settings.py` | +1 Many2one 欄位 |
| `src/views/res_config_settings_views.xml` | +1 下拉 row |
| `src/controllers/ai_assistant.py` | `_resolve_agent_template` +第 4 條 |
| `src/hooks.py` | `_CONFIG_PARAMETERS` +1 行 |
| `src/tests/test_agent_template_default_fallback.py` | 新測試檔（D-1~D-5） |

`agent_template` model / migration / seed / ir.rule **皆不動**。

## Related

- 觸發脈絡：`docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md`（gap #3 手動綁 template 的痛點）。
- 既有 pattern 參照：`res_config_settings.py` 的 `woow_ai_config_id` / `woow_ai_assistant_id`（`config_parameter` Many2one）。
- resolution 落點：`src/controllers/ai_assistant.py:755` `_resolve_agent_template`。
- ACL 背景：`src/security/ir_rules.xml` agent_template 4 rules（`is_public` carve-out 是 default 全員可讀的依據）。

# Pitfall: 平台預設 agent_template 被綁到 project → auto-enable 靜默全失效

> 來源:stg debug session 2026-06-02。症狀是 stg 上已開「Auto-enable Agent Runtime for New Projects」,但新建的 project(`test2`)`use_agent_runtime` 沒被翻 True。逐條驗 readiness gate 才發現:**平台預設 template `agent_template_default`(id=2)被綁到了某個 project(`test1`)**,觸發 `_platform_default_agent_template` 的跨租戶外洩 guard(`not project_ids`)→ 判它不合格 → gate 第 3 條(合格平台預設)永遠 False → **每個新 project 都靜默不 auto-enable,且無任何錯誤訊息**。
>
> 這是 [[agent-runtime-stg-enablement-gap-chain]] gap #3「project 無 agent_template」的**緩解措施(seed 平台預設 + post_init auto-point)反被打破**的失敗模式:seed 機制讓「新環境天生有合格平台預設」,但**任何人把那筆 seed 預設綁到一個 project,就同時毀掉全 cluster 的 fallback**。tenant-neutral invariant 的設計依據見 [`docs/superpowers/specs/2026-06-01-seed-agent-template-design.md`](../../superpowers/specs/2026-06-01-seed-agent-template-design.md) 與 `CLAUDE.md`「Seeded 平台預設 agent_template 的 tenant-neutral invariant」條。
>
> 程式守衛(防再犯):PR #125 `fix/agent-template-default-bind-guard`。

## Symptom

- Settings 的「Auto-enable Agent Runtime for New Projects」**已開**(`default_enable_agent_runtime='True'`),operator url+api_key **也都設了**,但 `/woow` 新建 project 的 `use_agent_runtime` 仍是 `False`。
- **完全沒有錯誤訊息** —— auto-enable 只是「沒發生」。project 表單 / DB 看不出任何異常,`agent_template_id` 欄位本來就空(設計如此,見下)。
- 既有 project 可能混合狀態:平台曾經 ready 時建的(`test1`)是 True,後來綁壞之後建的(`test2`)是 False —— 容易誤以為「有時有用有時沒用」。

## Why it happens — 兩個「靜默」疊在一起

### 1. auto-enable 受 3 條 fail-closed gate 控制,toggle 只是其一

`_agent_runtime_autoenable_ready`(`src/controllers/ai_assistant.py:43`)是 `/woow` 建立 project 時**唯一**會翻 `use_agent_runtime=True` 的閘門(`_create_project`,`ai_assistant.py:1336-1337`)。三條 AND,任一 False 就**靜默** no-op:

| # | 條件 | False 時 |
|---|------|---------|
| 1 | toggle `default_enable_agent_runtime` ∈ ('true','1') | admin 沒開 / 沒按儲存 |
| 2 | `get_agent_runtime_client(env)` 非 None | operator url 或 api_key 缺一 |
| 3 | `_platform_default_agent_template(env)` 非空 | **見下** |

### 2. 平台預設「合格」判定含 `not project_ids` —— 綁 project 就不合格

`_platform_default_agent_template`(`ai_assistant.py:18-40`)要求 config 指向的 template:`exists + is_public + state=='published' + 無 project_ids`。最後一條是**跨租戶外洩 guard**(鏡像 `_resolve_agent_template` ④,`ai_assistant.py:850-856`):一個綁了 project 的 template 帶該租戶的 user-scope MCP/skill,不能當全域 fallback 廣播。

所以當有人(UI / 後台 / sudo)把 `default_agent_template_id` 指向的那筆 template **綁到任一 project**:
- gate 第 3 條 → False → **新 project 不 auto-enable**(本 incident)。
- 即使 `use_agent_runtime` 已是 True,chat-time `_resolve_agent_template` ④ fallback 也回空 → 聊天會報 `no_template`。

兩條路徑都**無錯誤訊息**:guard 是 fail-closed「悄悄回空」,不是 raise。

### 關鍵:「連上 default template」是 runtime fallback,不是欄位寫入

`_create_project` **只**塞 `use_agent_runtime`,**從不**寫 `project.agent_template_id`(`ai_assistant.py:1317-1339`);model `create()` override 也不碰(`src/models/project_project.py:121`);`agent_template_id` 欄位無 default/compute/onchange(`project_project.py:75`)。新 project 的該欄位**空白是正常的**——「連上 default」是 chat 時經 `_resolve_agent_template` ④ 動態 resolve。**別把欄位空白誤判成「沒連上」**(那是另一種認知落差,非本 bug)。

## How to avoid / 診斷 checklist

一鍵驗 readiness gate(Odoo shell,把哪一條 False 印出來):

```python
from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
    _agent_runtime_autoenable_ready, _platform_default_agent_template)
from odoo.addons.woow_paas_platform.services.agent_runtime_client import get_agent_runtime_client
ICP = env['ir.config_parameter'].sudo()
print('1 toggle =', repr(ICP.get_param('woow_paas_platform.default_enable_agent_runtime')))
print('2 client =', 'OK' if get_agent_runtime_client(env) else None)
print('3 tmpl   =', bool(_platform_default_agent_template(env)))   # ← 本 incident 是這條 False
print('GATE     =', _agent_runtime_autoenable_ready(env))
```

第 3 條 False 時,查那筆平台預設**為何**不合格:

```python
T = env['woow_paas_platform.agent_template'].sudo()
tid = int(ICP.get_param('woow_paas_platform.default_agent_template_id'))
t = T.browse(tid)
print(t.name, 'is_public=', t.is_public, 'state=', t.state, 'project_ids=', t.project_ids.ids)
# project_ids 非空 → 就是這個坑;它被綁到 project 了
```

**修法(資料層,本 incident)**:把那筆平台預設從 project 解綁,恢復 unbound:

```python
T.browse(tid).write({'project_ids': [(3, <project_id>)]}); env.cr.commit()
```

解綁後該 project 仍經 ④ fallback 拿到同一 template(行為不變),但全 cluster 的 fallback 恢復。既有 project 不會回溯翻 `use_agent_runtime`,需另跑 `backfill_existing_projects_use_agent_runtime(env)`。

**防再犯(程式層,PR #125)**:`agent_template` 新增 `@api.constrains('project_ids')` `_check_not_platform_default_when_bound`(`src/models/agent_template.py`)—— 當被綁的 template 正是設定的平台預設時,寫入即 raise `ValidationError`,在綁定當下 fail loud。**不對 superuser/admin 放行**(沒有合法路徑會綁設定的預設);反方向(選已綁 project 的 template 當預設)已由 `res.config.settings` 欄位 domain 擋住(`src/models/res_config_settings.py:52`)。

### 正確的分工(別再搞混)

- `agent_template_default`(seed)= **平台 cluster-wide fallback**,**必須永遠 unbound**。要拿來測 project-bound 行為,**用 `agent_template_demo`**(seed,出廠 `state='draft'`,可手動 publish + 綁 project),**別 repurpose default**。
- 本 incident 正是把這兩個搞反:該綁的 demo 沒動,不該綁的 default 反而被綁。

## Related

- 上游 gap chain:[[agent-runtime-stg-enablement-gap-chain]] —— 本篇是其 gap #3 緩解措施(seed default)被打破的失敗模式;gate 第 3 條 = 該 chain 的 template 關卡在 auto-enable 路徑上的對應。
- 同 family(fail-closed「悄悄回空」難 debug、單點失效不可觀測):[[agent-runtime-stg-enablement-gap-chain]] 的「refused ≠ timeout」辨錯;本篇的「無錯誤訊息的 auto-enable no-op」。
- 設計依據:`docs/superpowers/specs/2026-06-01-seed-agent-template-design.md`(seed tenant-neutral invariant)+ `docs/plans/2026-Q2/2026-06-01-auto-enable-agent-runtime.md`(auto-enable 只翻 flag、template 交給 resolve fallback 的決策)。
- 關鍵 code 落點:`src/controllers/ai_assistant.py:18`(`_platform_default_agent_template` 合格判定)/ `:43`(`_agent_runtime_autoenable_ready` 三 gate)/ `:831`+`:850`(`_resolve_agent_template` ④ fallback)/ `:1336`(`_create_project` 唯一翻 flag 處);`src/models/agent_template.py` `_check_not_platform_default_when_bound`(PR #125 守衛);`src/hooks.py:143`(`_ensure_default_agent_template` post_init auto-point,不覆寫)。

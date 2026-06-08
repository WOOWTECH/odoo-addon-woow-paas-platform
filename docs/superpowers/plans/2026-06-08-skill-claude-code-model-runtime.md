# Skill Claude Code 模型重設計 — Runtime 端 Implementation Plan（handoff）

> **執行者：canvas 上的 `paas-agent-runtime` agent（repo owner）**，在 `paas-agent-runtime` repo 內執行。本 session（woow_paas_platform / Odoo 端）**不直接改該 repo**。
> 本檔是 handoff brief：合約 + 安全要求 + 任務拆解。**接手 agent 請依 [[phase-leader-brief-as-pointer-not-source]] 紀律，以 repo 實際程式碼為準**（canvas agent 正在 `feat/tool-call-args-trace` 改 loop，行號可能位移），撞到與本 brief 不符處 → 信 source、回報校正，勿照字面硬幹。

**Goal:** 把 runtime skill 模型從「skill = 確定性 subprocess StructuredTool（固定 entrypoint）」改為 Claude Code 模型：SKILL.md body 進 agent context、bundle 內 script 選配、由 LLM 自己決定呼叫通用 bash tool 執行。

**Architecture:** 不動手刻 bind_tools loop（Non-Goal，見 spec §3）；skill-roster 注入做成獨立純函式併入既有 `system_context`；新增 `skill:load_skill` 與 `bash:execute` 兩個 tool；通用 bash runner 複用既有 `subprocess_runner` 的 env allowlist / rlimit，**新增異 UID 執行**。

**Spec:** `woow_paas_platform/docs/superpowers/specs/2026-06-08-skill-claude-code-model-redesign-design.md`（§4–§6）。
**資安追蹤：** `woow-paas/paas-agent-runtime#26`（同 pod untrusted bash 曝險，bash 異 UID 為必要緩解）。

---

## 合約（interfaces，SSOT — Odoo 端鏡像此處）

### build_skill_roster（純函式）

```
build_skill_roster(effective_skills: list[SkillMeta]) -> str
```
- 輸出 progressive-disclosure roster：每 skill 一行 `- <name>: <description>`，前綴 `## Available skills`，尾句提示用 `skill:load_skill`。
- cap：超過上限（建議 50 條 / ~4KB）截斷並註明 `N more skills omitted`。
- **不放全文 body**（body 由 load_skill 按需回）。
- 輸出**併入既有 `system_context`**（runtime 本就把 Odoo per-turn `system_context` 置頂為 SystemMessage）；**不寫進 `_llm_stream` 函式體**。

### skill:load_skill tool

```
skill:load_skill(name: str) -> {
  "name": str, "description": str,
  "body": str,                       # SKILL.md frontmatter 之後的完整指令本體
  "workdir": str,                    # 該 skill cache 目錄絕對路徑
  "files": [{"path": str, "executable": bool}]  # 相對 workdir
}
```
- 內部呼叫既有 `ensure_skill_cached(name, version)`（`cache.py`）lazy fetch+extract，回 `skill_cache_dir(name, version)`。
- 回傳結果進 conversation history 供 agent 下一輪參考。

### bash:execute tool

```
bash:execute(cmd: str, timeout_seconds: int = <host cap>) -> {
  "stdout": str, "stderr": str, "exit_code": int, "truncated": bool
}
```
- cwd 預設 `/workspace`；agent 通常先 `cd` 進 load_skill 回的 `workdir`。
- runner 複用 `subprocess_runner._safe_env`（positive allowlist）+ `_set_rlimits` + `_capped_stdout_bytes` + `_capped_timeout`。
- **異 UID 執行（見安全要求）。**

### tool 命名

全部加 namespace prefix：`skill:load_skill`、`bash:execute`、MCP tool 為 `mcp:<orig>`；pod 啟動偵測同名 → fail-loud 拒啟。

---

## 安全要求（issue #26，bash 異 UID 為**必要**非 nice-to-have）

1. **bash 子行程 UID ≠ agent process UID**。agent 是 `USER 10001`（且常為 container PID 1）。同 UID 會：(a) 讓 `cat /proc/1/environ` 還原 `LLM_API_KEY` → env scrub 失效；(b) `RLIMIT_NPROC=16` per-UID 累計 → bash fork-bomb 餓死 runtime。bash 須跑另一非特權 UID（如 65534），無 `CAP_SYS_PTRACE`/`CAP_DAC_READ_SEARCH`。
2. **env allowlist**：複用 `_safe_env`（已是 positive allowlist）。
3. **掛載祕密檔對 bash UID 不可讀**：`/run/secrets/agent-runtime-auth-secret` 等 mode 0400、owner=agent UID、bash UID 不在其 group。
4. **誠實限制**（本期不解，記錄於 spec §6.4 + issue #26）：egress 同 netns 擋不住、IPC/socket/`/dev/shm` 同 pod 不隔離。
5. **adjacent bug（動 secret mount 時一併修）**：AUTH secret env 名不一致 — chart 注入 `AUTH_SECRET_PATH`（`deployment.yaml:107`），runtime 只讀 `AUTH_SECRET_DIR`（`auth.py:52`）。

---

## File Structure（runtime repo，行號以 `feat/tool-call-args-trace` 為基，請現場確認）

| 檔案 | 動作 |
| --- | --- |
| `runtime/skills/loader.py` | 移除 `build_skill_tool`/`build_lazy_skill_tool`、entrypoint 預設（`:80`）、body 丟棄；新增讀 body + 檔案清單供 load_skill |
| `runtime/skills/subprocess_runner.py` | 擴 `spawn_bash(cmd, cwd, …)` 共用 `_safe_env`/`_set_rlimits`；新增異 UID 執行 |
| `runtime/skills/` (new) | `roster.py`：`build_skill_roster` 純函式 |
| `runtime/skills/` (new) | `tools.py`（或既有 tool 模組）：`skill:load_skill`、`bash:execute` StructuredTool |
| `runtime/langgraph_runtime.py` | `_resolve_tool_set`（`:280-288`）toolset 改 `[skill:load_skill, bash:execute, *mcp]`；roster 併入 system_context（不進 `_llm_stream` 體） |

---

## Tasks（canvas agent 請各自展開為 bite-sized TDD）

- [ ] **R1: `build_skill_roster` 純函式** — 單測：給定 skill 清單回正確 roster 字串、空清單回空、超 cap 截斷標註。
- [ ] **R2: roster 併入 system_context** — 單測：effective skills 存在時 SystemMessage 含 roster；無 skill 時不破壞既有 system_context 注入。
- [ ] **R3: `skill:load_skill` tool** — 單測：回 body/workdir/files schema 正確；呼叫 `ensure_skill_cached`（mock）；skill 不存在的錯誤路徑。
- [ ] **R4: `bash:execute` tool + `spawn_bash`** — 單測：在 workdir 跑 `echo`/`ls` 得 stdout、exit_code；stdout 超 cap → `truncated=true`；timeout 行為。
- [ ] **R5: 移除 skill-as-tool + entrypoint 預設 + 保留 body** — 改 `loader.py`；toolset 改組（R1-R4 接上）；移除舊 subprocess per-skill 執行路徑。確認 MCP 整合不回歸。
- [ ] **R6（安全，issue #26）：bash 異 UID + 祕密隔離** — 單測/整合：`ps` 驗 bash UID ≠ agent UID；bash 內 `printenv LLM_API_KEY`/任意 `*_KEY|*_SECRET|*_TOKEN` → 空；`cat /proc/1/environ` → EACCES；`cat /run/secrets/agent-runtime-auth-secret/*` → EACCES。chart securityContext / UID 設定（`woow-paas-charts` 跨 repo）。
- [ ] **R7: 端到端冒煙** — 上傳純知識 skill → 對話依 SKILL.md 指令回應（不跑 script）；上傳帶 script skill → agent `load_skill` → `bash` 跑 script → 結果回對話。

> canvas owner 已表示可直接草「roster 注入 system_context 獨立函式 + 走 subprocess_runner 的 bash StructuredTool（不動 loop）+ untrusted-bash 隔離設計」的最小實作藍圖 — R1-R6 可由其主導。

## 跨 repo 部署（spec §9）

runtime（本計畫）先 land + pod rollout，Odoo 計畫的 Task 3-4（demo re-pack + migration）再合併部署；Task 1（移除 warning A）可先行。

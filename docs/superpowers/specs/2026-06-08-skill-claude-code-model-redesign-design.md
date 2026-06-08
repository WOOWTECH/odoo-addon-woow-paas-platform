---
title: Skill 執行模型重設計 — 從 subprocess-tool 改為 Claude Code 模型
date: 2026-06-08
status: draft
authors: [eugene, claude]
repos:
  - woow_paas_platform (Odoo 端，本 repo)
  - woow-paas/paas-agent-runtime (runtime 端，canvas agent 驅動)
runtime_baseline: paas-agent-runtime @ branch feat/tool-call-args-trace（本 spec 引用的行號以此為基）
related:
  - src/wizard/skill_bundle_validation.py
  - src/wizard/skill_upload_wizard.py
  - docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md
  - docs/decisions/patterns/two-layer-acl-lockstep.md
  - docs/tracking/pitfalls/agent-runtime-coldstart-cpu-throttle-timeout.md
  - paas-agent-runtime security issue: woow-paas/paas-agent-runtime#26（https://git-prod.woowtech.io/woow-paas/paas-agent-runtime/issues/26）
---

# Skill 執行模型重設計：subprocess-tool → Claude Code 模型

> **本 spec 的兩種敘述要分清**：§1 是**現況基線（descriptive）**，標注既有程式碼行號；§4 起是
> **提案改動（prescriptive）**。SkillMiddleware / load_skill / bash tool 在 runtime **目前都不存在**，
> 是本提案的主要交付物。

## 1. 現況基線（Current State Baseline）

平台目前的「skill」與業界（agentskills.io / Anthropic Agent Skills、LangChain 官方
`multi-agent/skills`）的 skill 概念**撞名但語意不同**：

| | 業界（Claude Code / LangChain 官方） | paas-agent-runtime 現況 |
| --- | --- | --- |
| skill 本質 | SKILL.md 指令（prompt）＋ 選配 bundled 檔案 | 一個 `StructuredTool`，invoke = 跑固定 entrypoint |
| SKILL.md body | **即 skill 本體**，注入 agent context | 被丟棄（只取 frontmatter） |
| 跑 script | LLM 讀完指令後，自己決定用通用 execute tool 去跑 | skill 自我執行（確定性 subprocess） |
| 最小 skill | 只要 SKILL.md | 必須有可執行 `scripts/main.py` |
| 執行結果 | 非確定性（LLM 決定要不要跑、怎麼跑） | 確定性（stdout == tool_result） |

### 1.1 runtime 現況（SSOT，@ feat/tool-call-args-trace）

- `loader.py:90-127` `build_skill_tool`（eager）/ `loader.py:130-165` `build_lazy_skill_tool`
  （lazy）一律把 skill 包成 `StructuredTool`；`_run`（`:101` / `:142`）→
  `subprocess_runner.spawn_skill_subprocess(skill_path, args, skill_meta, invoker_role)`
  （`subprocess_runner.py:73`）。實際呼叫把 frontmatter 的 `runtime` 字串（如 `python3.11`）當
  `skill_path`、entrypoint 當 args，即 `subprocess.run([interpreter, entrypoint], …)`，input_args
  走 stdin、stdout == tool_result。
- entrypoint 預設 `scripts/main.py`（`loader.py:80`，**本提案 §5 會移除此預設**）。
- SKILL.md 的 markdown body 被丟棄（`loader.py:64-87` 只取 frontmatter）。
- agent loop 手刻：`_resolve_tool_set`（`langgraph_runtime.py:280-288`）聚合 skill tools + MCP
  tools → `_history_to_messages`（`:364`）→ `llm.bind_tools(tools)`（`:362`）→ `model.astream`
  （`:372`）while 迴圈。**非** LangGraph prebuilt `create_agent`。
- **已安裝套件僅 `langchain_core 1.4.0` + `langchain_openai 1.2.2` + `langchain_mcp_adapters
  0.2.2`；沒有安裝 `langchain` meta-package，也沒有 `langchain/agents/middleware/` 模組。**
  → LangChain 官方的 `AgentMiddleware` / `create_agent` 中間件機制在此 runtime **不可用**（見 §4.1）。
- MCP 走官方 `langchain_mcp_adapters` 的 `MultiServerMCPClient.get_tools()`（外加 SSRF /
  no-redirect 包裝）。
- **可複用的既有安全基礎**（`subprocess_runner.py`）：`_safe_env`（`:49-59`，**positive
  allowlist**：只放 PATH/HOME/LANG/PROJECT_HASH，明確不 pass-through 任何 secret）、
  `_set_rlimits`（CPU/NOFILE/NPROC/FSIZE/AS 資源上限）、`_capped_timeout`、`_capped_stdout_bytes`。
- **skill cache 路徑 SSOT**：`cache.py:72-76` `skill_cache_dir(name, version)` =
  `/workspace/project-{PROJECT_ID}/skills/cache/<name>/<version>`；`ensure_skill_cached`
  （`:80+`）lazy fetch → SHA256 verify → atomic extract → 回該目錄；subprocess cwd 亦用此。
  fetch bearer 走 `SKILL_BUNDLE_TOKEN` / `SHARED_SECRET` env（`cache.py:99`）。
- pod 以非 root 跑：`Dockerfile:50` `USER 10001:10001`（appuser）。
- pod 持有的祕密：`LLM_API_KEY`（env，`langgraph_runtime.py:263`，operator provision）、
  `LLM_MODEL_CONFIG`（env，`:162`）、`SKILL_BUNDLE_TOKEN`/`SHARED_SECRET`（env，`cache.py:99`）、
  agent-runtime auth 簽章祕密（掛載檔 `/run/secrets/agent-runtime-auth-secret`，`auth.py:26,52`）。

Odoo 端（本 repo）：

- `src/wizard/skill_bundle_validation.py` 刻意鏡像 runtime 合約，含 entrypoint 預設
  `scripts/main.py`（`DEFAULT_ENTRYPOINT`，`:61`）＋「缺檔發軟警告 A」（`:404-410`）。
- `src/wizard/skill_upload_wizard.py` 上傳當下 fail-fast 驗證。
- `src/tests/test_skill_upload_wizard.py:452-463` `test_warn_missing_entrypoint_still_creates`
  斷言「缺 entrypoint → 仍建 skill + 含 'entrypoint' 字樣的 warning」（**移除 warning A 後會紅**，見 §8）。
- `src/data/demo_system_skill.xml` 的 `demo_skill_hello_world`（system scope、published）inlined
  base64 bundle = `SKILL.md` + `scripts/main.py`，frontmatter 宣告 `entrypoint: scripts/main.py`；
  XML `:7-16` 含一段引用 subprocess 合約（issue #135/#139）的註解。
- `src/__manifest__.py` 目前版號 `18.0.1.0.34`（`.34` 已被 issue #156 dual-bind cleanup 佔用）。

### 1.2 為什麼要改

- 平台 skill 是**使用者上傳、非確定性**的能力。確定性 subprocess-tool 不符需求：使用者要的是
  「上傳一包指令（＋選配輔助 script），讓 agent 讀懂後依情境自己決定怎麼用」。
- **SKILL.md body 目前被丟棄**（`loader.py:64-87`）→ body 文字永不進 agent context，逼使用者把
  skill 邏輯硬塞進可執行 script，而非用指令表達。這是本提案要解的核心問題。
- 順帶解決使用者實際痛點：上傳「只有 SKILL.md」的最小 skill 被警告「缺 `scripts/main.py`」。

## 2. 決策摘要

把 runtime 的 skill 模型整個換成 Claude Code 模型。**乾淨替換、不做雙模式共存**（平台仍在早期，
無真實 executable skill 依賴 — 經確認）。

- skill = SKILL.md 指令 ＋ bundle 內**選配**檔案。**取消** `kind` 型別分流：所有 skill 統一一種。
- runtime 新增 progressive disclosure（**自刻** skill-roster system-prompt 注入 ＋ `load_skill`
  tool）＋ 一個通用 `bash`/`execute` tool（full shell、可 pip install），cwd 在既有 skill cache 目錄。
- 沙箱 = **agent pod 自身**（每個 pod 單一 workspace，1:1，pod 邊界即租戶邊界）。同 pod 內以
  **異 UID + env allowlist + 祕密檔權限** 做 defense-in-depth；殘留風險（同 pod 內平台憑證曝險）以
  另開 issue 追蹤，本期先交付、後續再硬化。

## 3. Goals / Non-Goals

**Goals**

- 最小 skill 只需 SKILL.md（name + description）即可上傳、生效。
- SKILL.md body 進入 agent context；agent 能依指令用通用 bash tool 跑 bundle 內 script。
- 上傳驗證合約與新 runtime 合約 lock-step（不再要求 entrypoint）。
- 同 pod 沙箱的 defense-in-depth：bash 子行程**異於 agent UID**（封 `/proc/<agent-pid>/environ`）、
  env allowlist（複用 `_safe_env`）、祕密檔對 bash UID 不可讀。

**Non-Goals（本期不做）**

- 強隔離沙箱（gVisor/Kata 方案 B、ephemeral pod 方案 C）— 列未來工作，另 issue 追蹤。
- 把手刻 agent loop 換成 LangGraph `create_agent` — 與本次正交，不動（亦因此不能用官方
  `AgentMiddleware`，見 §4.1）。canvas owner 裁決：create_agent 對本 skill 需求零新增價值，卻要動
  已被 #129/#23/#52 穿刺的關鍵 loop。**backlog 一張 create_agent spike**，待 {resume（disconnect
  後 in-flight turn 不丟，`ws_server.py:309`）、CANCEL（`envelope.py:32` 已有 type，手刻 loop 無法乾淨
  中斷）、HITL、或同時疊 2+ 橫切關注} 第一個成真需求時再評估遷移。
- executable→knowledge 的資料遷移相容層 — 早期無依賴，直接替換。
- kind 分流 — 明確不做。
- bash 跑在獨立 network namespace / sidecar / PID namespace 隔離 — 列未來硬化。

## 4. 新架構

> 以下三個元件 runtime 目前都**不存在**，是本提案主要交付物。

### 4.1 Skill 浮現（progressive disclosure）— skill-roster 注入（自刻）

**BLOCKER 釐清**：LangChain 官方文件的 `SkillMiddleware(AgentMiddleware)` / `wrap_model_call`
也是「你自己 subclass + 自己寫注入邏輯」、且只能掛進 `create_agent` 的 middleware pipeline；本
runtime **未裝 `langchain` meta-package、無 middleware 模組、且手刻 bind_tools loop（Non-Goal 不改
create_agent）**，故**不能**用官方 middleware。官方那套唯一省到的是 agent loop（見 §3 為何不遷）。
改**自刻**，並採 canvas owner 的 refinement：

- roster 組裝寫成**獨立、可單測的純函式** `build_skill_roster(effective_skills) -> str`（progressive
  disclosure：只放 `name + description`，不放全文），**不塞進 `_llm_stream` 函式體**（該路徑已被
  #129/#23/#52 反覆穿刺，避免續腫）。
- 注入點：**複用既有 `system_context` 機制** —— runtime 本就把 Odoo per-turn 傳來的 `system_context`
  原樣置頂為 `SystemMessage`（見 `langgraph_runtime.py` 檔頭 agent-context-enrichment）。
  `build_skill_roster` 的輸出**併入該 `system_context`**（roster 段 + 既有背景），不另開 prepend 路徑。
- **注入格式**（明確化，供 runtime 實作與測試對齊）：markdown bullet list，例
  ```
  ## Available skills
  - <name>: <description>
  ...
  Use the `skill:load_skill` tool to load a skill's full instructions before using it.
  ```
- **上限**：roster 只放 name+description；若 effective skill 數超過上限（建議 cap 50 條 / 約 4KB），
  超出者截斷並在 system prompt 註明「N more skills omitted」（避免 system prompt 無界膨脹）。

### 4.2 Tool 介面定義

兩個新 tool，皆以 namespace prefix 命名避免與 MCP tool 撞名（見 §5）：

**`skill:load_skill(name: str)`** — agent 判斷要用某 skill 時呼叫。
- 行為：呼叫既有 `ensure_skill_cached(name, version)` lazy fetch+extract（§4.3），讀回 SKILL.md。
- 回傳（StructuredTool return；結果進 conversation history 供 agent 下一輪參考）：
  ```json
  {
    "name": "string",
    "description": "string",
    "body": "string",            // SKILL.md frontmatter 之後的完整 markdown 指令本體
    "workdir": "string",         // 該 skill cache 目錄絕對路徑（agent 可 cd 進去）
    "files": [{"path": "string (相對 workdir)", "executable": true}]
  }
  ```

**`bash:execute(cmd: str, timeout_seconds?: int)`** — 通用 shell 執行。
- 輸入：`cmd`（shell 指令字串）、`timeout_seconds`（選配，預設 = host wall cap，bundle 不可上調）。
- cwd：預設 `/workspace`（PVC RW），agent 通常先 `cd` 進 `load_skill` 回的 `workdir`。
- 隔離：複用 `subprocess_runner._safe_env`（env allowlist）+ `_set_rlimits`（資源上限）+
  異 UID（§6.3）。
- 回傳：
  ```json
  {"stdout": "string", "stderr": "string", "exit_code": 0, "truncated": false}
  ```
  stdout/stderr 受 host byte cap（複用 `_capped_stdout_bytes`），超出 `truncated=true`。

### 4.3 Bundle 落地（materialization）— 複用既有 cache SSOT

**不另發明路徑**。skill bundle 已由既有 lazy cache 機制落地：

- `skill:load_skill(name)` → `ensure_skill_cached(name, version)`（`cache.py:80+`）→ fetch + SHA256
  verify + atomic extract → 回 `skill_cache_dir(name, version)` =
  `/workspace/project-{PROJECT_ID}/skills/cache/<name>/<version>`（既有 SSOT，含 project 隔離 +
  version，root 直接是 SKILL.md）。
- bash tool cwd 預設 `/workspace`；agent 依 `load_skill` 回的 `workdir` 自行 `cd` 進該 cache 目錄
  跑 script。
- **lazy（在 `load_skill` 時才 fetch+extract），非 eager 全解壓**：對齊既有 cache 行為，省啟動成本，
  也避免「pod 啟動要一次抓全部 effective skill」。
- `/workspace` 是 PVC RW mount（`cache.py:13` 註：extract 落 PVC，不落 readOnlyRootFs）；pod
  securityContext 與 bash 異 UID 的相容性見 §6.3 與 §11。

### 4.4 保留不動

- MCP 官方 adapter（`MultiServerMCPClient.get_tools()`）。
- 手刻 `bind_tools` 迴圈 + `_history_to_messages`。
- skill-bundle 下載端點 wire 合約（raw bytes + pod 端 sha256 驗證）。

## 5. Runtime 端改動（paas-agent-runtime）

**移除**

- `build_skill_tool` / `build_lazy_skill_tool`（skill-as-StructuredTool）。
- `loader.py` 丟棄 body 的邏輯 — 改為保留 body 供 `load_skill`。
- entrypoint 預設（`loader.py:80`）。

**新增**

- 自刻 `build_skill_roster(effective_skills) -> str` 純函式（獨立可單測），輸出併入既有
  `system_context`（§4.1）；**不寫進 `_llm_stream` 函式體**。
- `skill:load_skill` tool（§4.2，呼叫 `ensure_skill_cached` + 讀 body/檔案清單）。
- `bash:execute` tool（§4.2）。其 runner：**複用** `subprocess_runner` 的 `_safe_env` /
  `_set_rlimits` / `_capped_*`，**新增「異 UID」執行**（§6.3）。建議在 `subprocess_runner` 內擴一個
  `spawn_bash(cmd, cwd, …)`（與既有 `spawn_skill_subprocess` 並存或取代），共用 env/rlimit/cap 邏輯。

**調整**

- `_resolve_tool_set`：toolset 改為 `[skill:load_skill, bash:execute, *mcp_tools]`。
- **tool name collision**：所有 tool 加 namespace prefix（`skill:` / `bash:` / `mcp:<orig>`），pod 啟動
  時若仍偵測到同名 → fail-loud 拒啟（不靜默覆蓋）。

## 6. 安全模型與緩解

### 6.1 邊界選擇與威脅範圍（pod : workspace = 1:1）

沙箱 = agent pod 自身，且 pod 與 workspace **1:1**。故：

- **跨租戶**：不同 workspace 不同 pod → untrusted skill code 只跑在自己 workspace 的 pod，**無跨
  租戶洩漏**。
- 因 1:1，pod-level 威脅 == 該 workspace 自身的威脅範圍。真正的殘留風險**不是**鄰居租戶，而是
  **同 pod 內持有的「平台級」憑證**（見 §6.2）被該 workspace 的 untrusted skill code 撈走。

### 6.2 殘留風險：平台級憑證在租戶自己的 pod 內

同 pod 內 untrusted bash 可竊取 pod 持有、但屬**平台級**的祕密：

| 祕密 | 位置/形式 | 被竊後果 |
| --- | --- | --- |
| `LLM_API_KEY` | env（`langgraph_runtime.py:263`，平台 operator provision） | 平台 LLM key 外洩 → billing 盜用 |
| `SKILL_BUNDLE_TOKEN` | env（`cache.py:99`，plain env、TTL 已放寬至 7 天） | bundle fetch bearer → 抓其他 skill bundle（`SHARED_SECRET` 僅為其 fallback，prod chart 不注入該值，runtime owner 校正） |
| auth 簽章祕密 | 掛載檔 `/run/secrets/agent-runtime-auth-secret`（`auth.py:26,52`） | 偽造 WS token |

**同 pod 不是對抗 untrusted code 的硬隔離邊界。** 本期以 §6.3 defense-in-depth 壓低風險，殘留以
資安 issue 追蹤、後續硬化（§6.4）。

### 6.3 本期緩解（defense-in-depth）

1. **bash 子行程 UID 必須異於 agent process UID（關鍵）**。agent 跑 `USER 10001`（且通常是
   container PID 1）；若 bash 也跑 10001：
   - 攻擊者可 `cat /proc/1/environ` 直接還原 `LLM_API_KEY` → env scrub 形同虛設；
   - **`RLIMIT_NPROC=16` 是 per-UID 累計**（`subprocess_runner.py:42`）→ 同 UID 的 untrusted bash
     fork 與 runtime 自身**共用同一份配額**，fork-bomb 可餓死 runtime（DoS，canvas owner 確認）。
   bash 須以**另一個非特權 UID**（如 `nobody`/65534 或專用 `skillrunner` UID）執行，且該 UID 無
   `CAP_SYS_PTRACE` / `CAP_DAC_READ_SEARCH`。異 UID 同時封住上述 `/proc` 旁路與 NPROC 共用 DoS。
2. **env allowlist**：複用 `subprocess_runner._safe_env`（已是 positive allowlist，只放
   PATH/HOME/LANG/PROJECT_HASH）。新增測試覆蓋任意 `LLM_*`/`*_KEY`/`*_SECRET`/`*_TOKEN` 前綴皆不存在。
3. **掛載祕密檔權限**：`/run/secrets/agent-runtime-auth-secret`（及建議改掛 secret volume 的
   `SKILL_BUNDLE_TOKEN`，見下）`mode 0400`、owner = agent UID、bash UID 不在其 group → bash 讀不到。
4. **`SKILL_BUNDLE_TOKEN` 從 env 改 secret volume mount**（降低 env 曝面；現為 plain env + 7 天 TTL，
   風險高）。若本期無法改，至少確保 §6.3.1 異 UID + （token 在 agent env）→ bash 異 UID 無法讀
   agent env / `/proc`。
5. **資源上限**：複用 `_set_rlimits`（CPU/NOFILE/NPROC/FSIZE/AS）擋 pip 失控 / fork bomb；確認涵蓋
   bash 子行程及其孫行程。

### 6.4 誠實標註的已知限制（本期不解，列未來硬化）

- **網路 egress（runtime owner 校正後收斂）**：chart NetworkPolicy 已 except 雲端 metadata
  endpoint（169.254.169.254），且 `externalEgress` 預設 false → metadata 旁路**已擋**、預設無對外
  egress。殘留風險僅在「LLM provider egress 被開啟時」：bash 與 agent 同 network namespace，可循該
  已開路徑直連 provider（用偷到的 key）或外送資料。**env scrub / 異 UID 擋不住此網路向量**（須靠
  network namespace 隔離，方案 B/C）。
- **IPC / Unix socket / `/dev/shm`**：同 pod 不隔離；agent 開的 socket / shared memory bash 可達。
- 真正封住以上需 PID/NET/IPC namespace 隔離或獨立 sandbox（方案 B：gVisor/Kata；方案 C：每次起
  短命 sandbox pod，需解 cold-start，見
  `docs/tracking/pitfalls/agent-runtime-coldstart-cpu-throttle-timeout.md`）。

### 6.5 「同 pod」邊界的精確定義（已定案：sidecar container）

**決策（user + runtime owner 拍板）**：bash runner 跑在**同 pod 的 sidecar container**，由 k8s
`securityContext.runAsUser` 宣告式給**異於 agent 的 uid**，並取得 **PID namespace 隔離**（sidecar
預設不共享 agent 的 PID ns）。理由：agent container 非 root（`USER 10001`、無 `CAP_SETUID`）→
**程序內無法 setuid**，sidecar 是達成異 UID 最乾淨且不破壞非 root 姿態的路徑；sidecar 仍在同一個
pod，符合「同 pod」決定。

此選擇比「同 container + 異 uid」更強：PID ns 隔離讓 bash **看不到** `/proc/<agent-pid>`（不只
靠檔案權限擋讀）。代價：需 image + chart 改動（加 sidecar container、共享 `/workspace` PVC、agent↔
sidecar 的指令通道），由 **canvas agent 開 PR 到 `woow-paas-charts`**（Drop B 跨 repo）。

## 7. Bundle 合約（新 SKILL.md 合約）

- tar.gz，root 直接含 `SKILL.md`（不可包 wrapper dir）— 不變。
- frontmatter 必填 `name`、`description`；`name` 規則同 agentskills.io（≤64 字、小寫字母/數字/
  連字號、首尾非連字號）。
- **不再要求 `entrypoint`**；bundle **不再要求任何可執行檔**。
- bundle **可選**含任意輔助檔，由 agent 依 SKILL.md 指令決定用法。
- SKILL.md body = skill 指令本體，經 `load_skill` 進 agent context。

## 8. Odoo 端改動（本 repo）

### 8.1 `src/wizard/skill_bundle_validation.py`

- **移除** `DEFAULT_ENTRYPOINT` 常數定義（約 `:60-61`）與 warning A「entrypoint 檔不存在」邏輯
  （`_scan_members` 尾聲約 `:404-410`）。移除後行號會位移，以語義定位為準。
- **保留**：合法 gzip tar、SKILL.md at tar root、frontmatter 必填 name/description、AppleDouble /
  `__MACOSX`、special-file / link 硬擋、path traversal、zip-bomb caps、warning B（AppleDouble）、
  warning C（frontmatter name vs wizard name）。
- 改 module docstring：從「鏡像 subprocess 執行合約（entrypoint 預設）」改為「鏡像 Claude Code
  skill bundle 結構合約」，刪除第 3 點 entrypoint 預設整段。

### 8.2 `src/wizard/skill_upload_wizard.py`

- `action_upload` docstring 內「mirror runtime cache/loader entrypoint 合約」字句更新；邏輯不變。

### 8.3 測試改動（**review 補漏，原 spec 未涵蓋**）

- `src/tests/test_skill_upload_wizard.py:452-463` `test_warn_missing_entrypoint_still_creates`：移除
  warning A 後此測試必紅。**改寫**為「純 SKILL.md（無 script）bundle → 0 warning、skill 仍建成」，
  正面鎖定本提案主訴求（見 §10）。

### 8.4 `skill` model（`src/models/skill.py`）

- **不需加欄位**（無 kind 分流）。`skill_metadata`（parsed frontmatter JSON）續用；scope/state/
  ir.rule/unique index 不動。

### 8.5 demo skill（`src/data/demo_system_skill.xml` + `src/data/demo_skills/`）

- 重打包 `hello-world-0.1.0`：建議改成**純知識 demo**（只有 SKILL.md，示範最小 skill），SKILL.md
  移除 `entrypoint` frontmatter、body 寫成指令。
- **人工刪除** XML `:7-16` 的 subprocess 合約註解（noupdate 手寫紀錄，由人清理後重打包，非 migration
  處理）；更新 XML 內 base64、`bundle_sha256`、`skill_metadata`。

### 8.6 Migration（**review 補正版號 + noupdate 陷阱**）

- 新增 `src/migrations/18.0.1.0.35/post-migrate.py`（`.34` 已被 issue #156 佔用），bump
  `__manifest__.py` → `18.0.1.0.35`。
- 沿用 `18.0.1.0.33` 的 idempotent in-place 改寫模式：讀磁碟最新 bundle → 算新 sha → 既存
  `skill.bundle_sha256` 已是新 sha 則 no-op，否則 `attachment.write({'datas': …})` +
  `skill.write({'bundle_sha256': …})`。
- **noupdate 陷阱澄清**（見 `docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md`）：
  `demo_skill_hello_world` 是 `noupdate="1"`。**但 migration 走「直接 ORM write attachment.datas」
  而非「XML data 重載」，noupdate 只擋後者、不擋前者**——故與 `.33` 同模式，migration 能正確改寫，
  fresh install 走 XML 新 bytes、升級走 migration 修舊 DB。§10 須同時測 fresh + upgrade 兩路。

## 9. 跨 repo 協調與順序

兩 repo lock-step（contract SSOT 在 runtime；Odoo 鏡像之；見
`docs/decisions/patterns/two-layer-acl-lockstep.md`）：

1. **runtime（canvas agent）先行**：land 自刻 roster 注入 + `skill:load_skill` + `bash:execute` +
   toolset 改組；移除 skill-as-tool + entrypoint 預設。定義新 bundle 合約。
2. **Odoo 跟上**：依新合約改 validation、wizard docstring、改寫測試、demo skill + migration。
3. 兩端各自測試後跑端到端（§10）。

### 9.1 Partial deployment 失效模式與觀測

| 情境 | 失效信號 | 檢查 |
| --- | --- | --- |
| Odoo 先放寬（移除 warning A），runtime 仍跑舊 subprocess | 使用者上傳純知識 skill，invoke 時舊 runtime 找不到 entrypoint → `SKILL_NOT_FOUND` / 空結果 | pod log 無 `load_skill`/`bash` tool |
| runtime 推新但 pod 未重啟 | 同上（pod 跑舊 image） | pod image tag / 啟動時間 |
| runtime 新但 Odoo 舊 | Odoo 仍對純知識 bundle 報「缺 entrypoint」軟警告（不擋上傳，影響輕） | wizard 警告文案 |

→ **建議 runtime 先、Odoo 後，且 runtime pod 確認 rollout 完成（image tag + 重啟）再放 Odoo**；
或兩端同一 release window 部署並以「上傳純知識 skill→對話觸發」端到端驗收作關卡。

## 10. 測試策略

**Odoo 單元**（明確「0 warning」定義 = `validation.warnings` 陣列長度）

- (i) 純 SKILL.md（無 script）→ `len(warnings)==0`、skill 建成（**主訴求回歸**，改寫
  `test_warn_missing_entrypoint_still_creates`）。
- (ii) SKILL.md + script → `len(warnings)==0`、skill 建成。
- (iii) SKILL.md + AppleDouble → `len(warnings)==1`（既有 warning B 不回歸）。
- (iv) 缺 name / wrapper-dir / special file → 既有硬擋不回歸。

**Odoo migration**：fresh install（XML 新 bytes 生效）＋ 先裝舊後升級（migration in-place 改寫
sticky DB）兩路皆驗 `skill.bundle_sha256` == 新 sha。

**runtime 單元**

- `load_skill` 回 body + workdir + files 清單正確（schema 對齊 §4.2）。
- roster system-prompt 注入內容/順序正確、超 cap 截斷標註。
- `bash:execute` 在 workdir 跑得動、stdout cap/truncated 行為正確。
- **安全（對抗式）**：bash 子行程 UID **≠** agent UID（`ps -eo pid,uid` 驗）；bash 內
  `printenv LLM_API_KEY` / 任意 `*_KEY|*_SECRET|*_TOKEN` → 空；`cat /proc/<agent-pid>/environ` →
  EACCES；`cat /run/secrets/agent-runtime-auth-secret/*` → EACCES。
- **網路向量測試**：bash `curl 169.254.169.254`（metadata）→ 應**被 NetworkPolicy 擋**（runtime owner
  校正：已 except + externalEgress 預設 false）；若 LLM provider egress 開啟，該路徑對 bash 仍可達 →
  記錄性斷言（已知限制，非回歸失敗），佐證 §6.4。

**E2E（k8s-dev-sandbox / deploy-stg）**——前置 checklist：`/workspace` PVC RW mount 存在、pod
securityContext（agent UID 10001、bash 異 UID、`/workspace` 對兩 UID 可寫或 fsGroup 處理）、
readOnlyRootFs 與 `/workspace`/`/tmp` RW 的相容。

- 上傳帶 script 的 skill → 對話觸發 → agent `load_skill` → `bash` 跑 script → 結果回對話。
- 上傳純知識 skill → agent 依 SKILL.md 指令回應（不跑任何 script）。

## 11. Open Questions（待 canvas agent 於 writing-plans 階段定）

- ~~bash 異 UID 的隔離原語~~ **已定案：sidecar container**（§6.5）。k8s `securityContext.runAsUser`
  給異 uid + PID ns 隔離；chart PR 由 canvas agent 開到 `woow-paas-charts`。待 plan 階段定的細節：
  sidecar 與 agent 的指令通道（共享 `/workspace` 上的 socket / file queue？）、`/workspace` PVC 對兩
  container 的 fsGroup 可寫設定、sidecar image 內容（含 bash + pip）。
- `SKILL_BUNDLE_TOKEN` 本期能否從 env 改 secret volume mount（§6.3.4）。
- `/workspace` securityContext / fsGroup 設定（agent 與 bash 異 UID 同時可寫）。
- **`LLM_API_KEY` blast radius**：是平台共用 key 還是 per-workspace？canvas owner 三方判定 uncertain，
  須查 operator `agent_runtimes.py::_build_install_values` 才能定（決定 §6.2 嚴重度）。
- **AUTH secret env 名不一致（adjacent latent bug，runtime 自身）**：chart 注入 `AUTH_SECRET_PATH`
  （`deployment.yaml:107`），runtime 只讀 `AUTH_SECRET_DIR`（`auth.py:52`，註明測試用），生產靠
  `_DEFAULT_SECRET_DIR` 硬編碼預設路徑剛好相同才 work；非本提案範圍，但動 secret mount 時須一併修。

## 12. References

- agentskills.io（Anthropic Agent Skills）：SKILL.md frontmatter name/description 規則。
- LangChain `multi-agent/skills` / `skills-sql-assistant`：prompt-driven skill 概念（注意官方
  `AgentMiddleware` 綁 `create_agent`，本 runtime 不適用，見 §4.1）。
- 資安 issue（paas-agent-runtime）：woow-paas/paas-agent-runtime#26。
- `docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md`（§8.6 noupdate 陷阱）。
- `docs/decisions/patterns/two-layer-acl-lockstep.md`（§9 跨 repo lock-step）。
- `docs/tracking/pitfalls/agent-runtime-coldstart-cpu-throttle-timeout.md`（§6.4 方案 C cold-start）。

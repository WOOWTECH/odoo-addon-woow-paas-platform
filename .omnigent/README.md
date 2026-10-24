# woow Omnigent multi-agent bundle

woow 六個 repo 的 issue／實作編制。**本檔是給維護者看的**，不進任何 agent 的 prompt。

同目錄另有 `woow-security-sweep`（每日安全掃描）；它和 setup skill、前台 agent 的關係見
[§woow-security-sweep 整體關係](#woow-security-sweep-整體關係)，bundle 細節見該目錄的 README。

```
cd /home/elmo/woow-repos && omnigent run .omnigent/woow-issue-leader --server <url>
   ↑ workspace = 六 repo 的共同祖先（不是任何單一 repo——理由見 §workspace）

woow-issue-leader/          claude-sdk／claude-fable-5-1[1m]／xhigh／spawn: true
   ⚠ 頂層一定是 claude-sdk，⛔ 不可以是 *-native（UI picker 會過濾掉，見 §UI picker）
├── config.yaml             產物
├── AGENTS.md               手改
├── hooks/                  ⭐ dispatcher：把子 repo 的 hooks 拿回來（§dispatcher）
├── skills/                 ⚠ **必須存在**，否則不發 --plugin-dir、dispatcher 不生效
│   ├── issue-triage/  pr-merge-gate/  board-reconcile/  stale-sweep/（按需掃 stale）
│   ├── dispatch-policy/    對照表與 alias map 的使用規則
│   └── omni-msg/           opt-in 跨層 messaging（CLI／bridge 見 omni-msg/README.md）
├── _templates/             ⭐ 產生器的模板來源（8 份 worker prompt ＋ skills；`_shared/` 共用片段）
├── generate-worker.py      ⭐ 執行期產生器（五道防線，§產生器）
└── agents/
    ├── repo_leader_cc/     claude-native／claude-fable-5-1[1m]／xhigh／spawn: true
    └── repo_leader_pi/     pi／gpt-6-astra／xhigh／spawn: true
        ← 擁有 issue 全程（impl→review→驗收→PR→Gitea label）；issue-leader 派完即結束
        （⚠ 底下**沒有** agents/——worker 在執行期產生）
```

## 🔴 worker 是執行期產生的，不是靜態 sub-agent

這是本架構的核心決定。原因是實測出來的硬限制：

> **`executor.config.harness` 是 bundle 屬性，派工當下換不掉。**
> `sys_session_send` 只能覆寫 `args.model` / `args.reasoning_effort`，harness 不在其中。

而需求是依 `ROSTER` 為不同 purpose 指定 harness，並在 config 裡帶入 worktree 守線；#1729
起 implement 與 review 都可 pi／claude-native、配對須跨 vendor。這些都是派工時才知道的資料 ⇒ 靜態 bundle
做不到，只有**執行期產生 config** 做得到。

`sys_session_create(config_path=<dir>)` 實測可行，而且：

| 實測結果 | 意義 |
| --- | --- |
| 動態 config 的 harness / model / `guardrails.policies` / `spawn` 全部生效 | 守線不會因為動態而變弱 |
| **不需要**在父層列靜態 allowlist | 不再需要為每個組合預先宣告 |
| 可遞迴再生下一層 | worker 自己也能再派工（若給 `spawn: true`） |
<!-- retired-ok:start 這一列在說明新架構為何免疫兩個已廢止設計的問題，必須點名它們 -->
| 產出都是實體檔 | 天然免疫 symlink 與副本 drift 兩個舊問題（見 §已廢止的設計） |
<!-- retired-ok:end -->

派工流程（repo-leader 做）：

```bash
DIR=$(python3 <paas-platform>/.omnigent/woow-issue-leader/generate-worker.py \
        --purpose implement --harness pi --repo paas-platform \
        --repo-root /home/elmo/woow-repos/paas/woow_paas_platform \
        --worktree /home/elmo/woow-repos/paas/wt-1032 --name impl-1032-pi)
# --name 同時是 worker 的 agent name 與目錄名（預設 <purpose>_<cc|pi>；規則 ^[a-zA-Z0-9_-]+$）。
# 同名不衝突：omnigent 每次 config_path 上傳都建一個新的 session-scoped agent row，name 只是
# 給 UI／log 看的標籤（review F13 查證：server/routes/_sessions/orchestration.py
# ::_create_session_from_bundle 明寫「Agent names are user-authored labels, not global content
# identities」）。建議 <purpose>-<issue>-<cc|pi>，才分得出是哪張 issue 的 worker。
# conversation_id = sys_session_create(config_path=$DIR) 回傳的 conversation_id（⛔ 不帶工單）
# sys_session_send(session_id=<conversation_id>, args.purpose="implement", args.input=<工單>)
```

⚠ **worktree 由 implement worker 自己建**（2026-09-14 裁決）：repo-leader 只指定路徑、不代建。
`--repo-root` 與 `--worktree` 兩個路徑都進 worker 的 `block_working_dir_changes.allowed_dirs`
（`git -C <repo-root> worktree add` 才進得去），該層的 `block_worktree` 因此關掉；「進 repo root
只為建 worktree、改檔只能在 worktree 裡」是紀律規則，機器擋不住。worker 完成後把 worktree
路徑、branch、PR 回報給 repo-leader；PR 開出且 CI 綠後由 repo-leader 移除。runner 只有一個、
永遠在共同祖先啟動——⛔ 不要為某張 issue 另起「以 worktree 當 workspace」的 runner（dispatcher
會找不到任何 sibling repo，全部 repo hooks 靜默消失）。

### Session 建立配額（#1277）

`spawn_bounds` 每輪最多計 12 次 `sys_session_create`；`sys_session_send` 初次送工單與續談
不計數，讓長時間運行的 agent 可持續對話。這是每輪建立配額，不是同時運行的 worker 上限；
每個 inbound request（含 subagent wake notice）重設計數。policy 來源為產生器的
`P_LEADER_ORCH`，三份 leader config 由 build 同步。先前疑似 server／runner 雙倍計數仍未
取得 log 證據；本次不宣稱已修正或已證實。

## 誰通知誰（喚醒只保證一跳）

1. 每一段「誰呼叫 `sys_session_send` → 誰被喚醒」都可靠；完成結果會投遞給該次 send 的
   發起者。
2. `sys_session_create` 一步起（帶 message）沒有 task handle，完成無處投遞，永遠不會喚醒
   發起者。必須先 create，再對回傳的 conversation_id 呼叫 send。
3. 被 inbox 叫醒的那一輪做完，不會再往上通知父代；三層鏈的第二跳傳不回來。omnigent
   是第三方，本案不改。

因此狀態放 Gitea label，escalation 第一收件人是人；issue-leader 派完即結束，repo_leader
擁有 issue 全程。

### opt-in 跨層 messaging：`omni-msg`

上面是一跳 completion 的既有路徑；主動往 parent／sibling 發訊息可另外走
[`omni-msg` CLI／bridge](omni-msg/README.md)。它使用已授權的 session REST API，
不是放寬 `sys_session_send`，也不建立新 session。`list / send / ask / reply / check`
有 actor ACL、SQLite receipts 與 explicit reply correlation；`ask` 非同步、不 blocking。

skill 的單一來源是 `woow-issue-leader/skills/omni-msg/`，build 複製到兩個 repo-leader，
worker 產生器再複製到每個動態 worker。**skill 存在不等於已啟用**：需 operator 安裝 CLI、
啟動 bridge 並授權真實 session IDs。可用人工 registry，或 `provision --roots` 授權各 root
一次，由 registrar 核對 descendants；亦可用 `provision --auto-roots` 預先核准 owner／host／
workspace／bundle 內容指紋，讓 app 新 root 自動納管，不必每次交付 ID。各 session 的 config
獨立，contacts 仍依 root 分組，不同 issue-leader 不自動互通（操作與失敗語意見上方 README）。未配置或 policy
DENY 時維持原 Gitea 流程；不放寬唯讀、worktree、人工 merge／approval gate。

### Bundle 改版後更新信任指紋

修改 `woow-issue-leader/` 或會配送進去的 builder/shared 輸入後，用 project operator skill
[`woow-issue-leader-bundle-refresh`](../.agents/skills/woow-issue-leader-bundle-refresh/SKILL.md)
規劃 stored bundle 與 auto-root rule 更新。Pi：`/skill:woow-issue-leader-bundle-refresh plan`；
Claude Code：`/woow-issue-leader-bundle-refresh plan`。預設只讀預覽，owner 核准具體計畫後才 apply；
merge 不會自動信任新 fingerprint，也不順便重啟 runner。此 operator skill 不配送進 runtime bundle。

## 用法

```bash
python3 .omnigent/build-bundles.py           # 重產三份靜態 config ＋ repo-leader 的副本
python3 .omnigent/build-bundles.py --check   # 驗收 22 項（含產生器自檢與 dispatcher 端到端）
python3 .omnigent/woow-issue-leader/generate-worker.py --self-check   # 只驗產生器
python3 .omnigent/test-dispatcher.py         # 只驗 dispatcher（fixture repo，零副作用）
python3 .omnigent/test-spawn-bounds.py       # create-only 配額／續談／request 重設（非 live gate）
bash .omnigent/mutation-tests.sh [case 代號]  # 突變測試（每個 case 斷言「紅在哪」）
```

| 路徑 | 內容 | 誰維護 |
| --- | --- | --- |
| `_templates/<role>/AGENTS.md` ＋ `skills/` | 8 份 worker prompt／skill（**產生器的來源**） | 手改 |
| `_templates/_shared/<片段>.md` | worker prompt 的共用片段（鐵律／守線強度／跨 repo 唯讀／回報／不要離開 worktree），模板用 `<!-- include: <片段> -->` 引用、產生器複製時展開（見該目錄 README） | 手改 |
| `_shared/repo_leader/AGENTS.md` | repo-leader 共用 prompt | 手改 |
| `woow-issue-leader/AGENTS.md` ＋ `skills/` | issue-leader 的 prompt 與 skills；repo-leader 的副本清單見 `REPO_LEADER_SKILLS`，`omni-msg` 另由產生器提供給所有 worker | 手改 |
| `generate-worker.py` | 執行期產生器 ＋ 對照表 ＋ **7 支 CEL 與所有 policy 片段** | 手改 |
| `hooks/dispatch-hook.py` ＋ `hooks.json` | dispatcher | 手改 |
| `build-bundles.py` | 三份靜態 config 的產生器 ＋ **22 項驗收** ＋ repo-leader 身分行 | 手改 |
| `test-dispatcher.py` | dispatcher 端到端（fixture、零副作用；51 情境） | 手改 |
| `mutation-tests.sh` ＋ `mutation_lib.py` | 突變測試與它的套用函式庫——**套用前**斷言定位字串仍有效（見 §定位字串的唯一性）。⚠ `mutation_lib` 用底線命名因為它要被 `import` | 手改 |
| **三份 `config.yaml`** ＋ repo-leader 的 `AGENTS.md`／`skills/`／`hooks/` | 產物 | ⛔ **勿手改**（drift 由第 0 項對帳） |

⚠ **CEL 與 policy 片段住在 `generate-worker.py`，不在 `build-bundles.py`。** 因為產生器要在
**執行期**跑（repo-leader 派工前呼叫它），而 `build-bundles.py` 不在 bundle 內、執行期拿不到。
真相放在執行期拿得到的那一邊，`build-bundles.py` import 過來用 ⇒ 只有一份。

## UI picker：頂層 leader 的 harness ⛔ 不可以是 `*-native`

Omnigent Web UI 的 Agents → **Custom agents** 子選單**選不到** `*-native` harness 的 custom
agent，而且**不報錯**——選單裡就是沒有那一列，看起來像 agent 沒上傳成功。

根因在 `web/src/hooks/useAvailableAgents.ts:295-305`：enrich harness 之後，若該 custom agent
解析出的 native key 與清單中某個 builtin 相同，就 `enriched.filter(a => a.id !== agent.id)`
把自己剔除。`claude-native` → key `claude`，而 seeded builtin `claude-native-ui` 的 harness
正是 `claude-native` ⇒ **必然命中**。`claude-sdk` 不在 native 表內，所以安全（polly／debby
也都是 `claude-sdk`）。

實測對照（同一台 server，逐個 `sys_agent_get`；`h1-probe-pi` 與 `h1-probe-cn` 是同一組實驗、
只差 harness 一欄）：

| agent | harness | 出現在 picker |
| --- | --- | --- |
| `h1-probe-pi` | `pi` | ✅ |
| `q3-probe` | `pi` | ✅ |
| `claude` | `claude-sdk` | ✅ |
| `h1-probe-cn` | `claude-native` | ❌ |
| `h2-dispatch-bundle` | `claude-native` | ❌ |
| `woow_issue_leader`（修正前） | `claude-native` | ❌ |

⇒ **驗收第 17 項**機器強制頂層 harness 不得以 `-native` 結尾（突變 AE 實證）。

⛔ **只管頂層。** `repo_leader_cc` 與執行期產生的 `_cc` worker 不出現在頂層 picker（它們是
sub-agent／`sys_session_create` 起的 session），不受這個 filter 影響，而且它們**需要**
`claude-native` 才有 hook 機制 ⇒ 不要因為這一項去動它們的 harness，也不要動
`generate-worker.py` 的 `ROSTER` 對照表。

## workspace：為什麼一定要抬到共同祖先

```bash
cd /home/elmo/woow-repos && omnigent run .omnigent/woow-issue-leader --server <url>
```

⚠ **不能**在單一 repo 內啟動：**top-level session 之間不能互相 `sys_session_send`**，
跨 repo 統籌只有「同一棵樹」這個形狀成立。

代價（實測）：**子 repo 的 `.claude/` 三項全失**。

| 失去的 | 能不能拿回來 |
| --- | --- |
| **hooks** | ✅ **能**——`hooks/dispatch-hook.py`（§dispatcher） |
| **CLAUDE.md** | ❌ 拿不回來（見下方 `--add-dir` 評估）⇒ 關鍵守線知識已寫進各 worker 的 prompt |
| **skills** | ❌ 拿不回來 ⇒ worker 要用 shell 直接跑對應 script，或回報給上層 |

⚠ on-demand 注入**只綁 `Read` 工具** ⇒ 用 `cat` / `grep` 連那點補償都沒有。

### ⛔ `--add-dir` ＋ `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` 不可行（已查證）

這兩者同時給才能把 CLAUDE.md 與 skills 疊加回來，但 omnigent 給不了：

- 能透傳 CLI flag 的只有 `harness.<canonical>.args`（config 層），而
  `resolve_harness_args` **只有 `cli.py` 一個呼叫點**——即**只影響 CLI 直接啟動的 harness**。
- **sub-agent / `sys_session_create` 路徑完全不經過它**；`executor.config` 能帶進
  `terminal_launch_args` 的只有 `permission_mode` 與 `yolo` 兩個欄位，沒有任意 args 透傳。

⇒ 單給環境變數無效（缺 `--add-dir`）。**損失就是 CLAUDE.md 與 repo skills**，補償是
dispatcher 把**機器強制那一層**（hooks）完整拿回來，以及把關鍵知識寫進 prompt。

## dispatcher

`hooks/dispatch-hook.py` 讀 PreToolUse／PostToolUse 的 payload，判斷指令落在哪些子 repo，
**轉呼叫那些 repo 自己註冊的 hook command**，原樣傳遞 stdout／stderr／exit code。

repo 實際註冊的 4 支（dispatcher 兩種事件都要支援）：

| 事件 | matcher | hook |
| --- | --- | --- |
| PreToolUse | `Edit\|Write\|MultiEdit\|NotebookEdit` | `pattern-guard.py` |
| PreToolUse | `Bash` | `kubectl-ns-guard.py` |
| PostToolUse | `Edit\|Write\|MultiEdit\|NotebookEdit` | `repo-lint.py --hook` |
| PostToolUse | `Edit\|Write\|MultiEdit\|NotebookEdit` | `memory-links-guard.py` |

### 🔴 哪幾層真的有 dispatcher（頂層 leader **沒有**）

| 層 | harness | 模型看到的 shell／編輯工具 | dispatcher |
| --- | --- | --- | --- |
| `woow_issue_leader`（頂層） | `claude-sdk` | `mcp__omnigent__sys_os_shell` / `sys_os_write` / `sys_os_edit` | ⛔ **不生效** |
| `repo_leader_cc` | `claude-native` | `Bash` / `Edit` / `Write`（claude CLI 原生） | ✅ |
| `implement`／`review`／`ops_k8s` 的 `_cc` worker | `claude-native` | 同上 | ✅ |
| `repo_leader_pi`／`_pi` worker | `pi` | pi 自己的工具 | ⛔（pi 無 hook 機制） |

#1729 起 `implement` 與 `review` 都可 pi／claude-native：正常配對 `implement_pi ⇒ review_cc`、
`implement_cc ⇒ review_pi`；pi 額度池不可用時才准 `implement_cc ⇒ review_cc` 降級（條件見
`_shared/repo_leader/AGENTS.md`「跨 vendor 覆核」）。

⚠ 頂層那格的機制：`claude-sdk` 一樣會發 `--plugin-dir <bundle>`
（`claude_agent_sdk/_internal/transport/subprocess_cli.py:726`，與 native 的
`inner/bundle_skills.py:109` 等價），所以 `hooks/hooks.json` **有被註冊**；但
`inner/claude_sdk_executor.py:2381` 把模型的 base tool set 釘成 `["Skill", "ToolSearch"]`，
OS 操作一律改走 `mcp__omnigent__sys_os_*` ⇒ matcher `Bash` 與 `Edit|Write|MultiEdit|NotebookEdit`
**永遠不會命中**。**這不影響守線**：頂層 leader 自己不跑 kubectl、不寫產品碼，真正會跑的
`repo_leader_cc` 與 `_cc` worker 都還是 `claude-native`，各自帶自己那份 dispatcher。

⛔ **`hooks/` 目錄不可以因此刪掉**——`generate-worker.py` 的 `HOOKS_DIR` 就指向它
（`BUNDLE_DIR / "hooks"`），每個 `claude-native` worker 的 dispatcher 都是從這裡複製的
（`generate-worker.py:747`）。刪了它，worker 那層的守線會**靜默**消失。

⚠ 頂層 leader 自己跑 shell 的守線改由 `guardrails.policies` 承擔：**它掛的 5 道 CEL**（見
下表）的 `event.data.name in [...]` 都**已經列了 `sys_os_shell`**，而 `mcp__omnigent__*` 的
tool call 是在 server 端的 `ProxyMcpManager` 用原始工具名（`sys_os_shell`）評估的
（`inner/claude_sdk_executor.py:2113-2122` 的 double-evaluation guard 說明了這條路徑），所以照樣命中。

### 🔴 「這一層掛幾道」≠「全樹有幾道」

⚠ 兩個數字很容易混：`generate-worker.py` 裡共有 **7 支** CEL 運算式（驗收第 8 項掃的就是這 7
支，事件數以 `--check` 當下印出的為準，涵蓋全樹含執行期產生的 worker），但**沒有任何一層同時
掛滿 7 支**——P-kubectl 只掛 ops_k8s、P-path 只掛 implement，兩支都只存在於動態 config。實查（`yaml.safe_load` 各層 config 的 `guardrails.policies`，數 `path` 結尾為
`cel.cel_policy` 的；2026-09-14 那次退役後重數，見 §已廢止的設計 第 5 條）：

**基本四支**＝P-merge／P-approve／P-tag／P-escape（對 shell 字串比對，每一層都掛）。各層在此
之上的增減：

| 層 | policies | CEL | ＝基本四支 ＋ | 沒掛的 |
| --- | --- | --- | --- | --- |
| `woow_issue_leader`（頂層） | 9 | **4** | — | **P-board**（刻意，見下）；`max_tool_calls_per_session`；花費改掛 `user_daily_cost_budget`（兩者見 §頂層 issue-leader 改掛） |
| `repo_leader_cc` | 11 | 5 | P-board | — |
| `repo_leader_pi` | 10 | 5 | P-board | `cost_budget`（非 CEL，見 §cost_budget） |
| 執行期產生的 `ops_k8s` worker | 11 | 6 | P-board ＋ **P-kubectl**（只存在於動態 config） | P-path |
| 執行期產生的 `implement_pi` worker | 12 | 6 | P-board ＋ **P-path**（只存在於動態 config；前綴每個 worker 不同，由派工當下的 worktree 算出） | P-kubectl、`cost_budget` |
| 執行期產生的 `implement_cc` worker | 12 | 6 | P-board ＋ **P-path**（同上） | P-kubectl、**`worktree_guard`**（#1729：對原生 Edit／Write 的絕對路徑一律 DENY，改由 P-path＋`block_working_dir_changes` 單獨守）；`cost_budget` 只有 350 硬上限、無 ask |
| 執行期產生的 `review_cc` worker | 11 | 5 | P-board | P-kubectl、P-path |
| 執行期產生的 `review_pi`／`explore_pi`／`researcher_pi` worker | 10 | 5 | P-board | P-kubectl、P-path、`cost_budget` |
| 執行期產生的 `tracker_pi` worker | 9 | 5 | P-board | `read_only_os`（它要寫產物）、P-kubectl、P-path、`cost_budget` |

<!-- retired-ok:start 下面這段是本節自己抓過的目視推導事故紀錄，必須點名當時那支 policy -->
🔴 **這張表的每一格都必須用實跑產生器取得，⛔ 不可以目視推導。** 本節的第一版就是目視推導寫
的，把動態 `ops_k8s` worker 標成「基本四支 ＋ P-model ＋ P-kubectl」——實跑
`generate("ops_k8s", "claude-native", …)` 後才發現它**根本沒掛 P-model**（P-model 只掛派工層，
而且已於 2026-09-14 整支退役）。同一類事故第二次：README 曾把「唯讀 worker 省略 `os_env`」寫成
A 類守線，實跑 `_worker_config()` 才發現唯讀 worker 一樣有 `os_env`（見 §已廢止的設計 第 6 條）。
兩次都是：**文件描述與實際 policy 組成不符，而且不會有任何 gate 報錯**。
<!-- retired-ok:end -->
重跑指令（零副作用，寫進臨時目錄）：

```python
import importlib.util, pathlib, tempfile, yaml
spec = importlib.util.spec_from_file_location("gw", ".omnigent/woow-issue-leader/generate-worker.py")
gw = importlib.util.module_from_spec(spec); spec.loader.exec_module(gw)
def counts(d):
    pol = d["guardrails"]["policies"]
    return len(pol), sorted(k for k, v in pol.items()
                            if v.get("function", {}).get("path", "").endswith("cel.cel_policy"))
# 靜態三層讀 config.yaml；動態層先 gw.generate(purpose, harness, <tmpdir>, **gw.sample_kwargs(purpose))
# 再讀產出的 config.yaml（implement 沒帶 worktree 會被拒絕，sample_kwargs 補樣本路徑）
```

⛔ **頂層少的那一道是 `p_board_read_only`，這是刻意的，不是漏掛。** P-board 擋的是「對
`.woow-issues/board.md` 的寫入」，而 issue-leader 本人就是**唯一該寫看板的角色**（沿用 Maestri
`paas-issue-memory` note「只有你可改、leaders 唯讀」的設計）⇒ 掛上去會擋到 leader 自己。
⛔ 不要「補齊」它。`build-bundles.py` 的 `issue_leader_config()` 在 `body` 組裝處、以及產物
`config.yaml` 的 `guardrails:` 之前，各有一句同義註記。

### ⛔ 為什麼不改寫成 CEL policy

把 `kubectl-ns-guard` 的 namespace 白名單搬進 CEL，它會變成**第四份副本**，而且不在
repo-lint `kubectl-ns-whitelist-sync` 的對帳範圍內 ⇒ 必然漂移。dispatcher 跑的是 repo 裡
**原封不動**那一支，真相只有一個來源。

### 🔴 強度分級：repo hook 是 A 類，dispatcher 的 target detection 不是

⚠ 這兩件事**強度完全不同**，混為一談會高估整條鏈：

| 環節 | 強度 | 理由 |
| --- | --- | --- |
| **repo hook 執行後的判定** | **A 類 sound** | 跑的是 repo 裡原封不動那支（`kubectl-ns-guard` 用 `shlex` 正確解析、剝 heredoc、認 `bash -c`），且白名單有 repo-lint 對帳 |
| **dispatcher 的 target detection** | ⚠ **單點 best-effort glue** | 它要從 payload 猜「這道指令作用到哪些 repo」。猜錯 ⇒ 那支 hook 不會被呼叫，**而且是靜默的** |

⇒ **整條鏈的強度由較弱的那一環決定**：只要 target detection 漏判，A 類的 repo hook 就不會
被執行到。FIX ROUND 6 的 BLOCKING 1 正是這個——`cd ../repoA`（從 sibling repo 進入）零命中。

target detection 採**兩層聯集**（寧可多跑一次檢查、不可漏守）：

| 層 | 判斷 |
| --- | --- |
| A | payload 的 `cwd` 落在某個 repo 底下 |
| B | **path-token 解析**：`shlex.split` 抽出路徑型 token（含 `/`、以 `.` 開頭，或 `cd`／`pushd`／`git -C`／`env -C` 的下一個 token，以及 `-c`／`-lc` 類 flag 的 shell payload 遞迴一層），**以 cwd 與 workspace 根兩個基準各 `normpath` 一次**，等於某個 repo 或落在它底下就命中。`normpath` 順帶解掉 `../repoA//x`、尾斜線與 `..`；取路徑前先剝掉 `shlex` 留在 token 邊緣的 shell 標點（`(cd`、`group/repoA;`） |

⛔ **FIX ROUND 7 移除了原本的第三層「對整條 command 做裸字串 regex」。** 那層把 `repo.name`
當裸字串比對 ⇒ `git commit -m "fix repoA bug"` **誤命中**（覆核實測）。

為什麼這個誤擋不能照「寧可誤擋」原則接受：它與 fail-closed **相乘**——誤命中一個不相干的
repo、而那個 repo 的 hook 又因任何理由失敗 ⇒ 一條**完全合法的 `git commit` 被擋下**。
commit message／PR body 提到 repo 名是極常見的寫法，所以這裡的誤擋不是便宜的保守選擇，
而是會阻斷日常工作。

⚠ 唯一例外：`shlex` 解析失敗（引號不對稱、未終止字串…）時**仍退回寬鬆比對**。
「看不懂」屬於「無法證明安全」，必須倒向保守。⛔ 不可把解析失敗當成「沒有路徑」。

### ⚠ hook 失敗的事件分級（fail-closed 的取捨）

**判準是「有沒有拿到判斷」，不是「失敗長什麼樣」。** PreToolUse 是安全 gate，所以
**所有**「無法確認這道指令安全」的理由都同樣 fail-closed：

| 失敗形態 | PreToolUse | PostToolUse |
| --- | --- | --- |
| hook `timeout` | ⛔ **exit 2** | warning-only |
| 非 0 且 **非 2** 的 exit code（crash／exception／exit 1／exit 7） | ⛔ **exit 2** | warning-only |
| hook 檔不存在／不可執行（shell 回 127） | ⛔ **exit 2** | warning-only |
| 呼叫本身拋例外（`OSError`…） | ⛔ **exit 2** | warning-only |
| 命中的 repo **有** `settings.json` 但讀不到／解不開 | ⛔ **exit 2** | warning-only |
| repo hook 回 **exit 2**（刻意 block） | **原樣透傳** | 原樣透傳 |
| 命中的 repo **根本沒有** `settings.json` | ✅ **不擋** | 不擋 |
| 有 `settings.json` 但沒有 matching hook | ✅ **不擋**（留零命中告警） | 不擋 |

### 🔴 還有一層：**dispatcher 自己**拿不到判斷（FIX ROUND 8）

上面整張表講的是「repo hook 拿不到判斷」。但還有更危險的一層——**dispatcher 自己壞掉**：

| 失敗形態 | PreToolUse | PostToolUse |
| --- | --- | --- |
| stdin 不是合法 JSON／讀不到 | ⛔ **exit 2** | warning-only |
| 缺 `tool_name`（沒有工具名就無法比對 matcher） | ⛔ **exit 2** | warning-only |
| 缺 `cwd`／`cwd` 指向不存在的目錄 | ⛔ **exit 2** | warning-only |
| `CLAUDE_PROJECT_DIR` 未設／指向不存在的目錄 | ⛔ **exit 2** | warning-only |
| dispatcher 拋出**未預期例外**（traceback 進 stderr） | ⛔ **exit 2** | warning-only |
| `WS_ROOT` 存在但底下**沒有任何** repo | ✅ **不擋**（本來就沒有守線要轉） | 不擋 |

⚠ **為什麼這層比上一層更危險**：repo hook 失敗只影響那一個 repo；dispatcher 自己壞掉
（最典型是 `CLAUDE_PROJECT_DIR` 沒設）會讓 `targets()` 回空 ⇒ **所有 repo 的所有守線一起
靜默消失**，而唯一徵兆是一個「零命中告警」——而那個告警**區分不了**「真的無關指令」與
「守線全掉」。

⚠ stdin 解不開時**無法判斷事件類型** ⇒ 保守當成 PreToolUse（fail-closed）。

### ⚠ `CLAUDE_PROJECT_DIR` 沒有替代來源（實測，這是敢 fail-closed 的前提）

把它改成 fail-closed 有「**brick 整台機器**」的風險——若它在正常啟動路徑上本來就沒值，
每個 worker 的每次 tool call 都會被擋。所以動手前用真實 claude CLI 量過：

| 問題 | 實測結果 |
| --- | --- |
| omnigent 會設它嗎？ | ❌ 不設（源碼 0 命中）——是 **claude CLI 自己注入**（二進位 27 處） |
| `--plugin-dir` 啟動時的值 | ✅ **啟動 claude 時的 cwd**（＝ runner workspace） |
| sub-agent 情境 | ✅ 仍然有設（實測那次唯一的 hook 觸發就是 sub-agent 的 Bash 呼叫） |
| 能不能改從 hook 檔位置推導 workspace 根？ | ⛔ **不行**——`CLAUDE_PLUGIN_ROOT` 指向 materialize 後的 bundle，與 workspace **無包含關係** |

最後一列是關鍵：這個變數**沒有替代來源**，所以它缺失時只能 fail-closed ＋ 留逆轉門。

⚠ **`exit 2` 為什麼要原樣透傳**：那是 repo hook 刻意的 block，它的 stderr 就是給使用者看的
理由，不可被 dispatcher 的診斷訊息蓋掉。`kubectl-ns-guard` 走 stdout JSON 協定（exit 0）的
那條路徑也不可被破壞——dispatcher 偵測到 deny JSON 會短路。

⚠ **「沒有 settings.json」與「有但載不起來」的分界點**：前者是正常情況（那個 repo 本來就
沒有守線），後者是**宣稱有守線却載不起來** ⇒ 只有後者擋。實作上
`guarded_repos()` 只會回傳有 `settings.json` 的 repo，所以「沒有」那條理論上走不到；
留著處理 glob 與 read 之間檔案被刪的 TOCTOU，而那時「不存在」仍然算「本來就沒有守線」。

⚠ **fail-closed 的誤擋風險是真的**：repo hook 自己有 bug 的話，**該 repo 的所有操作都會被
擋**。所以拒絕訊息設計成可診斷——明寫 repo、對象、事件／原因、細節，以及**依失敗形態給出
對應的處置建議**（timeout 要調 `timeout`、crash 要看它的 stderr、settings 壞了要修 JSON）。
### 🔴 緊急逆轉門 `DISPATCH_HOOK=off`

fail-closed 的代價是「壞掉時全面停擺」，所以照 repo 既有慣例
（`.claude/hooks/kubectl-ns-guard.py` 的 `KUBECTL_NS_GUARD=off`）留一個逆轉門，兩種形式：

```bash
DISPATCH_HOOK=off <指令>          # 一次性（指令前綴）
export DISPATCH_HOOK=off          # 整個 session
```

⛔ **這是緊急逆轉門，不是日常用法。** 開啟它等於**關掉所有 repo 的所有守線**——kubectl
namespace 白名單、`pattern-guard`、`repo-lint`、`memory-links-guard` 全部不再觸發。
只在「dispatcher 或某支 repo hook 自己壞掉、擋住了正常工作」時當逃生用，**修好就關掉**。
每次放行都會在 stderr 留一行警告，寫明這是未經守線檢查的指令。

它同時覆蓋兩層（repo hook 失敗與 dispatcher 自己失敗）。舊名
`OMNIGENT_DISPATCH_FAIL_OPEN`（FIX ROUND 6 引入）保留為別名，避免已寫進筆記的人踩空。

⚠ 每一則拒絕訊息都會印出逆轉方式——人被擋住時不需要翻文件。

⚠ FIX ROUND 6 只補了 `timeout` 一條，其餘三條仍 fail-open（覆核實跑：`Pre hook exits 7
→ rc=7`、`Pre invalid settings JSON → rc=0`）。**而當時的突變測試也只錐在 timeout** ⇒
26 個 case 全綠卻抓不到：**測試的盲點跟程式的盲點一模一樣**。現在拆成 Z1／Z2／Z3／Z4
四個 case 各自錐一種失敗形態。

### ⚠ 零命中告警抓不到「targets() 回空」那一類

dispatcher 在「命中了 repo、但沒有任何 hook 真的跑起來」時寫一行 stderr
（只對 `Bash`／`Edit`／`Write`／`MultiEdit`／`NotebookEdit` 告警，避免噪音）。

⛔ **它抓不到 BLOCKING 1 那一類漏守**——那一類的症狀就是 `targets()` 回**空**，
連「命中了 repo」這個前提都不成立。告警是 path-token 解析的**輔助，不是替代品**。
真正的守線在 targets() 的兩層聯集。

### known_gap（已知涵蓋不到，刻意不追）

| 形式 | 為什麼不追 |
| --- | --- |
| 路徑藏在變數裡（`D=../repoA; cd $D`） | 需要追蹤變數賦值，那是 shell 解譯器的工作 |
| 指令寫進 script 檔再執行（`bash run.sh`） | 本 hook 看不到檔案內容（repo 的 `kubectl-ns-guard` 檔頭也明寫它不涵蓋這層） |
| `bash -c` 巢狀兩層以上 | payload 遞迴限深度 1——再深就該懷疑是刻意混淆，而本 hook 防的是**手滑不是攻擊** |
| 路徑被 shell 展開才成形（`cd "$(git rev-parse --show-toplevel)"`） | 同上，需要真的跑 shell 才知道結果 |

### 三個接線前提（缺一個就完全不生效，驗收第 15 項機器強制）

1. **同層必須有 `skills/`**——否則不發 `--plugin-dir`，`hooks.json` 根本不被讀取
   （`inner/bundle_skills.py`）。
2. **command 不可靠 shebang**——bundle 打包會掉 exec bit（755 → 644）⇒ 一律寫
   `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/dispatch-hook.py"`。
3. **轉呼叫時要把 `CLAUDE_PROJECT_DIR` 覆寫成該 repo 根**——repo 的 hook command 都寫
   `"$CLAUDE_PROJECT_DIR/.claude/hooks/x.py"`，不覆寫就找不到檔。

### 兩種 block 協定都要能過

- `kubectl-ns-guard` 走 **stdout JSON**（`permissionDecision: deny`），exit code 是 **0**
  ⇒ dispatcher 必須偵測 stdout 並**短路**，否則後面的 hook 會再吐一份 JSON，兩份混在
  stdout 上 claude 解析不了。
- 其餘三支走 **exit 2**。

### pi 拿不回來

`pi --help | grep -ci hook` = **0**，pi 沒有 hook 機制 ⇒ **`ops_k8s` 固定 claude-native**
（產生器會直接拒絕 `ops_k8s + pi`），`repo_leader_pi` 不配 `hooks/`。

## 產生器的五道防線

⛔ 任一道不過就 **fail loud 並拒絕輸出路徑**——寧可不派工，也不要派一個會在第一輪才爆、
或守線靜默失效的 worker。

| # | 防的東西 | 做法 |
| --- | --- | --- |
| 1 | `executor.model` 與 harness 不相容 | **寫檔前**呼叫 `model_override.model_family_mismatch()`。⚠ 實測這條路徑 omnigent **完全不驗**（家族檢查只掛在 `sys_session_send` 的 `args.model`），create 會成功，等 child 第一輪才爆成通用 `runner_error` |
| 2 | `instructions:` 路徑失效 → **路徑字串變成 system prompt**；共用片段沒串進去 | 只寫實體 `AGENTS.md`（模板的 `<!-- include: -->` 行換成 `_templates/_shared/<片段>.md` 全文）；寫完 `parse()` 驗 instructions 非空、含 marker、不像路徑字串、無殘留 include 標記 |
| 3 | 檔名不是 `config.yaml` | 硬編檔名。⚠ 實測錯誤訊息是 `config.yaml not found in /tmp/tmpXXXX/agent`，完全誤導 |
| 4 | `skills/` 有目錄無 `SKILL.md` → 靜默空 | 產生後驗 skills 數 == 模板預期數 |
| 5 | ⛔ `skills_filter` 設 `none` | 它會發 `--setting-sources ""`，**壓掉 dispatcher 與所有 repo hooks** ⇒ 一律固定 `all` |

## 對照表

真相在 `generate-worker.py` 的 `ROSTER`；給人看的摘要在 `skills/dispatch-policy/SKILL.md`。
#1729 起 implement 與 review 都可 pi／claude-native，配對須跨 vendor（pi 池不可用時的同 vendor
降級由 repo_leader 依紀律規則執行，產生器不擋）。
硬規則：claude 家族只吃 id 含 `claude` 的 model（`opus`／`sonnet`／`fable` 等 CLI alias 一律
被防線 1 擋下）；effort 只用 `high`／`xhigh`（`ultra` 只有 pi 原生支援，claude 上**靜默降級**）。

## ⛔ 十條會靜默壞掉的規則

全部實測過，違反時**不會有錯誤訊息**——有些連 `validate()` 都是綠的。

⚠ 這份清單**只列現行仍成立的規則**。因架構變更而不再成立的，移到 §已廢止的設計 ——
**過時的警告與錯的警告一樣危險**。

### 產生 config 時

1. **`executor.config.harness` 是 bundle 屬性，派工當下換不掉。** `sys_session_send` 只能
   覆寫 `args.model` / `args.reasoning_effort`。寫了別的覆寫不會報錯，**只是沒效果**。
   ⇒ 要換 harness 只有「產生 config ＋ `sys_session_create`」這條路。這就是本架構的起點。
2. 🔴 **`sys_session_create(config_path=…)` 這條路徑完全不驗 harness × model 家族相容性。**
   家族檢查只掛在 `sys_session_send` 的 `args.model`。create 會**成功**，等 child 第一輪才爆
   成通用 `runner_error`（訊息不會告訴你是 model 不相容）。
   ⇒ 產生器防線 1 在**寫檔前**自己驗。→ 驗收第 4 項擋（突變 V 實證）。
3. **prompt 只能用同目錄的 `AGENTS.md` 實體檔承載。** ⛔ 不要改寫成
   `instructions: <相對路徑>`：containment 檢查失敗時回 `None`，接著**整條路徑字串被當成
   system prompt**，不報錯。→ 產生器防線 2 ＋ 驗收第 3 項擋。
4. **檔名必須恰好是 `config.yaml`。** ⚠ 寫錯的錯誤訊息是
   `config.yaml not found in /tmp/tmpXXXX/agent`——路徑指向 omnigent 的臨時解壓目錄，
   完全看不出是你寫錯檔名。→ 產生器防線 3。
5. **`skills/` 有目錄但沒有 `SKILL.md` ⇒ 靜默為空。** `validate()` 不會紅。
   → 產生器防線 4 ＋ 驗收第 4 項。
6. 🔴 **`skills:` 一律固定 `all`，⛔ 絕不可設 `none`。** `none` 發出
   `--setting-sources ""`（`inner/bundle_skills.py`），把 settings sources 整個關掉——
   bundle skills 還在（走 `--plugin-dir`）所以**看起來正常**，但 repo 的 `kubectl-ns-guard`
   / `pattern-guard` / `repo-lint` 全部不再觸發。名單模式在 claude-native 下形同 `all`、
   在 pi 下卻等於 `none` ⇒ 跨 harness 行為相反。→ 產生器防線 5（突變 U 實證）。
7. **`os_env.cwd` 寫什麼都不生效，但寫絕對路徑會讓 deployed server 拒收整個 bundle。**
   runner 一律用 `OMNIGENT_RUNNER_WORKSPACE` 覆寫它（harness 實際 cwd **與** `sys_os_*` 的
   containment 錨點兩者都不看 spec）；絕對路徑在沒有 workspace 的 runner 上會變成 agent
   environment root 與 copytree source（**GHSA-p8rw-8qj3-hf33**，high）。
   ⇒ 一律 `.`。→ 驗收第 10／11 項擋（突變 R 實證，兩道同時紅）。
8. **skill name 取自 frontmatter（目錄名被忽略），正則 `^[a-z0-9-]+$`（不允許底線），
   而且模板之間必須全域唯一**（`discover_host_skills` first-wins 去重會吃掉同名後者）。
   → 驗收第 5b 項擋。

### hook／dispatcher 接線（缺一個就是整層守線靜默消失）

9. 🔴 **三個接線前提，缺任一個 dispatcher 完全不生效，而且不會有任何錯誤訊息：**
   - **同層必須有 `skills/`**——否則不發 `--plugin-dir`，`hooks.json` 根本不被讀取。
   - **command 不可靠 shebang**——bundle 打包會掉 exec bit（755 → 644），hook 變成不可執行
     而靜默不跑 ⇒ 一律寫 `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/x.py"`。
   - **轉呼叫時要覆寫 `CLAUDE_PROJECT_DIR` 成該 repo 根**——repo 的 hook command 都寫
     `"$CLAUDE_PROJECT_DIR/.claude/hooks/x.py"`，不覆寫就找不到檔。
   → 驗收第 14／15 項擋（突變 T／W／X 實證）。
   ⚠ 第四個前提（FIX ROUND 6 補）：**target detection 猜錯 repo 也是靜默漏守**——
   `cd ../repoA`（從 sibling repo 進入）在只有「cwd ＋ 字面比對」兩層時完全零命中，
   後面接 kubectl 就等於鐵律失效。現在補了 path-token 解析（突變 Y 實證）。
   ⚠ 第五個（ROUND 6 補、ROUND 7 補完）：**repo hook 沒能給出判斷時原本 fail-OPEN**。
   ROUND 6 只修了 `timeout`；`exit 7`（crash）、hook 檔不存在、以及**命中的 repo 有
   `settings.json` 但解不開**三條路徑仍然放行（覆核實跑 `rc=7` / `rc=0`）。
   現在 PreToolUse 下**所有**失敗形態一律 exit 2（見 §hook 失敗的事件分級），
   PostToolUse 一律 warning-only（突變 Z1／Z2／Z3／Z4 各錐一種）。
   ⚠ 第六個（ROUND 7 補）：**target detection 誤命中也會害人**——裸字串比對讓
   `git commit -m "fix repoA bug"` 命中不相干的 repo，與 fail-closed 相乘就會擋掉一條
   合法的 commit。已移除那層（突變 AA 實證）。
   ⚠ 第八個（ROUND 8 補）：**dispatcher 自己拿不到判斷時原本也 fail-open**。
   ROUND 7 修的是「repo hook 拿不到判斷」，但 `CLAUDE_PROJECT_DIR` 沒設／stdin 壞掉／
   缺 `tool_name` 或 `cwd`／dispatcher 自己拋例外這幾條仍 rc=0——而這層**更危險**：
   它會讓**所有 repo 的守線一起**靜默消失，不是只影響一個 repo。現已全部 exit 2
   （突變 AC 實證），並附緊急逆轉門 `DISPATCH_HOOK=off`（突變 AD 驗它不可預設開啟）。
   ⚠ 第七個（ROUND 7 補）：**兩份 dispatcher 副本漂移**——一份補了守線一份沒補，守線只在
   其中一條派工路徑生效且不會報錯。→ 驗收第 16 項對 hash（突變 AB 實證）。
10. 🔴 **CLAUDE.md 的 on-demand 注入只綁 `Read` 工具。** 用 `cat` / `grep` 讀檔不會觸發注入
    ⇒ 在共同祖先 workspace 下，worker 連那點補償都沒有。
    ⇒ 關鍵守線知識必須**寫進 prompt**，⛔ 不要假設 worker 讀得到 repo 的 CLAUDE.md 或 skills。

另外兩條會影響行為但不會壞掉的：

- **child 不繼承 parent 的 `guardrails.policies`** ⇒ 每個 worker 自帶完整 policy
  （產生器負責，驗收第 8 項對產生器輸出也跑 CEL 雙向測試）。
- **唯讀 worker 的保證只有 `read_only_os` 一層**（比對工具名，A 類）。⚠ 唯讀 worker 的 config
  **一樣有** `os_env` 區塊、`sys_os_*` 會註冊——本 README 舊版把「config 不寫 `os_env` ⇒ 不註冊」列為第二層 A 類守線，是
  文件先於實作的錯誤描述，2026-09-14 實跑 `_worker_config()` 確認後裁決照實作改文件
  （見 §已廢止的設計 第 6 條）。

## Guardrails

內建 policy 見各 `config.yaml`。自訂守線一律用
`omnigent.policies.builtins.cel.cel_policy`（⛔ 不寫自訂 Python policy 模組——那要裝進
omnigent 的 venv，而且 `function.path` 打錯字會讓 worker 連 `Read` 都被 DENY）。

### 判斷方向：無法證明安全就 DENY

🔴 **這是 FIX ROUND 1 的核心修正。** 原本每一道都寫成「整條 command 看起來安全 → 放行」，
而 shell 沒辦法用單一 regex 可靠解析——覆核者實跑證明了 9 個繞過（串接、重複 `-n`、把安全
動詞當 resource 名、heredoc 餵 `python3`、`gh api -f body=/approve`、`gh api -X PUT
.../merge`…）。

反轉成「**無法證明安全就 DENY**」之後，**失敗方向變成 fail-closed**，大幅降低常見的漏擋：
誤擋會 fail loud（worker 會回報被擋、人看得到），漏擋才是靜默的。

⚠ 但這**仍然只是 best-effort**——**regex 對 shell 字串永遠不可能 sound**，漏擋仍可能靜默
發生（已知向量見下方 §守線的強度分級）。反轉改善的是**失敗方向**，不是把 regex 變成可靠
的解析器。

| 守線 | 掛在哪 | 擋什麼 |
| --- | --- | --- |
| `p_board_read_only` | 除 issue-leader 外**全部** | 任何提到 `.woow-issues/` 而**無法證明是唯讀**的指令（唯讀＝單一不串接、且以 `cat`/`less`/`head`/`tail`/`grep`/`rg`/`ls`/`wc`/`diff`/`git diff`/`git show` 開頭） |
| `p_merge_no_agent_merge` | 全部 | forge CLI 的 `pr`／`pulls`／`mr merge` 子命令，或 `gh`／`glab` API、`curl` 直打 merge endpoint |
| `p_approve_no_agent_signoff` | 全部 | 指令中**出現 `/approve` 字串**（不再限定工具名） |
| `p_tag_no_agent_tag_push` | 全部 | `git push`＋tag 形態、`refs/tags/`、forge release 子命令（tea `releases`／`release`／`r`、gh／glab `release`）、forge API 或 curl 對 `/releases` collection endpoint（可帶單一尾斜線／query）的 POST（含未明示 method 但帶 body 的隱含 POST）、`git tag <name>` |
| `p_kubectl_namespace_scope` | 僅 `ops_k8s_*` | 提到 `kubectl` 且：含串接（`&&`／`\|\|`／`;`／`` ` ``／`$(`／換行；**pipe 只在第一段是唯讀 kubectl、且 pipe 之後沒有 kubectl 時放行**）／第一段 `-n` 出現多次／寫入 verb 但 ns 不在白名單／寫入 verb 但沒帶 `--context`。`--context docker-desktop` 整條免除，但**整條只能有一個 `kubectl`** |
| `p_path_write_inside_worktree` | 僅 `implement_*` | 檔案工具（`sys_os_write`／`Edit`／`NotebookEdit`…）的 `path`／`file_path`／`notebook_path` 不以本 issue 的 worktree 為前綴（相對 workspace 或絕對形式）、或含 `..`。**比對結構化參數，A 類**；前綴由產生器在派工當下算出。claude-native implement 的檔案寫入邊界只靠它（不疊 `worktree_guard`，#1729） |

### ⚠ 已知的刻意誤擋（fail loud，不放寬）

| 被擋的合法操作 | 替代做法 | 為什麼不放寬 |
| --- | --- | --- |
| `grep -rn '/approve' docs/` | 搜尋字串去掉斜線：`grep -rn approve docs/` | 放寬就要回到「枚舉工具名」，那正是被繞過的原因 |
| `kubectl ... get ... && echo done`（唯讀＋`&&` 串接） | **拆成兩次 tool call**（pipe 接 `grep`／`jq`／`tail` 已放行，不必拆） | 串接之後做什麼無法用 regex 證明 |
| `kubectl exec … \| grep x`（exec 接 pipe） | 先單獨跑 `exec`，回報裡自己篩 | pipe 只放行第一段是**唯讀**動詞的；exec 是寫入類 |
| `kubectl --context docker-desktop get … && kubectl --context docker-desktop logs …`（本機叢集、兩個 kubectl） | 拆成兩次 tool call | docker-desktop 例外只在整條一個 `kubectl` 時成立，否則 `… && kubectl --context woow-k3s delete …` 會整條滑過 |
| `kubectl get pods \| grep -i delete` **放行**（不是誤擋，是刻意放） | — | pipe 之後不得再有 `kubectl`，所以後段的 `delete`／`-n` 只可能是 grep／jq 的參數，寫入動詞與 `-n` 重複的比對都只看第一段 |
| `kubectl --context woow-k3s apply -f x.yaml` | 先 `--dry-run=server`；真要套用走 `deploy-stg` skill 或交給人 | manifest 內的 `metadata.namespace` 會覆蓋 `-n`，regex 看不到檔案內容 |
| `git tag v1.0`（本機建 tag，未推） | 讀取用 `git tag -l` / `--list`；建 tag 交給人 | 建 tag 是發版動作的第一步 |
| `git push origin 1032-v18-upgrade`／`git push origin HEAD:v2-x`（branch 名以 `<字>-v<數字>` 開頭、或 `HEAD:v…`） | branch 一律 `<type>/<issue-id>-…`（`chore/1032-v18-upgrade` 就過） | tag 形態是 `[\s:]["']?(<字>-)?v<數字>` 的字串比對；放寬 `v<數字>` 後面的邊界，每一種都會多一個真 tag 滑過的形狀（review F12，2026-09-14 裁決不放寬） |
| `git fetch origin refs/tags/v1.7.6`（唯讀） | `git fetch origin --tags`／`git ls-remote --tags origin`／`git tag -l` | `refs/tags/` 出現即擋、不分讀寫——限定 push 語境就會漏掉 `git update-ref refs/tags/v1 HEAD` 這種不經 push、也不是 `git tag <name>` 的建 tag plumbing（forge 的 `gh api … git/refs` 另有一條分支擋，不受影響；突變 AS 驗這件事） |
| PR／留言的 raw shell 文字完整引用 forge CLI 發版子命令（例如 `gh release create v1.7.6`） | 先把內文寫入檔案，再用 CLI 的 file flag 帶入 | P-tag 比對 raw command，不解析 shell AST；只保證單純的 release 字首案例放行，完整 CLI 子命令即使位於引號內仍可能命中 |

⚠ `kubectl --context docker-desktop ...` **不再被擋**（FIX ROUND 2）：docker-desktop 是本
機測試／開發用叢集，鐵律限定的是 woow-k3s 這個共用叢集。舊版不分 context，會毀掉
`/woow-dev:k8s-dev-sandbox` 的 create／destroy／test 工作流。2026-09-14（review F11）收緊：
這個例外只在**整條只有一個 `kubectl`** 時成立——第二個 `kubectl` 不論帶什麼 context 都讓整條
回到嚴格規則，否則 `--context docker-desktop … && kubectl --context woow-k3s delete …` 會滑過
（`CEL_VECTORS` 有回歸向量）。

### 🔴 守線的強度分級（讀這段再決定要不要信任某條守線）

**不是每一條守線都一樣強。** 混為一談會導致兩種錯誤：把 best-effort 的當成保證（危險），
或把可靠的也當成擺設（浪費）。

#### A. Sound —— 繞不過，可以當成保證

| 守線 | 為什麼可靠 |
| --- | --- |
| `read_only_os` | 比對**工具名**（`Write`/`Edit`/`sys_os_write`…），不看 shell 字串 |
| `headless_subagent_purpose_guard` / `spawn_bounds` | 同上，結構化參數 |
| `block_working_dir_changes` | 有自己的 shell 解析（非單一 regex），且只需判斷 `cd`/`git -C` 這種固定形狀 |
| `p_path_write_inside_worktree`（僅 implement） | 比對檔案工具的 `path`／`file_path`／`notebook_path` **結構化參數**是否以 worktree 為前綴、是否含 `..`，不看 shell 字串。⚠ 它管的只是**檔案工具**那一側——shell 重導向寫檔（`echo x > …`、`sed -i`）沒有任何機器守線，見下方已知繞過向量 |
| repo 的 `.claude/hooks/kubectl-ns-guard.py`（**被執行到之後**的判定） | 用 `shlex` 正確解析，剝 heredoc、認 `bash -c`、認 `--`；且 repo-lint 對帳白名單 |

⚠ **但它要先被執行到。** 在共同祖先 workspace 下，repo hook 只能透過 dispatcher 被呼叫，
而 **dispatcher 的 target detection 是 B 類 best-effort glue**（它要猜「這道指令作用到哪些
repo」，猜錯就靜默不呼叫）⇒ **整條鏈的強度由較弱的那一環決定**。詳見 §dispatcher 的
「強度分級」。⛔ 不要因為 repo hook 本身是 A 類，就以為整條鏈是 A 類。

#### B. Best-effort —— 防手滑，**不是安全邊界**

`p_merge_no_agent_merge`、`p_approve_no_agent_signoff`、`p_tag_no_agent_tag_push`、
`p_escape_obfuscated_command`、`p_kubectl_namespace_scope`、以及 `p_board_read_only`
的 **shell 那一側**（它比對工具名的那一側屬於 A 類）。

這些都是**對 raw shell 字串做 regex 比對**。**regex 對 shell 字串永遠不可能 sound**——
shell 有太多等價寫法。它們擋得掉常見寫法，擋不住刻意規避。

**威脅模型是「防 agent 犯錯」，不是「防 agent 攻擊」。** LLM 不會為了規避一道它不知道存
在的守線，去把指令名寫成轉義形式。所以本 bundle **刻意不再追 regex 完備性**——每加一層都
在增加誤擋，而且贏不了。

#### 已知繞過向量（刻意不修，全部記在 `CEL_VECTORS` 的 `known_gap`）

| 形式 | 例子 |
| --- | --- |
| 變數展開 | `V=v1.7.6; git push origin $V`；REST method 或 endpoint 名稱放進變數（`M=POST; gh api -X $M repos/o/r/releases`、`E=releases; curl -X POST …/$E`） |
| CLI 與子命令間夾全域 flag | `tea --debug releases create …`、`gh -R o/r release create …`、`glab --repo o/r release create …`；#1110 裁決是「CLI 名稱後緊接子命令」，是否擴張由 Eugene 另行決定 |
| 使用者自訂 forge alias | 例如使用者先把 `gh ship` 設成 release 建立命令後再執行 `gh ship v1.7.6`；raw command 看不到 alias 展開內容 |
| 命令替換 | `curl -X PUT "$API/pulls/1/$(printf 'me'; printf 'rge')"` |
| 引號切斷 | `tea comment 823 '/appr''ove'` |
| base64 解碼後由 stdin 送 body | `echo L2FwcHJvdmU= \| base64 -d \| tea comment 823 -F -` |
| 檔案 body | `base64 -d /tmp/b.b64 > /tmp/b && tea comment 823 -F /tmp/b` |
| `$'\x..'` hex 轉義 | `tea comment 823 $'\x2f\x61\x70\x70\x72\x6f\x76\x65'` |
| shell 重導向寫檔（P-path 只看檔案工具的參數） | `echo x > WOOW/ecpay_odoo18/x.py`、`sed -i 's/a/b/' paas/woow_paas_platform/src/x.py` |

P-tag 的 REST collection endpoint 另以 path boundary 限定：collection 可帶單一尾斜線／
query，並可在引號後緊接 `)`／`;`／`&`／`|`／`>`／`<` shell 分隔符；GET 讀取、
`/releases/<id>` 與 `/releases-notes` 不命中，明示 GET 搭配 field，以及 curl 的 `-G`／
`--get` 搭配 body flag 也維持放行。

`p_escape_obfuscated_command` 只便宜地攔掉**反斜線轉義**那一類（`kube\ctl`、`me\rge`、
`pu\sh`）——因為那一招最常見且成本最低。其餘不追。

`known_gap` 向量會實際執行：它們**不判 FAIL**（記錄的是「我們知道這裡漏」），但若哪天變
成 DENY，驗收第 8 項會在 PASS 訊息裡講出來，提示可以升級成正式的 deny 向量。

### ⚠ CEL 是 fail-OPEN 的

CEL 參照不存在的欄位時回 `None`＝**abstain＝ALLOW**。所以：

- 每一次欄位存取都用 `has()` 包住，整條式子寫成
  `<條件> ? {"result":"DENY", ...} : {"result":"ALLOW"}`。
- **每一支運算式都要雙向測試**（違規必 DENY／合法必不 DENY），向量寫在
  `build-bundles.py` 的 `CEL_VECTORS`。沒有向量的 CEL policy 會被驗收第 8 項直接判
  FAIL——**沒測等於守線不存在**。

### ⚠ 內建 `github_policy` 對 tag push 無效

實測 `git push origin v1.2.3` 只回 **ASK**（URL regex 硬編 `github.com`，Gitea 判不出
repo），而且完全不認 `tea`。所以 `p_tag_no_agent_tag_push` 自己對 command 字串做 regex。

### ⛔ 為什麼不用 `enforce_sandbox` 斷網（評估過，不可行）

`P-merge`／`P-approve`／`P-tag` 防的都是**對 Gitea 的網路寫入**，所以「乾脆讓唯讀 worker
斷網」看起來是那個真正 sound 的一層。實測後**三個獨立原因都否決了它**：

1. **會斷掉 harness 自己的 LLM 呼叫。** `enforce_sandbox` 覆寫的是 agent **子行程**的
   sandbox（`sys_agent_start` 攔截點），而 bwrap 後端在 `allow_network=False` 時加
   `--unshare-net`；對 `claude-native` 來說被包住的就是 `claude` CLI 本身 ⇒ 連不到
   Anthropic API，worker 直接不能跑。`enforce_sandbox` 的參數只有
   `sandbox_type / allow_network / write_paths / read_paths / env_passthrough`，
   **沒有** egress allowlist 可以替 LLM API 開洞。
2. runner 套用 verdict 的 `_apply_sandbox_override_from_verdict` 會做
   `if spec.os_env is None: spec.os_env = OSEnvSpec()`（實測對唯讀 researcher 套下去，`os_env`
   變成一個完整的 `OSEnvSpec`）。⚠ 舊版 README 在此主張這會「拆掉唯讀 worker 不寫 `os_env`
   的 sound 守線」——但實作上唯讀 worker 本來就有 `os_env`（2026-09-14 實跑確認，見 §已廢止的
   設計 第 6 條），所以這一點**不再是否決理由**，留作紀錄。
3. `sandbox: {type: none}` 搭 `allow_network: false` 會直接
   `ValueError: sandbox type 'none' cannot restrict network`。

⇒ 唯讀 worker 的保證維持在 **A 類**：`read_only_os`（一層）。

### ⚠ `cost_budget` 只掛 claude 家族（claude-native／claude-sdk）

#1729 D2：claude-native **implement** 掛的是 `P_COST_IMPLEMENT`（只有 `max_cost_usd: 350`、不設
`ask_thresholds_usd`）——ASK 在無人值守時等於停機，且卡在 ASK 的 session 不發完成通知、不標
blocked。其餘 claude 家族 worker 仍是 `P_COST`（350／70／210）。產生器自檢斷言這兩件事。

pi 是 subscription 且無 catalog pricing，掛上去會走 `_UNPRICED_ASK`，**每個 session 卡一次
人工批准**，會毀掉無人值守。claude 家族兩種 harness 都有**計價路徑**（⚠ 計價路徑 ≠ 一定查得到
價格，見下方條件句），差別在精度：

| harness | 計價路徑 | 精度 |
| --- | --- | --- |
| `claude-native` | Claude Code 的 statusLine 直接回報 `cumulative_cost_usd` | 實際帳單 |
| `claude-sdk` | 回報 token 用量＋model，server 端查 MLflow catalog 定價（`_sessions/orchestration.py` 的 `has_tokens` 分支） | 估算值 |

✅ **已驗證（2026-09-10，雙人各自實跑 `fetch_model_pricing('claude-fable-5-1')`）**：頂層 leader
當時使用的 `claude-fable-5-1` **在 catalog 內**，per-million `input 10.0 / output 50.0`
（`cache_read 0.25 / cache_write 12.5`）⇒ `claude-sdk` 這條估算路徑查得到價，
`cost_budget` 正常計價，**不會**走 `_UNPRICED_ASK`。

⚠ #1277 將 leader 改為 `claude-fable-5-1[1m]`；上述歷史 pricing 證據不代表新 ID
已計價，stored bundle 發布前須重驗。

⚠ **這是有前提的事實，前提是 model。** 若日後把頂層 leader 換成別的 model，**必須重驗那個
model 在不在 catalog**：查不到時 `_usage_is_unpriced()` 會讓 `cost_budget` 回
`_UNPRICED_ASK`，而 headless 的 leader 沒人能回答 ApprovalCard ⇒ 每個 session 卡住。真的踩到
就把 `P_COST` 從 `issue_leader_config()` 拿掉——那是**人工裁決**，⛔ 不要靠 agent 自己判斷。
重驗指令：

```
python3 -c "from omnigent.llms.context_window import fetch_model_pricing; print(fetch_model_pricing('<model-id>'))"
```

### 頂層 issue-leader 改掛 `user_daily_cost_budget`（按日計）＋不掛 `max_tool_calls_per_session`

issue-leader 是**長時間運作的單一 session**，任何「整個 session 累計」的上限遲早撞到：
`cost_budget` 的 `max_cost_usd`（#1094 抬到 350 只是延後）與 `max_tool_calls_per_session`
（400 次，會比花費更早撞到）都是這種形狀。2026-09-11 起頂層改成：

| 守線 | 頂層 issue-leader | repo-leader／worker |
| --- | --- | --- |
| 花費 | `user_daily_cost_budget`：owner **當天（UTC）跨所有 session** 累計，`max_cost_usd: 700`，不設 ask 門檻 | `cost_budget`（per-session，350／70／210）不變 |
| 工具呼叫次數 | 不掛（只留滑動視窗的 `detect_loop`／`detect_thrashing`） | `max_tool_calls_per_session: 400` 不變 |

**撞到上限隔天會自動繼續**：rollup 按 UTC 日分桶（UTC 00:00＝台灣 08:00 歸零），engine 每個
turn／tool call 都重建並重讀 store（`routes_hooks.py::_build_engine`），所以跨日後的**下一輪**
直接放行，不必重開 session。但被 DENY 的那一輪不會自動重試——隔天要有新的觸發
（子代完成的喚醒、或人在 UI 送一句話）它才會動。

⚠ 三個前提／代價：

1. **額度是 owner 當日所有 session 的總和**——含派出去的 worker、含同一人在 UI 開的其他
   session（例如 Claude Code 終端 session）。700 是先寬鬆觀察的值，看實際運作再收。
2. **session 要有 owner grant**。UI 建的頂層 session 有（2026-09-11 實測
   `GET /v1/sessions/<id>/owner` → `{"owner": "eugene"}`）；single-user mode 沒有 ⇒
   `_load_user_daily_cost` 回零、policy 永遠不觸發＝**無煞車**。換 server 部署要重驗。
3. **不設 `ask_thresholds_usd`**：ASK 是 fail-closed（`ask_timeout: 900`），無人值守時
   一個 ASK 等於硬停，而且 decline 不記錄門檻、下一輪再問。要提醒就看 UI 的花費，不靠 ASK。

### 🔴 白名單同步債

`p_kubectl_namespace_scope` 的 namespace 白名單是**第五份副本**。權威三份由 repo-lint
`kubectl-ns-whitelist-sync` 對帳（`CLAUDE.md` 表格、
`docs/guides/deployment/shared-cluster-paas-scope-guard.md`、
`.claude/hooks/kubectl-ns-guard.py`）；第四份在 `_templates/ops_k8s/AGENTS.md`，第五份在
`generate-worker.py` 的 `K8S_ALLOWED_NAMESPACES`。**後兩份不在 repo-lint 對帳範圍內——改白
名單時五份都要一起改。**

**權威守線是 repo 的 `.claude/hooks/kubectl-ns-guard.py`**（用 `shlex` 正確解析，且
repo-lint 對帳白名單）；workspace 設成本 repo／其 worktree 時它會自動載入。
`p_kubectl_namespace_scope` 是 **第二層 defense-in-depth、best-effort**：regex 比對、不做
shell 解析，認不出 `bash -c` 包一層或 script 檔裡的 kubectl，**已知可被 shell 轉義繞過**。
⛔ 不要在任何地方把它寫成「機器強制」或暗示它擋得住刻意規避。它的價值是「workspace 不在
本 repo 時仍有一層接得住手滑」。

## 驗收與突變測試的關係

`--check` 有 22 項驗收。但**「全部 PASS」本身不代表驗收有效**——本 repo 已經抓到三次
「檢查看起來在運作、其實沒驗到」的事故，三次都是同一個根因：**只看有沒有 `[FAIL]`，
不看紅在哪**。

| 事故 | 症狀 |
| --- | --- |
| 突變 G | 定位字串被改過 → 突變變成 no-op，卻顯示 PASS（**假綠**） |
| 第 12 項 | 在非 git repo 的臨時目錄假紅 → 每個 case 都紅（**失去鑑別力**） |
| 突變 K | 同一份 config 命中兩處、改壞了另一支 policy → **紅錯地方** |

⇒ 每個 mutation case 都宣告**失敗指紋**，由 `mutation-tests.sh` 機械斷言：

```
run_mut <代號> <說明> <預期紅的項號> <預期紅的 CEL policy> <突變指令>
```

- **項號集合必須完全相等**——多紅一項（誤傷別的檢查）與少紅一項（守線失效）都算該 case
  失敗，不是「有紅就算過」。
- **CEL 相關要指名 policy**，它必須出現在 FAIL 訊息裡。⚠ 這道斷言不可省：突變 K 的舊寫法
  項號是 `[0,8]`，與預期**完全相同**，只有 policy 名能揭露它其實紅在 `p_board_read_only`
  的 compile error。
- **無條件斷言突變真的改到檔案**（內容＋權限 hash 有變），否則是 no-op 假綠。
- **baseline case**（不施加突變）必須全 PASS，否則其餘 case 的結果都不可信。

### 🔴 定位字串的唯一性是機器斷言（FIX ROUND 9 建立，ROUND 12 簡化成一條規則）

突變靠「在目標檔案裡找一段字串再改掉」定位，而那個定位字串會隨被測程式重構而失效——
**失效的症狀是突變靜默變成 no-op 或改到錯的位置**：測試照樣綠，但它其實什麼都沒驗。
這類事故已經三次：

| 輪次 | 突變 | 失效方式 |
| --- | --- | --- |
| FIX ROUND 5 | `G` | `sed` 的目標字串被改掉 ⇒ no-op 卻顯示 PASS |
| FIX ROUND 7 | `Y` | 比對行尾註解的層號字母（`# C`），重編層號後 ⇒ no-op |
| FIX ROUND 8 | `Z1` | 字串變兩處，`replace(…, 1)` 改到了**錯的函式** |

三次都是靠人工稽核或下游指紋斷言**碰巧**接住。依 `CLAUDE.md`
「可機械判定 → 加 check，gate 比散文強」，這條升成 gate：所有改既有檔案內容的突變一律走
`mutation_lib.edit()` / `edit_span()`，它在**套用之前**斷言——規則只有一條：

> **定位字串必須在目標檔案裡全域命中恰好 1 次。** 0 次或 >1 次一律 fail loud。

這條是 **sound 的**：全域唯一的字串只可能對到一個位置，**不需要知道任何語法**——
沒有邊界要猜，就沒有猜錯的洞。要定到特定位置就把定位字串加長到唯一（多行也可以，
把上下文包進來）；要連同後面一段可變內容一起換掉就用 `edit_span()`。

錯誤訊息會說出**哪個突變、哪個檔案、哪個字串、命中幾次**，並指出該怎麼修。
`mutation-tests.sh` 把這個例外當成該 case 的**硬失敗**並原樣印出訊息
（⛔ 不是降級成「no-op 警告」——那就退回成我們要修的那個問題）。

⚠ 這是**加強**而不是取代 `_tree_hash` 的 no-op 守衛：那一層抓「有套用但內容／權限沒有實際
差異」（例如突變 `N` 只 `chmod`），兩層管不同的事。

⚠ 只有「會改既有檔案內容」的突變需要這道斷言。純新增／刪除檔案的突變
（`rm -rf`、`chmod`、寫入新檔）沒有定位字串，不在範圍內。

#### ⛔ 為什麼規則這麼笨——`anchor=`（縮範圍）機制連補三輪後被整套拆掉

ROUND 9 的第一版不是這樣：它允許定位字串命中多處，再給一個 `anchor=`（通常是所屬函式的
`def` 行）把搜尋範圍縮到「錨點所屬區塊」，區塊邊界用**縮排**判斷。之後每一輪覆核都在同一
個地方撞牆：

| 輪次 | 補了什麼 | 立刻冒出的新洞 |
| --- | --- | --- |
| ROUND 9 | `anchor=` ＋ 縮排區塊 | 錨點**自己**不唯一；定位字串落在區塊**外**也會被改 |
| ROUND 10 | 錨點須唯一 ＋ 定位字串須區塊內唯一 | 函式體內的**頂格註解**、**shell heredoc** 讓區塊提早截斷 |
| ROUND 11 | 跳過註解行 ＋「縮排不可信就拒絕」 | 註解裡的**假三引號**把奇偶判斷帶歪；heredoc 內容裡**縮排的假結束符**被誤認 |

每次都是**靜默改到錯的行**，不是漏報——截斷之後只看到截斷點前面那一個命中，
本該報「區塊內歧義」的情況就變成默默改它。

⚠ **每補一次就冒一個新洞，那是抽象錯了的訊號，不是實作不夠仔細。** 根因是：想用
「不解析語法的啟發式」去判斷「語法上的區塊邊界」，這件事本身不可能 sound——而 ROUND 11 的
「不確定就拒絕」雖然方向對，判斷「確不確定」的那三個訊號本身又是同一種啟發式。

⇒ ROUND 12 整套拆掉（`_block_bounds()`、縮排計算、三個不可信訊號、奇偶成對檢查全部刪除，
`mutation_lib.py` 280 → 132 行）。

**代價**：定位字串會長一點。實測全部 33 個突變裡只有 2 個用過 `anchor=`
（`Z1`／`AC`，兩者的 needle 都是 `    if event != "PreToolUse":`——它在
`_hook_failure` 與 `_untrusted` 各一份），把它們各往下包兩行到該函式獨有的那句 stderr
訊息就全域唯一了（3 行／121 與 127 字元），**解析到的行號與 anchor 版完全相同**
（`Z1` → 463、`AC` → 392）。為了那 2 個去養一個永遠補不完的啟發式，不划算。

⛔ **不要再加 `anchor=` 或任何「縮範圍」參數。** 同樣的理由寫在 `mutation_lib.py` 檔頭
（改那個檔的人會先看到）。

突變一律用 Python 寫、不用 `sed`：`sed` 只能按字面定位，而 K 的事故正是定位不準。需要改
某支 policy 時先定位它的 YAML block 再改。

## 已廢止的設計（歷史紀錄，⛔ 不要走回去）

<!-- legacy-ok: 本章節刻意保留已廢止設計的名詞與機制，供「為什麼不能那樣做」的追溯。
     廢止詞彙掃描（驗收第 13 項）只在本章節內放行這些詞。 -->

這一節記錄**已經移除**的設計與當初移除的理由。⛔ 這裡寫的東西**都不是現行做法**——
不要照著做。放在這裡是因為每一條都有人會想「為什麼不乾脆……」，而答案有實測依據。

### 1. symlink 佈局（FIX ROUND 0–3 用過，ROUND 4 移除）

原本用 symlink 讓 16 個 thin shell 共用 8 份 prompt／skills（`AGENTS.md` → 來源檔、
`skills/` → 來源目錄）。**廢止原因**：`omnigent run <目錄>` 最終走
`chat._bundle_agent()` → `cli._bundle()`，而 `_bundle()` 對**目錄**來源是
`source.rglob("*")` ＋ `if file_path.is_file()`（`cli.py:5529-5538`），**不解 symlink**
（只有 source 是**單檔 YAML** 時才會先 materialize，`cli.py:5508`）。兩種型態各有壞法：

| 型態 | 當時數量 | 行為 |
| --- | --- | --- |
| symlink **檔**（`AGENTS.md`） | 18 | **loud fail**：tar 留下 link entry，server 端 `extract_safe` 直接拒（`spec/tar_utils.py:126`）：`tarball contains a link (not allowed)` |
| symlink **目錄**（`skills/`） | 16 | ⛔ **silent drop**：`rglob` 不遞迴進去，**tar 內 SKILL.md 從 19 筆掉到 3 筆**（只剩 leader 自己的），而 `validate` 照樣回 valid、那 16 個 agent 的 `skills` 變成 `[]`，**不報錯** |

⚠ **改用 local server 規避不了**：三條啟動路徑全呼叫同一個 `_bundle_agent(spec_path)` ——
`chat._chat_via_daemon()`（remote）、`chat._chat_local()`（「Start a **local** server with
the agent」）、`chat._run_local_headless_prompt()`。唯一不經過這條打包路徑的是
`omnigent server --agent <path>` 的 preregister（`cli.py:1483-1487` 自己 materialize）。

**取捨**：實體副本的失效模式是 drift，由 `--check` 第 0 項逐檔比對內容、**會紅、看得見**；
symlink 的失效模式是**靜默**的。用「會紅的風險」換掉「會靜默的風險」。
代價在 git 層面是負的（78 項 → 77 項，見 §啟動 上方的數字）。

**附帶廢止的一條規則**：當時有「symlink 一律用 `os.path.relpath` 推算，不要手數 `../`」
（斷鏈時 `is_dir()` 回 False → 靜默少發現一個 sub-agent）。沒有 symlink 之後這條不再適用，
已從現行清單移除。

### 2. `run-leader.py` launcher（FIX ROUND 3 新增，ROUND 4 移除）

symlink 時代需要一支 launcher，先 `materialize_bundle()` 解引用再交給 `omnigent run`。
改成實體副本後原始樹可以直接上傳（實測：link entry 0、SKILL.md 19 筆、
`enforce_handler_allowlist` True／False 皆過）⇒ launcher 唯一的存在理由消失。

**為什麼移除而不是降級成選用工具**：留一個只剩包裝功能的入口，會讓維護者困惑「我到底該走
哪個」——而那個困惑正是當時那個 blocking 的來源。自檢價值保留在 `--check` 第 10 項。

### 3. `os_env.cwd` 寫絕對路徑（FIX ROUND 0 定案，ROUND 4 移除）

原本寫 `/home/elmo/woow-repos`（六 repo 的共同父目錄），本意是當 `sys_os_*` 的 containment
錨點、讓跨 repo 唯讀落在邊界內。**廢止原因**：那個本意從來沒達成（runner 覆寫 cwd），
而且絕對路徑有兩個實際代價——deployed server 直接拒收、沒有 workspace 的 runner 上會暴露
host filesystem（GHSA-p8rw-8qj3-hf33）。詳見 §`os_env.cwd`。

### 4. 突變定位的 `anchor=`（縮範圍）機制（FIX ROUND 9 新增，ROUND 12 移除）

定位字串命中多處時，給一個錨點（所屬函式的 `def` 行）把搜尋範圍縮到「錨點所屬區塊」，
區塊邊界用縮排判斷。**廢止原因**：連補三輪，每補一次就冒一個新的**靜默改錯**（不是漏報）
——根因是「用不解析語法的啟發式去判斷語法上的區塊邊界」本身不可能 sound。改成單一條
「全域命中恰好 1 次」的 sound 規則，代價只是 2 個突變的定位字串要加長。
三輪軌跡與實測數字見 §定位字串的唯一性。

⛔ 不要重新引入 `anchor=` 或任何「縮範圍」參數。

### 5. P-model CEL（`p_model_fast_tier_only`；FIX ROUND 1 起，2026-09-14 退役）

原本掛在三層 leader 上，要求對 scout／tracker／researcher 派工必須顯式帶落在 `FAST_MODELS`
名單內的 `args.model`。**廢止原因**（review 實跑）：它的 agent regex 只認
`^(scout_pi|tracker|researcher)_via_(cc|pi)$`——那是靜態 sub-agent 時代的名字；worker 改成執行期
產生後 `_safe_name()` 產的是 `explore_pi`／`tracker_pi`／`researcher_pi`，六個名字**零命中**，而
兩步起法的 `sys_session_send(session_id=…)` 也未必帶 `agent` 欄位 ⇒ 這支 CEL **從未擋過任何事件**。
驗收第 8 項的向量還在測 `scout_pi_via_cc` 這些舊名，所以雙向測試全綠——綠在一個沒人會送出的
事件上。更矛盾的是 `FAST_MODELS` 全是 claude 系，而 ROSTER 給這三個角色的預設是
`gpt-5.6-luna`／`terra`：若它真的活著，每次用預設派工都 DENY。

裁決是退役而不是修：model 已由 `ROSTER` 在產生 config 時釘死，`args.model` 覆寫不是常規路徑，
沒有第二層的必要。⛔ 不要重新引入——要限制 model 就改 `ROSTER` 那一格；驗收第 13 項的
`RETIRED_TERMS` 已把 `P-model`／`FAST_MODELS`／`快層名單` 列為廢止詞彙。

### 6. 「唯讀 worker 省略 `os_env`」敘述（FIX ROUND 0 起，2026-09-14 改正）

README 與七份 worker 模板都曾把「唯讀 worker 省略 `os_env` ⇒ `sys_os_*` 四支不註冊」列為唯讀
worker **最強**的 A 類保證，並以此為由之一否決 `enforce_sandbox`。**改正原因**：實跑
`_worker_config("review"|"explore"|"researcher", …)`，三種唯讀 worker 的 config **都有** `os_env:`
區塊（`_worker_config` 算了 `readonly` 卻沒用它）。這是文件先於實作、且沒有 gate 會紅的錯誤描述。
裁決：照實作改文件——唯讀保證只有 `read_only_os` 一層（比對工具名，仍是 A 類）。
`RETIRED_TERMS` 已把「省略 `os_env`」列為廢止詞彙，prompt 層零容忍。

### 7. repo_leader 的「harness 自辨」段（2026-09-14 改為 build 蓋身分行）

舊 prompt 要 agent 從 session metadata 猜自己是 `repo_leader_cc` 還是 `repo_leader_pi`，猜不到
就標 blocked。**廢止原因**：兩份副本本來就是 `build-bundles.py` 從單一來源複製的，複製時蓋一行
身分成本為零，而「猜不到就停」是自己製造的失敗模式（review F7）。現在來源檔留
`identity:start／end` 佔位符，`repo_leader_prompt()` 換成身分行，驗收第 19 項驗副本真的蓋到。

### 8. 「排程 tracker 做 stale 掃描」（從未實作，2026-09-14 改為按需 skill）

兩份 leader prompt 曾寫 stale 掃描「由小時級 fresh-session tracker 排程」執行，但 bundle 裡沒有
任何東西會建那個 scheduled task、tracker prompt 也沒有 stale 判準（review F14）。裁決：不排程，
改成 issue-leader 的 `stale-sweep` skill，人叫才跑（N 預設 6 小時），掃完換 `blocked`＋留言＋
重建看板。

### 9. issue-leader 直接派 tracker／scout／researcher（2026-09-14 裁決收回）

`issue-triage` skill 曾叫 issue-leader 自己派 `tracker_*` 查重、`scout_pi_*` 補資訊，與頂層
prompt「你只派工給兩個 repo_leader」互斥（review F9）。裁決：issue-leader 只分配 issue，
**所有** worker 由 repo_leader 產生；同時 repo-leader 不再收到 issue-triage／board-reconcile／
stale-sweep 的副本（`REPO_LEADER_SKILLS` 當時只列 dispatch-policy 與 pr-merge-gate；
現在另加 opt-in 的 omni-msg，不改這項分工）。

### 10. P-kubectl 把 pipe 當串接一律 DENY、`--context docker-desktop` 出現即整條放行（FIX ROUND 1／2 起，2026-09-14 改）

舊版 `SHELL_CHAIN_RE` 含 `\|`，所以 `kubectl get pods -n paas-system -o json | jq …` 這種最常見的唯讀寫法
一律被擋，ops_k8s prompt 只能教「拆成兩次 tool call」——但 `jq` 那一半根本沒有輸出可接（review F11）。
同一支 CEL 又把「指令任一處出現 `--context docker-desktop`」當成整條放行，`… && kubectl --context
woow-k3s delete …` 接在後面就滑過。改法：pipe 在「第一段是唯讀 kubectl、且 pipe 之後沒有 kubectl」時放行
（寫入動詞／`-n` 重複／`-f` 的比對改成只看第一段），其餘串接不變；docker-desktop 例外改成「整條只有一個
`kubectl`」才成立。⛔ `SHELL_CHAIN_RE`（含 pipe）仍是 P-board 用的，不要拿它回去給 kubectl。
`RETIRED_TERMS` 把「、pipe、反引號」列為廢止詞彙（那是舊版 ops_k8s prompt 把 pipe 列進串接的寫法）。

### 11. 八份 worker 模板各自手抄鐵律／守線強度／跨 repo／回報段（FIX ROUND 0 起，2026-09-14 改為 `_shared/` 片段）

約 3.6 KB 逐字相同的段落在八份 `_templates/<role>/AGENTS.md` 各存一份，已經漂出變體（ops_k8s 的鐵律結構不同、
唯讀角色的回報段講 pytest collected cases、每個角色的守線清單都列只掛在 ops_k8s 的 P-kubectl；review F10）。
改法：片段抽到 `_templates/_shared/<片段>.md`，模板用 `<!-- include: <片段> -->` 引用，`generate-worker.py`
複製 AGENTS.md 時展開；驗收第 20 項對所有模板（不論 ROSTER 是否引用）解析 include 並抓孤兒片段。片段裡提到只掛在
部分角色的 policy 要寫明「只掛在誰身上」。

### 12. 「進 repo root 只為建 worktree、只在 worktree 裡改檔」純靠紀律（#1099 起，2026-09-14 補機器守線）

#1099 把 worktree 改由 implement worker 自建，`block_working_dir_changes.allowed_dirs` 因此含 repo root，
而 `worktree_guard` 的真規則只是「絕對路徑或含 `..` 一律 DENY」——相對路徑寫到 sibling repo
（`WOOW/ecpay_odoo18/x.py`）或 repo root 的主 checkout（`paas/woow_paas_platform/src/x.py`）兩條都放行，
prompt 只能誠實寫成紀律規則（review F6）。改法：產生器在派工當下用 worktree 相對 workspace 的路徑多掛一支
`p_path_write_inside_worktree`（比對檔案工具的 `path`／`file_path`，A 類），implement 沒帶 worktree 直接拒絕產生。
shell 重導向那一側仍是紀律（記在 `known_gap`）。`RETIRED_TERMS` 把「「只在 worktree 裡改檔」是紀律規則」列為廢止詞彙。

## woow-security-sweep 整體關係

（#1978）sweep 要給多人使用：每個人建自己的排程，也能從 Omnigent UI 直接叫起。拆成三個角色：

```
setup skill（repo 層：.agents/skills/woow-security-sweep-setup）
  ├─ doctor：環境與權限檢查（全部由程式判斷，不問人）
  ├─ 註冊／接手／原地更新 ──▶ sweep agent（.omnigent/woow-security-sweep）
  │                            claude-native，agent_id 固定；只由排程或 agent_id 啟動，不在 UI picker
  └─ 註冊／原地更新 ────────▶ 前台 agent（claude-sdk，出現在 UI picker）［階段 B］

sweep-launch skill（前台 agent 與 CLI 共用同一份，build 時複製進前台 bundle）［階段 B］
  ├─ 跑一次：sys_session_create(agent_id=sweep) ＋ sys_session_send(參數)；判斷題照常進 Inbox
  └─ 排程：直接以 sweep 的 agent_id 建 Omnigent 排程，不經前台（少一層成本與失敗點）
```

| 角色 | 位置 | 誰用 |
|---|---|---|
| setup skill | `.agents/skills/woow-security-sweep-setup/`（`.claude/skills/` 有同名入口指向同一目錄） | 維護者發新版；新同事第一次在自己主機設定；排程跑不起來時 |
| sweep agent | `.omnigent/woow-security-sweep/` | 排程（無人值守） |
| 前台 agent［階段 B］ | `.omnigent/<前台 bundle>/` | 任何人從 UI 叫起 |
| sweep-launch skill［階段 B］ | 正本在 repo 層 `.agents/skills/`，build 時複製成實體檔放進前台 bundle | 前台 agent；CLI 的 Claude Code／Pi session |

**為什麼前台與 sweep 分開**：sweep 必須是 `claude-native` 才能把判斷題送進 Inbox，但 UI picker
會過濾頂層 `*-native`（§UI picker）。前台用 `claude-sdk` 進 picker；從 UI 叫起時人就在對話框前，
參數直接在聊天裡問，不需要 Inbox。

### 由 server 程式碼決定的規則（setup skill 的 doctor 逐項檢查）

| 規則 | 出處 | 後果 |
|---|---|---|
| 排程只能建在呼叫者自己的主機 | `routes/_host_launch.py::resolve_host_owner`（別人的 host 回 403 `not your host`） | 每個人用自己的主機；setup 在哪台跑就裝在哪台。不同人不可能共用同一台主機的 state |
| 上傳的 agent 是 session-scoped，對 anchor session 有 READ 即可使用 | `routes/_session_create_validation.py::validate_session_agent` | 維護者把 anchor session 分享給同事，同事就能用同一個 agent_id |
| 原地更新需要 anchor session 的 OWNER | `routes/sessions/routes_agent.py::update_session_agent`（`PUT /v1/sessions/{id}/agent`） | 共用時只有維護者能發新版；同事要自己維護就自己註冊一份 |
| `sys_session_create(config_path=…)` 每次都建新的 agent row | `_create_session_from_bundle` | 改版要用 PUT 原地更新，否則 agent_id 會換、排程要跟著改 |

每台主機的 agent 紀錄存在 `<WS>/.omnigent-runtime/woow-security-sweep/agent.json`（anchor session、
agent_id、能否更新），不進 repo。發新版走 setup skill 的 `bundle-plan` → 人核准 plan digest →
`bundle-apply`；打包只取已 commit 的檔案（`git archive`），不會把 `__pycache__` 帶上去。

## 機器相關的值

**產生出來的 bundle 裡沒有任何機器相關的絕對路徑**——`os_env.cwd` 一律是 `.`（理由見
§`os_env.cwd`）。換機器不需要改 `build-bundles.py`。

唯一與本機綁定的是 worker prompt 裡舉例用的跨 repo 唯讀路徑（`/home/elmo/woow-repos/<repo>/…`
這種 `grep` 範例），那些在 `_templates/*/AGENTS.md` 與 `_templates/_shared/*.md` 的來源池裡，改了重跑產生器即可。

快層 model 同理，釘在 `generate-worker.py` 的 `ROSTER`（scout／tracker／researcher 那幾格）；
model 名會隨 CLI／catalog 版本漂移，當下可用的用 `sys_list_models` 查。

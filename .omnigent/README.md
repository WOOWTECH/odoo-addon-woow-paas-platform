# woow Omnigent multi-agent bundle

woow 六個 repo 的 issue／實作編制。**本檔是給維護者看的**，不進任何 agent 的 prompt。

```
cd /home/elmo/woow-repos && omnigent run .omnigent/woow-issue-leader --server <url>
   ↑ workspace = 六 repo 的共同祖先（不是任何單一 repo——理由見 §workspace）

woow-issue-leader/          claude-native／claude-fable-5-1／xhigh／spawn: true
├── config.yaml             產物
├── AGENTS.md               手改
├── hooks/                  ⭐ dispatcher：把子 repo 的 hooks 拿回來（§dispatcher）
├── skills/                 ⚠ **必須存在**，否則不發 --plugin-dir、dispatcher 不生效
│   ├── issue-triage/  pr-merge-gate/  board-reconcile/
│   └── dispatch-policy/    對照表與 alias map 的使用規則
├── _templates/             ⭐ 產生器的模板來源（8 份 worker prompt ＋ skills）
├── generate-worker.py      ⭐ 執行期產生器（五道防線，§產生器）
└── agents/
    ├── repo_leader_cc/     claude-native／claude-fable-5-1／xhigh／spawn: true
    └── repo_leader_pi/     pi／gpt-6-astra／xhigh／spawn: true
        （⚠ 底下**沒有** agents/——worker 在執行期產生）
```

## 🔴 worker 是執行期產生的，不是靜態 sub-agent

這是本架構的核心決定。原因是實測出來的硬限制：

> **`executor.config.harness` 是 bundle 屬性，派工當下換不掉。**
> `sys_session_send` 只能覆寫 `args.model` / `args.reasoning_effort`，harness 不在其中。

而需求是「harness 由 issue-leader 的判斷決定」（同一個 purpose 可能要 claude-native 或 pi，
視額度池與跨 vendor 覆核方向而定）⇒ 靜態 bundle 做不到，只有**執行期產生 config** 做得到。

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
        --purpose implement --harness claude-native --repo paas-platform \
        --worktree /home/elmo/woow-repos/paas/wt-1032 --name impl-1032-cc)
# → sys_session_create(config_path=$DIR, title="1032-impl-cutover")
```

## 用法

```bash
python3 .omnigent/build-bundles.py           # 重產三份靜態 config ＋ repo-leader 的副本
python3 .omnigent/build-bundles.py --check   # 驗收 15 項（含產生器自檢與 dispatcher 端到端）
python3 .omnigent/woow-issue-leader/generate-worker.py --self-check   # 只驗產生器
python3 .omnigent/test-dispatcher.py         # 只驗 dispatcher（fixture repo，零副作用）
bash .omnigent/mutation-tests.sh [case 代號]  # 突變測試（每個 case 斷言「紅在哪」）
```

| 路徑 | 內容 | 誰維護 |
| --- | --- | --- |
| `_templates/<role>/AGENTS.md` ＋ `skills/` | 8 份 worker prompt／skill（**產生器的來源**） | 手改 |
| `_shared/repo_leader/AGENTS.md` | repo-leader 共用 prompt | 手改 |
| `woow-issue-leader/AGENTS.md` ＋ `skills/` | issue-leader 的 prompt 與 4 個 skill | 手改 |
| `generate-worker.py` | 執行期產生器 ＋ 對照表 ＋ **7 支 CEL 與所有 policy 片段** | 手改 |
| `hooks/dispatch-hook.py` ＋ `hooks.json` | dispatcher | 手改 |
| `build-bundles.py` | 三份靜態 config 的產生器 ＋ 15 項驗收 | 手改 |
| **三份 `config.yaml`** ＋ repo-leader 的 `AGENTS.md`／`skills/`／`hooks/` | 產物 | ⛔ **勿手改**（drift 由第 0 項對帳） |

⚠ **CEL 與 policy 片段住在 `generate-worker.py`，不在 `build-bundles.py`。** 因為產生器要在
**執行期**跑（repo-leader 派工前呼叫它），而 `build-bundles.py` 不在 bundle 內、執行期拿不到。
真相放在執行期拿得到的那一邊，`build-bundles.py` import 過來用 ⇒ 只有一份。

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
| 2 | `instructions:` 路徑失效 → **路徑字串變成 system prompt** | 只寫實體 `AGENTS.md`；寫完 `parse()` 驗 instructions 非空、含 marker、不像路徑字串 |
| 3 | 檔名不是 `config.yaml` | 硬編檔名。⚠ 實測錯誤訊息是 `config.yaml not found in /tmp/tmpXXXX/agent`，完全誤導 |
| 4 | `skills/` 有目錄無 `SKILL.md` → 靜默空 | 產生後驗 skills 數 == 模板預期數 |
| 5 | ⛔ `skills_filter` 設 `none` | 它會發 `--setting-sources ""`，**壓掉 dispatcher 與所有 repo hooks** ⇒ 一律固定 `all` |

## 對照表

真相在 `generate-worker.py` 的 `ROSTER`；給人看的摘要在 `skills/dispatch-policy/SKILL.md`。
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
- **唯讀 worker 完全省略 `os_env` block** ⇒ 四支 `sys_os_*` 一支都不註冊，這是 A 類 sound
  的守線。

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
| `p_model_fast_tier_only` | 三層 leader | 對 `scout_pi` / `tracker` / `researcher` 派工未帶或帶了非快層 `args.model` |
| `p_merge_no_agent_merge` | 全部 | 任何 forge CLI（`tea`/`gh`/`glab`）同句出現 `merge`，或 `curl` 打 merge endpoint |
| `p_approve_no_agent_signoff` | 全部 | 指令中**出現 `/approve` 字串**（不再限定工具名） |
| `p_tag_no_agent_tag_push` | 全部 | `git push`＋tag 形態、`refs/tags/`、forge `release`、`git tag <name>` |
| `p_kubectl_namespace_scope` | 僅 `ops_k8s_*` | 提到 `kubectl` 且：含串接／`-n` 出現多次／寫入 verb 但 ns 不在白名單／寫入 verb 但沒帶 `--context` |

### ⚠ 已知的刻意誤擋（fail loud，不放寬）

| 被擋的合法操作 | 替代做法 | 為什麼不放寬 |
| --- | --- | --- |
| `grep -rn '/approve' docs/` | 搜尋字串去掉斜線：`grep -rn approve docs/` | 放寬就要回到「枚舉工具名」，那正是被繞過的原因 |
| `kubectl ... get ... && echo done`（唯讀＋串接） | **拆成兩次 tool call** | 串接之後做什麼無法用 regex 證明 |
| `kubectl --context woow-k3s apply -f x.yaml` | 先 `--dry-run=server`；真要套用走 `deploy-stg` skill 或交給人 | manifest 內的 `metadata.namespace` 會覆蓋 `-n`，regex 看不到檔案內容 |
| `git tag v1.0`（本機建 tag，未推） | 讀取用 `git tag -l` / `--list`；建 tag 交給人 | 建 tag 是發版動作的第一步 |

⚠ `kubectl --context docker-desktop ...` **不再被擋**（FIX ROUND 2）：docker-desktop 是本
機測試／開發用叢集，鐵律限定的是 woow-k3s 這個共用叢集。舊版不分 context，會毀掉
`/woow-dev:k8s-dev-sandbox` 的 create／destroy／test 工作流。

### 🔴 守線的強度分級（讀這段再決定要不要信任某條守線）

**不是每一條守線都一樣強。** 混為一談會導致兩種錯誤：把 best-effort 的當成保證（危險），
或把可靠的也當成擺設（浪費）。

#### A. Sound —— 繞不過，可以當成保證

| 守線 | 為什麼可靠 |
| --- | --- |
| `read_only_os` | 比對**工具名**（`Write`/`Edit`/`sys_os_write`…），不看 shell 字串 |
| **省略 `os_env`** | `sys_os_read/write/edit/shell` 四支**根本不註冊**——沒有這個工具就做不到 |
| `p_model_fast_tier_only` | 讀的是 `sys_session_send` 的**結構化參數**，不是 shell 字串 |
| `headless_subagent_purpose_guard` / `spawn_bounds` | 同上，結構化參數 |
| `block_working_dir_changes` | 有自己的 shell 解析（非單一 regex），且只需判斷 `cd`/`git -C` 這種固定形狀 |
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
| 變數展開 | `V=v1.7.6; git push origin $V` |
| 命令替換 | `curl -X PUT "$API/pulls/1/$(printf 'me'; printf 'rge')"` |
| 引號切斷 | `tea comment 823 '/appr''ove'` |
| base64 解碼後由 stdin 送 body | `echo L2FwcHJvdmU= \| base64 -d \| tea comment 823 -F -` |
| 檔案 body | `base64 -d /tmp/b.b64 > /tmp/b && tea comment 823 -F /tmp/b` |
| `$'\x..'` hex 轉義 | `tea comment 823 $'\x2f\x61\x70\x70\x72\x6f\x76\x65'` |

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
2. **會把「刻意省略 `os_env`」這個 sound 的守線反過來拆掉。** runner 套用 verdict 的
   `_apply_sandbox_override_from_verdict` 會做 `if spec.os_env is None: spec.os_env =
   OSEnvSpec()`——實測對 `researcher_via_pi` 套下去，`os_env` 從 `None` 變成一個完整的
   `OSEnvSpec`。而「省略 `os_env` ⇒ 四支 `sys_os_*` 都不註冊」正是唯讀 worker 最強的那道
   保證。等於為了加一層 best-effort，拆掉一層 sound 的。
3. `sandbox: {type: none}` 搭 `allow_network: false` 會直接
   `ValueError: sandbox type 'none' cannot restrict network`。

⇒ 唯讀 worker 的保證維持在 **A 類**：`read_only_os` ＋ **省略 `os_env`**。

### ⚠ `cost_budget` 只掛 claude-native

pi 是 subscription 且無 catalog pricing，掛上去會走 `_UNPRICED_ASK`，**每個 session 卡一次
人工批准**，會毀掉無人值守。claude-native 的用量由 Claude Code 自己的 statusLine 回報
`total_cost_usd`，有定價可比。

### 🔴 白名單同步債

`p_kubectl_namespace_scope` 的 namespace 白名單是**第五份副本**。權威三份由 repo-lint
`kubectl-ns-whitelist-sync` 對帳（`CLAUDE.md` 表格、
`docs/guides/deployment/shared-cluster-paas-scope-guard.md`、
`.claude/hooks/kubectl-ns-guard.py`）；第四份在 `_workers/ops_k8s/AGENTS.md`，第五份在
`build-bundles.py` 的 `K8S_ALLOWED_NAMESPACES`。**後兩份不在 repo-lint 對帳範圍內——改白
名單時五份都要一起改。**

**權威守線是 repo 的 `.claude/hooks/kubectl-ns-guard.py`**（用 `shlex` 正確解析，且
repo-lint 對帳白名單）；workspace 設成本 repo／其 worktree 時它會自動載入。
`p_kubectl_namespace_scope` 是 **第二層 defense-in-depth、best-effort**：regex 比對、不做
shell 解析，認不出 `bash -c` 包一層或 script 檔裡的 kubectl，**已知可被 shell 轉義繞過**。
⛔ 不要在任何地方把它寫成「機器強制」或暗示它擋得住刻意規避。它的價值是「workspace 不在
本 repo 時仍有一層接得住手滑」。

## 驗收與突變測試的關係

`--check` 有 13 項驗收。但**「全部 PASS」本身不代表驗收有效**——本 repo 已經抓到三次
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

## 機器相關的值

**產生出來的 bundle 裡沒有任何機器相關的絕對路徑**——`os_env.cwd` 一律是 `.`（理由見
§`os_env.cwd`）。換機器不需要改 `build-bundles.py`。

唯一與本機綁定的是 worker prompt 裡舉例用的跨 repo 唯讀路徑（`/home/elmo/woow-repos/<repo>/…`
這種 `grep` 範例），那些在 `_workers/*/AGENTS.md` 的來源池裡，改了重跑產生器即可。

快層 model 名單同理，只有 `build-bundles.py` 的 `FAST_MODELS` 一處；model 名會隨 CLI／
catalog 版本漂移，當下可用的用 `sys_list_models` 查。

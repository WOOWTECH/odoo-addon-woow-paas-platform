---
name: dispatch-policy
description: 派工對照表與 repo alias map 的使用規則：怎麼選 harness／model／effort、怎麼解析 repo 路徑、解析不到要 ASK。
---

# dispatch-policy

## 什麼時候用

要派工給 worker 之前（決定 harness／model），或要把 repo alias 變成實際路徑之前。

## 1. worker 是執行期產生的，不是靜態 sub-agent

⚠ **`executor.config.harness` 是 bundle 屬性，派工當下換不掉**——`sys_session_send` 只能用
`args.model` / `args.reasoning_effort` 覆寫。所以「同一個 purpose 要換 harness」只有一條路：
**先產生 config，再 `sys_session_create(config_path=…)`**。

`spawn_bounds` 每輪最多計 12 次 `sys_session_create`；`sys_session_send` 初次送工單與續談不計數。
此上限限制 session 建立，不代表同時運行的 worker 上限；收到新 request（含 wake notice）會重設。

### 四步流程

```bash
# 1. 依 §3 alias map 解析 repo 實際路徑（⛔ 不可字串拼接）
#    paas-platform → /home/elmo/woow-repos/paas/woow_paas_platform

# 2. 產生 worker config（⛔ 一定要走這支，它有下列五道防線）
DIR=$(python3 /home/elmo/woow-repos/paas/woow_paas_platform/.omnigent/woow-issue-leader/generate-worker.py --purpose implement --harness pi --repo paas-platform --repo-root /home/elmo/woow-repos/paas/woow_paas_platform --worktree /home/elmo/woow-repos/paas/wt-1032 --name impl-1032-pi)

# 3. 建 session（⛔ 不帶工單）
#    conversation_id = sys_session_create(config_path=$DIR) 回傳的 conversation_id
# 4. 送工單——這是唯一的喚醒線
#    sys_session_send(session_id=<conversation_id>, args.purpose="implement", args.input=<工單>)
```

`implement` 必須同時給 `--repo-root` 與 `--worktree`：worktree 由 worker **自己建**（你只指定
路徑，⛔ 不要替它建），兩個路徑都會進 `block_working_dir_changes.allowed_dirs`。

`--name` 同時是 worker 的 agent name 與目錄名（預設 `<purpose>_<cc|pi>`）；寫成
`<purpose>-<issue>-<cc|pi>`，讓 UI／log 分得出是哪張 issue。同名不衝突（每次上傳都是新的
session-scoped agent）；會撞的是同一 parent 下的 `sys_session_send` title，依 AGENTS.md
「一張 issue 一個 title」規則命名。

`sys_session_create` 一步起（帶 message）沒有 task handle；worker 完成後你永遠不會被叫醒。
只有上面的兩步起法會建立可靠的一跳喚醒線。送完工就結束這一輪，收到
完成通知才呼叫 `sys_read_inbox`；⛔ 不在派工前讀 inbox。

任何一次 `sys_session_send` 回 `requires parent session inbox`，就只試那一次：立即停手，標
`agent-status:blocked` 並用 Gitea comment 貼錯誤原文。⛔ 不重建 child、不重送、不插入無關
呼叫閃避守線。

⛔ **不要自己手寫 worker 的 config.yaml**。五道防線擋的全是**靜默**失敗：

| 防線 | 擋什麼 |
| --- | --- |
| 1 | harness × model 不相容。⚠ omnigent 在 create 這條路徑**完全不驗**，會等 child 第一輪才爆成通用 `runner_error`（訊息不會告訴你原因） |
| 2 | `instructions:` 路徑失效 ⇒ **整條路徑字串變成 system prompt**，不報錯 |
| 3 | 檔名不是 `config.yaml`（錯誤訊息指向 omnigent 的臨時目錄，完全誤導） |
| 4 | `skills/` 有目錄但沒有 `SKILL.md` ⇒ 靜默為空 |
| 5 | `skills_filter: none` ⇒ 發出 `--setting-sources ""`，**壓掉 dispatcher 與所有 repo hooks** |

產生器拒絕時**不要硬派**——它印的理由就是要修的東西。修不掉就標
`agent-status:blocked`，並在 issue comment 貼產生器原文。

## 2. 對照表（產生器的 `ROSTER` 是唯一真相，這裡是給人看的摘要）

| purpose | harness | model／effort | 定位 | 寫權 |
|---|---|---|---|---|
| `repo-leader` | claude-native／pi | `claude-fable-5-1[1m]`／`gpt-6-astra`；xhigh | issue 全程與派工 | ⛔ 不寫產品碼 |
| `implement` | pi／claude-native | `gpt-6.1-sol`／`claude-fable-5-1[1m]`；xhigh | 多檔重構、寫測試 | ✅ |
| `review` | claude-native／pi | `claude-sonnet-5-5[1m]`／`gpt-6-astra`；high | 審 diff | ⛔ 唯讀 |
| `explore`（＝`search`） | pi | `gpt-6-luna`；high | recon／lookup | ⛔ 唯讀 |
| `tracker`（＝`triage`） | pi | `gpt-5.6-terra`；high | 六 repo `tea` 對帳、查重、開單 | ✅（只寫產物） |
| `ops_k8s`（＝`deploy`） | **固定 claude-native** | `claude-sonnet-5-5[1m]`；xhigh | kubectl／deploy-stg／sandbox | ✅ |
| `researcher` | pi | `gpt-6-luna`；high | 上游 addon／Odoo 18 行為查證 | ⛔ 唯讀 |

model／effort 用對照表的預設；要覆寫就由 `generate-worker.py` 帶 `--model`／`--effort`，
仍會過防線 1。

硬規則：

- **implement 與 review 跨 vendor 配對**（#1729 D1=B）：正常 `implement pi ⇒ review
  claude-native`、`implement claude-native ⇒ review pi`。pi 額度池不可用時才准用降級模式
  `implement claude-native ⇒ review claude-native`，三條件缺一不可：reviewer model 與
  implementer 不同、PR body 第一段標「⚠ 同 vendor 覆核（pi 池不可用）」、掛 `pr-open` 前在 issue
  留言貼覆核摘要讓人再看一眼（細節見 AGENTS.md「跨 vendor 覆核」）。⚠ 產生器**不擋**同 vendor
  組合——這是紀律規則；派 reviewer 的工單必帶「implementer harness／model」兩欄。
- **implement + claude-native 不掛上游 `worktree_guard`**：它對 Claude 原生 Edit／Write 必帶的
  絕對 `file_path` 一律 DENY；改由 `P-path`（放行 worktree 前綴下的絕對路徑）＋
  `block_working_dir_changes` 守 worktree。implement + pi 三條都掛。
- **implement + claude-native 的 `cost_budget` 只有 350 USD 硬上限、不設 ask**（#1729 D2）：撞頂
  時 worker 會 DENY 每一輪工具呼叫並回報 ⇒ 標 `agent-status:blocked`＋留言貼原文，⛔ 不重派。
- **`ops_k8s` 固定 claude-native**。pi 沒有 hook 機制（`pi --help` 完全沒有 hook），換成 pi
  會讓 dispatcher 失效 ⇒ repo 的 `kubectl-ns-guard` 拿不回來。產生器會直接拒絕 `ops_k8s + pi`。
- **claude 家族只吃 id 含 `claude` 的 model**：`opus` / `sonnet` / `fable` 這些 CLI alias
  一律被擋（產生器防線 1）。要完整 id。
- **effort 只用 `high` / `xhigh`**。`ultra` 只有 pi 原生支援，在 claude 上會**靜默降級**成
  `xhigh`——寫了也不會報錯，只是沒效果，所以不要寫。

## 3. repo alias map

⚠ **不可用字串拼接推路徑**：`paas-platform` 的目錄名是 `woow_paas_platform`，而且在 `paas/`
子層；`paas-operator` **不是獨立 repo**，它是 `paas-platform` 底下的 `extra/paas-operator`。

| alias | root | subpath |
|---|---|---|
| `paas-platform` | `/home/elmo/woow-repos/paas/woow_paas_platform` | — |
| `paas-operator` | `/home/elmo/woow-repos/paas/woow_paas_platform` | `extra/paas-operator` |
| `ecpay` | `/home/elmo/woow-repos/WOOW/ecpay_odoo18` | — |
| `paas-charts` | ⛔ 不在本機 | — |
| `paas-ci` | ⛔ 不在本機 | — |
| `woow-components` | ⛔ 不在本機 | — |
| `sh_subscription` | ⛔ 不在本機 | — |
| `paas-agent-runtime` | ⛔ 不在本機 | — |

**解析不到就 ASK**（問使用者要不要 clone），⛔ 不要猜路徑、不要自己 clone。
⚠ ASK 是 fail-closed 的：`ask_timeout` 已刻意調短（900 秒，不是預設的 86400），逾時就失敗
而不是卡住整條派工鏈。所以 ASK 之後要盯著結果，逾時了要重新決策而不是重送。

## 4. workspace 必須是共同祖先

```bash
cd /home/elmo/woow-repos && omnigent run <bundle> --server <url>
```

⚠ **不能**在單一 repo 內啟動：top-level session 之間**不能互相 `sys_session_send`**，
跨 repo 統籌只有「同一棵樹」這個形狀成立。

代價與補償：子 repo 的 `.claude/` 三項（hooks／CLAUDE.md／skills）在共同祖先下全失。
**hooks 由 bundle 的 dispatcher 拿回來**（`hooks/dispatch-hook.py`，所以
`kubectl-ns-guard`、`pattern-guard`、`repo-lint` 都還在）；CLAUDE.md 與 repo skills 拿不回來。
詳見 `.omnigent/README.md` §workspace。

因此工單要**自帶** worker 該知道的指令、指定 worktree 路徑與驗收條件，不得依賴 worker
自動看到 CLAUDE.md／skills。⛔ 不要寫「照 repo 的 CLAUDE.md 做」或「用 deploy-stg skill」；
worker 看不到那些。

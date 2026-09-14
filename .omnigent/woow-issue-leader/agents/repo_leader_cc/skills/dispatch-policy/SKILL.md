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
**先寫出 config，再 `sys_session_create(config_path=…)`**。

流程（repo-leader 做）：

```bash
# 1. 產生（⛔ 一定要走這支，它有五道防線）
DIR=$(python3 <paas-platform root>/.omnigent/woow-issue-leader/generate-worker.py \
        --purpose implement --harness pi \
        --repo paas-platform --repo-root /home/elmo/woow-repos/paas/woow_paas_platform \
        --worktree /home/elmo/woow-repos/paas/wt-1032 \
        --name impl-1032-pi)
# implement 必須同時給 --repo-root 與 --worktree：worktree 由 worker 自己
# `git -C <repo-root> worktree add <worktree>`，兩個路徑都進 allowed_dirs；你只指定路徑。
# 2. 建 session（⛔ 不帶工單）
#    conversation_id = sys_session_create(config_path=$DIR) 回傳的 conversation_id
# 3. 送工單——這是唯一的喚醒線
#    sys_session_send(session_id=<conversation_id>, args.purpose="implement", args.input=<工單>)
```

`sys_session_create` 一步起（帶 message）沒有 handle，worker 完成後無處投遞；只有上面的
兩步起法會建立可靠的一跳喚醒線。

⛔ **不要自己手寫 worker 的 config.yaml**。五道防線擋的都是**靜默**失敗：harness×model 不
相容（omnigent 在這條路徑完全不驗，會等 child 第一輪才爆成通用 `runner_error`）、
`instructions:` 路徑變成 system prompt、檔名不是 `config.yaml`（錯誤訊息指向 omnigent 的
臨時目錄，完全誤導）、`skills/` 有目錄無 `SKILL.md` 靜默為空、`skills_filter: none` 壓掉
所有 hooks。

產生器拒絕時**不要硬派**——它印的理由就是要修的東西。

## 2. 對照表（產生器的 `ROSTER` 是唯一真相，這裡是給人看的摘要）

| purpose | harness | model | effort |
|---|---|---|---|
| `repo-leader` | claude-native／pi | `claude-fable-5-1`／`gpt-6-astra` | xhigh |
| `implement` | **固定 pi** | `gpt-5.6-sol` | xhigh |
| `review` | **固定 claude-native** | `claude-sonnet-5` | high |
| `explore`（＝`search`） | pi | `gpt-5.6-luna` | high |
| `tracker`（＝`triage`） | pi | `gpt-5.6-terra` | high |
| `ops_k8s`（＝`deploy`） | **固定 claude-native** | `claude-sonnet-5` | xhigh |
| `researcher` | pi | `gpt-5.6-luna` | high |

硬規則：

- **實作固定 pi ⇒ 覆核固定 claude-native（單向）**，跨 vendor 自動成立。產生器會拒絕
  `implement + claude-native`：上游 `worktree_guard` 對 Claude 原生 Edit／Write 必帶的
  絕對 `file_path` 一律 DENY，implement_cc 實測不能改檔。
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
**hooks 由 dispatcher 拿回來**（`hooks/dispatch-hook.py`）；CLAUDE.md 與 skills 拿不回來，
所以關鍵守線知識已經寫進各 worker 的 prompt。詳見 `.omnigent/README.md` §workspace。

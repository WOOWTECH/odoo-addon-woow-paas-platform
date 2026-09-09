# woow Omnigent multi-agent bundle

取代原本 Maestri 的 `woow-issue-leader` / `woow-repo-agent-leader` 編制。

```
woow-issue-leader                     ← issue 層總指揮（claude-native）
├── repo_leader_cc   (claude-native)  ← repo 層 leader，Claude 額度池
│   └── 8 個 worker thin shell（*_via_cc）
└── repo_leader_pi   (pi)             ← repo 層 leader，pi 額度池
    └── 8 個 worker thin shell（*_via_pi）
```

**16 個 thin shell = 8 個 worker 角色 × 2 個 repo_leader**，但 prompt 與 skill 只有
**8 份實體**：thin shell 的 `AGENTS.md` 與 `skills/` 都是 symlink 指回
`_workers/<worker>/`。兩份 `repo_leader` 的 `AGENTS.md` 同樣 symlink 到
`_shared/repo_leader/AGENTS.md`（同一個檔，靠「harness 自辨」段落服務兩邊）。

## 目錄

| 路徑 | 內容 | 誰維護 |
| --- | --- | --- |
| `_workers/<worker>/AGENTS.md` | 8 份 worker prompt（**實體**） | 手改 |
| `_workers/<worker>/skills/<skill>/SKILL.md` | 8 份 worker skill（**實體**） | 手改 |
| `_shared/repo_leader/AGENTS.md` | repo_leader 共用 prompt（**實體**） | 手改 |
| `woow-issue-leader/AGENTS.md` ＋ `skills/` | issue-leader 的 prompt 與 3 個 skill | 手改 |
| **所有 `config.yaml`** | 19 份（1 leader ＋ 2 repo_leader ＋ 16 thin shell） | ⛔ **產物，勿手改** |
| 所有 `AGENTS.md` / `skills` symlink | 34 條 | ⛔ **產物，勿手改** |
| `build-bundles.py` | 產生器 ＋ 驗收 | 手改 |
| `config.yaml`（根目錄那一份） | Omnigent CLI 的 **專案層預設**（`omnigent config`，untracked，機器本地） | 不屬本 bundle |

## 用法

```bash
python3 .omnigent/build-bundles.py           # 重產所有 config.yaml 與 symlink
python3 .omnigent/build-bundles.py --check   # 驗收（產物不同步也會判 FAIL）
```

改了 `build-bundles.py` **一定要重跑一次不帶參數的版本**再 commit，否則 `--check` 的第 0
項會紅。

啟動一輪工（一張 issue 一個 runner，workspace 就是該 issue 的 worktree）：

```bash
git -C <repo> worktree add ../wt-<issue-id> -b <type>/<issue-id> origin/<整合分支>
cd ../wt-<issue-id>
omnigent run --server <url> .omnigent/woow-issue-leader
```

## ⛔ 六條會靜默壞掉的規則

全部實測過，違反時 **`validate()` 不會紅**：

1. **prompt 只能用 `AGENTS.md` symlink 承載。** 改寫成 `instructions: <相對路徑>` 搭外指
   symlink 時，containment 檢查會回 `None`，接著**整條路徑字串被當成 system prompt**，
   而且不報錯。→ 驗收第 3 項擋。
2. **symlink 一律用 `os.path.relpath` 推算，不要手數 `../`。** 斷鏈時 `is_dir()` 回
   False → **靜默少發現一個 sub-agent／少一批 skill**。→ 驗收第 4、7 項擋。
3. **每個 thin shell 都要列進上層 `tools.agents`。** 這是第 2 條的安全網：引用不存在的
   sub-agent 會 **loud fail**。→ 驗收第 6 項擋。
4. **`tools.agents` 列的是 YAML `name`，不是資料夾名。** `name` 省略時是 `None`、不會
   fallback 成目錄名。本樹刻意讓兩者相同，直接填掉這個坑。
5. **skill name 一律取自 frontmatter（目錄名被忽略），正則 `^[a-z0-9-]+$`（不允許底
   線），而且來源池必須全域唯一**（`discover_host_skills` first-wins 去重會吃掉同名後
   者）——所以每個 skill 都加了 worker 前綴。→ 驗收第 5b 項擋。
6. **`skills:` 一律固定 `all`。** 名單模式在 claude-native 下形同 `all`、不強制；在 pi
   下卻會下 `--no-skills` **把 host skill 一併關掉**——跨 harness 行為相反。

另外兩條會影響行為但不會壞掉的：

- **child 不繼承 parent 的 `guardrails.policies`** ⇒ 每個 worker 自帶完整 policy。
- **`os_env.cwd` 不決定 harness process cwd**，它只是邊界與 containment 錨點；唯讀
  worker（`verifier_pi` / `scout_pi` / `researcher`）**完全省略** `os_env` block，四支
  `sys_os_*` 就一支都不註冊。

## Guardrails

內建 policy 見各 `config.yaml`。自訂守線一律用
`omnigent.policies.builtins.cel.cel_policy`（⛔ 不寫自訂 Python policy 模組——那要裝進
omnigent 的 venv，而且 `function.path` 打錯字會讓 worker 連 `Read` 都被 DENY）。

| 守線 | 掛在哪 | 擋什麼 |
| --- | --- | --- |
| `p_board_read_only` | 除 issue-leader 外**全部** | 寫 `.woow-issues/board.md`（相對／`./`／絕對三種寫法，以及 shell 改寫） |
| `p_model_fast_tier_only` | 三層 leader | 對 `scout_pi` / `tracker` / `researcher` 派工未帶或帶了非快層 `args.model` |
| `p_merge_no_agent_merge` | 全部 | `tea pr\|pulls merge`、`gh pr merge`、`glab mr merge` |
| `p_approve_no_agent_signoff` | 全部 | 送出 `/approve` 留言的指令 |
| `p_tag_no_agent_tag_push` | 全部 | `git push` 推 tag（`v<版號>` / `--tags` / `refs/tags/`） |
| `p_kubectl_namespace_scope` | 僅 `ops_k8s_*` | `--context woow-k3s` 下寫入／exec 類 kubectl 的越界 namespace |

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

`p_kubectl_namespace_scope` 是 **regex 比對，不做 shell 解析**：認不出 `bash -c` 包一層、
認不出 script 檔裡的 kubectl。它是**提醒層**；解析等級的保證仍在
`.claude/hooks/kubectl-ns-guard.py`。

## 機器相關的值

全樹只有一個機器相關的絕對路徑：`os_env.cwd: /home/elmo/woow-repos`（六個 repo 的共同父
目錄，作為 `sys_os_*` containment 的錨點）。換機器時改 `build-bundles.py` 的
`OS_ENV_BLOCK` 再重跑。

快層 model 名單同理，只有 `build-bundles.py` 的 `FAST_MODELS` 一處；model 名會隨 CLI／
catalog 版本漂移，當下可用的用 `sys_list_models` 查。

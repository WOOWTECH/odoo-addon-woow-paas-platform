# woow Omnigent multi-agent bundle

woow 六個 repo 的 issue／實作編制。**本檔是給維護者看的**，不進任何 agent 的 prompt。

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
| thin shell 與 repo_leader 的 `AGENTS.md` ＋ `skills/` | 18 份 `AGENTS.md` ＋ 16 份 `skills/` 的**實體副本**（FIX ROUND 4 前是 symlink） | ⛔ **產物，勿手改**（drift 由 `--check` 第 0 項對帳） |
| `build-bundles.py` | 產生器 ＋ 驗收（`--check` 12 項） | 手改 |
| `mutation-tests.sh` | 突變測試：證明驗收有牙齒（baseline ＋ 18 個 case） | 手改 |
| `config.yaml`（根目錄那一份） | Omnigent CLI 的 **專案層預設**（`omnigent config`，untracked，機器本地） | 不屬本 bundle |

## 用法

```bash
python3 .omnigent/build-bundles.py           # 重產所有 config.yaml 與 symlink
python3 .omnigent/build-bundles.py --check   # 驗收（產物不同步也會判 FAIL）
bash .omnigent/mutation-tests.sh             # 突變測試：確認驗收真的有牙齒
```

改了 `build-bundles.py` **一定要重跑一次不帶參數的版本**再 commit，否則 `--check` 的第 0
項會紅。

啟動一輪工（一張 issue 一個 runner，workspace 就是該 issue 的 worktree）：

```bash
# ⚠ 這三行在 **launch host 的 shell** 裡跑，是「建 runner 之前」的準備動作。
#   它們**不是**在任何 agent session 裡跑的——implementer 身上的
#   block_working_dir_changes 會 DENY `cd` / `git -C` / `git worktree`，那是刻意的，
#   worktree 的建立與銷毀屬於人／launch host，不屬於 worker。
git -C <repo> worktree add ../wt-<issue-id> -b <type>/<issue-id> origin/<整合分支>
cd ../wt-<issue-id>

omnigent run .omnigent/woow-issue-leader --server <url>
```

⚠ 這條指令曾經是壞的（symlink 佈局時代），FIX ROUND 4 改成實體副本後才能直接用。
**不要**再加任何 launcher 包裝——見 §啟動。

## 啟動

```bash
omnigent run .omnigent/woow-issue-leader --server <url>
```

就這樣，沒有 launcher、沒有包裝 script。上傳前想先自檢就跑
`python3 .omnigent/build-bundles.py --check`（第 10 項會拿 server 端真正用的那兩個函式模擬
一次上傳）。

### 🔴 為什麼這裡曾經需要一支 launcher（歷史，別再走回去）

本樹原本用 **symlink** 讓 16 個 thin shell 共用 8 份 prompt／skills。而
`omnigent run <目錄>` 最終走 `chat._bundle_agent()` → `cli._bundle()`，`_bundle()` 對
**目錄**來源是 `source.rglob("*")` ＋ `if file_path.is_file()`（`cli.py:5529-5538`），
**不解 symlink**（只有 source 是**單檔 YAML** 時才會先 materialize，`cli.py:5508`）。
於是兩種 symlink 各有各的壞法：

| 型態 | 當時數量 | 行為 |
| --- | --- | --- |
| symlink **檔**（`AGENTS.md`） | 18 | **loud fail**：tar 留下 link entry，server 端 `extract_safe` 直接拒（`spec/tar_utils.py:126`） |
| symlink **目錄**（`skills/`） | 16 | ⛔ **silent drop**：`rglob` 不遞迴進去，**tar 內 SKILL.md 從 19 筆掉到 3 筆**（只剩 leader 自己的實體 skills），而 `validate` 照樣回 valid、那 16 個 agent 的 `skills` 變成 `[]`，**不報錯** |

⚠ 三條啟動路徑全都呼叫同一個 `_bundle_agent(spec_path)`——`chat._chat_via_daemon()`（remote）、
`chat._chat_local()`（「Start a **local** server with the agent」）、
`chat._run_local_headless_prompt()` ⇒ **改用 local server 也規避不了**。唯一不經過這條打包
路徑的是 `omnigent server --agent <path>` 的 preregister（`cli.py:1483-1487` 自己 materialize）。

**FIX ROUND 4 的裁決：放棄 symlink，改成版控 16 份實體副本**（`build-bundles.py` 產生）。
理由是失效模式的性質——symlink 方案把一個**靜默**失效擺在「人要記得走某個入口」的前提上；
實體副本的失效模式是 drift，而 drift 由 `--check` 第 0 項逐檔比對**內容**、**會紅、看得見**。
用「會紅的風險」換掉「會靜默的風險」。

代價：`.omnigent/` 從 42 個實體檔＋34 條 symlink 變成 **78 個實體檔、0 條 symlink**。
那 16 份副本是**產物**，性質跟 19 份 `config.yaml` 完全相同。

⇒ 所以這套組合是一體的，**不要只改其中一半**：
- 實體副本（第 7／12 項鎖住「一條 symlink 都不能有」）
- `--check` 第 0 項是副本 drift 的唯一守線（突變 Q 實證它會紅）
- 第 10 項直接對原始樹模擬上傳（不再經 materialize），鎖住「這條路真的會通」

### ⚠ 為什麼原本的驗收沒抓到

原本驗的是「原始樹 parse/validate」＋「materialize 後的樹 parse/validate」，兩者都綠。
但 `materialize_bundle()` **在實際啟動路徑上根本不會被呼叫**（只有單檔 YAML 或
`server --agent` 才觸發）⇒ 驗的是一條**不存在於實際流程**的路。

教訓：驗收要對著**實際會跑的那條 code path**，不是對著一個語意相近的 API。現在第 10 項
直接 import `cli._bundle` ＋ `server.bundles.validate_agent_bundle`，跟 server 收到的
是同一份 bytes。

## 🔴 `os_env.cwd` 寫什麼都不生效——但寫絕對路徑會出事

這是本 bundle 最容易被誤解的一點，兩件事要分清楚：

**1. 正常 runner 路徑下，spec 裡的 `cwd` 根本不生效。**
runner 一律用 `OMNIGENT_RUNNER_WORKSPACE` 覆寫它——
`runner/tool_dispatch.py::_resolve_os_env_spec`（約 6247-6259 行）：
「Runner workspace is authoritative — overrides whatever the spec declared
(relative or absolute)」。覆寫發生在 `OSEnvironment` 建立**之前**，所以 harness 的實際
cwd **與** `sys_os_*` 的 containment 錨點**兩者都**以 workspace 為準。

⇒ **實際錨點 = 啟動 `omnigent run` 時的 shell cwd**（本 bundle 的用法就是那個 worktree）。
⇒ 想讓跨 repo 唯讀落在邊界內，**不能**靠在 spec 寫父目錄，要靠**啟動時站在哪裡**。

**2. 沒有 workspace 的 runner 上，絕對 `cwd` 會生效——而且那是已登錄的漏洞面。**
`server/bundles.py::_cwd_escapes_workspace`（約 58-70 行）：絕對或含 `..` 的 cwd
「on a runner without `OMNIGENT_RUNNER_WORKSPACE` it becomes the agent environment root
and copytree source, exposing the host filesystem」（**GHSA-p8rw-8qj3-hf33**）。
所以 deployed（multi-user）server 以 `enforce_handler_allowlist=True` 直接**拒收整個
bundle**（約 140-152 行）。

⇒ 結論：絕對路徑在「有 workspace」時**無效**、在「沒有 workspace」時**危險**、上 deployed
server 時**被拒**。三種情況沒有一種是好的 ⇒ **一律寫 `.`**。驗收第 11 項機器強制。

⚠ 本 repo 的檢查比 server **更嚴**：`server/bundles.py` 只看 **root spec** 的 cwd（不遞迴
sub-agent），所以只有 sub-agent 寫絕對路徑時 server 不會拒——但曝光面還在。第 11 項掃全樹。

## ⛔ 八條會靜默壞掉的規則

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
7. 🔴 **symlink 目錄在上傳路徑上會被 silent drop。**（FIX ROUND 3 發現，ROUND 4 根治）
   `omnigent run <目錄>` 的打包器不解 symlink：symlink **目錄**被 `rglob` 跳過，
   **tar 內 SKILL.md 從 19 筆掉到 3 筆**——16 個 agent 的 skills 整批消失，而 `validate`
   照樣回 valid、`skills` 變 `[]`，⛔ 不報錯（symlink **檔**則是 loud fail，看得到）。
   ⇒ **已根治**：全樹改成實體副本，驗收第 7／12 項鎖住「一條 symlink 都不能有」。
   ⚠ 這一條**不是**前六條那種「寫 config 時的規則」，而是**佈局／啟動方式**的規則——
   前六條全寫對也躲不掉它，因為它發生在打包階段。⛔ 不要為了「少一點重複」把 symlink
   加回來；那 16 份副本是產物，drift 由 `--check` 第 0 項對帳。完整說明見 §啟動。
8. 🔴 **`os_env.cwd` 寫什麼都不生效，但寫絕對路徑會讓 deployed server 拒收整個 bundle。**
   （FIX ROUND 4 新增）runner 一律用 `OMNIGENT_RUNNER_WORKSPACE` 覆寫它，harness 的實際
   cwd 與 `sys_os_*` 的 containment 錨點**兩者都**不看 spec；而絕對路徑在沒有 workspace
   的 runner 上會變成 agent environment root 與 copytree source（**GHSA-p8rw-8qj3-hf33**）。
   ⇒ **一律寫 `.`**。→ 驗收第 11 項擋。容易誤解成「寫父目錄就能擴大 containment 範圍」，
   ⛔ 不能——實際錨點是啟動 `omnigent run` 時的 shell cwd。完整說明見 §`os_env.cwd`。

另外兩條會影響行為但不會壞掉的：

- **child 不繼承 parent 的 `guardrails.policies`** ⇒ 每個 worker 自帶完整 policy。
- **唯讀 worker 完全省略 `os_env` block**（`verifier_pi` / `scout_pi` / `researcher`）⇒
  四支 `sys_os_*` 一支都不註冊，這是 A 類 sound 的守線。
  （`os_env.cwd` 本身的坑已升格為上面第 8 條。）

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
| repo 的 `.claude/hooks/kubectl-ns-guard.py` | 用 `shlex` 正確解析，剝 heredoc、認 `bash -c`、認 `--`；且 repo-lint 對帳白名單 |

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

## 機器相關的值

全樹只有一個機器相關的絕對路徑：`os_env.cwd: /home/elmo/woow-repos`（六個 repo 的共同父
目錄，作為 `sys_os_*` containment 的錨點）。換機器時改 `build-bundles.py` 的
`OS_ENV_BLOCK` 再重跑。

快層 model 名單同理，只有 `build-bundles.py` 的 `FAST_MODELS` 一處；model 名會隨 CLI／
catalog 版本漂移，當下可用的用 `sys_list_models` 查。

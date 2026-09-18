#!/usr/bin/env python3
"""執行期 worker 產生器——repo-leader 派工前用它寫出 worker 的 bundle 目錄。

## 為什麼是執行期產生，不是靜態 bundle

實測：**`executor.config.harness` 是 bundle 屬性，派工當下換不掉**——`sys_session_send`
只能用 `args.model` / `args.reasoning_effort` 覆寫，harness 不在可覆寫之列。而需求是
「harness 由 issue-leader 的判斷決定」（同一個 purpose 可能要 claude-native 或 pi，
視額度池與跨 vendor 覆核方向而定）⇒ 靜態 bundle 做不到，只有執行期產生 config 做得到。

`sys_session_create(config_path=<dir>)` 實測可行：動態 config 的 harness / model /
`guardrails.policies` / `spawn: true` 全部生效、**不需要在父層 `tools.agents` 列名**、
可遞迴再生下一層。附帶好處是產出都是實體檔 ⇒ 天然免疫 symlink 與副本 drift。

## 用法

    generate-worker.py --purpose implement --harness pi --repo paas-platform \\
                       --repo-root /home/elmo/woow-repos/paas/woow_paas_platform \\
                       --worktree /home/elmo/woow-repos/paas/wt-1032 --name impl-1032-pi
    # --name 同時是 worker 的 **agent name 與目錄名**（預設 <purpose>_<cc|pi>）。同名不衝突——
    # omnigent 每次 config_path 上傳都建一個新的 session-scoped agent row，name 只是標籤
    # （server/routes/_sessions/orchestration.py::_create_session_from_bundle 明寫「Agent names
    # are user-authored labels, not global content identities」）；它的用途是讓 UI／log 分得出
    # 這是哪張 issue 的 worker，建議寫成 <purpose>-<issue>-<cc|pi>。
    # implement 必須同時給 --repo-root 與 --worktree：worktree 由 worker **自己** `git -C
    # <repo-root> worktree add <worktree>`（2026-09-14 裁決），兩個路徑都要進 allowed_dirs。
    # P-path 的前綴＝worktree 相對於 runner workspace（$OMNIGENT_RUNNER_WORKSPACE，可用
    # --workspace 覆寫）的路徑；worktree 不在 workspace 底下就拒絕產生。
    # → 寫出 <out-dir>/<name>/{config.yaml, AGENTS.md, skills/, hooks/}
    #   印出該目錄的絕對路徑，交給 sys_session_create(config_path=<那個路徑>)

    generate-worker.py --self-check      # 產生每一種 purpose × harness 組合並自檢，不留檔

⛔ 任一道防線不過就 **fail loud 並拒絕輸出路徑**——寧可不派工，也不要派一個會在第一輪
才爆、或守線靜默失效的 worker。

## 五道必要防線（每一道都有實測依據）

| # | 防的東西 | 做法 |
| --- | --- | --- |
| 1 | `executor.model` 與 harness 不相容 | **寫檔前**呼叫 `model_override.model_family_mismatch()`。⚠ 實測這條路徑 omnigent **完全不驗**（家族檢查只掛在 `sys_session_send` 的 `args.model`），create 會成功，等 child 第一輪才爆成通用 `runner_error` |
| 2 | `instructions:` 路徑失效 → **路徑字串變成 system prompt**；共用片段沒串進去 | 只寫實體 `AGENTS.md`（模板的 `<!-- include: -->` 行換成 `_templates/_shared/<片段>.md` 全文，見該目錄 README）；寫完 `parse()` 驗 instructions 非空、含 marker、不等於任何路徑字串、無殘留 include 標記 |
| 3 | 檔名不是 `config.yaml` | 硬編檔名。⚠ 實測錯誤訊息是 `config.yaml not found in /tmp/tmpXXXX/agent`，完全誤導 |
| 4 | `skills/` 有目錄無 `SKILL.md` → 靜默空 | 產生後驗 skills 數 == 模板的預期數 |
| 5 | ⛔ `skills_filter` 絕不可設 `none` | 它會發出 `--setting-sources ""`，**壓掉 dispatcher 與所有 repo hooks**（`inner/bundle_skills.py:110-113`）⇒ 本檔一律不寫 `skills:` 名單模式，固定 `all` |
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BUNDLE_DIR / "_templates"
SHARED_FRAGMENTS_DIR = TEMPLATES_DIR / "_shared"   # worker prompt 的共用片段（見該目錄 README）
HOOKS_DIR = BUNDLE_DIR / "hooks"
MESSAGING_SKILL_DIR = BUNDLE_DIR / "skills" / "omni-msg"
DEFAULT_SITE_PACKAGES = (
    "/home/elmo/.local/share/uv/tools/omnigent/lib/python3.12/site-packages"
)


def _omnigent_on_path() -> None:
    sp = os.environ.get("OMNIGENT_SITE_PACKAGES", DEFAULT_SITE_PACKAGES)
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)


# ── 工具名稱面（權威來自 omnigent.policies.builtins）──────────────────────────
# 同一支「shell」在不同 harness 有不同名字，但參數一律正規化成 `command`。
# 出處：omnigent/policies/builtins/_shell.py::SHELL_TOOLS
SHELL_TOOLS = ["Bash", "Shell", "bash", "developer__shell", "shell", "sys_os_shell", "terminal"]
# 出處：omnigent/policies/builtins/orchestration.py::read_only_os / worktree_guard
# 參數：omnigent／pi 用 `path`，claude-native 用 `file_path`。
WRITE_TOOLS = ["sys_os_write", "sys_os_edit", "Write", "Edit", "MultiEdit", "write", "edit"]

# ── 快層 model ───────────────────────────────────────────────────────────────
# scout／tracker／researcher 的 model 由 ROSTER 釘在快層（gpt-5.6-luna／terra）。
# 舊的 P-model CEL 已於 2026-09-14 退役（見 README §已廢止的設計 第 5 條）：它的 agent  # legacy-ok
# regex 只認 `<role>_via_(cc|pi)` 這種靜態 sub-agent 時代的名字，對 `_safe_name()` 產的
# `explore_pi`／`tracker_pi`／`researcher_pi` 零命中，實際從未擋過任何事件；而且它的
# 快層名單全是 claude 系，與 ROSTER 給這三個角色的 pi 預設互斥。⛔ 不要重新引入——要限制
# model 就改 ROSTER 那一格。

# ── woow-k3s namespace 白名單（P-kubectl）────────────────────────────────────
# 🔴 這是白名單的**第五份副本**。權威三份由 repo-lint `kubectl-ns-whitelist-sync` 對帳：
#      1. CLAUDE.md「⛔ 鐵律」表格
#      2. docs/guides/deployment/shared-cluster-paas-scope-guard.md
#      3. .claude/hooks/kubectl-ns-guard.py::ALLOWED_NAMESPACES
#    第四份是 .omnigent/woow-issue-leader/_templates/ops_k8s/AGENTS.md 的表格。
#    ⚠ 本檔與 AGENTS.md 那兩份**不在** repo-lint 對帳範圍——改白名單時五份都要一起改。
K8S_ALLOWED_NAMESPACES = [
    "paas-ws-*",
    "paas-ws-stg-*",
    "paas-system",
    "paas-stg-system",
    "paas-cloudflare-system",
    "paas-stg-cloudflare-system",
    "dev-sandbox-*",
    "fleet-local",
]
# 唯讀動詞 + 不碰叢集的動詞（照抄 .claude/hooks/kubectl-ns-guard.py 的 READ_VERBS /
# NO_CLUSTER_VERBS）。動詞不在這兩組 ⇒ 一律當寫入類（fail-closed）。
K8S_SAFE_VERBS = [
    "get", "describe", "logs", "top", "wait", "events", "explain", "diff",
    "api-resources", "api-versions", "cluster-info", "auth",
    "config", "version", "options", "help", "completion",
]


def _cel_list(items: list[str]) -> str:
    """把 Python list 轉成 CEL list literal。"""
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def _ns_alternation() -> str:
    """白名單 glob 轉成 regex alternation（`*` → 一段非空的 name 字元）。"""
    parts = []
    for ns in K8S_ALLOWED_NAMESPACES:
        parts.append(
            ns[:-1].replace(".", r"\\.") + "[A-Za-z0-9._-]+" if ns.endswith("*")
            else ns.replace(".", r"\\.")
        )
    return "|".join(parts)


# ── CEL 運算式 ───────────────────────────────────────────────────────────────
#
# ⛔ 每一次欄位存取都必須用 `has()` 包住。CEL 參照不存在的欄位時回 None，那是
#    **abstain → fail-OPEN**（cel.py：eval 例外 → return None → 視同 ALLOW），守線會
#    靜默不生效。整條式子一律寫成 `<條件> ? {"result":"DENY", ...} : {"result":"ALLOW"}`。
#
# ⛔ 也不要改寫成自訂 Python policy 模組：那需要裝進 omnigent 的 venv，而且
#    `function.path` 打錯字會讓 worker 連 Read 都被 DENY。CEL 的語法錯在 compile 階段
#    就 ValueError，錯得響亮。

# 判斷方向：**「無法證明安全就 DENY」**，不是「看起來安全就放行」。
#
# 🔴 這是 FIX ROUND 1 的核心修正。原本的寫法是「整條 command 看起來安全 → 放行」，
#    而 shell 沒辦法用單一 regex 可靠解析，覆核者實跑證明可以繞過（每一條都已納入
#    CEL_VECTORS 的 deny 向量做回歸）：
#      kubectl ... get pods -A && kubectl ... delete pod x -n gitea   → 舊版 ALLOW
#      kubectl ... delete pod get -n gitea                            → 舊版 ALLOW（resource 叫 get）
#      kubectl ... delete pod x -n paas-system -n gitea               → 舊版 ALLOW（重複 -n）
#      kubectl delete pod x -n gitea                                  → 舊版 ALLOW（缺 --context）
#      python3 - <<'PY' / open('.woow-issues/board.md','w') / PY       → 舊版 ALLOW
#      gh api repos/o/r/issues/1/comments -f body=/approve             → 舊版 ALLOW
#      tea comments create 823 '/approve'                             → 舊版 ALLOW
#      gh api -X PUT repos/o/r/pulls/1/merge                          → 舊版 ALLOW
#    反轉之後**失敗方向**變成 fail-closed，大幅降低常見的漏擋：誤擋會 fail loud（worker
#    會回報被擋、人看得到），漏擋才是靜默的。
#    ⚠ 但這仍是 **best-effort**——regex 對 shell 字串永遠不可能 sound，漏擋仍可能靜默
#    發生。改善的是失敗方向，不是把 regex 變成可靠的解析器（見 README §守線的強度分級）。
#
# ⚠ 跳脫層數：Python raw string → YAML block scalar（不處理跳脫）→ CEL 字串（`\\` → `\`）
#    → regex。所以 regex 的每一個反斜線在這裡都寫成 `\\`。

# shell 串接／替換：出現任何一個就代表「這條 command 不只做一件事」，無法用 regex 證明安全。
SHELL_CHAIN_RE = r"(&&|\\|\\||;|\\||`|\\$\\(|\\n)"

# ── P-escape（組 3-1，便宜加固）────────────────────────────────────────────
# shell 允許在指令名中間插反斜線轉義（`kube\ctl`、`gh pr me\rge`）來規避字串比對。
# 這條**不追完備性**——它只便宜地攔掉「同一條指令裡同時出現反斜線轉義序列與敏感字根」
# 這個組合，就能擋掉覆核者列的大多數轉義向量。
# ⚠ 它攔不住 `''` 切斷、`$'\x..'` hex 轉義、base64 解碼後由 stdin 送 body 等其他形式
#    （見 README §已知繞過向量，以及 CEL_VECTORS 的 known_gap 區）。
#    威脅模型是**防 agent 犯錯，不是防 agent 攻擊**：LLM 不會為了規避一道它不知道存在
#    的守線，去把指令名寫成轉義形式。繼續追 regex 完備性是贏不了的仗。
ESCAPE_SEQ_RE = r"\\\\[a-zA-Z0-9]"
# 敏感字根本身也可能被轉義切斷（`me\rge`、`pu\sh`），所以字元之間要容許一個可選的反斜線。
# `\\\\?` 經 YAML → CEL 兩層還原後是 regex 的「可選的字面反斜線」。
_SENSITIVE_ROOTS = ["kube", "merge", "approve", "tag", "push", "board", "woow-issues"]


def _escape_tolerant(word: str) -> str:
    """把 word 轉成「允許字元之間插入反斜線轉義」的 regex 片段。"""
    return r"\\\\?".join(word)


SENSITIVE_ROOT_RE = "(" + "|".join(_escape_tolerant(w) for w in _SENSITIVE_ROOTS) + ")"

CEL_ESCAPE = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("{ESCAPE_SEQ_RE}")
&& event.data.arguments.command.matches("{SENSITIVE_ROOT_RE}")
? {{"result": "DENY", "reason": "指令同時含反斜線轉義序列與敏感字根（kube／merge／approve／tag／push／board）——這種寫法無法用字串比對判斷真正要執行什麼。請改用不含轉義的直白寫法。"}} : {{"result": "ALLOW"}}"""


# ── P-board ──────────────────────────────────────────────────────────────────
# 「提到 .woow-issues/ 且無法證明是唯讀」就 DENY。⛔ 不再枚舉寫入動詞——枚舉永遠列不完
#    （heredoc 餵給 python3／perl／node 就繞過了，覆核者實跑證明過）。
BOARD_DIR_RE = r"\\.woow-issues/"
# 唯讀白名單：必須**從頭**就是這些動詞，而且整條不得有串接（串接之後做什麼無法證明）。
BOARD_READ_ONLY_RE = (
    r"^\\s*(cat|less|head|tail|grep|rg|ls|wc|diff|git\\s+diff|git\\s+show)\\b"
)

CEL_BOARD = f"""event.type == "tool_call" && (
  (event.data.name in {_cel_list(WRITE_TOOLS)} && (
    (has(event.data.arguments.path) && event.data.arguments.path.matches("{BOARD_DIR_RE}"))
    || (has(event.data.arguments.file_path) && event.data.arguments.file_path.matches("{BOARD_DIR_RE}"))
  ))
  || (event.data.name in {_cel_list(SHELL_TOOLS)}
      && has(event.data.arguments.command)
      && event.data.arguments.command.matches("{BOARD_DIR_RE}")
      && !(event.data.arguments.command.matches("{BOARD_READ_ONLY_RE}")
           && !event.data.arguments.command.matches("{SHELL_CHAIN_RE}")))
) ? {{"result": "DENY", "reason": "看板 .woow-issues/ 只有 woow-issue-leader 能寫。唯讀請用單一不串接的 cat / less / head / tail / grep / rg / ls / wc / diff / git diff / git show；要更新內容就寫進回報，由上層代寫。"}} : {{"result": "ALLOW"}}"""

# ── P-merge ──────────────────────────────────────────────────────────────────
# 只擋四種真正的 merge 路徑：`<cli> pr|pulls|mr merge`（tea / gh / glab；含 `sh -c`、串接、
# 多空白變形）、`gh|glab api … /merge`、`curl … /merge`（REST 直打 merge endpoint），以及
# `gh|glab|curl … graphql … [Mm]erge`。GraphQL mutation 不走 `/merge` 路徑，前三條吃不到；
# 第四條鎖在 `graphql` 字面值語境，才不會誤擋 PR 標題。
# ⚠ 已知取捨：`gh api graphql` 查詢 `mergeable` 這類唯讀查詢也會被誤擋；守線寧可錯殺，
#    且這不阻斷核心工作流，因此刻意接受。
# ⛔ 不用「指令裡有 tea/gh/glab ＋ 任何位置出現 merge 字」的寬鬆版——那會誤擋 PR 標題前綴
#    `⛔ 勿代 merge｜…`（鐵律 4）、issue／留言描述含 merge 字、`tea … && git merge-tree`
#    （2026-09-11 #979 實測：管線因此開不了 PR；詳見 #1096）。
# 本機 `git merge` 不受影響（沒有 tea/gh/glab token）——那是合法的分支整併。
CEL_MERGE = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("(\\\\b(tea|gh|glab)\\\\s+(pulls?|pr|mr)\\\\s+merge\\\\b|\\\\b(gh|glab)\\\\s+api\\\\b[\\\\s\\\\S]*/merge\\\\b|\\\\bcurl\\\\b[\\\\s\\\\S]*/merge\\\\b|\\\\b(gh|glab|curl)\\\\b[\\\\s\\\\S]*\\\\bgraphql\\\\b[\\\\s\\\\S]*[Mm]erge)")
? {{"result": "DENY", "reason": "merge 是人工 gate，agent 不代 merge（含 gh api / tea pulls merge 等變形）。開好 PR 等人來 merge。"}} : {{"result": "ALLOW"}}"""

# ── P-approve ────────────────────────────────────────────────────────────────
# 只要 command 裡出現 `/approve` 就 DENY。⛔ 拿掉「且匹配 tea/gh/glab/curl」那個連言——
#    worker 沒有任何合法理由打出這個字串，而枚舉工具名永遠漏（gh api -f body=/approve、
#    tea comments create 都是覆核者實跑證明的繞過）。
CEL_APPROVE = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("/approve")
? {{"result": "DENY", "reason": "prod build 的 /approve 核可只能由授權者本人送出——即使人類當場明確授權、即使手上憑證就是授權者本人，agent 也不代簽。"}} : {{"result": "ALLOW"}}"""

# ── P-tag ────────────────────────────────────────────────────────────────────
# ⚠ 內建 github_policy 對這個 case 無效：實測 `git push origin v1.2.3` 只回 ASK（URL regex
#    硬編 github.com，Gitea 判不出 repo），且完全不認 `tea`。所以自己比對。
# 保守化四條，任一成立即 DENY：
#   1. git push ＋ tag 形態（v<版號> / --tags / --follow-tags / refs/tags/）
#   2. 指令裡出現 refs/tags/（任何工具寫 tag ref）
#   3. forge release 子命令（CLI 後緊接正式名稱／alias），或 REST 對 `/releases` collection
#      endpoint 的建立動作（明示 POST，或未明示 method 但帶 request body）
#   4. `git tag <非 flag>`＝建立 tag（`git tag -l` / `--list` / `-n` 是讀，放行）
# 🔴 FIX ROUND 3：`[\\s:]` 後面要容許一個引號——`git push origin "v1.7.6"` 舊版滑過。
# ⚠ 刻意誤擋（review F12，2026-09-14 裁決不放寬）：branch 名以 `<字>-v<數字>` 開頭
#    （`git push origin 1032-v18-upgrade`）、`HEAD:v2-x`、以及唯讀的 `git fetch origin refs/tags/v1`
#    都會 DENY。放寬要改的是 regex 的邊界（`v[0-9]` 後面接什麼、`refs/tags/` 限定 push 語境），
#    每一種放寬都會多一個真 tag 滑過的形狀；worker 照 `<type>/<issue-id>-…` 命名 branch 就不會
#    撞到，讀 tag 用 `git fetch --tags`／`git ls-remote --tags`／`git tag -l`。deny 訊息與 README
#    「已知的刻意誤擋」表都有替代寫法；CEL_VECTORS 把這三條列為 deny 向量，讓取捨被測到。
TAG_SHAPE_RE = r"(--tags|--follow-tags|refs/tags/|[\\s:][\"\']?([A-Za-z0-9._-]+-)?v[0-9])"
# 第 3 條的 CLI 名稱／alias 依據：tea 0.14.1 頂層 help 列出 releases／release／r；
# gh 2.45.0 頂層 help 只列 release；本機未安裝 glab，官方 gitlab-org/cli
# f42a0784 的 internal/commands/release/release.go 使用 `release` 且未設定 Cobra Aliases。
# ⛔ 依 #1110 裁決只認「CLI 後緊接子命令」；中間夾全域 flag 的形狀刻意列 known_gap，
#    不在這裡自行放寬。
FORGE_RELEASE_SUBCOMMAND_RE = r"\\b(tea\\s+(releases|release|r)|gh\\s+release|glab\\s+release)\\b"
FORGE_API_SUBCOMMAND_RE = r"\\b(tea|gh|glab)\\s+api\\b"
RELEASES_ENDPOINT_RE = r"/releases(\\?[^\\s\"\']*)?[\"\']?(\\s|$)"
# 三支 forge API 都以 -X／--method 指定 method；curl 另有 --request。`\\s*` 刻意涵蓋
# `-XPOST` 無空白形態。POST 大小寫只涵蓋 CLI help 與常見人工輸入的兩種形態。
REST_POST_METHOD_RE = r"(^|\\s)(-X(=|\\s*)(POST|post)|--(method|request)(=|\\s+)(POST|post))(\\s|$)"
REST_EXPLICIT_METHOD_RE = r"(^|\\s)(-X(=|\\s*)[A-Za-z]+|--(method|request)(=|\\s+)[A-Za-z]+)(\\s|$)"
# tea／gh／glab 的 API help 都說：未明示 method 時，body flag 會把預設 GET 改成 POST。
REST_API_BODY_RE = r"(^|\\s)((-f|-F|-d)([^\\s-]|\\s+)|--(field|Field|raw-field|form|input|data)(=|\\s+))"
# curl 的 -d／-F／--data*／--form／--json 也會隱含 POST；-G／--get 則維持讀取。
CURL_BODY_RE = r"(^|\\s)((-d|-F)([^\\s-]|\\s+)|--(data|data-ascii|data-binary|data-raw|data-urlencode|form|json)(=|\\s+))"
CURL_GET_RE = r"(^|\\s)(-G|--get)(\\s|$)"

CEL_TAG = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& (
     (event.data.arguments.command.matches("\\\\bgit\\\\b[\\\\s\\\\S]*\\\\bpush\\\\b")
      && event.data.arguments.command.matches("{TAG_SHAPE_RE}"))
  || event.data.arguments.command.matches("refs/tags/")
  || event.data.arguments.command.matches("{FORGE_RELEASE_SUBCOMMAND_RE}")
  || ((event.data.arguments.command.matches("{FORGE_API_SUBCOMMAND_RE}")
       || event.data.arguments.command.matches("\\\\bcurl\\\\b"))
      && event.data.arguments.command.matches("{RELEASES_ENDPOINT_RE}")
      && (event.data.arguments.command.matches("{REST_POST_METHOD_RE}")
          || (event.data.arguments.command.matches("{FORGE_API_SUBCOMMAND_RE}")
              && event.data.arguments.command.matches("{REST_API_BODY_RE}")
              && !event.data.arguments.command.matches("{REST_EXPLICIT_METHOD_RE}"))
          || (event.data.arguments.command.matches("\\\\bcurl\\\\b")
              && event.data.arguments.command.matches("{CURL_BODY_RE}")
              && !event.data.arguments.command.matches("{REST_EXPLICIT_METHOD_RE}")
              && !event.data.arguments.command.matches("{CURL_GET_RE}"))))
  || event.data.arguments.command.matches("\\\\bgh\\\\b[\\\\s\\\\S]*\\\\bapi\\\\b[\\\\s\\\\S]*git/refs")
  || event.data.arguments.command.matches("-X\\\\s+(POST|PATCH|PUT)[\\\\s\\\\S]*git/refs")
  || event.data.arguments.command.matches("\\\\bgit\\\\s+tag\\\\s+[^-\\\\s]")
)
? {{"result": "DENY", "reason": "發版 tag 由人工推，agent 不推 tag、不建 release（v<版號> / --tags / refs/tags/ / git tag <name> / forge release 子命令 / POST /releases 建立端點皆擋）。⚠ 刻意誤擋：branch 名以 <字>-v<數字> 開頭（如 1032-v18-upgrade）或 HEAD:v… 也會命中——branch 名改用 <type>/<issue-id>-… 形式；只是要讀 tag 用 git fetch --tags / git ls-remote --tags / git tag -l。"}} : {{"result": "ALLOW"}}"""

# ── P-kubectl（僅 ops_k8s）──────────────────────────────────────────────────
# ⚠ 這一層是 **defense-in-depth、best-effort**，**不是安全邊界**。權威守線是 repo 的
#    PreToolUse hook `.claude/hooks/kubectl-ns-guard.py`（用 shlex 正確解析，且 repo-lint
#    有對帳）。本層已知可被 shell escape 繞過（見 README §守線的強度分級），只擋「agent
#    寫錯」，擋不住刻意規避。
#
# 🔴 FIX ROUND 2：整條規則**只在 woow-k3s 相關時才啟動**。
#    舊版不分 context，把 `--context docker-desktop delete pod x -n gitea` 也擋掉了——
#    那是本機測試／開發用叢集（CLAUDE.md），擋它會毀掉 /woow-dev:k8s-dev-sandbox 的
#    create／destroy／test 工作流。鐵律限定的是 woow-k3s 這個共用叢集。
K8S_WRITE_VERBS = [
    "delete", "apply", "patch", "edit", "scale", "exec", "cp", "drain", "cordon",
    "annotate", "label", "replace", "rollout", "set",
    "create", "run", "expose", "autoscale", "taint", "uncordon", "attach",
    "port-forward", "proxy", "debug",
]
# 會從檔案讀 manifest 的動詞——manifest 內的 metadata.namespace 會**覆蓋**命令列的 -n，
# 而那份檔案的內容 regex 看不到（FIX ROUND 2 / 1-2 的寫實失誤案例）。
K8S_FILE_VERBS = ["apply", "create", "replace", "patch"]
_K8S_WRITE = "|".join(K8S_WRITE_VERBS)
_K8S_FILE = "|".join(K8S_FILE_VERBS)

# 🔴 2026-09-14（review F11）：pipe 不再一律當串接擋。
#    `kubectl get … | jq`／`| grep`／`| tail` 是 kubectl 最常見的寫法，舊版把 `|` 也算進
#    SHELL_CHAIN_RE，唯讀查詢全被擋、prompt 只能教「拆兩次 tool call」（而 jq 那一半根本沒有
#    輸出可接）。放行條件（三個同時成立；仍是 regex、仍是 best-effort）：
#      1. 第一段（第一個 `|` 之前）以 `kubectl` 開頭且含唯讀動詞（K8S_SAFE_VERBS）；
#      2. 第一段不含任何寫入動詞（`exec … | grep` 不放——exec 是寫入類）；
#      3. pipe 之後**不再出現 `kubectl`**（`| xargs kubectl delete`、`| kubectl apply -f -` 擋）。
#    其餘串接（`&&`、`||`、`;`、反引號、`$(`、換行）維持一律 DENY。
#    ⚠ 寫入動詞／`-n` 重複／`-f` 的比對因此都只看**第一段**（`^[^|]*…`）：pipe 之後的段落
#      已由第 3 條保證沒有 kubectl，那裡的 `delete`／`-n` 是 grep／jq 的參數，不該算。
#      對沒有 pipe 的指令，`^[^|]*` 就是整條，行為與舊版相同。
#    ⚠ regex 後端是 RE2（cel-python）：**沒有 lookahead**，而且 regex 編譯失敗會讓整條運算式
#      回 None＝abstain＝ALLOW（靜默 fail-open）——所以段落邊界只用 `[^|]` 這類字元類，
#      ⛔ 不要寫 `(?!…)`。
SHELL_CHAIN_NOPIPE_RE = r"(&&|\\|\\||;|`|\\$\\(|\\n)"
# 單一 pipe（`||` 不算——那是上面那組的）。
PIPE_RE = r"(^|[^|])\\|([^|]|$)"
_K8S_SAFE = "|".join(K8S_SAFE_VERBS)
# 第一段＝`kubectl` 開頭、含唯讀動詞、直到第一個 `|`。
K8S_PIPE_HEAD_SAFE_RE = rf"^\\s*kubectl\\b[^|]*\\b({_K8S_SAFE})\\b[^|]*\\|"
# pipe 之後任一段又出現 kubectl。
K8S_PIPE_TAIL_KUBECTL_RE = r"\\|[\\s\\S]*\\bkubectl\\b"
# 整條出現兩個以上 kubectl（docker-desktop 例外的收緊條件，見 CEL 內註解）。
K8S_TWO_KUBECTL_RE = r"\\bkubectl\\b[\\s\\S]*\\bkubectl\\b"
# 寫入類／從檔案讀 manifest 的動詞：只看第一段。
K8S_WRITE_HEAD_RE = rf"^[^|]*\\b({_K8S_WRITE})\\b"
K8S_FILE_HEAD_RE = rf"^[^|]*\\b({_K8S_FILE})\\b"
# -n 出現兩次：只看第一段（`| grep -n foo` 的 -n 不算）。
K8S_NS_TWICE_HEAD_RE = r"^[^|]*(^|\\s)(-n|--namespace)[=\\s][^|]*(^|\\s)(-n|--namespace)[=\\s]"

# ⚠ context 比對一律經過這兩個 regex，不要在別處另寫一份。
# 🔴 FIX ROUND 3：舊版寫 `--context[=\s]+woow-k3s`，認不到引號形式——
#    `kubectl --context "woow-k3s" delete pod x -n gitea` 會整個滑過（實測 ALLOW）。
#    而且「沒帶 --context」那條又因為 `\S` 看到了 `"` 而認為有帶，兩條同時失效。
#
# 判斷方向也一併改成 allowlist：**只有明確認得出來的本機 context 才脫離共用叢集規則**，
# 其餘（`woow-k3s`、`woow-k3s-prod`、拼錯的、認不出來的）一律走嚴格分支。這比「比對
# woow-k3s 子字串」更 sound——認不出來時 fail-closed，而不是放行。
_CTX = r"--context(=|\\s+)[\"']?"
# 明確認得的本機開發叢集（CLAUDE.md：docker-desktop 是本機測試／開發用）。
K8S_LOCAL_CONTEXT_RE = _CTX + r"docker-desktop[\"']?(\\s|$)"
# 有帶 context 且值看起來是個真的 context 名（不是空字串或孤零零一個引號）。
K8S_CONTEXT_PRESENT_RE = _CTX + r"[A-Za-z0-9]"

CEL_KUBECTL = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("\\\\bkubectl\\\\b")
// 明確指向本機 docker-desktop、且整條只有這一個 kubectl 才放行——共用叢集鐵律不管本機測試
// 叢集；但第二個 kubectl（不論帶什麼 context）會讓整條回到嚴格規則（review F11）
&& !(event.data.arguments.command.matches("{K8S_LOCAL_CONTEXT_RE}")
     && !event.data.arguments.command.matches("{K8S_TWO_KUBECTL_RE}"))
&& (
     // 寫入類但沒帶（可辨識的）--context：預設 context 可能就是 woow-k3s
     (event.data.arguments.command.matches("{K8S_WRITE_HEAD_RE}")
      && !event.data.arguments.command.matches("{K8S_CONTEXT_PRESENT_RE}"))
     // 串接／替換（pipe 以外）：無法證明「每一段」都在白名單內
  || event.data.arguments.command.matches("{SHELL_CHAIN_NOPIPE_RE}")
     // pipe：只放行「第一段是唯讀 kubectl、且 pipe 之後沒有 kubectl」
  || (event.data.arguments.command.matches("{PIPE_RE}")
      && !(event.data.arguments.command.matches("{K8S_PIPE_HEAD_SAFE_RE}")
           && !event.data.arguments.command.matches("{K8S_WRITE_HEAD_RE}")
           && !event.data.arguments.command.matches("{K8S_PIPE_TAIL_KUBECTL_RE}")))
     // -n 出現超過一次（第一段內）：無法確定哪一個生效
  || event.data.arguments.command.matches("{K8S_NS_TWICE_HEAD_RE}")
     // 寫入類但 namespace 不在白名單（含完全沒帶 -n）
  || (event.data.arguments.command.matches("{K8S_WRITE_HEAD_RE}")
      && !event.data.arguments.command.matches("(^|\\\\s)(-n|--namespace)[=\\\\s]+[\\"']?({_ns_alternation()})[\\"']?(\\\\s|$)"))
     // 從檔案套 manifest：檔案內的 metadata.namespace 會蓋掉命令列的 -n
  || (event.data.arguments.command.matches("{K8S_FILE_HEAD_RE}")
      && event.data.arguments.command.matches("(^|\\\\s)(-f|--filename|-k|--kustomize)[=\\\\s]")
      && !event.data.arguments.command.matches("--dry-run"))
)
? {{"result": "DENY", "reason": "woow-k3s 是共用叢集，且 shell／manifest 內容無法用 regex 證明安全：寫入類 kubectl 必須明確帶 --context、不串接（&&／;／$( 等一律擋；pipe 只在第一段是唯讀 kubectl 且後段沒有 kubectl 時放行）、帶恰好一個白名單內的 -n；從檔案套 manifest 需 --dry-run（檔案內的 metadata.namespace 會覆蓋 -n）。本機 docker-desktop 不受此規則限制，但整條只能有一個 kubectl。"}} : {{"result": "ALLOW"}}"""


# ── P-path（僅 implement）────────────────────────────────────────────────────
# 2026-09-14（review F6）。worktree_guard 的真規則是「絕對路徑或含 `..` 一律 DENY，相對且
# 不上跳的放行」；而 harness 行程的 cwd 是六 repo 的共同祖先 ⇒ pi 的檔案工具寫
# `WOOW/ecpay_odoo18/x.py`（相對、無 `..`、別的 repo）舊的兩條守線都放行，寫進 repo root 的
# 主 checkout（`paas/woow_paas_platform/src/x.py`）也放行。這一支補上「寫入 path 必須以本
# issue 的 worktree 為前綴」：
#   - 相對形式 `<worktree 相對 workspace 的路徑>/…`（正常寫法），或絕對形式 `<worktree>/…`
#     （worktree_guard 會擋絕對路徑，但本條不依賴它——兩條各自獨立成立）；
#   - 不得含 `..` 段（`paas/wt-1/../woow_paas_platform/x` 前綴對了也擋）。
# 比對的是工具的**結構化參數**（`path`／`file_path`），不看 shell 字串 ⇒ A 類，與 read_only_os
# 同級。⚠ 它管不到 shell 重導向（`echo x > WOOW/x.py`、`sed -i`）——那一側沒有機器守線，
# 記在 CEL_VECTORS 的 known_gap，prompt 寫成紀律規則。欄位缺席時不 DENY（與 P-board 同款）。
# 前綴由 generate() 在派工當下算出（worktree 相對 workspace），所以這支 CEL 是**每個 worker
# 不同**的，只存在於動態 config；驗收第 8 項用 SELF_CHECK_* 那組樣本路徑測它。
DOTDOT_SEG_RE = r"(^|/)\\.\\.(/|$)"


def _re_literal(s: str) -> str:
    """字面字串 → 經 YAML→CEL 兩層之後仍是字面比對的 regex 片段。"""
    return re.escape(s).replace("\\", "\\\\")


def _cel_path(worktree_rel: str, worktree: str) -> str:
    inside = "^(\\\\./)?" + _re_literal(worktree_rel) + "/|^" + _re_literal(worktree) + "/"

    def _bad(field: str) -> str:
        f = f"event.data.arguments.{field}"
        return (f"(has({f}) && (!{f}.matches(\"{inside}\")"
                f" || {f}.matches(\"{DOTDOT_SEG_RE}\")))")

    return f"""event.type == "tool_call"
&& event.data.name in {_cel_list(WRITE_TOOLS)}
&& ({_bad("path")}
    || {_bad("file_path")})
? {{"result": "DENY", "reason": "implement worker 只能寫進本 issue 的 worktree（{worktree}）：檔案工具的 path 必須以 {worktree_rel}/ 開頭（相對 workspace，或用 worktree 絕對路徑）且不含 ..；sibling repo 與 repo root 的主 checkout 都不能寫。"}} : {{"result": "ALLOW"}}"""


def _p_path(worktree_rel: str, worktree: str) -> str:
    return _cel_yaml("p_path_write_inside_worktree", _cel_path(worktree_rel, worktree))


# ── policy YAML 片段 ─────────────────────────────────────────────────────────

def _fn_policy(name: str, path: str, args: dict[str, object] | None = None,
               on: list[str] | None = None, note: str | None = None) -> str:
    lines = []
    if note:
        lines += [f"    # {ln}" for ln in note.strip().splitlines()]
    lines += [f"    {name}:", "      type: function"]
    if on:
        lines.append(f"      on: [{', '.join(on)}]")
    lines += ["      function:", f"        path: {path}"]
    if args is not None:
        lines.append("        arguments:")
        for k, v in args.items():
            if isinstance(v, bool):
                lines.append(f"          {k}: {'true' if v else 'false'}")
            elif isinstance(v, list):
                lines.append(f"          {k}: [{', '.join(str(i) for i in v)}]")
            else:
                lines.append(f"          {k}: {v}")
    return "\n".join(lines)


def _cel_yaml(name: str, expression: str) -> str:
    """CEL policy 片段。運算式用 YAML block scalar（`|-`）承載——block scalar 不做跳脫
    處理，regex 裡的反斜線原樣進到 CEL 字串，少一層心算。"""
    indented = "\n".join(f"            {ln}" if ln else "" for ln in expression.splitlines())
    return (
        f"    {name}:\n"
        "      type: function\n"
        "      on: [tool_call]\n"
        "      function:\n"
        "        path: omnigent.policies.builtins.cel.cel_policy\n"
        "        arguments:\n"
        "          expression: |-\n"
        f"{indented}"
    )


P_SAFETY = [
    _fn_policy("max_tool_calls_per_session",
               "omnigent.policies.builtins.safety.max_tool_calls_per_session",
               {"limit": 400}),
    _fn_policy("detect_loop", "omnigent.policies.builtins.safety.detect_loop",
               {"window": 10, "threshold": 3}),
    _fn_policy("detect_thrashing", "omnigent.policies.builtins.context.detect_thrashing",
               {"consecutive_threshold": 5, "window": 10, "action": "ASK"}),
]
# ⚠ pi 系刻意不掛 cost_budget：subscription 模型在 catalog 無定價時會回 _UNPRICED_ASK
#    （cost.py），每個 session 卡一次人工批准，會毀掉無人值守。claude 家族兩種 harness
#    都有**計價路徑**（⚠ 不等於「一定查得到價格」——實際有沒有價格取決於該 model 在不在
#    catalog，換 model 要重驗），差別在精度：claude-native 由 Claude Code 的 statusLine 直接回報
#    total_cost_usd（claude_native_status.py，實際帳單）；claude-sdk 回報 token 用量＋
#    model，由 server 端查 catalog 定價（orchestration.py 的 has_tokens 分支，估算值）。
P_COST = _fn_policy(
    "cost_budget", "omnigent.policies.builtins.cost.cost_budget",
    {"max_cost_usd": 350.0, "ask_thresholds_usd": [70.0, 210.0]},
    note="⛔ 只掛 claude 家族（claude-native／claude-sdk）。pi 系掛上去會被 _UNPRICED_ASK 卡住每個 session（見上方註解）。")

# ── 頂層 issue-leader 專用：按日計的花費上限 ─────────────────────────────────
# issue-leader 是**長時間運作的單一 session**，per-session 累計的 cost_budget 遲早撞到上限
# （#1094 抬到 350 只是延後）。改掛 user_daily_cost_budget：閘門邏輯與 cost_budget 相同，
# 但計的是 session owner **當天（UTC）跨所有 session** 的累計，每天歸零 ⇒ 撞到上限隔天
# （UTC 00:00＝台灣 08:00）下一輪自動放行，不必重開 session。
# ⚠ 額度是 owner 當日**所有** session 的總和（含派出去的 worker、含同一人在 UI 開的其他
#   session），不是 leader 自己的。先寬鬆（700），看實際運作再收。
# ⚠ 刻意不設 ask_thresholds_usd：ASK 是 fail-closed（ASK_TIMEOUT 900s），無人值守時等於硬停。
# ⚠ 前提：session 要有 owner grant。UI 建的頂層 session 有（2026-09-11 實測
#   GET /v1/sessions/<id>/owner → {"owner": "eugene"}）；single-user mode 沒有 ⇒ policy
#   永遠不觸發＝無煞車（builder.py::_load_user_daily_cost 回零）。換 server 部署要重驗。
# engine 每個 turn／tool call 重建並重讀 rollup（routes_hooks.py::_build_engine），跨日不需重啟。
P_COST_DAILY = _fn_policy(
    "user_daily_cost_budget", "omnigent.policies.builtins.cost.user_daily_cost_budget",
    {"max_cost_usd": 700.0},
    note="頂層 issue-leader 專用：owner 當天（UTC）跨 session 累計，每天歸零；不設 ask 門檻（ASK fail-closed）。\n"
         "前提是 session 有 owner grant，single-user mode 下永遠不觸發。")

# 頂層 issue-leader 不掛 max_tool_calls_per_session：它是整個 session 累計、超過就 DENY，
# 對長時間運作的單一 session 是錯的形狀（會比花費更早撞到）。detect_loop／detect_thrashing
# 是滑動視窗，保留。
P_SAFETY_LEADER = [p for p in P_SAFETY
                   if not p.lstrip().startswith("max_tool_calls_per_session:")]
assert len(P_SAFETY_LEADER) == len(P_SAFETY) - 1, "P_SAFETY 形狀變了，重新確認要拿掉哪一條"

P_MERGE = _cel_yaml("p_merge_no_agent_merge", CEL_MERGE)
P_APPROVE = _cel_yaml("p_approve_no_agent_signoff", CEL_APPROVE)
P_TAG = _cel_yaml("p_tag_no_agent_tag_push", CEL_TAG)
P_ESCAPE = _cel_yaml("p_escape_obfuscated_command", CEL_ESCAPE)
P_BOARD = _cel_yaml("p_board_read_only", CEL_BOARD)
P_KUBECTL = _cel_yaml("p_kubectl_namespace_scope", CEL_KUBECTL)

P_LEADER_ORCH = [
    _fn_policy("spawn_bounds", "omnigent.policies.builtins.orchestration.spawn_bounds",
               {"max_dispatches_per_turn": 6,
                "dispatch_tools": ["sys_session_send", "sys_session_create"]}),
    _fn_policy("headless_subagent_purpose_guard",
               "omnigent.policies.builtins.orchestration.headless_subagent_purpose_guard",
               {"allowed_purposes": ["implement", "review", "explore", "search",
                                     "deploy", "triage", "researcher"]}),
]
P_BLAST = _fn_policy("blast_radius", "omnigent.policies.builtins.orchestration.blast_radius",
                     {"gate_pushes": True}, on=["tool_call"])
P_READONLY = _fn_policy("read_only_os", "omnigent.policies.builtins.orchestration.read_only_os",
                        None, on=["tool_call"])
# ⚠ allowed_root 只出現在 deny 訊息裡，**不是**判斷依據——實際規則是「絕對路徑或含
#   `..` 的寫入路徑一律 DENY，相對且不上跳的放行」。所以它只能靠「cwd 從頭到尾不動」
#   來成立，必須與下面的 block_working_dir_changes 成對出現。
P_WORKTREE = _fn_policy(
    "worktree_guard",
    "omnigent.policies.builtins.orchestration.worktree_guard",
    {"allowed_root": "'<工單指定的 worktree>'"}, on=["tool_call"],
    note="allowed_root 只用在 deny 訊息；真正的規則是「絕對路徑或含 .. 的寫入路徑一律\n"
         "DENY」。它要成立，前提是 cwd 不會被搬走 → 見下面的 block_working_dir_changes。")
# ⚠ worktree 由 implement worker **自己建**（2026-09-14 裁決）：repo_leader 只指定路徑，
#   worker 開工第一件事是 `git -C <repo_root> worktree add <worktree> -b <branch> origin/<base>`。
#   所以 allowed_dirs 要含 **repo_root 與 worktree 兩處**（`git -C <repo_root>` 才進得去），
#   且 block_worktree 必須關掉（否則 worktree add 本身就被 DENY）。其餘（`..`、別的 repo、
#   共同祖先）的 cd / git -C 仍一律 DENY。
#   worktree_guard 擋不住「先 cd 出去、再寫相對路徑」；這條擋 cd 到 repo_root／worktree 以外。
#   ⚠ 但它擋不住「cd 進 repo_root 主 checkout 後寫相對路徑」——那是紀律規則（prompt 明寫進
#   repo_root 只為建 worktree、改檔只能在 worktree 裡），不是機器擋的。
#   runner 的 cwd 是共同祖先（OMNIGENT_RUNNER_WORKSPACE），不是 worktree ⇒ 沒有 allowed_dirs
#   它連自己的 worktree 都進不去。代價是 implementer 不能跨 repo——跨 repo 是 leader 的事。
def _p_workdir(worktree: str, repo_root: str) -> str:
    """allowed_dirs 含 worktree 與 repo_root，並放行 git worktree 子命令（worker 要自己 add）。
    implement 沒有這兩個路徑就不會走到這裡——generate() 會先拒絕。"""
    args: dict[str, object] = {"block_cd": True, "block_worktree": False, "action": "deny",
                               "allowed_dirs": [worktree, repo_root]}
    return _fn_policy(
        "block_working_dir_changes",
        "omnigent.policies.builtins.working_dir.block_working_dir_changes",
        args,
        note="allowed_dirs 含本 issue 的 worktree 與其 repo root ⇒ cd / git -C 只能進這兩處或其子目錄，\n"
             "其餘一律 DENY。block_worktree 關掉是刻意的：worktree 由 worker 自己 `git -C <repo root>\n"
             "worktree add`，之後只能在 worktree 裡改（進 repo root 只為建 worktree，這是紀律規則）。\n"
             "這是 worktree_guard 的另一半：擋掉「先 cd 出去、再寫相對路徑」的逃逸。\n"
             "副作用（刻意）：implementer 不能跨 repo，跨 repo 唯讀是 leader 的職責。")

# ── os_env ───────────────────────────────────────────────────────────────────
# 🔴 `cwd` 一律寫 `.`。⛔ 不要寫絕對路徑（本樹曾寫 `/home/elmo/woow-repos`，已移除）。
#
# 兩件事要分清楚，這是下游維護者最容易誤解的一點：
#
# 1. **正常 runner 路徑下，spec 裡的 `cwd` 根本不生效。** runner 一律用
#    `OMNIGENT_RUNNER_WORKSPACE` 覆寫它——見
#    `runner/tool_dispatch.py::_resolve_os_env_spec`（「Runner workspace is authoritative
#    — overrides whatever the spec declared (relative or absolute)」，約 6247-6259 行）。
#    覆寫發生在 `OSEnvironment` 建立**之前**，所以 harness 的實際 cwd **與**
#    `sys_os_*` 的 containment 錨點**兩者都**以 workspace 為準，不是以 spec 為準。
#    ⇒ 實際錨點 = 啟動 `omnigent run` 時的 shell cwd（本 bundle 用法是六 repo 的共同祖先
#       `/home/elmo/woow-repos`，**不是** worktree——implementer 靠 _p_workdir 填的
#       allowed_dirs 自己 `cd` 進去，見 #1092）。
#    ⇒ 想讓跨 repo 唯讀落在邊界內，**不能**靠在這裡寫父目錄，要靠啟動時站在哪裡。
#
# 2. **沒有 workspace 的 runner 上，絕對 `cwd` 會生效——而且那是一個已登錄的漏洞面。**
#    `server/bundles.py::_cwd_escapes_workspace`（約 58-70 行）：絕對或含 `..` 的 cwd
#    「on a runner without OMNIGENT_RUNNER_WORKSPACE it becomes the agent environment root
#    and copytree source, exposing the host filesystem」（GHSA-p8rw-8qj3-hf33）。
#    所以 deployed（multi-user）server 以 `enforce_handler_allowlist=True` 直接**拒收**
#    整個 bundle（`server/bundles.py` 約 140-152 行）。
#
# ⇒ 結論：寫絕對路徑在「有 workspace」時無效、在「沒有 workspace」時危險、上 deployed
#    server 時被拒。三種情況沒有一種是好的 ⇒ 一律 `.`。驗收第 13 項機器強制這件事。
OS_ENV_BLOCK = """
os_env:
  # ⚠ cwd 寫 `.` 是刻意的：runner 一律用 OMNIGENT_RUNNER_WORKSPACE 覆寫它（見
  #   build-bundles.py 的 OS_ENV_BLOCK 註解），所以這裡寫什麼都不會生效；而寫絕對路徑
  #   會讓 deployed server 直接拒收整個 bundle（GHSA-p8rw-8qj3-hf33）。
  #   ⛔ 不要改成絕對路徑——實際的 containment 錨點是啟動 omnigent run 時的 shell cwd。
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

#: ASK 的逾時（秒）。⚠ 刻意**不用**預設的 86400——ASK 是 fail-closed，無人值守時
#: 一個 ASK 會把 worker 卡住一整天，整條派工鏈跟著停。900 秒讓它快速失敗、由上層重派，
#: 人還在線上時也來得及回應。改這裡要一起想「無人值守會發生什麼」。
ASK_TIMEOUT = 900

# ── 對照表：產生器的唯一真相來源 ──────────────────────────────────────────────
#
# 硬規則（全部實測）：
#   - claude 家族 harness **只吃 id 含 `claude`** 的 model ⇒ `opus` / `sonnet` / `fable`
#     這些 CLI alias 一律被 `model_family_mismatch` 擋下（防線 1 會在寫檔前攔到）。
#   - `ultra` 只有 pi 原生支援，claude 上**靜默降級成 `xhigh`** ⇒ 只用 `high` / `xhigh`。
#   - `implement` **固定 pi**：上游 `worktree_guard` 對 Claude 原生 Edit／Write 必帶的絕對
#     `file_path` 一律 DENY，implement_cc 實測不能改檔（#1096）。
#   - `ops_k8s` **固定 claude-native**：pi 沒有 hook 機制（`pi --help | grep -ci hook` = 0），
#     換成 pi 會讓 dispatcher 失效 ⇒ repo 的 kubectl-ns-guard 拿不回來。
#
#: 實際可用的 model（`sys_list_models` 實測）。改這裡就是改全樹。
# `gpt-5.4-mini` 不列：Codex 以 ChatGPT 帳號拒絕 mini（2026-09-11 probe 1 實證）。
PI_MODELS = ["gpt-5.3-codex-spark", "gpt-5.4", "gpt-5.5", "gpt-5.6-luna",
             "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra"]
CC_MODELS = ["claude-opus-5", "claude-fable-5-1", "claude-sonnet-5", "claude-fable-5-1[1m]"]

#: purpose → {harness 選項: (model, effort)}；`template` 指向 `_templates/` 的來源目錄。
ROSTER: dict[str, dict[str, object]] = {
    "repo-leader": {
        "template": None,          # repo_leader 的 prompt 來自 _shared/，不由本檔產生
        "models": {"claude-native": ("claude-fable-5-1", "xhigh"),
                   "pi": ("gpt-6-astra", "xhigh")},
        "writes": True,
    },
    "implement": {
        # ⛔ 固定 pi：上游 `worktree_guard` 對 Claude 原生 Edit／Write 必帶的絕對
        # `file_path` 一律 DENY（`policies/builtins/orchestration.py:590`），implement_cc
        # 實測不能改檔（#1096）。`_templates/implementer_cc/` 保留、暫停使用。
        "template": {"pi": "implementer_pi"},
        "models": {"pi": ("gpt-5.6-sol", "xhigh")},
        "writes": True,
    },
    "review": {
        # ⚠ 覆核固定 claude-native：實作固定 pi ⇒ 跨 vendor 自動成立（單向）。
        "template": {"claude-native": "verifier_cc"},
        "models": {"claude-native": ("claude-sonnet-5", "high")},
        "writes": False,
    },
    "explore": {
        "template": {"pi": "scout_pi"},
        "models": {"pi": ("gpt-5.6-luna", "high")},
        "writes": False,
    },
    "tracker": {
        "template": {"pi": "tracker"},
        # terra 在 #1092 review 與 probe 2／3 實跑過。
        "models": {"pi": ("gpt-5.6-terra", "high")},
        "writes": True,            # 只寫產物（issue／PR／對帳報告），不碰產品碼
    },
    "ops_k8s": {
        "template": {"claude-native": "ops_k8s"},   # ⛔ 固定 claude-native，見上方硬規則
        "models": {"claude-native": ("claude-sonnet-5", "xhigh")},
        "writes": True,
    },
    "researcher": {
        "template": {"pi": "researcher"},
        "models": {"pi": ("gpt-5.6-luna", "high")},
        "writes": False,
    },
}

#: `search` 與 `explore` 同義（`headless_subagent_purpose_guard` 的 allowed_purposes 兩者都有）。
PURPOSE_ALIASES = {"search": "explore", "deploy": "ops_k8s", "triage": "tracker"}


def resolve_purpose(purpose: str) -> str:
    """把 purpose 別名正規化成 ROSTER 的 key。"""
    return PURPOSE_ALIASES.get(purpose, purpose)


# ── worker config 產生 ───────────────────────────────────────────────────────

def _worker_config(purpose: str, harness: str, *, model: str, effort: str,
                   description: str, name: str | None = None, worktree: str | None = None,
                   repo_root: str | None = None, worktree_rel: str | None = None) -> str:
    """組出 worker 的 config.yaml 內容。

    ⛔ **不要直接呼叫這支。** 它只負責「把欄位組成 YAML 字串」，**不做任何驗證**——
    五道防線全在 `generate()` 裡。覆核實測：直接呼叫
    `_worker_config("ops_k8s", "pi", model="gpt-6-astra", …)` 能產出 9030 bytes 的
    config，而那是個 **ops_k8s 不支援的 harness ＋ 不相容的 model** 的組合。

    ⇒ 一律走 `generate()`（或 CLI）。前綴 `_` 就是在說這件事。
    """
    lines = [
        "# ⚠ 由 generate-worker.py 在**執行期**產生——⛔ 不要手改，改 generate-worker.py。",
        f"#   purpose={purpose} harness={harness} model={model} effort={effort}",
        "",
        "spec_version: 1",
        f"name: {name or _safe_name(purpose, harness)}",
        f"description: {description}",
        "",
        "executor:",
        "  type: omnigent",
        f"  reasoning_effort: {effort}",
        f"  model: {model}",
        "  config:",
        f"    harness: {harness}",
    ]
    if harness == "claude-native":
        lines += [
            "    # headless worker 沒人能回答 ApprovalCard，而 managed Claude settings 停用",
            "    # bypassPermissions；`auto` 自動放行且在 managed settings 下合法。",
            "    permission_mode: auto",
        ]
    lines += [
        "",
        "# prompt 由同目錄的 AGENTS.md 承載（實體檔，由產生器從 _templates/ 複製並串接 _shared/ 片段）。",
        "# ⛔ 不要改寫成 `instructions: <相對路徑>`：containment 檢查失敗時會回 None，",
        "#    接著整條路徑字串被當成 system prompt，而且不報錯（防線 2）。",
        "",
        "# ⛔ 一律固定 `all`，絕不可設 `none`——`none` 會發出 --setting-sources \"\"，",
        "#    壓掉 dispatcher 與所有 repo hooks（inner/bundle_skills.py）。防線 5。",
        "skills: all",
        # ⚠ 唯讀 worker **一樣有** os_env（sys_os_* 會註冊）——唯讀保證只靠 read_only_os 一層
        #   （比對工具名，A 類）。README 舊版寫「唯讀 worker 省略 os_env」是文件先於實作的
        #   錯誤描述，2026-09-14 實跑本函式確認後裁決照實作改文件。
        OS_ENV_BLOCK.rstrip(),
        "",
        _guardrails(purpose, harness, worktree=worktree, repo_root=repo_root,
                    worktree_rel=worktree_rel),
    ]
    return "\n".join(lines) + "\n"


def _safe_name(purpose: str, harness: str) -> str:
    """預設 agent name：`^[a-zA-Z0-9_-]+$`，且要能看出 purpose 與 harness。"""
    via = "cc" if harness == "claude-native" else "pi"
    return f"{purpose.replace('-', '_')}_{via}"


#: omnigent 的 agent name 規則（spec/parser）：`^[a-zA-Z0-9_-]+$`。
AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _agent_name(purpose: str, harness: str, name: str | None) -> str:
    """`--name` 同時是 agent name 與目錄名（review F13）；沒給就用 `<purpose>_<cc|pi>`。

    同名**不衝突**：omnigent 每次 `config_path` 上傳都建一個新的 session-scoped agent row，
    name 只是給 UI／log 看的標籤，不是全域身分。所以這裡只驗字元集，不驗唯一性。
    """
    if not name:
        return _safe_name(purpose, harness)
    if not AGENT_NAME_RE.match(name):
        raise GenerateFailure(
            f"--name {name!r} 不合 agent name 規則 ^[a-zA-Z0-9_-]+$（它同時是 agent name 與目錄名；"
            "建議 <purpose>-<issue>-<cc|pi>，例如 impl-1032-pi）")
    return name


def _guardrails(purpose: str, harness: str, *, worktree: str | None = None,
                repo_root: str | None = None, worktree_rel: str | None = None) -> str:
    """worker 的完整 policy 集合。

    ⚠ child **不繼承** parent 的 `guardrails.policies`（實測：create body 只帶
    agent_id / parent_session_id / title / sub_agent_name）⇒ 每個 worker 必須自帶完整
    policy，不能靠 leader 罩下來。
    """
    spec = ROSTER[purpose]
    readonly = not spec["writes"]
    out = ["guardrails:",
           "  # ⚠ child 不繼承 parent 的 guardrails.policies ⇒ 每個 worker 自帶完整 policy。",
           f"  ask_timeout: {ASK_TIMEOUT}",
           "  policies:"]
    body = [P_BLAST]
    if readonly:
        body.append(P_READONLY)
    if purpose == "implement":
        # ⛔ 三個路徑缺一不可——generate() 已驗過；這裡再斷言一次，免得有人直接呼叫本函式
        #   產出一份「有 worktree_guard 卻沒有 P-path」的 config（那正是 review F6 的洞）。
        assert worktree and repo_root and worktree_rel, "implement 必須帶 worktree／repo_root／worktree_rel"
        body += [P_WORKTREE, _p_workdir(worktree, repo_root), _p_path(worktree_rel, worktree)]
    if purpose == "ops_k8s":
        body.append(P_KUBECTL)
    body += [P_BOARD, P_MERGE, P_APPROVE, P_TAG, P_ESCAPE]
    body += P_SAFETY
    if harness == "claude-native":
        # ⚠ pi 系刻意不掛 cost_budget：subscription 模型無 catalog pricing 時會回
        #   _UNPRICED_ASK，每個 session 卡一次人工批准，會毀掉無人值守。
        body.append(P_COST)
    return "\n".join(out + body)


# ── 五道防線 ─────────────────────────────────────────────────────────────────

class GenerateFailure(RuntimeError):
    """任一道防線不過。⛔ 絕不降級成 warning——寧可不派工。"""


# ── 模板的共用片段（review F10）────────────────────────────────────────────────
# 八份模板逐字相同的段落（鐵律／守線強度／跨 repo 唯讀／回報／不要離開 worktree）抽到
# `_templates/_shared/<片段>.md`，模板用獨占一行的 `<!-- include: <片段> -->` 引用，複製
# AGENTS.md 時在這裡展開。⛔ 不支援巢狀、不支援參數——片段裡提到只掛在部分角色的 policy
# 要自己寫明「只掛在誰身上」。片段不存在／標記格式錯 ⇒ fail loud（防線 2）。
INCLUDE_RE = re.compile(r"^<!--\s*include:\s*([a-z0-9-]+)\s*-->[ \t]*$", re.M)


def fragment_names() -> list[str]:
    """`_shared/` 裡可被引用的片段名（README 不算）。"""
    return sorted(q.stem for q in SHARED_FRAGMENTS_DIR.glob("*.md") if q.name != "README.md")


def render_prompt(template_dir: Path) -> str:
    """模板 AGENTS.md → 展開 include 之後的 prompt 全文。"""
    src = (template_dir / "AGENTS.md").read_text()

    def _sub(m: re.Match) -> str:
        frag = SHARED_FRAGMENTS_DIR / f"{m.group(1)}.md"
        if not frag.is_file():
            raise GenerateFailure(
                f"防線 2：模板 {template_dir.name} 引用不存在的共用片段 "
                f"{frag.relative_to(BUNDLE_DIR)}（可用：{fragment_names()}）")
        return frag.read_text().rstrip("\n")

    out = INCLUDE_RE.sub(_sub, src)
    if "<!-- include:" in out:
        raise GenerateFailure(
            f"防線 2：模板 {template_dir.name} 有無法解析的 include 標記"
            "（格式必須是獨占一行的 `<!-- include: <name> -->`）")
    return out


def _check_model_family(harness: str, model: str) -> None:
    """防線 1：harness × model 家族相容性，**在寫檔前**擋下。

    ⚠ 這條路徑 omnigent **完全不驗**：家族檢查只掛在 `sys_session_send` 的 `args.model`，
    走 `sys_session_create(config_path=...)` 時 create 會成功，等 child 第一輪才爆成
    通用 `runner_error`（訊息不會告訴你是 model 不相容）⇒ 必須自己先驗。
    """
    _omnigent_on_path()
    try:
        from omnigent.models.model_override import model_family_mismatch  # noqa: PLC0415
    except ImportError as new_path_exc:
        try:
            from omnigent.model_override import model_family_mismatch  # noqa: PLC0415
        except ImportError as old_path_exc:
            raise GenerateFailure(
                "防線 1：無法載入 omnigent model_override（已嘗試 "
                "omnigent.models.model_override 與 omnigent.model_override）："
                f"{new_path_exc}; {old_path_exc}"
            ) from old_path_exc

    reason = model_family_mismatch(harness, model)
    if reason:
        raise GenerateFailure(
            f"防線 1：harness {harness!r} 不能跑 model {model!r} —— {reason}\n"
            "  ⚠ omnigent 在 create 這條路徑不驗這件事，不擋的話會等 child 第一輪才爆成"
            "通用 runner_error。"
        )


def _verify_generated(out_dir: Path, *, expect_skills: int, expect_name: str) -> dict:
    """防線 2／3／4：對產出的目錄跑 parse() + validate() 與三組斷言。"""
    _omnigent_on_path()
    import omnigent.spec as spec_mod  # noqa: PLC0415

    # 防線 3：檔名必須恰好是 config.yaml
    # ⚠ 實測錯誤訊息是 `config.yaml not found in /tmp/tmpXXXX/agent`——路徑是 omnigent 的
    #   臨時解壓目錄，完全看不出是「你寫錯檔名」⇒ 自己先驗，訊息才有用。
    cfg = out_dir / "config.yaml"
    if not cfg.is_file():
        others = sorted(q.name for q in out_dir.glob("*.y*ml"))
        raise GenerateFailure(
            f"防線 3：{out_dir} 裡沒有 config.yaml"
            + (f"（只有 {others}——檔名必須恰好是 config.yaml）" if others else "")
        )

    try:
        spec = spec_mod.parse(out_dir, expand_env=False)
    except Exception as exc:  # noqa: BLE001
        raise GenerateFailure(f"防線 2/3：parse() 失敗 {type(exc).__name__}: {exc}") from exc
    result = spec_mod.validate(spec)
    if result.errors:
        detail = "; ".join(f"{e.path}: {e.message}" for e in result.errors[:5])
        raise GenerateFailure(f"防線 2/3：validate() 有 errors —— {detail}")

    if spec.name != expect_name:
        raise GenerateFailure(f"防線 3：name 應為 {expect_name!r}，實得 {spec.name!r}")

    # 防線 2：instructions 必須是**內容**，不是路徑字串
    ins = spec.instructions
    if not ins or not ins.strip():
        raise GenerateFailure("防線 2：instructions 為空（AGENTS.md 沒寫進去？）")
    if "omnigent-prompt-marker:" not in ins:
        raise GenerateFailure("防線 2：instructions 缺 marker，可能不是預期的 prompt")
    if "<!-- include:" in ins:
        raise GenerateFailure("防線 2：instructions 殘留未展開的 <!-- include: --> 標記"
                              "（共用片段沒有被串接進去）")
    body = ins.strip()
    if "\n" not in body and (body.endswith(".md") or "/" in body):
        raise GenerateFailure(
            f"防線 2：instructions 看起來是**路徑字串**而不是內容：{body!r}\n"
            "  ⚠ 這正是 `instructions: <相對路徑>` containment 失敗時的靜默症狀。"
        )

    # 防線 4：skills 數必須等於模板的預期數（`skills/` 有目錄無 SKILL.md 會靜默為空）
    if len(spec.skills) != expect_skills:
        raise GenerateFailure(
            f"防線 4：skills 數 {len(spec.skills)} != 預期 {expect_skills}"
            "（skills/ 有目錄但沒有 SKILL.md 會靜默為空）"
        )

    # 防線 5：skills_filter 絕不可是 none
    if getattr(spec, "skills_filter", "all") == "none":
        raise GenerateFailure(
            "防線 5：skills_filter 是 none —— 它會發出 --setting-sources \"\"，"
            "壓掉 dispatcher 與所有 repo hooks。"
        )

    return {"name": spec.name, "skills": len(spec.skills),
            "policies": len(spec.guardrails.policies) if spec.guardrails else 0,
            "model": spec.executor.model, "effort": spec.executor.reasoning_effort}


#: 路徑字元白名單：worktree／repo_root／workspace 會進 CEL regex 與 YAML，不容許引號與空白。
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")


def _worktree_rel(worktree: str, repo_root: str, workspace: str) -> str:
    """P-path 的相對前綴＝worktree 相對於 workspace；三個路徑都要是乾淨的絕對路徑，
    worktree 不在 workspace 底下就拒絕（worker 的 cwd 是 workspace，它根本進不去）。"""
    for label, val in (("worktree", worktree), ("repo_root", repo_root), ("workspace", workspace)):
        if not _ABS_PATH_RE.match(val) or "/../" in val or val.endswith("/..") or val.endswith("/"):
            raise GenerateFailure(
                f"{label} 必須是不含 `..`、不以 / 結尾、只含 [A-Za-z0-9._/-] 的絕對路徑：{val!r}")
    rel = os.path.relpath(worktree, workspace)
    if rel == "." or rel.startswith(".."):
        raise GenerateFailure(
            f"worktree {worktree} 不在 workspace {workspace} 底下——runner 的 cwd 是 workspace，"
            "worker 進不去；P-path 也沒有前綴可掛。")
    return rel


def generate(purpose: str, harness: str, out_dir: Path, *,
             model: str | None = None, effort: str | None = None,
             repo: str | None = None, worktree: str | None = None,
             repo_root: str | None = None, workspace: str | None = None,
             name: str | None = None) -> dict:
    """產生一個 worker bundle 目錄並跑完五道防線。回傳自檢摘要。

    `workspace` 預設 `$OMNIGENT_RUNNER_WORKSPACE`（再退回 cwd）——它是 worker 檔案工具相對
    路徑的基準，P-path 的前綴由 worktree 相對它算出。`name` 是 agent name（＝CLI 的目錄名），
    沒給就 `<purpose>_<cc|pi>`。
    """
    purpose = resolve_purpose(purpose)
    worktree_rel: str | None = None
    if purpose == "implement":
        if not (worktree and repo_root):
            raise GenerateFailure(
                "implement 必須同時給 worktree 與 repo_root：worktree 由 worker 自建、兩個路徑都要"
                "進 allowed_dirs，而 P-path 要用 worktree 算寫入前綴。")
        workspace = workspace or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or os.getcwd()
        worktree_rel = _worktree_rel(worktree, repo_root, workspace)
    if purpose not in ROSTER:
        raise GenerateFailure(f"未知的 purpose {purpose!r}（可用：{sorted(ROSTER)}）")
    spec = ROSTER[purpose]
    templates = spec["template"]
    if templates is None:
        raise GenerateFailure(
            f"purpose {purpose!r} 的 prompt 不由本檔產生（repo-leader 是靜態 bundle）")
    if harness not in templates:
        detail = ""
        if purpose == "implement":
            detail = ("。⛔ implement 固定 pi：上游 worktree_guard 對 Claude 原生 Edit／Write "
                      "的絕對 file_path 一律 DENY，implement_cc 實測不能改檔。")
        elif purpose == "ops_k8s":
            detail = "。⛔ ops_k8s 固定 claude-native：pi 沒有 hook 機制，換了 dispatcher 就失效。"
        raise GenerateFailure(
            f"purpose {purpose!r} 不支援 harness {harness!r}（只支援 {sorted(templates)}）"
            + detail)
    default_model, default_effort = spec["models"][harness]
    model = model or default_model
    effort = effort or default_effort

    _check_model_family(harness, model)      # ← 防線 1，寫檔前

    template_dir = TEMPLATES_DIR / templates[harness]
    if not (template_dir / "AGENTS.md").is_file():
        raise GenerateFailure(f"模板不存在：{template_dir / 'AGENTS.md'}")
    if not (MESSAGING_SKILL_DIR / "SKILL.md").is_file():
        raise GenerateFailure("共用 omni-msg skill 不存在，拒絕產生不完整 worker")
    if (template_dir / "skills" / "omni-msg").exists():
        raise GenerateFailure("omni-msg 只能由共用來源提供，模板不可另存副本")
    expect_skills = len(list((template_dir / "skills").glob("*/SKILL.md"))) + 1

    name = _agent_name(purpose, harness, name)
    out_dir.mkdir(parents=True, exist_ok=True)
    desc = f"{purpose} worker（{harness}／{model}）"
    if repo:
        desc += f"，目標 repo {repo}"
    (out_dir / "config.yaml").write_text(
        _worker_config(purpose, harness, model=model, effort=effort, description=desc,
                       name=name, worktree=worktree, repo_root=repo_root, worktree_rel=worktree_rel))
    # 防線 2：只寫**實體** AGENTS.md（模板＋展開 _shared/ 片段），不用 instructions 路徑、不用 symlink
    (out_dir / "AGENTS.md").write_text(render_prompt(template_dir))
    skills_dst = out_dir / "skills"
    if skills_dst.exists():
        shutil.rmtree(skills_dst)
    shutil.copytree(template_dir / "skills", skills_dst)
    # 每個 session 都能讀到 skill；是否可發訊仍取決於 operator 配置與原有 policy。
    shutil.copytree(MESSAGING_SKILL_DIR, skills_dst / "omni-msg")
    # dispatcher：claude-native worker 才有 hook 機制。⚠ `skills/` 必須存在才會發
    # --plugin-dir，hooks/ 才會被 claude 讀到——兩者缺一不可。
    if harness == "claude-native" and HOOKS_DIR.is_dir():
        hooks_dst = out_dir / "hooks"
        if hooks_dst.exists():
            shutil.rmtree(hooks_dst)
        shutil.copytree(HOOKS_DIR, hooks_dst)

    info = _verify_generated(out_dir, expect_skills=expect_skills, expect_name=name)
    info.update({"purpose": purpose, "harness": harness, "dir": str(out_dir),
                 "hooks": (out_dir / "hooks" / "hooks.json").is_file()})
    if worktree:
        info["worktree"] = worktree
    if repo_root:
        info["repo_root"] = repo_root
    if worktree_rel:
        info["worktree_rel"] = worktree_rel
    return info


#: self-check／驗收用的樣本路徑。implement 沒有 worktree 就拒絕產生（P-path 沒有前綴可掛），
#: 所以自檢與驗收第 8 項都用這一組；build-bundles.py 的 P-path 向量也對著它寫。
SELF_CHECK_WORKSPACE = "/home/elmo/woow-repos"
SELF_CHECK_REPO_ROOT = f"{SELF_CHECK_WORKSPACE}/paas/woow_paas_platform"
SELF_CHECK_WORKTREE = f"{SELF_CHECK_WORKSPACE}/paas/wt-selfcheck"
SELF_CHECK_WORKTREE_REL = "paas/wt-selfcheck"


def sample_kwargs(purpose: str) -> dict[str, str]:
    """generate() 在自檢時要補的參數（只有 implement 需要）。"""
    if resolve_purpose(purpose) == "implement":
        return {"worktree": SELF_CHECK_WORKTREE, "repo_root": SELF_CHECK_REPO_ROOT,
                "workspace": SELF_CHECK_WORKSPACE}
    return {}


def self_check() -> tuple[bool, str]:
    """產生每一種 purpose × harness 組合並跑完防線，不留檔。供驗收呼叫。"""
    made: list[str] = []
    bad: list[str] = []
    with tempfile.TemporaryDirectory(prefix="gen-worker-selfcheck-") as td:
        for purpose, spec in ROSTER.items():
            if spec["template"] is None:
                continue
            for harness in spec["template"]:
                try:
                    info = generate(purpose, harness, Path(td) / f"{purpose}-{harness}",
                                    **sample_kwargs(purpose))
                except GenerateFailure as exc:
                    bad.append(f"{purpose}/{harness}: {exc}")
                else:
                    made.append(f"{info['name']}({info['model']},{info['effort']},"
                                f"skills={info['skills']},pol={info['policies']}"
                                f"{',hooks' if info['hooks'] else ''})")
        # 反面案例：不合法的 harness × model 組合**必須**在寫檔前被擋下
        for harness, model in (("claude-sdk", "gpt-6-astra"), ("claude-native", "opus"),
                               ("claude-native", "gpt-5.6-sol")):
            try:
                generate("review", "claude-native", Path(td) / "neg", model=model)
            except GenerateFailure:
                pass
            else:
                bad.append(f"反面案例未被擋下：claude-native + {model}")
        # harness 不在該 purpose 的支援清單（implement 固定 pi）
        try:
            generate("implement", "claude-native", Path(td) / "neg3")
        except GenerateFailure:
            pass
        else:
            bad.append("反面案例未被擋下：implement + claude-native（worktree_guard 擋 Claude 原生寫檔）")
        # harness 不在該 purpose 的支援清單（ops_k8s 固定 claude-native）
        try:
            generate("ops_k8s", "pi", Path(td) / "neg2")
        except GenerateFailure:
            pass
        else:
            bad.append("反面案例未被擋下：ops_k8s + pi（pi 沒有 hook 機制）")
        # implement 缺 worktree／repo_root ⇒ 拒絕（否則產出一份沒有 P-path 前綴的 config）
        try:
            generate("implement", "pi", Path(td) / "neg4")
        except GenerateFailure:
            pass
        else:
            bad.append("反面案例未被擋下：implement 不帶 worktree（P-path 沒有前綴可掛）")
        # --name 是 agent name（review F13）：合法的要真的寫進 config；不合規則的要拒絕
        try:
            info = generate("review", "claude-native", Path(td) / "impl-1032-cc", name="impl-1032-cc")
        except GenerateFailure as exc:
            bad.append(f"--name 合法卻被拒絕：{exc}")
        else:
            if info["name"] != "impl-1032-cc":
                bad.append(f"--name 沒有成為 agent name（預期 'impl-1032-cc'，實得 {info['name']!r}）")
        try:
            generate("review", "claude-native", Path(td) / "neg6", name="impl 1032")
        except GenerateFailure:
            pass
        else:
            bad.append("反面案例未被擋下：--name 含空白（不合 ^[a-zA-Z0-9_-]+$）")
        # worktree 不在 workspace 底下 ⇒ 拒絕（worker 的 cwd 是 workspace，進不去）
        try:
            generate("implement", "pi", Path(td) / "neg5", worktree="/tmp/wt-x",
                     repo_root=SELF_CHECK_REPO_ROOT, workspace=SELF_CHECK_WORKSPACE)
        except GenerateFailure:
            pass
        else:
            bad.append("反面案例未被擋下：worktree 不在 workspace 底下")
    ok = not bad
    checked = len(made) + 4  # 另計四種固定拒絕：implement+claude-native（#1096）、implement 缺 worktree、worktree 不在 workspace、--name 不合規則
    return ok, (f"{checked} 種組合自檢通過（{len(made)} 種可產生＋4 種固定拒絕案例：implement 固定 pi／"
                f"implement 缺 worktree／worktree 不在 workspace／--name 不合規則；--name 合法時成為 agent name）：{', '.join(made)}" if ok
                else f"{len(bad)} 個問題：" + "；".join(bad[:4]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--purpose", help=f"可用：{sorted(ROSTER)} ＋別名 {sorted(PURPOSE_ALIASES)}")
    ap.add_argument("--harness", choices=["claude-native", "pi"])
    ap.add_argument("--name",
                    help="worker 的 agent name 兼目錄名（預設 <purpose>_<cc|pi>；規則 ^[a-zA-Z0-9_-]+$）。"
                         "同名不衝突（每次上傳都是新的 session-scoped agent），只用來在 UI／log 分辨是哪張 "
                         "issue 的 worker；建議 <purpose>-<issue>-<cc|pi>，例如 impl-1032-pi")
    ap.add_argument("--model", help="覆寫對照表的 model（仍會過防線 1）")
    ap.add_argument("--effort", choices=["high", "xhigh"], help="覆寫對照表的 effort")
    ap.add_argument("--repo", help="目標 repo alias（寫進 description）")
    ap.add_argument("--worktree",
                    help="該 issue 的 worktree 絕對路徑（由 worker 自建；implement 填進 "
                         "block_working_dir_changes.allowed_dirs）")
    ap.add_argument("--repo-root",
                    help="目標 repo 的絕對路徑（implement 必給：填進 allowed_dirs，worker 才能 "
                         "`git -C <repo-root> worktree add`）")
    ap.add_argument("--workspace",
                    help="runner 的 workspace（六 repo 的共同祖先；預設 $OMNIGENT_RUNNER_WORKSPACE，"
                         "再退回 cwd）。P-path 的寫入前綴＝worktree 相對於它的路徑")
    ap.add_argument("--out-dir", default=None,
                    help="worker 目錄的父層（預設 $OMNIGENT_RUNNER_WORKSPACE/.omnigent-runtime/workers）")
    ap.add_argument("--self-check", action="store_true", help="自檢所有組合，不留檔")
    ap.add_argument("--json", action="store_true", help="輸出 JSON 而非純路徑")
    args = ap.parse_args()

    if args.self_check:
        ok, detail = self_check()
        print(("✅ " if ok else "✗ ") + detail)
        return 0 if ok else 1

    if not args.purpose or not args.harness:
        ap.error("--purpose 與 --harness 必填（或用 --self-check）")

    if resolve_purpose(args.purpose) == "implement" and not (args.worktree and args.repo_root):
        ap.error("implement 必須同時給 --worktree 與 --repo-root（worktree 由 worker 自建，"
                 "兩個路徑都要進 block_working_dir_changes.allowed_dirs）")

    parent = Path(args.out_dir) if args.out_dir else _default_out_parent()
    name = args.name or _safe_name(resolve_purpose(args.purpose), args.harness)
    try:
        info = generate(args.purpose, args.harness, parent / name, model=args.model,
                        effort=args.effort, repo=args.repo, worktree=args.worktree,
                        repo_root=args.repo_root, workspace=args.workspace, name=args.name)
    except GenerateFailure as exc:
        print(f"✗ 拒絕產生：{exc}", file=sys.stderr)
        print("⛔ 不要用 sys_session_create 派這個 worker。", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(info["dir"])          # ← 直接餵給 sys_session_create(config_path=...)
    return 0


def _default_out_parent() -> Path:
    """worker 目錄的預設父層。

    放在 workspace 底下的 `.omnigent-runtime/workers/`：`config_path` 的 containment 以
    workspace 為錨點，放外面會被擋；而放 workspace 根（不是任何 repo 內）不會污染 repo。
    """
    ws = os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
    base = Path(ws) if ws else Path.cwd()
    return base / ".omnigent-runtime" / "workers"


if __name__ == "__main__":
    sys.exit(main())

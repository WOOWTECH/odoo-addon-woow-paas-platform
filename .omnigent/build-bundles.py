#!/usr/bin/env python3
"""產生 woow multi-agent bundle 的 16 個 thin shell 與全部 config.yaml，並自檢。

用法::

    python3 .omnigent/build-bundles.py           # 重產（generate）
    python3 .omnigent/build-bundles.py --check   # 只驗收，不寫檔（CI／PR 用）

樹形：`woow-issue-leader` → 2 個 `repo_leader` → 各 8 個 worker thin shell。
thin shell 只有自己的 `config.yaml`，`AGENTS.md` 與 `skills/` 都是 symlink 指回
`_workers/<worker>/` 的**單一實體**——8 份 prompt 服務 16 個 agent。

⚠ 本檔是全樹**所有** `config.yaml` 與所有 CEL 運算式的唯一來源。config.yaml 是產物，
   ⛔ 不要手改；改這裡再重跑。

── 為什麼寫成這樣（每一條都是實測過的靜默失敗，不要「優化」掉）────────────────

1. **prompt 只能用 `AGENTS.md` symlink 承載**。⛔ 禁止改寫成 `instructions: <相對路徑>`
   搭外指 symlink：`spec/parser.py::_read_contained_file` 的 containment 檢查會讓外指
   symlink 回 None，接著**整條路徑字串被當成 system prompt**，而且不報錯。
   context-file 路徑（`AGENTS.md`）沒有 containment 檢查，symlink 正常跟隨。
2. **symlink 一律用 `os.path.relpath` 推算**，不要手數 `../`。斷鏈時 `is_dir()` 回 False
   → **靜默少發現一個 sub-agent**。
3. **每個 thin shell 都必須列進上層 `tools.agents`**——這是第 2 條的安全網：
   `tools.agents` 引用不存在的 sub-agent 會 **loud fail**（`spec/validator.py`），把靜默
   消失變成明確錯誤。
4. **`tools.agents` 列的是 YAML `name`，不是資料夾名**。`name` 省略時是 `None`、
   **不會** fallback 成目錄名，那個 sub-agent 就永遠無法被引用。本檔一律讓
   資料夾名 == `name`，把這個坑直接填掉。
5. **agent `name` 全樹唯一**，正則 `^[a-zA-Z0-9_-]+$`。
6. **skill frontmatter `name` 正則是 `^[a-z0-9-]+$`（不允許底線）**，與 agent name 規則
   不同。skill 名**一律取自 frontmatter，目錄名被忽略**。
7. **來源池的 skill name 必須全域唯一**（所以一律加 worker 前綴）——
   `discover_host_skills` 是 first-wins 去重，同名後者會被吃掉。
8. **`skills:` 一律固定寫 `all`**。⛔ 禁止名單模式：claude-native 下形同 `all`、
   **不強制**；pi 下卻會下 `--no-skills` **把 host skill 一併關掉**——跨 harness 行為相反。
9. **child 不繼承 parent 的 `guardrails.policies`**（實測：create body 只帶
   agent_id/parent_session_id/title/sub_agent_name）⇒ **每個 worker 自帶完整 policy**，
   不能靠 leader 罩下來。
10. **`os_env.cwd` 不決定 harness process cwd**，它只是 server 模式的**邊界**與
    `sys_os_*` / `config_path` containment 的錨點。唯讀 worker 完全**省略** os_env block
    ——省略時 `sys_os_read/write/edit/shell` 四支一支都不註冊，唯讀幾乎免費。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OMNIGENT_DIR = Path(__file__).resolve().parent
WORKERS_DIR = OMNIGENT_DIR / "_workers"
SHARED_DIR = OMNIGENT_DIR / "_shared"
LEADER_DIR = OMNIGENT_DIR / "woow-issue-leader"

GENERATED_HEADER = (
    "# ⚠ 由 .omnigent/build-bundles.py 產生——⛔ 不要手改。\n"
    "#   改 build-bundles.py 再重跑：python3 .omnigent/build-bundles.py\n"
)

# ── 工具名稱面（權威來自 omnigent.policies.builtins）──────────────────────────
# 同一支「shell」在不同 harness 有不同名字，但參數一律正規化成 `command`。
# 出處：omnigent/policies/builtins/_shell.py::SHELL_TOOLS
SHELL_TOOLS = ["Bash", "Shell", "bash", "developer__shell", "shell", "sys_os_shell", "terminal"]
# 出處：omnigent/policies/builtins/orchestration.py::read_only_os / worktree_guard
# 參數：omnigent／pi 用 `path`，claude-native 用 `file_path`。
WRITE_TOOLS = ["sys_os_write", "sys_os_edit", "Write", "Edit", "MultiEdit", "write", "edit"]

# ── 快層 model 名單（P-model）────────────────────────────────────────────────
# 🔴 **這裡是唯一來源**。model 名會隨 CLI／catalog 版本漂移；改這一處再重跑本腳本。
#    當下真的可用的 model，在 session 內用 `sys_list_models` 查。
# ⚠ claude-native 只吃 id 含 `claude` 的 model——裸別名 `opus`／`sonnet`／`haiku` 會在
#    dispatch 當下 fail loud（實測 omnigent.model_override.model_family_mismatch）。
#    被 P-model 管的三個角色都是 pi harness，pi 接受任何通過驗證的 gateway model id。
FAST_MODELS = [
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "databricks-claude-haiku-4-5",
    "claude-sonnet-5",
    "databricks-claude-sonnet-5",
]

# 被 P-model 強制「必須顯式指定快層 model」的角色（簡單查尋任務）。
FAST_ONLY_ROLES = ["scout_pi", "tracker", "researcher"]

# ── woow-k3s namespace 白名單（P-kubectl）────────────────────────────────────
# 🔴 這是白名單的**第五份副本**。權威三份由 repo-lint `kubectl-ns-whitelist-sync` 對帳：
#      1. CLAUDE.md「⛔ 鐵律」表格
#      2. docs/guides/deployment/shared-cluster-paas-scope-guard.md
#      3. .claude/hooks/kubectl-ns-guard.py::ALLOWED_NAMESPACES
#    第四份是 .omnigent/_workers/ops_k8s/AGENTS.md 的表格。
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

BOARD_PATH_RE = r"(^|/)\\.woow-issues/board\\.md$"
# shell 改寫看板：把「改寫動作」與「看板路徑」綁在同一條 regex 裡，避免
# `cat board.md 2>/dev/null` 這種純讀取被誤殺。
BOARD_SHELL_RE = (
    r"(>>?\\s*[^\\s;|&]*\\.woow-issues/board\\.md"
    r"|\\b(tee|rm|truncate|patch|cp|mv|dd)\\b[^;|&]*\\.woow-issues/board\\.md"
    r"|\\bsed\\b[^;|&]*(-i|--in-place)[^;|&]*\\.woow-issues/board\\.md)"
)

CEL_BOARD = f"""event.type == "tool_call" && (
  (event.data.name in {_cel_list(WRITE_TOOLS)} && (
    (has(event.data.arguments.path) && event.data.arguments.path.matches("{BOARD_PATH_RE}"))
    || (has(event.data.arguments.file_path) && event.data.arguments.file_path.matches("{BOARD_PATH_RE}"))
  ))
  || (event.data.name in {_cel_list(SHELL_TOOLS)}
      && has(event.data.arguments.command)
      && event.data.arguments.command.matches("{BOARD_SHELL_RE}"))
) ? {{"result": "DENY", "reason": "看板 .woow-issues/board.md 只有 woow-issue-leader 能寫；把要更新的內容寫進回報，由上層代寫。"}} : {{"result": "ALLOW"}}"""

CEL_MODEL = f"""event.type == "tool_call"
&& event.data.name == "sys_session_send"
&& has(event.data.arguments.agent)
&& event.data.arguments.agent.matches("^({"|".join(FAST_ONLY_ROLES)})_via_(cc|pi)$")
&& !(has(event.data.arguments.args)
     && has(event.data.arguments.args.model)
     && event.data.arguments.args.model in {_cel_list(FAST_MODELS)})
? {{"result": "DENY", "reason": "scout_pi / tracker / researcher 是簡單查尋任務：派工必須顯式帶 args.model，且必須落在 build-bundles.py 的 FAST_MODELS 名單內。"}} : {{"result": "ALLOW"}}"""

CEL_MERGE = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("\\\\b(tea\\\\s+(pr|pulls)\\\\s+merge|gh\\\\s+pr\\\\s+merge|glab\\\\s+mr\\\\s+merge)\\\\b")
? {{"result": "DENY", "reason": "merge 是人工 gate，agent 不代 merge。開好 PR 等人來 merge。"}} : {{"result": "ALLOW"}}"""

CEL_APPROVE = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("/approve")
&& event.data.arguments.command.matches("(\\\\btea\\\\b[^;|&]*\\\\bcomment\\\\b|\\\\bgh\\\\b[^;|&]*\\\\bcomment\\\\b|\\\\bglab\\\\b[^;|&]*\\\\bnote\\\\b|\\\\bcurl\\\\b)")
? {{"result": "DENY", "reason": "prod build 的 /approve 核可只能由授權者本人送出——即使人類當場明確授權、即使手上憑證就是授權者本人，agent 也不代簽。"}} : {{"result": "ALLOW"}}"""

# ⚠ 內建 github_policy 對這個 case 無效：實測 `git push origin v1.2.3` 只回 ASK
#    （URL regex 硬編 github.com，Gitea 判不出 repo），而且完全不認 `tea`。所以自己比對。
CEL_TAG = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("\\\\bgit\\\\b[^;|&]*\\\\bpush\\\\b")
&& event.data.arguments.command.matches("(--tags|--follow-tags|refs/tags/|[\\\\s:]([A-Za-z0-9._-]+-)?v[0-9])")
? {{"result": "DENY", "reason": "發版 tag 由人工推，agent 不推 tag（v<版號> / --tags / refs/tags/ 三種形態都擋）。"}} : {{"result": "ALLOW"}}"""

CEL_KUBECTL = f"""event.type == "tool_call"
&& event.data.name in {_cel_list(SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("\\\\bkubectl\\\\b")
&& event.data.arguments.command.matches("--context[=\\\\s]+woow-k3s")
&& !event.data.arguments.command.matches("\\\\bkubectl\\\\s+(?:\\\\S+\\\\s+){{0,10}}?(?:{"|".join(K8S_SAFE_VERBS)})\\\\b")
&& !event.data.arguments.command.matches("(^|\\\\s)(-n|--namespace)[=\\\\s]+({_ns_alternation()})(\\\\s|$)")
? {{"result": "DENY", "reason": "woow-k3s 是共用叢集：寫入／exec 類 kubectl 的 namespace 必須在 paas 白名單內，而且要明確帶 -n（fail-closed，不吃 current namespace）。"}} : {{"result": "ALLOW"}}"""


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
#    （cost.py），每個 session 卡一次人工批准，會毀掉無人值守。claude-native 的用量由
#    Claude Code 自己的 statusLine 回報 total_cost_usd（claude_native_status.py），有定價。
P_COST = _fn_policy(
    "cost_budget", "omnigent.policies.builtins.cost.cost_budget",
    {"max_cost_usd": 25.0, "ask_thresholds_usd": [5.0, 15.0]},
    note="⛔ 只掛 claude-native。pi 系掛上去會被 _UNPRICED_ASK 卡住每個 session（見上方註解）。")

P_MERGE = _cel_yaml("p_merge_no_agent_merge", CEL_MERGE)
P_APPROVE = _cel_yaml("p_approve_no_agent_signoff", CEL_APPROVE)
P_TAG = _cel_yaml("p_tag_no_agent_tag_push", CEL_TAG)
P_BOARD = _cel_yaml("p_board_read_only", CEL_BOARD)
P_MODEL = _cel_yaml("p_model_fast_tier_only", CEL_MODEL)
P_KUBECTL = _cel_yaml("p_kubectl_namespace_scope", CEL_KUBECTL)

P_LEADER_ORCH = [
    _fn_policy("spawn_bounds", "omnigent.policies.builtins.orchestration.spawn_bounds",
               {"max_dispatches_per_turn": 6,
                "dispatch_tools": ["sys_session_send", "sys_session_create"]}),
    _fn_policy("headless_subagent_purpose_guard",
               "omnigent.policies.builtins.orchestration.headless_subagent_purpose_guard",
               {"allowed_purposes": ["implement", "review", "explore", "search",
                                     "deploy", "triage"]}),
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
# ⚠ 沒給 allowed_dirs ⇒ **所有** cd / pushd / popd / git -C 一律 DENY。這是刻意的：
#   worktree_guard 擋不住「先 cd 出去、再寫相對路徑」，兩條合起來才是完整的封閉。
#   代價是 implementer 完全不能跨 repo（連 git -C 都不行）——跨 repo 是 leader 的事。
P_WORKDIR = _fn_policy(
    "block_working_dir_changes",
    "omnigent.policies.builtins.working_dir.block_working_dir_changes",
    {"block_cd": True, "block_worktree": True, "action": "deny"},
    note="未給 allowed_dirs ⇒ 所有 cd / pushd / popd / git -C / git worktree 一律 DENY。\n"
         "這是 worktree_guard 的另一半：擋掉「先 cd 出去、再寫相對路徑」的逃逸。\n"
         "副作用（刻意）：implementer 不能跨 repo，跨 repo 唯讀是 leader 的職責。")

OS_ENV_BLOCK = """
os_env:
  # ⚠ cwd 不決定 harness 的 process cwd（實測）——它是 server 模式的**邊界**與
  #   sys_os_* / config_path containment 的錨點。設在六個 repo 的共同父目錄，讓跨 repo
  #   唯讀查詢落在邊界內。這是全樹唯一一個機器相關的絕對路徑。
  type: caller_process
  cwd: /home/elmo/woow-repos
  sandbox:
    type: none
"""

# ── worker 名冊 ──────────────────────────────────────────────────────────────
# key = `_workers/` 底下的來源目錄名；`short` = thin shell 名字的前半。
WORKERS: dict[str, dict[str, object]] = {
    "implementer_cc": {
        "short": "impl_cc", "harness": "claude-native", "permission_mode": "auto",
        "os_env": True, "readonly": False,
        "desc": "實作 worker（claude-native）：多檔重構、寫測試，在自己的 worktree 內做完並開 PR。",
    },
    "implementer_pi": {
        "short": "impl_pi", "harness": "pi", "permission_mode": None,
        "os_env": True, "readonly": False,
        "desc": "實作 worker（pi）：同 implementer_cc，換 pi 額度池、跨 vendor 的另一個方向。",
    },
    "verifier_cc": {
        "short": "ver_cc", "harness": "claude-native", "permission_mode": None,
        "os_env": True, "readonly": True,
        "desc": "覆核 worker（claude-native）：唯讀，只對著驗收契約審 pi 實作的 diff。",
    },
    "verifier_pi": {
        "short": "ver_pi", "harness": "pi", "permission_mode": None,
        "os_env": False, "readonly": True,
        "desc": "覆核 worker（pi）：唯讀，只對著驗收契約審 claude-native 實作的 diff。",
    },
    "scout_pi": {
        "short": "scout_pi", "harness": "pi", "permission_mode": None,
        "os_env": False, "readonly": True,
        "desc": "偵察 worker（pi）：唯讀 recon / lookup，附 file:line 證據。",
    },
    "ops_k8s": {
        "short": "ops_k8s", "harness": "claude-native", "permission_mode": "auto",
        "os_env": True, "readonly": False,
        "desc": "叢集維運 worker（claude-native）：kubectl / deploy-stg / dev sandbox，掛 namespace 白名單守線。",
    },
    "tracker": {
        "short": "tracker", "harness": "pi", "permission_mode": None,
        "os_env": True, "readonly": False,
        "desc": "對帳 worker（pi）：六 repo tea 對帳、查重、開單；有寫權但只寫產物。",
    },
    "researcher": {
        "short": "researcher", "harness": "pi", "permission_mode": None,
        "os_env": False, "readonly": True,
        "desc": "查證 worker（pi）：唯讀，查上游 addon / Odoo 18 的實際行為。",
    },
}

LEADERS = {
    "repo_leader_cc": {"harness": "claude-native", "via": "cc", "permission_mode": "auto"},
    "repo_leader_pi": {"harness": "pi", "via": "pi", "permission_mode": None},
}


def thin_name(worker: str, via: str) -> str:
    return f"{WORKERS[worker]['short']}_via_{via}"


def _executor(harness: str, permission_mode: str | None) -> str:
    out = ["executor:", "  type: omnigent", "  config:", f"    harness: {harness}"]
    if permission_mode:
        out += [
            "    # headless worker 沒人能回答 ApprovalCard，而 managed Claude settings 停用",
            "    # bypassPermissions；`auto` 自動放行且在 managed settings 下合法。",
            f"    permission_mode: {permission_mode}",
        ]
    return "\n".join(out)


def _prompt_note(target: str) -> str:
    return (
        f"# prompt 由 AGENTS.md symlink 承載（→ {target}）。\n"
        "# ⛔ 不要改寫成 `instructions: <相對路徑>`：containment 檢查會讓外指 symlink 回\n"
        "#    None，接著整條路徑字串被當成 system prompt，而且不報錯。"
    )


def worker_config(worker: str, via: str) -> str:
    w = WORKERS[worker]
    name = thin_name(worker, via)
    parts = [
        GENERATED_HEADER,
        "spec_version: 1",
        f"name: {name}",
        f"description: {w['desc']}",
        "",
        _executor(str(w["harness"]), w["permission_mode"]),  # type: ignore[arg-type]
        "",
        _prompt_note(f"_workers/{worker}/AGENTS.md"),
        "",
        "# ⛔ 一律固定 `all`。名單模式在 claude-native 下形同 all、不強制；在 pi 下卻會下",
        "#    --no-skills 把 host skill 一併關掉——跨 harness 行為相反。",
        "skills: all",
    ]
    if w["os_env"]:
        parts.append(OS_ENV_BLOCK.rstrip())
    else:
        parts.append(
            "\n# ⚠ 刻意**完全省略** os_env block：省略時 sys_os_read / write / edit / shell\n"
            "#   四支一支都不註冊，唯讀角色幾乎免費。⛔ 不要為了「看起來一致」補回來。"
        )
    parts += ["", _guardrails_worker(worker, via)]
    return "\n".join(parts) + "\n"


def _guardrails_worker(worker: str, via: str) -> str:
    w = WORKERS[worker]
    pol = ["guardrails:",
           "  # ⚠ child 不繼承 parent 的 guardrails.policies（實測）⇒ 每個 worker 自帶完整",
           "  #   policy，不能靠 leader 罩下來。",
           "  ask_timeout: 86400",
           "  policies:"]
    body = [P_BLAST]
    if w["readonly"]:
        body.append(P_READONLY)
    if worker in ("implementer_cc", "implementer_pi"):
        body += [P_WORKTREE, P_WORKDIR]
    if worker == "ops_k8s":
        body.append(P_KUBECTL)
    body += [P_BOARD, P_MERGE, P_APPROVE, P_TAG]
    body += P_SAFETY
    if w["harness"] == "claude-native":
        body.append(P_COST)
    return "\n".join(pol + body)


def leader_config(leader: str) -> str:
    cfg = LEADERS[leader]
    via = str(cfg["via"])
    agents = [thin_name(w, via) for w in WORKERS]
    pol = ["guardrails:", "  ask_timeout: 86400", "  policies:"]
    body = P_LEADER_ORCH + [P_BOARD, P_MODEL, P_MERGE, P_APPROVE, P_TAG] + P_SAFETY
    if cfg["harness"] == "claude-native":
        body.append(P_COST)
    return "\n".join([
        GENERATED_HEADER,
        "spec_version: 1",
        f"name: {leader}",
        f"description: repo 層 leader（{cfg['harness']}）：把 issue 工單拆成 worker 任務、"
        "派工、收斂驗收，自己不寫產品程式碼。",
        "",
        _executor(str(cfg["harness"]), cfg["permission_mode"]),  # type: ignore[arg-type]
        "",
        _prompt_note("_shared/repo_leader/AGENTS.md"),
        "",
        "skills: all",
        OS_ENV_BLOCK.rstrip(),
        "",
        "tools:",
        "  # ⛔ 每個 thin shell 都必須列在這裡。這是斷鏈 symlink 的安全網：引用不存在的",
        "  #    sub-agent 會 loud fail，把「靜默少一個 sub-agent」變成明確錯誤。",
        "  #    列的是 YAML `name`，不是資料夾名（本樹刻意讓兩者相同）。",
        "  agents:",
        *[f"    - {a}" for a in agents],
        "",
        "\n".join(pol + body),
    ]) + "\n"


def issue_leader_config() -> str:
    pol = ["guardrails:", "  ask_timeout: 86400", "  policies:"]
    # ⚠ P-board 刻意**不掛**在 issue-leader 身上：全樹只有它能寫看板。
    body = P_LEADER_ORCH + [P_MODEL, P_MERGE, P_APPROVE, P_TAG] + P_SAFETY + [P_COST]
    return "\n".join([
        GENERATED_HEADER,
        "spec_version: 1",
        "name: woow_issue_leader",
        "description: woow 六 repo 的 issue 層總指揮：讀 issue、拆工單、派給兩個 repo_leader、"
        "收斂回 Gitea 與看板；自己不寫碼。",
        "",
        _executor("claude-native", "auto"),
        "",
        _prompt_note("./AGENTS.md（實體檔，非 symlink）"),
        "",
        "skills: all",
        OS_ENV_BLOCK.rstrip(),
        "",
        "tools:",
        "  agents:",
        "    - repo_leader_cc",
        "    - repo_leader_pi",
        "",
        "# ⚠ P-board 刻意不掛在這裡：全樹只有 woow_issue_leader 能寫 .woow-issues/board.md。",
        "\n".join(pol + body),
    ]) + "\n"


# ── 產生 ─────────────────────────────────────────────────────────────────────

def _write(path: Path, text: str, *, check: bool, diffs: list[str]) -> None:
    if check:
        current = path.read_text() if path.exists() else None
        if current != text:
            diffs.append(str(path.relative_to(OMNIGENT_DIR.parent)))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _symlink(link: Path, target: Path, *, check: bool, diffs: list[str]) -> None:
    """建相對 symlink。⛔ 一律用 os.path.relpath 推算，不要手數 `../`——斷鏈時
    `is_dir()` 回 False，會**靜默**少發現一個 sub-agent／少一批 skill。"""
    rel = os.path.relpath(target, link.parent)
    if check:
        if not link.is_symlink() or os.readlink(link) != rel:
            diffs.append(f"{link.relative_to(OMNIGENT_DIR.parent)} -> {rel}")
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(rel)


def build(*, check: bool = False) -> list[str]:
    diffs: list[str] = []
    _write(LEADER_DIR / "config.yaml", issue_leader_config(), check=check, diffs=diffs)
    for leader, cfg in LEADERS.items():
        base = LEADER_DIR / "agents" / leader
        _write(base / "config.yaml", leader_config(leader), check=check, diffs=diffs)
        _symlink(base / "AGENTS.md", SHARED_DIR / "repo_leader" / "AGENTS.md",
                 check=check, diffs=diffs)
        for worker in WORKERS:
            shell = base / "agents" / thin_name(worker, str(cfg["via"]))
            _write(shell / "config.yaml", worker_config(worker, str(cfg["via"])),
                   check=check, diffs=diffs)
            _symlink(shell / "AGENTS.md", WORKERS_DIR / worker / "AGENTS.md",
                     check=check, diffs=diffs)
            _symlink(shell / "skills", WORKERS_DIR / worker / "skills",
                     check=check, diffs=diffs)
    return diffs


# ── 驗收（--check）─────────────────────────────────────────────────────────────
#
# 每一項都機械驗證並印出結果。⛔ 第 8 項（CEL 雙向測試）不可省略：CEL 參照不存在的
# 欄位時是 **abstain → fail-OPEN**，守線會靜默不生效——沒有雙向測試，等於守線不存在。

DEFAULT_SITE_PACKAGES = (
    "/home/elmo/.local/share/uv/tools/omnigent/lib/python3.12/site-packages"
)


def _import_omnigent():
    sp = os.environ.get("OMNIGENT_SITE_PACKAGES", DEFAULT_SITE_PACKAGES)
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)
    import omnigent.spec as spec_mod  # noqa: PLC0415
    return spec_mod


def _walk(spec, path=""):
    """深度優先走訪整棵 spec 樹，yield (dotted-path, spec)。"""
    here = f"{path}.{spec.name}" if path else str(spec.name)
    yield here, spec
    for sa in spec.sub_agents:
        yield from _walk(sa, here)


# ── CEL 雙向測試向量 ─────────────────────────────────────────────────────────
# 每一支 CEL policy 都必須在這裡有向量，否則第 8 項直接判 FAIL（見 check_cel）。

def _shell(cmd: str, name: str = "Bash") -> dict:
    return {"type": "tool_call", "target": name,
            "data": {"name": name, "arguments": {"command": cmd}}}


def _write_ev(path: str, key: str = "file_path", name: str = "Edit") -> dict:
    return {"type": "tool_call", "target": name,
            "data": {"name": name, "arguments": {key: path}}}


def _send(agent: str, args: dict | None = None) -> dict:
    arguments: dict = {"agent": agent, "title": "t"}
    if args is not None:
        arguments["args"] = args
    return {"type": "tool_call", "target": "sys_session_send",
            "data": {"name": "sys_session_send", "arguments": arguments}}


CEL_VECTORS: dict[str, dict[str, list]] = {
    "p_board_read_only": {
        "deny": [
            _write_ev(".woow-issues/board.md"),
            _write_ev("./.woow-issues/board.md"),
            _write_ev("/home/elmo/woow-repos/paas/woow_paas_platform/.woow-issues/board.md"),
            _write_ev(".woow-issues/board.md", key="path", name="sys_os_write"),
            _write_ev(".woow-issues/board.md", key="path", name="edit"),
            _shell("echo x >> .woow-issues/board.md"),
            _shell("echo x > ./.woow-issues/board.md"),
            _shell("sed -i 's/a/b/' .woow-issues/board.md"),
            _shell("rm .woow-issues/board.md"),
            _shell("cp /tmp/new .woow-issues/board.md", name="sys_os_shell"),
        ],
        "allow": [
            _write_ev("docs/index.md"),
            _write_ev(".woow-issues/notes.md"),
            _write_ev("src/models/skill.py", key="path", name="sys_os_write"),
            _shell("cat .woow-issues/board.md"),
            _shell("cat .woow-issues/board.md 2>/dev/null"),
            _shell("grep -n TODO .woow-issues/board.md"),
            _shell("echo x >> docs/notes.md"),
            # 欄位缺席時不可 DENY（也不可因為讀不到欄位而 abstain 成漏擋）
            {"type": "tool_call", "target": "Edit",
             "data": {"name": "Edit", "arguments": {}}},
            {"type": "tool_call", "target": "Read",
             "data": {"name": "Read", "arguments": {"file_path": ".woow-issues/board.md"}}},
        ],
    },
    "p_model_fast_tier_only": {
        "deny": [
            _send("scout_pi_via_cc", {"purpose": "explore"}),          # 沒帶 model
            _send("scout_pi_via_cc"),                                   # 連 args 都沒有
            _send("tracker_via_pi", {"purpose": "triage"}),
            _send("researcher_via_cc", {"purpose": "search", "model": "claude-opus-5"}),
            _send("scout_pi_via_pi", {"purpose": "explore", "model": "opus"}),
        ],
        "allow": [
            _send("scout_pi_via_cc", {"purpose": "explore", "model": "claude-haiku-4-5"}),
            _send("tracker_via_pi", {"purpose": "triage", "model": "claude-sonnet-5"}),
            _send("researcher_via_cc", {"purpose": "search",
                                        "model": "databricks-claude-haiku-4-5"}),
            # 不受管的角色，不帶 model 也合法
            _send("impl_cc_via_cc", {"purpose": "implement"}),
            _send("ver_pi_via_cc", {"purpose": "review"}),
            _send("ops_k8s_via_pi", {"purpose": "deploy"}),
            _shell("echo hi"),
        ],
    },
    "p_merge_no_agent_merge": {
        "deny": [
            _shell("tea pr merge 1079"),
            _shell("tea pulls merge 1079"),
            _shell("gh pr merge 42 --squash"),
            _shell("glab mr merge 7"),
            _shell("cd /repo && tea  pr   merge 3", name="sys_os_shell"),
        ],
        "allow": [
            _shell("tea pr create --title x"),
            _shell("tea pulls list"),
            _shell("gh pr view 42"),
            _shell("git merge origin/develop"),
            _shell("echo 'merge is a human gate'"),
            {"type": "tool_call", "target": "Bash", "data": {"name": "Bash", "arguments": {}}},
        ],
    },
    "p_approve_no_agent_signoff": {
        "deny": [
            _shell("tea comment 823 '/approve'"),
            _shell('gh issue comment 42 --body "/approve"'),
            _shell("glab mr note 7 -m '/approve'"),
            _shell("curl -X POST -d '{\"body\":\"/approve\"}' https://git-prod.woowtech.io/api/v1/x"),
        ],
        "allow": [
            _shell("tea comment 823 '已完成，等人工核可'"),
            _shell("grep -rn '/approve' docs/"),
            _shell("cat .claude/commands/woow/deploy.md"),
            _shell("gh pr view 42"),
        ],
    },
    "p_tag_no_agent_tag_push": {
        "deny": [
            _shell("git push origin v1.7.6"),
            _shell("git push --tags"),
            _shell("git push origin refs/tags/v1.7.6"),
            _shell("git push origin operator-v2.0.1"),
            _shell("git push --follow-tags origin develop"),
        ],
        "allow": [
            _shell("git push origin develop"),
            _shell("git push -u origin feat/omnigent-agent-bundles"),
            _shell("git push origin fix/823-cp12"),
            _shell("git push origin release/v1.7.6"),   # 這是 branch，不是 tag
            _shell("git tag -l"),
            _shell("git log --oneline -1"),
        ],
    },
    "p_kubectl_namespace_scope": {
        "deny": [
            _shell("kubectl --context woow-k3s delete pod foo -n gitea"),
            _shell("kubectl --context woow-k3s apply -f x.yaml -n longhorn-system"),
            _shell("kubectl --context woow-k3s exec -it pod -n default -- sh"),
            # fail-closed：寫入類沒帶 -n 一律擋（不吃 current namespace）
            _shell("kubectl --context woow-k3s delete pod foo"),
            _shell("kubectl delete -f manifests/get-config.yaml --context woow-k3s"),
        ],
        "allow": [
            _shell("kubectl --context woow-k3s get pods -A"),
            _shell("kubectl get pods -n gitea --context woow-k3s"),
            _shell("kubectl --context woow-k3s describe pod foo -n longhorn-system"),
            _shell("kubectl --context woow-k3s delete pod foo -n paas-system"),
            _shell("kubectl --context woow-k3s apply -f x.yaml -n paas-ws-stg-demo"),
            _shell("kubectl --context woow-k3s rollout restart deploy/x -n dev-sandbox-abc"),
            _shell("kubectl --context woow-k3s delete gitrepo paas-app -n fleet-local"),
            _shell("kubectl --context docker-desktop delete pod foo -n whatever"),
            _shell("kubectl --context woow-k3s logs pod -n gitea"),
        ],
    },
}


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def item(self, n: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {n}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failed += 1

    def guard(self, n: str, fn) -> None:
        """跑一項檢查；例外一律轉成 FAIL。

        ⚠ 這不是裝飾用的：實測過「斷鏈 symlink 讓 materialize_bundle 直接拋例外」，
        沒有這層包裝的話後面每一項檢查都不會跑到，一個壞掉的檢查就能遮住其餘全部。
        """
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.item(n, ok, detail)


def run_checks() -> int:
    """跑完第 5 節的每一項驗收並印出結果。回傳 exit code。

    ⚠ 順序刻意先做**結構**檢查（斷鏈 symlink / skills 數）再做 materialize：斷鏈的
    symlink 會讓 materialize_bundle 直接拋例外，先驗結構才看得到真正的病因。
    """
    spec_mod = _import_omnigent()
    r = Report()

    # 0. 產物是否與 build-bundles.py 同步（等同 gen-docs-index.py --check 的角色）
    def c0():
        diffs = build(check=True)
        return not diffs, ("已同步" if not diffs
                           else f"{len(diffs)} 個不同步: {diffs[:5]}")
    r.guard("0. 產物與 build-bundles.py 同步", c0)

    # 7. 斷鏈 symlink 數為 0（提前跑：斷鏈是其他檢查的病因）
    all_links = [p for p in OMNIGENT_DIR.rglob("*") if p.is_symlink()]
    broken = [str(p.relative_to(OMNIGENT_DIR)) for p in all_links if not p.exists()]
    r.item("7. 斷鏈 symlink == 0", not broken,
           f"{len(all_links)} 個 symlink，0 斷鏈" if not broken else str(broken))

    # 1. 原始樹 parse + validate
    spec = spec_mod.parse(LEADER_DIR, expand_env=False)
    res = spec_mod.validate(spec)
    r.item("1. parse() + validate()（原始樹）", not res.errors,
           "errors=[]" if not res.errors
           else "; ".join(f"{e.path}: {e.message}" for e in res.errors[:6]))
    nodes = dict(_walk(spec))

    # 4. 每個 thin shell 的 skills 數 == 來源池對應 worker 的 skill 數
    #    ⚠ 這一項是「skills symlink 指到沒有 SKILL.md 的目錄 → 靜默為空」的唯一守線：
    #    實測 validate() 對斷鏈 skills symlink 完全不會紅。
    def c4():
        src_counts = {
            w: len(list((WORKERS_DIR / w / "skills").glob("*/SKILL.md"))) for w in WORKERS
        }
        bad = []
        for worker in WORKERS:
            for via in ("cc", "pi"):
                n = thin_name(worker, via)
                sp_ = next((x for k, x in nodes.items() if k.endswith(f".{n}")), None)
                if sp_ is None:
                    bad.append(f"{n}: 樹上找不到")
                elif len(sp_.skills) != src_counts[worker]:
                    bad.append(f"{n}: {len(sp_.skills)} != 來源池 {src_counts[worker]}")
        return not bad, (f"來源池={src_counts}" if not bad else "; ".join(bad[:5]))
    r.guard("4. thin shell skills 數 == 來源池 skill 數", c4)

    # 2. materialize_bundle 後：0 個 symlink，且 re-validate 全綠
    def c2():
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "bundle"
            spec_mod.materialize_bundle(LEADER_DIR, dest)
            links = [q for q in dest.rglob("*") if q.is_symlink()]
            if links:
                return False, f"複製後仍有 {len(links)} 個 symlink"
            mspec = spec_mod.parse(dest, expand_env=False)
            mres = spec_mod.validate(mspec)
            if mres.errors:
                return False, "; ".join(f"{e.path}: {e.message}" for e in mres.errors[:6])
            mnodes = dict(_walk(mspec))
            if len(mnodes) != len(nodes):
                return False, f"複製後 agent 數 {len(mnodes)} != 原始樹 {len(nodes)}"
            return True, (f"symlinks=0、errors=[]、{len(mnodes)} 個 agent 全數複製")
    r.guard("2. materialize_bundle 後 0 symlink + re-validate 全綠", c2)

    # 3. 每個 leaf agent 的 instructions 非空、含 marker、且不等於任何檔案路徑字串
    def c3():
        bad = []
        for dotted, sp_ in nodes.items():
            ins = sp_.instructions
            if not ins or not ins.strip():
                bad.append(f"{dotted}: instructions 空")
                continue
            if "omnigent-prompt-marker:" not in ins:
                bad.append(f"{dotted}: 缺 marker")
            # ⛔ 靜默失敗的形狀：整條路徑字串被當成 system prompt
            body = ins.strip()
            if "\n" not in body and (body.endswith(".md") or "/" in body):
                bad.append(f"{dotted}: instructions 看起來是路徑字串 {body!r}")
        return not bad, (f"檢查 {len(nodes)} 個 agent 全數帶 marker"
                         if not bad else "; ".join(bad[:5]))
    r.guard("3. instructions 非空 + 含 marker + 不是路徑字串", c3)

    # 5. agent name 唯一 + 正則；skill name 正則 + 全域唯一
    import re as _re  # noqa: PLC0415

    def c5a():
        names = [sp_.name for sp_ in nodes.values()]
        dup = {n for n in names if names.count(n) > 1}
        bad = [n for n in names if not n or not _re.match(r"^[a-zA-Z0-9_-]+$", str(n))]
        return (not dup and not bad,
                f"{len(names)} 個 name，0 重複" if not dup and not bad
                else f"dup={dup} bad={bad}")
    r.guard("5a. agent name 唯一且符合 ^[a-zA-Z0-9_-]+$", c5a)

    def c5b():
        pool = []
        for w in WORKERS:
            pool += [f.parent.name for f in sorted((WORKERS_DIR / w / "skills").glob("*/SKILL.md"))]
        pool += [f.parent.name for f in sorted((LEADER_DIR / "skills").glob("*/SKILL.md"))]
        parsed = sorted({sk.name for sp_ in nodes.values() for sk in sp_.skills})
        bad = [n for n in parsed if not _re.match(r"^[a-z0-9-]+$", n)]
        dup = {n for n in pool if pool.count(n) > 1}
        return (not bad and not dup,
                f"{len(pool)} 份實體 skill、{len(parsed)} 個不重複 name，0 重複"
                if not bad and not dup else f"bad={bad} dup={dup}")
    r.guard("5b. skill name 符合 ^[a-z0-9-]+$ 且來源全域唯一", c5b)

    # 6. 每個 thin shell 都出現在上層 tools.agents
    def c6():
        bad = []
        n_ref = 0
        for leader, cfg in LEADERS.items():
            ls = next((x for k, x in nodes.items() if k.endswith(f".{leader}")), None)
            if ls is None:
                bad.append(f"{leader}: 樹上找不到")
                continue
            listed = set(ls.tools.agents)
            for worker in WORKERS:
                n = thin_name(worker, str(cfg["via"]))
                n_ref += 1
                if n not in listed:
                    bad.append(f"{leader}.tools.agents 缺 {n}")
        if set(spec.tools.agents) != set(LEADERS):
            bad.append(f"woow_issue_leader.tools.agents={spec.tools.agents}")
        n_ref += len(LEADERS)
        return not bad, (f"{n_ref} 個引用全中" if not bad else "; ".join(bad[:5]))
    r.guard("6. 每個 thin shell 都列在上層 tools.agents", c6)

    # 8. 每一支 CEL 運算式雙向測試
    r.guard("8. CEL 雙向測試（違規必 DENY／合法必不 DENY）", lambda: _check_cel(nodes))

    # 9. 每支內建 policy 的 function.path 實跑 resolve
    r.guard("9. 內建 policy function.path resolve", lambda: _check_policy_paths(nodes))

    print()
    print(f"總計：{r.failed} 項 FAIL" if r.failed else "總計：全部 PASS")
    return 1 if r.failed else 0


def _cel_policies(nodes) -> dict[str, str]:
    """從**已 parse 的樹**收集所有 CEL 運算式（不是從原始碼猜）。"""
    out: dict[str, str] = {}
    for s in nodes.values():
        if not s.guardrails or not s.guardrails.policies:
            continue
        for p in s.guardrails.policies:
            fn = getattr(p, "function", None)
            if fn and fn.path.endswith("cel.cel_policy"):
                out[p.name] = fn.arguments["expression"]
    return out


def _check_cel(nodes) -> tuple[bool, str]:
    sp = os.environ.get("OMNIGENT_SITE_PACKAGES", DEFAULT_SITE_PACKAGES)
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)
    from omnigent.policies.builtins.cel import cel_policy  # noqa: PLC0415

    exprs = _cel_policies(nodes)
    bad: list[str] = []
    tested = 0
    missing = sorted(set(exprs) - set(CEL_VECTORS))
    if missing:
        # ⛔ 沒有向量的 CEL policy 直接判 FAIL：守線 fail-OPEN，沒測等於不存在。
        bad.append(f"缺雙向測試向量: {missing}")
    for name, expr in sorted(exprs.items()):
        if name not in CEL_VECTORS:
            continue
        evaluate = cel_policy(expression=expr, reason="policy")
        for ev in CEL_VECTORS[name]["deny"]:
            tested += 1
            got = evaluate(ev)
            if not got or got.get("result") != "DENY":
                bad.append(f"{name}: 違規事件未被 DENY → {got!r} :: "
                           f"{ev['data'].get('arguments')}")
        for ev in CEL_VECTORS[name]["allow"]:
            tested += 1
            got = evaluate(ev)
            if got and got.get("result") == "DENY":
                bad.append(f"{name}: 合法事件被 DENY → "
                           f"{ev['data'].get('arguments')}")
    ok = not bad
    detail = (f"{len(exprs)} 支運算式 / {tested} 個事件全部符合預期" if ok
              else f"{len(bad)} 個失敗: " + "; ".join(bad[:6]))
    return ok, detail


def _check_policy_paths(nodes) -> tuple[bool, str]:
    sp = os.environ.get("OMNIGENT_SITE_PACKAGES", DEFAULT_SITE_PACKAGES)
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)
    from omnigent.policies.function import resolve_function_policy  # noqa: PLC0415

    seen: dict[str, object] = {}
    bad: list[str] = []
    for s in nodes.values():
        if not s.guardrails or not s.guardrails.policies:
            continue
        for p in s.guardrails.policies:
            fn = getattr(p, "function", None)
            if not fn or fn.path in seen:
                continue
            seen[fn.path] = True
            try:
                resolve_function_policy(p)
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{fn.path}: {type(exc).__name__}: {exc}")
    ok = not bad
    return ok, (f"{len(seen)} 個不重複 function.path 全部 resolve 成功" if ok
                else "; ".join(bad[:5]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="只驗收，不寫檔（產物不同步會判 FAIL）")
    args = ap.parse_args()
    if args.check:
        return run_checks()
    diffs = build(check=False)
    n_shells = len(WORKERS) * len(LEADERS)
    print(f"已產生 {n_shells} 個 thin shell、{n_shells + len(LEADERS) + 1} 份 config.yaml、"
          f"{n_shells * 2 + len(LEADERS)} 條 symlink。")
    print("接著跑：python3 .omnigent/build-bundles.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())

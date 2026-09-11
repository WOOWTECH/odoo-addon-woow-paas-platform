#!/usr/bin/env python3
"""產生 woow multi-agent bundle 的 16 個 thin shell 與全部 config.yaml，並自檢。

用法::

    python3 .omnigent/build-bundles.py           # 重產（generate）
    python3 .omnigent/build-bundles.py --check   # 只驗收，不寫檔（CI／PR 用）

樹形：`woow-issue-leader` → 2 個 `repo_leader` → 各 8 個 worker thin shell。
thin shell 的 `config.yaml`、`AGENTS.md` 與 `skills/` **全部都是實體檔**，由本 script 從
`_workers/<worker>/` 的單一來源**複製**出來。⛔ 不要手改副本——改來源再重跑本 script。

⚠ 本檔是全樹**所有** `config.yaml` 與所有 CEL 運算式的唯一來源。config.yaml 是產物，
   ⛔ 不要手改；改這裡再重跑。

── 為什麼寫成這樣（每一條都是實測過的靜默失敗，不要「優化」掉）────────────────

1. **prompt 只能用同目錄的 `AGENTS.md` 實體檔承載**。⛔ 禁止改寫成
   `instructions: <相對路徑>`：`spec/parser.py::_read_contained_file` 的 containment 檢查
   失敗時會回 None，接著**整條路徑字串被當成 system prompt**，而且不報錯。
2. **每個 thin shell 的 `AGENTS.md` 與 `skills/` 都是實體副本，不是 symlink**。
   `omnigent run <目錄>` 的打包器不解 symlink（symlink 目錄會被 `rglob` 跳過，整批 skills
   **靜默**消失）⇒ 版控裡一條 symlink 都不能有。驗收第 7／12 項機器強制。
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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OMNIGENT_DIR = Path(__file__).resolve().parent
SHARED_DIR = OMNIGENT_DIR / "_shared"
LEADER_DIR = OMNIGENT_DIR / "woow-issue-leader"
TEMPLATES_DIR = LEADER_DIR / "_templates"

GENERATED_HEADER = (
    "# ⚠ 由 .omnigent/build-bundles.py 產生——⛔ 不要手改。\n"
    "#   改 build-bundles.py 再重跑：python3 .omnigent/build-bundles.py\n"
)

# ── 共用定義：一律從 bundle 內的 generate-worker.py 取回 ──────────────────────
#
# 🔴 CEL 運算式、policy 片段、對照表、`os_env` 區塊全部住在
#    `woow-issue-leader/generate-worker.py`，因為**那支要在執行期跑**（repo-leader 派工前
#    呼叫它寫 worker config），而本檔不在 bundle 內、執行期拿不到。
#    真相放在執行期拿得到的那一邊，本檔 import 過來用 ⇒ 只有一份。
def _load_generator():
    """載入 generate-worker.py（檔名有 `-`，不能直接 import）。"""
    import importlib.util  # noqa: PLC0415

    path = LEADER_DIR / "generate-worker.py"
    spec = importlib.util.spec_from_file_location("_omnigent_generate_worker", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法載入 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = _load_generator()

# 以下全部轉指向 generate-worker.py 的定義（⛔ 不要在本檔重新定義，那就是第二份真相）
SHELL_TOOLS = GEN.SHELL_TOOLS
WRITE_TOOLS = GEN.WRITE_TOOLS
FAST_MODELS = GEN.FAST_MODELS
K8S_ALLOWED_NAMESPACES = GEN.K8S_ALLOWED_NAMESPACES
OS_ENV_BLOCK = GEN.OS_ENV_BLOCK
ASK_TIMEOUT = GEN.ASK_TIMEOUT
ROSTER = GEN.ROSTER
P_SAFETY, P_COST = GEN.P_SAFETY, GEN.P_COST
P_SAFETY_LEADER, P_COST_DAILY = GEN.P_SAFETY_LEADER, GEN.P_COST_DAILY
P_MERGE, P_APPROVE, P_TAG, P_ESCAPE = GEN.P_MERGE, GEN.P_APPROVE, GEN.P_TAG, GEN.P_ESCAPE
P_BOARD, P_MODEL, P_KUBECTL = GEN.P_BOARD, GEN.P_MODEL, GEN.P_KUBECTL
P_LEADER_ORCH, P_BLAST, P_READONLY = GEN.P_LEADER_ORCH, GEN.P_BLAST, GEN.P_READONLY
P_WORKTREE = GEN.P_WORKTREE
# ⚠ 沒有 P_WORKDIR：#1092 起 block_working_dir_changes 改由 `GEN._p_workdir(worktree)` 在
#   執行期依 --worktree 填 allowed_dirs，不再是模組常數；leader 層本來就不掛這條 policy。
_fn_policy, _cel_yaml = GEN._fn_policy, GEN._cel_yaml
DEFAULT_SITE_PACKAGES = GEN.DEFAULT_SITE_PACKAGES



# ── worker 名冊 ──────────────────────────────────────────────────────────────
# key = `_workers/` 底下的來源目錄名；`short` = thin shell 名字的前半。
#: 兩份 repo-leader。職責相同，差別只有 harness／額度池／vendor 方向。
#: model 與 effort 的真相在 generate-worker.py 的 ROSTER["repo-leader"]。
LEADERS = {
    "repo_leader_cc": {"harness": "claude-native", "via": "cc", "permission_mode": "auto"},
    "repo_leader_pi": {"harness": "pi", "via": "pi", "permission_mode": None},
}


def _executor(harness: str, permission_mode: str | None, *,
              model: str | None = None, effort: str | None = None) -> str:
    out = ["executor:", "  type: omnigent"]
    if effort:
        out.append(f"  reasoning_effort: {effort}")
    if model:
        out.append(f"  model: {model}")
    out += ["  config:", f"    harness: {harness}"]
    if permission_mode:
        out += [
            "    # headless 的 leader 沒人能回答 ApprovalCard，而 managed Claude settings",
            "    # 停用 bypassPermissions；`auto` 自動放行且在 managed settings 下合法。",
            f"    permission_mode: {permission_mode}",
        ]
    return "\n".join(out)


def _prompt_note(source: str) -> str:
    return (
        f"# prompt 由同目錄的 AGENTS.md 承載（實體檔，來源：{source}）。\n"
        "# ⛔ 不要手改產物——改來源再跑 build-bundles.py（drift 由 --check 第 0 項對帳）。\n"
        "# ⛔ 不要改寫成 `instructions: <相對路徑>`：containment 檢查失敗時會回 None，\n"
        "#    接著整條路徑字串被當成 system prompt，而且不報錯。"
    )


def _spawn_note() -> str:
    return (
        "# spawn: true 是這個架構的關鍵——worker 不再是靜態 sub-agent，而是由本層在執行期\n"
        "# 用 generate-worker.py 寫出 config 後以 sys_session_create(config_path=…) 起來。\n"
        "# ⚠ 實測：動態 config 的 harness / model / guardrails.policies / spawn 全部生效，\n"
        "#   且**不需要**在 tools.agents 列名（那套靜態 allowlist 已廢）。\n"
        "spawn: true"
    )


def leader_config(leader: str) -> str:
    """repo-leader 的 config.yaml。它自己不寫產品碼，只拆工、產生 worker、收斂。"""
    cfg = LEADERS[leader]
    harness = str(cfg["harness"])
    model, effort = ROSTER["repo-leader"]["models"][harness]
    pol = ["guardrails:", f"  ask_timeout: {ASK_TIMEOUT}", "  policies:"]
    body = P_LEADER_ORCH + [P_BOARD, P_MODEL, P_MERGE, P_APPROVE, P_TAG, P_ESCAPE] + P_SAFETY
    if harness == "claude-native":
        body.append(P_COST)
    return "\n".join([
        GENERATED_HEADER,
        "spec_version: 1",
        f"name: {leader}",
        f"description: repo 層 leader（{harness}）：把 issue 工單拆成 worker 任務、"
        "在執行期產生 worker config 並派工、收斂驗收；自己不寫產品程式碼。",
        "",
        _executor(harness, cfg["permission_mode"], model=model, effort=effort),
        "",
        _spawn_note(),
        "",
        _prompt_note("_shared/repo_leader/AGENTS.md"),
        "",
        "# ⛔ 一律固定 `all`，絕不可設 `none`——`none` 會發出 --setting-sources \"\"，",
        "#    壓掉 dispatcher 與所有 repo hooks（inner/bundle_skills.py）。",
        "skills: all",
        OS_ENV_BLOCK.rstrip(),
        "",
        "\n".join(pol + body),
    ]) + "\n"


def issue_leader_config() -> str:
    """issue 層總指揮。workspace 是六 repo 的共同祖先，它持有 alias map。"""
    pol = ["guardrails:", f"  ask_timeout: {ASK_TIMEOUT}", "  policies:"]
    # ⚠ P-board 刻意**不掛**在 issue-leader 身上：全樹只有它能寫看板。
    # ⚠ 頂層是長時間運作的單一 session ⇒ 安全網用 P_SAFETY_LEADER（不含整 session 累計的
    #    max_tool_calls_per_session）、花費用 P_COST_DAILY（owner 當日總額，每天歸零），
    #    理由見 generate-worker.py 該兩個常數的註解。repo-leader／worker 仍用 per-session 版。
    body = (P_LEADER_ORCH + [P_MODEL, P_MERGE, P_APPROVE, P_TAG, P_ESCAPE]
            + P_SAFETY_LEADER + [P_COST_DAILY])
    # ⛔ 頂層 leader 的 harness 不可以是任何 `*-native`——Omnigent Web UI 的
    #    Agents → Custom agents 子選單會把它**整個過濾掉**（實測：同一組實驗只差
    #    harness 一欄，`pi` 出現、`claude-native` 不出現）。根因在
    #    `web/src/hooks/useAvailableAgents.ts` 的 enrich：custom agent 解析出的
    #    native key 若與清單中某個 builtin 相同就自我剔除，而 `claude-native`
    #    的 key `claude` 正好撞上 seeded builtin `claude-native-ui`。
    #    `claude-sdk` 不在 native 表內 ⇒ 選得到（polly／debby 亦同）。第 17 項是 gate。
    # ⚠ model 仍取 claude 家族那一格：claude-sdk 與 claude-native 同屬 claude 家族，
    #    都只吃 id 含 `claude` 的 model（`claude-fable-5-1` 合法）。
    model, effort = ROSTER["repo-leader"]["models"]["claude-native"]
    return "\n".join([
        GENERATED_HEADER,
        "spec_version: 1",
        "name: woow_issue_leader",
        "description: woow 六 repo 的 issue 層總指揮：讀 issue、拆工單、派給兩個 "
        "repo_leader、收斂回 Gitea 與看板；自己不寫碼。",
        "",
        _executor("claude-sdk", "auto", model=model, effort=effort),
        "",
        _spawn_note(),
        "",
        _prompt_note("./AGENTS.md（手改，非產物）"),
        "",
        "skills: all",
        OS_ENV_BLOCK.rstrip(),
        "",
        "tools:",
        "  # ⚠ 只列兩個 repo-leader。worker **不在這裡**——它們在執行期由 repo-leader 的",
        "  #   generate-worker.py 寫出、用 sys_session_create 起來（不需要 allowlist）。",
        "  agents:",
        "    - repo_leader_cc",
        "    - repo_leader_pi",
        "",
        "# ⚠ P-board 刻意不掛在這裡：全樹只有 woow_issue_leader 能寫 .woow-issues/board.md。",
        "\n".join(pol + body),
    ]) + "\n"


def _write(path: Path, text: str, *, check: bool, diffs: list[str]) -> None:
    if check:
        current = path.read_text() if path.exists() else None
        if current != text:
            diffs.append(str(path.relative_to(OMNIGENT_DIR.parent)))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _copy_file(dest: Path, source: Path, *, check: bool, diffs: list[str]) -> None:
    """把來源池的檔案複製成**實體副本**。

    🔴 這裡原本是 symlink，FIX ROUND 4 改成實體副本。理由是失效模式的性質：
    symlink 方案有一個**靜默**失效——`omnigent run <目錄>` 的打包器不解 symlink，
    symlink 目錄（`skills/`）會被 `rglob` 跳過，tar 內 SKILL.md 從 19 筆掉到 3 筆，
    而 `validate` 照樣回 valid、那些 agent 的 `skills` 變 `[]`，不報錯。
    實體副本的失效模式是 drift（有人手改其中一份副本），而 drift 由 `--check` 第 0 項
    機械對帳、**會紅、看得見**。用「會紅的風險」換掉「會靜默的風險」。

    ⚠ 所以第 0 項是這個方案的唯一守線：它比對的是**內容**（不是 mtime、不是大小），
    任何一份副本被手改都會讓它紅。突變測試 Q 實證這件事。
    """
    # ⚠ 一律走 bytes：副本裡可能有非 UTF-8 的檔案（hooks/ 的 .pyc 等），read_text 會炸。
    data = source.read_bytes()
    if check:
        current = dest.read_bytes() if dest.is_file() else None
        if current != data:
            diffs.append(str(dest.relative_to(OMNIGENT_DIR.parent)))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _copy_tree(dest: Path, source: Path, *, check: bool, diffs: list[str]) -> None:
    """把來源池的整個目錄複製成實體副本（逐檔比對內容；多餘的檔案也算 drift）。"""
    def _files(root: Path) -> dict[Path, Path]:
        # ⚠ 跳過 __pycache__：它是執行產生的，會讓對帳永遠不同步。
        return {q.relative_to(root): q for q in sorted(root.rglob("*"))
                if q.is_file() and "__pycache__" not in q.parts}

    wanted = _files(source)
    if check:
        have = set(_files(dest)) if dest.is_dir() else set()
        for rel, src_file in wanted.items():
            target = dest / rel
            if not target.is_file() or target.read_bytes() != src_file.read_bytes():
                diffs.append(str(target.relative_to(OMNIGENT_DIR.parent)))
        for extra in sorted(have - set(wanted)):
            diffs.append(f"{(dest / extra).relative_to(OMNIGENT_DIR.parent)}（多餘檔案）")
        return
    if dest.is_dir():
        shutil.rmtree(dest)
    for rel, src_file in wanted.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src_file.read_bytes())


def build(*, check: bool = False) -> list[str]:
    """產生三份 config 與 repo-leader 的 prompt／skills／hooks 副本。

    ⚠ worker **不在這裡產生**——它們在執行期由 generate-worker.py 寫出。本檔只負責
    靜態的三層骨幹：issue-leader ＋ 兩份 repo-leader。
    """
    diffs: list[str] = []
    _write(LEADER_DIR / "config.yaml", issue_leader_config(), check=check, diffs=diffs)
    for leader in LEADERS:
        base = LEADER_DIR / "agents" / leader
        _write(base / "config.yaml", leader_config(leader), check=check, diffs=diffs)
        _copy_file(base / "AGENTS.md", SHARED_DIR / "repo_leader" / "AGENTS.md",
                   check=check, diffs=diffs)
        # repo-leader 自己也需要 skills/ ＋ hooks/：
        #   `skills/` 存在才會發 --plugin-dir，hooks/hooks.json 才會被 claude 讀到
        #   （兩者缺一不可，見 inner/bundle_skills.py）。
        _copy_tree(base / "skills", LEADER_DIR / "skills", check=check, diffs=diffs)
        if LEADERS[leader]["harness"] == "claude-native":
            _copy_tree(base / "hooks", LEADER_DIR / "hooks", check=check, diffs=diffs)
    return diffs


def _import_omnigent():
    sp = os.environ.get("OMNIGENT_SITE_PACKAGES", DEFAULT_SITE_PACKAGES)
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)
    import omnigent.spec as spec_mod  # noqa: PLC0415
    return spec_mod


def _skill_frontmatter_name(skill_md: Path) -> str:
    """讀一支 SKILL.md 的 frontmatter `name`。

    ⚠ skill 名的**唯一**權威是 frontmatter，目錄名一律被忽略（`_parse_skill`）。查重必須
    看這個欄位——看目錄名會漏掉「不同目錄、同 frontmatter name」的 first-wins 吞噬。
    """
    try:
        text = skill_md.read_text()
    except OSError as exc:
        return f"<{skill_md} 讀取失敗: {exc}>"
    if not text.startswith("---"):
        return f"<{skill_md} 缺 frontmatter>"
    _, _, rest = text.partition("---")
    front, _, _ = rest.partition("\n---")
    for line in front.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip()
    return f"<{skill_md} frontmatter 缺 name>"


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
    # ⚠ 🔴 標記的向量＝FIX ROUND 1 覆核者**實跑證明可繞過**的 payload。它們現在是回歸
    #    測試：任何一條再度回到 ALLOW，第 8 項就會紅。
    "p_board_read_only": {
        "deny": [
            _write_ev(".woow-issues/board.md"),
            _write_ev("./.woow-issues/board.md"),
            _write_ev("/home/elmo/woow-repos/paas/woow_paas_platform/.woow-issues/board.md"),
            _write_ev(".woow-issues/board.md", key="path", name="sys_os_write"),
            _write_ev(".woow-issues/board.md", key="path", name="edit"),
            # 看板目錄整個唯讀，不只 board.md
            _write_ev(".woow-issues/notes.md"),
            _shell("echo x >> .woow-issues/board.md"),
            _shell("echo x > ./.woow-issues/board.md"),
            _shell("sed -i 's/a/b/' .woow-issues/board.md"),
            _shell("rm .woow-issues/board.md"),
            _shell("cp /tmp/new .woow-issues/board.md", name="sys_os_shell"),
            # 🔴 覆核者實跑：heredoc 餵給直譯器——枚舉寫入動詞永遠列不完
            _shell("python3 - <<'PY'\nopen('.woow-issues/board.md','w').write('x')\nPY"),
            _shell("perl -e 'open(F,\">\",\".woow-issues/board.md\"); print F \"x\"'"),
            _shell("node -e \"require('fs').writeFileSync('.woow-issues/board.md','x')\""),
            _shell("sh -c \"echo x > .woow-issues/board.md\""),
            _shell("cat <<'EOF' > .woow-issues/board.md\nx\nEOF"),
            _shell("tee .woow-issues/board.md <<< x"),
            # 唯讀動詞開頭但串接了寫入——無法證明安全
            _shell("cat .woow-issues/board.md && echo x > .woow-issues/board.md"),
            _shell("grep x .woow-issues/board.md; rm .woow-issues/board.md"),
        ],
        "allow": [
            _write_ev("docs/index.md"),
            _write_ev("src/models/skill.py", key="path", name="sys_os_write"),
            _shell("cat .woow-issues/board.md"),
            _shell("cat .woow-issues/board.md 2>/dev/null"),
            _shell("grep -n TODO .woow-issues/board.md"),
            _shell("head -50 .woow-issues/board.md"),
            _shell("git diff .woow-issues/board.md"),
            _shell("ls -la .woow-issues/"),
            _shell("echo x >> docs/notes.md"),
            # 欄位缺席時不可 DENY，也不可因為讀不到欄位而 abstain 成漏擋
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
            _shell("tea pulls  merge  1082"),
            _shell("gh pr merge 42 --squash"),
            _shell("glab mr merge 7"),
            _shell("cd /repo && tea  pr   merge 3", name="sys_os_shell"),
            # 🔴 REST API 直打 merge endpoint——舊版只認 `<cli> pr merge` 三種字面組合
            _shell("gh api -X PUT repos/o/r/pulls/1/merge"),
            _shell("gh api --method PUT /repos/o/r/pulls/1082/merge"),
            _shell("curl -X PUT https://git-prod.woowtech.io/api/v1/repos/o/r/pulls/1/merge"),
            _shell("sh -c 'gh pr merge 42'"),
            _shell("echo go && gh pr merge 42"),
        ],
        "allow": [
            _shell("tea pr create --title x"),
            _shell("tea pulls list"),
            _shell("gh pr view 42"),
            _shell("git merge origin/develop"),      # 本機分支整併，非 forge merge
            _shell("git merge --abort"),
            _shell("echo 'merge is a human gate'"),
            {"type": "tool_call", "target": "Bash", "data": {"name": "Bash", "arguments": {}}},
        ],
        "known_gap": [
            _shell("M=merge; gh pr $M 42"),                   # 子命令存在變數裡
            _shell("curl -X PUT \"$API/pulls/1/$(printf 'me'; printf 'rge')\""),
        ],
    },
    "p_approve_no_agent_signoff": {
        "deny": [
            _shell("tea comment 823 '/approve'"),
            _shell('gh issue comment 42 --body "/approve"'),
            _shell("glab mr note 7 -m '/approve'"),
            _shell("curl -X POST -d '{\"body\":\"/approve\"}' https://git-prod.woowtech.io/api/v1/x"),
            # 🔴 覆核者實跑：舊版的「且匹配 tea/gh/glab/curl」連言擋不住這兩條
            _shell("gh api repos/o/r/issues/1/comments -f body=/approve"),
            _shell("tea comments create 823 '/approve'"),
            _shell("echo /approve | tea comment 823 -"),
            _shell("printf '/approve' > /tmp/c && tea comment 823 -F /tmp/c"),
            # 過度擋（刻意）：worker 沒有任何合法理由打出這個字串
            _shell("grep -rn '/approve' docs/"),
        ],
        "allow": [
            _shell("tea comment 823 '已完成，等人工核可'"),
            _shell("cat .claude/commands/woow/deploy.md"),
            _shell("gh pr view 42"),
        ],
        # ⚠ known_gap：字串比對擋不住、且**刻意不修**的向量。威脅模型是「防 agent 犯錯」，
        #    不是「防 agent 攻擊」——LLM 不會為了規避一道它不知道存在的守線而這樣寫。
        #    留在這裡是為了：(1) 誠實記錄漏在哪 (2) 哪天行為變了會被講出來。
        "known_gap": [
            _shell("echo L2FwcHJvdmU= | base64 -d | tea comment 823 -F -"),
            _shell("tea comment 823 '/appr''ove'"),
            _shell("tea comment 823 $'\\x2f\\x61\\x70\\x70\\x72\\x6f\\x76\\x65'"),
            _shell("base64 -d /tmp/b.b64 > /tmp/b && tea comment 823 -F /tmp/b"),
        ],
    },
    "p_tag_no_agent_tag_push": {
        "deny": [
            _shell("git push origin v1.7.6"),
            _shell("git push --tags"),
            _shell("git push origin refs/tags/v1.7.6"),
            _shell("git push origin operator-v2.0.1"),
            _shell("git push --follow-tags origin develop"),
            _shell("git push origin HEAD:refs/tags/v9"),
            _shell("sh -c 'git push origin v1.7.6'"),
            _shell("echo ok && git push origin v1.7.6"),
            # 不經 git push 的建 tag／建 release 路徑
            _shell("gh api repos/o/r/git/refs -f ref=refs/tags/v1.0"),
            _shell("tea releases create --tag v1.7.6"),
            _shell("gh release create v1.7.6"),
            _shell("git tag v1.7.6"),
            # 🔴 FIX ROUND 3：tag 形態前面帶引號舊版滑過
            _shell('git push origin "v1.7.6"'),
            _shell("git push origin 'v1.7.6'"),
            _shell('git push origin "operator-v2.0.1"'),
        ],
        "allow": [
            _shell("git push origin develop"),
            _shell("git push -u origin feat/omnigent-agent-bundles"),
            _shell("git push origin fix/823-cp12"),
            _shell("git push origin release/v1.7.6"),   # 這是 branch，不是 tag
            _shell('git push origin "develop"'),         # 引號 branch 不得誤擋
            _shell("git push origin 'feat/omnigent-agent-bundles'"),
            _shell("git tag -l"),                        # 讀取用法（flag 開頭）
            _shell("git tag --list 'v1.*'"),
            _shell("git log --oneline -1"),
        ],
        "known_gap": [
            _shell("V=v1.7.6; git push origin $V"),          # 版號存在變數裡
            _shell("git push origin \"$(cat /tmp/tagname)\""),
        ],
    },
    "p_escape_obfuscated_command": {
        "deny": [
            _shell("kube\\ctl --context woow-k3s delete pod x -n gitea"),
            _shell("gh pr me\\rge 42"),
            _shell("git pu\\sh origin v1.0"),
            _shell("tea comment 823 '/appro\\ve'"),
            _shell("echo x > .woow-iss\\ues/board.md"),
            _shell("git push origin refs/ta\\gs/v1.0"),
        ],
        "allow": [
            _shell("echo hello"),
            _shell("grep -rn 'x' src/"),
            _shell("printf 'a\\tb'"),          # 有轉義但無敏感字根
            _shell("sed -e 's/a\\tb/x/' f.txt"),
            _shell("kubectl --context woow-k3s get pods -n paas-system"),  # 有字根但無轉義
            _shell("git push origin develop"),
        ],
    },
    "p_kubectl_namespace_scope": {
        "deny": [
            _shell("kubectl --context woow-k3s delete pod foo -n gitea"),
            _shell("kubectl --context woow-k3s apply -f x.yaml -n longhorn-system"),
            _shell("kubectl --context woow-k3s exec -it pod -n default -- sh"),
            _shell("kubectl --context woow-k3s delete pod foo"),
            _shell("kubectl delete -f manifests/get-config.yaml --context woow-k3s"),
            # 🔴 覆核者實跑的四條繞過
            _shell("kubectl --context woow-k3s get pods -A && kubectl --context woow-k3s delete pod x -n gitea"),
            _shell("kubectl --context woow-k3s delete pod x -n gitea && kubectl --context woow-k3s delete pod y -n paas-system"),
            _shell("kubectl --context woow-k3s delete pod get -n gitea"),
            _shell("kubectl --context woow-k3s delete pod x -n paas-system -n gitea"),
            _shell("kubectl delete pod x -n gitea"),                       # 缺 --context
            # 同型再補
            _shell("kubectl --context woow-k3s delete pod x -n paas-system; kubectl --context woow-k3s delete pod y -n gitea"),
            _shell("kubectl --context woow-k3s delete pod x -n paas-system | tee /tmp/l"),
            _shell("kubectl --context woow-k3s delete pod $(kubectl get pod -o name -n gitea) -n paas-system"),
            _shell("kubectl --context woow-k3s create ns evil"),
            _shell("kubectl --context woow-k3s scale deploy/x --replicas=0 -n gitea"),
            _shell("kubectl --context woow-k3s cp /tmp/x pod:/tmp -n gitea"),
            _shell("kubectl --context woow-k3s drain node-1"),
            # 🔴 1-2：manifest 內的 metadata.namespace 會覆蓋命令列的 -n，regex 看不到檔案內容
            _shell("kubectl --context woow-k3s apply -n paas-system -f /tmp/gitea.yaml"),
            _shell("kubectl --context woow-k3s create -f /tmp/x.yaml -n paas-system"),
            _shell("kubectl --context woow-k3s replace -f /tmp/x.yaml -n paas-system"),
            _shell("kubectl --context woow-k3s apply -k overlays/prod -n paas-system"),
            # 🔴 FIX ROUND 3：引號形式的 context（四種組合）舊版整個滑過
            _shell('kubectl --context "woow-k3s" delete pod x -n gitea'),
            _shell("kubectl --context='woow-k3s' delete pod x -n gitea"),
            _shell("kubectl --context 'woow-k3s' delete pod x -n gitea"),
            _shell('kubectl --context="woow-k3s" apply -f /tmp/x.yaml'),
            _shell('kubectl --context "woow-k3s" apply -f /tmp/x.yaml -n paas-system'),
            # 認不出來的 context 一律走嚴格分支（fail-closed，不是比對 woow-k3s 子字串）
            _shell("kubectl --context woow-k3s-prod delete pod x -n gitea"),
            _shell("kubectl --context some-unknown-cluster delete pod x -n gitea"),

        ],
        "allow": [
            _shell("kubectl --context woow-k3s get pods -A"),
            _shell("kubectl get pods -n gitea --context woow-k3s"),
            _shell("kubectl --context woow-k3s describe pod foo -n longhorn-system"),
            _shell("kubectl --context woow-k3s delete pod foo -n paas-system"),
            _shell("kubectl --context woow-k3s rollout restart deploy/x -n dev-sandbox-abc"),
            # 🔴 1-1：docker-desktop 是本機測試叢集，不套 woow-k3s 的白名單
            _shell("kubectl --context docker-desktop delete pod foo -n whatever"),
            _shell("kubectl --context docker-desktop apply -f x.yaml -n gitea"),
            # 🔴 FIX ROUND 3 回歸：本機 context 的引號形式一樣要放行
            _shell('kubectl --context "docker-desktop" delete pod x -n gitea'),
            _shell("kubectl --context='docker-desktop' apply -f x.yaml -n gitea"),
            # 引號形式的白名單 namespace 也要認得
            _shell('kubectl --context woow-k3s delete pod foo -n "paas-system"'),
            # -f 帶 --dry-run 可以（不會真的落地）
            _shell("kubectl --context woow-k3s apply -f x.yaml -n paas-ws-stg-demo --dry-run=server"),
            _shell("kubectl --context woow-k3s delete gitrepo paas-app -n fleet-local"),
            _shell("kubectl --context docker-desktop apply -f x.yaml -n dev-sandbox-abc"),
            _shell("kubectl --context woow-k3s logs pod -n gitea"),
            _shell("kubectl --context woow-k3s top pods -n longhorn-system"),
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

        ⚠ 這不是裝飾用的：實測過「壞掉的樹讓 materialize_bundle 直接拋例外」（當時是斷鏈
        symlink；symlink 已於 FIX ROUND 4 全面移除，但同類例外仍可能來自其他壞法），
        沒有這層包裝的話後面每一項檢查都不會跑到，一個壞掉的檢查就能遮住其餘全部。
        """
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.item(n, ok, detail)


def run_checks() -> int:
    """跑完第 5 節的每一項驗收並印出結果。回傳 exit code。

    ⚠ 順序刻意先做**結構**檢查（symlink 殘留 / skills 數）再做 materialize：結構壞掉時
    materialize_bundle 會直接拋例外，先驗結構才看得到真正的病因。
    """
    r = Report()
    try:
        spec_mod = _import_omnigent()
    except Exception as exc:  # noqa: BLE001
        r.item("(前置) 載入 omnigent.spec", False, f"{type(exc).__name__}: {exc}")
        print()
        print("總計：1 項 FAIL（omnigent 載不進來，無法驗收）")
        return 1

    # 0. 產物是否與 build-bundles.py 同步（等同 gen-docs-index.py --check 的角色）
    def c0():
        diffs = build(check=True)
        return not diffs, ("已同步" if not diffs
                           else f"{len(diffs)} 個不同步: {diffs[:5]}")
    r.guard("0. 產物與 build-bundles.py 同步", c0)

    # 7. 版控內不得有任何 symlink（FIX ROUND 4：改成實體副本後的硬性斷言）
    #    🔴 這一項取代了原本的「斷鏈 symlink == 0」。symlink 一條都不該有了——
    #    `omnigent run <目錄>` 的打包器不解 symlink，symlink 目錄會被 rglob 跳過而
    #    **靜默**掉整批 skills（實測：tar 內 SKILL.md 從 19 筆掉到 3 筆）。
    def c7():
        links = [str(q.relative_to(OMNIGENT_DIR)) for q in OMNIGENT_DIR.rglob("*")
                 if q.is_symlink()]
        n_files = sum(1 for q in OMNIGENT_DIR.rglob("*")
                      if q.is_file() and "__pycache__" not in q.parts)
        return not links, (f"0 條 symlink（{n_files} 個實體檔）" if not links
                           else f"仍有 {len(links)} 條 symlink：{links[:3]}")
    r.guard("7. 版控內 symlink == 0（實體副本）", c7)

    # 1. 原始樹 parse + validate
    #    ⚠ parse() 本身會對壞掉的 YAML 拋例外，必須包起來——否則一份手改壞的 config.yaml
    #    會用 traceback 中斷整輪驗收，後面每一項都不會跑到（實測 mutation K）。
    spec = None
    try:
        spec = spec_mod.parse(LEADER_DIR, expand_env=False)
        res = spec_mod.validate(spec)
        r.item("1. parse() + validate()（原始樹）", not res.errors,
               "errors=[]" if not res.errors
               else "; ".join(f"{e.path}: {e.message}" for e in res.errors[:6]))
    except Exception as exc:  # noqa: BLE001
        r.item("1. parse() + validate()（原始樹）", False,
               f"{type(exc).__name__}: {exc}")
    if spec is None:
        print()
        print(f"總計：{r.failed} 項 FAIL（原始樹 parse 失敗，其餘檢查無法進行）")
        return 1
    nodes = dict(_walk(spec))

    # 4. 產生器輸出自檢：每一種 purpose × harness 都能產出合法 worker，且反面案例被擋
    #    🔴 取代舊的「thin shell skills 數 == 來源池」——thin shell 已廢，worker 是執行期
    #    產生的，所以驗的是**產生器**而不是靜態產物。五道防線都在 generate-worker.py 內。
    def c4():
        ok, detail = GEN.self_check()
        return ok, detail
    r.guard("4. 產生器輸出自檢（每種 purpose × harness ＋ 反面案例）", c4)

    # 2. materialize_bundle 後仍然合法（FIX ROUND 4 後語意變了：樹裡本來就沒有 symlink
    #    了，所以這一項不再是「驗解引用」，而是驗「複製一份後 spec 仍等價」——
    #    `omnigent server --agent` 的 preregister 路徑會做這個複製）
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
    r.guard("2. materialize_bundle 後仍 0 symlink 且 re-validate 全綠", c2)

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
        # ⚠ 🔴 FIX ROUND 1：舊版用 `f.parent.name`（**目錄名**）查重是錯的欄位——
        #    skill 名一律取自 frontmatter，目錄名被忽略。兩個不同目錄、frontmatter 同名時
        #    `discover_host_skills` 會 first-wins 吃掉後者，而舊檢查完全看不到。
        #    這裡直接讀每支 SKILL.md 的 frontmatter `name`，保留 list（不是 set）再查重。
        pool: list[tuple[str, str]] = []   # (frontmatter name, 檔案路徑)
        template_skill_dirs = sorted(q for q in TEMPLATES_DIR.glob("*/skills") if q.is_dir())
        for src in [*template_skill_dirs, LEADER_DIR / "skills"]:
            for f in sorted(src.glob("*/SKILL.md")):
                pool.append((_skill_frontmatter_name(f), str(f.relative_to(OMNIGENT_DIR))))
        names = [n for n, _ in pool]
        bad = sorted({n for n in names if not _re.match(r"^[a-z0-9-]+$", n)})
        dup = sorted({n for n in names if names.count(n) > 1})
        # 目錄名與 frontmatter name 不一致本身不是錯，但會讓人讀錯樹 ⇒ 一併提醒。
        mismatch = [f"{path}: 目錄名 {path.split('/')[-2]!r} != frontmatter {n!r}"
                    for n, path in pool if path.split("/")[-2] != n]
        ok = not bad and not dup and not mismatch
        return ok, (f"{len(pool)} 份實體 skill 的 frontmatter name 全數唯一且合法"
                    if ok else f"bad={bad} dup={dup} mismatch={mismatch[:3]}")
    r.guard("5b. skill name 符合 ^[a-z0-9-]+$ 且來源全域唯一", c5b)

    # 6. tools.agents 只列兩個 repo-leader——worker **不該**在裡面
    #    🔴 取代舊的「每個 thin shell 都要列進 tools.agents」。新架構下 worker 由
    #    sys_session_create(config_path=…) 起來，**不需要** allowlist（實測確認）；
    #    若有人把 worker 名字加回 tools.agents，那是退回舊架構的徵兆 ⇒ 要紅。
    def c6():
        listed = set(spec.tools.agents)
        expect = set(LEADERS)
        if listed != expect:
            return False, f"issue-leader.tools.agents 應為 {sorted(expect)}，實得 {sorted(listed)}"
        bad = []
        for sa in spec.sub_agents:
            if sa.tools.agents:
                bad.append(f"{sa.name}.tools.agents 非空（{sa.tools.agents}）"
                           "——worker 是執行期產生的，不該列靜態 allowlist")
            if sa.sub_agents:
                bad.append(f"{sa.name} 底下有 {len(sa.sub_agents)} 個靜態 sub-agent"
                           "——thin shell 已廢，worker 應由 generate-worker.py 產生")
        return (not bad), ("；".join(bad) if bad else
                           f"issue-leader 列 {sorted(listed)}；兩份 repo-leader 皆無靜態 sub-agent")
    r.guard("6. tools.agents 只列 repo-leader（worker 為執行期產生）", c6)

    # 8. 每一支 CEL 運算式雙向測試
    # ⚠ 範圍＝靜態樹 ＋ **產生器輸出**。p_kubectl 只存在於動態 ops_k8s config，
    #   不納入的話那支守線完全沒被驗到（且是靜默的）。
    gen_nodes = dict(nodes)
    try:
        for gspec in _generated_worker_specs(spec_mod):
            gen_nodes.update(dict(_walk(gspec)))
    except Exception as exc:  # noqa: BLE001
        r.item("8. CEL 雙向測試（含產生器輸出）", False,
               f"無法產生 worker spec：{type(exc).__name__}: {exc}")
        gen_nodes = {}
    if gen_nodes:
        r.guard("8. CEL 雙向測試（靜態樹 ＋ 產生器輸出）", lambda: _check_cel(gen_nodes))

    # 9. 每支內建 policy 的 function.path 實跑 resolve
    r.guard("9. 內建 policy function.path resolve（含產生器輸出）",
            lambda: _check_policy_paths(gen_nodes or nodes))

    # 10-12：模擬**實際啟動路徑**（omnigent run → _bundle_agent → cli._bundle → server
    # validate）。第 1／2 項驗的是 parse/validate，證明不了這條路會通。
    r.guard("10. 實際上傳路徑：原始樹直接上傳必須成功且內容完整", _check_upload_path)
    r.guard("11. 所有 os_env.cwd 為相對路徑", _check_relative_cwd)
    r.guard("12. git index 無 symlink（mode 120000）", _check_no_tracked_symlinks)
    # 13：文件與實作矛盾已經發生三次，散文守不住 ⇒ 上 gate。
    r.guard("13. 無廢止詞彙殘留（prompt 零容忍／README 限歷史區）", _check_retired_terms)
    # 14／15：dispatcher —— workspace 抬到共同祖先後，repo hooks 只剩這條路拿回來。
    r.guard("14. dispatcher hook 端到端（fixture repo，零副作用）", _check_dispatcher)
    r.guard("15. dispatcher 接線前提（PreToolUse＋PostToolUse／不靠 shebang／skills 同層）",
            _check_hook_wiring)
    r.guard("16. 所有 dispatcher 副本逐字相同（漂移＝守線只在一條路徑生效）",
            _check_dispatcher_copies)
    # 17：UI picker 過濾——選不到就等於這個 agent 不存在，而且靜默。
    r.guard("17. 頂層 leader 的 harness 不得是 *-native（UI picker 會過濾掉）",
            lambda: _check_top_leader_harness_pickable(spec))

    print()
    print(f"總計：{r.failed} 項 FAIL" if r.failed else "總計：全部 PASS")
    return 1 if r.failed else 0


def _generated_worker_specs(spec_mod) -> list:
    """產生每一種 purpose × harness 的 worker config 並 parse 成 spec。

    🔴 **為什麼驗收一定要含這個**：worker 改成執行期產生之後，`p_kubectl_namespace_scope`
    只存在於動態產生的 `ops_k8s` config 裡——靜態樹**看不到它**。若 CEL 雙向測試只掃靜態
    樹，那支守線就完全沒被驗到（而且是靜默的：`missing` 檢查不會報，因為它只比「樹上有的
    policy 有沒有向量」）。所以把產生器輸出也當成驗收範圍的一部分。
    """
    specs = []
    with tempfile.TemporaryDirectory(prefix="verify-generated-") as td:
        for purpose, entry in GEN.ROSTER.items():
            templates = entry["template"]
            if templates is None:
                continue
            for harness in templates:
                out = Path(td) / f"{purpose}-{harness}"
                GEN.generate(purpose, harness, out)
                specs.append(spec_mod.parse(out, expand_env=False))
    return specs


def _cel_policies(nodes) -> dict[str, str]:
    """從**已 parse 的樹**收集所有 CEL 運算式（不是從原始碼猜）。"""
    out: dict[str, str] = {}
    for sp_ in nodes.values():
        if not sp_.guardrails or not sp_.guardrails.policies:
            continue
        for pol in sp_.guardrails.policies:
            fn = getattr(pol, "function", None)
            if not fn or not fn.path.endswith("cel.cel_policy"):
                continue
            # arguments 可能缺 expression（手改壞的 config）——不要讓 KeyError 中斷整輪
            expr = (fn.arguments or {}).get("expression")
            out[pol.name] = expr if isinstance(expr, str) else "<缺 expression>"
    return out


def _check_cel(nodes) -> tuple[bool, str]:
    """對每一支 CEL 運算式做雙向測試。

    ⚠ 這裡的每一個外部呼叫都各自包 try/catch 並**累積**錯誤，不是讓第一個例外炸掉整輪。
    實測過三次同型事故（materialize_bundle、parse、以及本函式的 cel_policy compile）：
    一支運算式編譯失敗，就會讓其餘 5 支與上百個向量完全不跑，而外層只看到一個 FAIL。
    """
    sp = os.environ.get("OMNIGENT_SITE_PACKAGES", DEFAULT_SITE_PACKAGES)
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)
    try:
        from omnigent.policies.builtins.cel import cel_policy  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return False, f"無法載入 cel_policy: {type(exc).__name__}: {exc}"

    exprs = _cel_policies(nodes)
    bad: list[str] = []
    tested = 0
    gap_open = 0
    gap_closed: list[str] = []

    missing = sorted(set(exprs) - set(CEL_VECTORS))
    if missing:
        # ⛔ 沒有向量的 CEL policy 直接判 FAIL：守線 fail-OPEN，沒測等於不存在。
        bad.append(f"缺雙向測試向量: {missing}")

    for name, expr in sorted(exprs.items()):
        if name not in CEL_VECTORS:
            continue
        try:
            evaluate = cel_policy(expression=expr, reason="policy")
        except Exception as exc:  # noqa: BLE001
            # 這一支編譯不過 ⇒ 記下來，繼續測其餘每一支
            bad.append(f"{name}: 編譯失敗 {type(exc).__name__}: {exc}")
            continue
        vectors = CEL_VECTORS[name]

        def _verdict(ev):
            try:
                got = evaluate(ev)
            except Exception as exc:  # noqa: BLE001
                return None, f"{type(exc).__name__}: {exc}"
            return got, None

        for ev in vectors.get("deny", []):
            tested += 1
            got, err = _verdict(ev)
            if err:
                bad.append(f"{name}: 評估違規事件時拋例外 {err}")
            elif not got or got.get("result") != "DENY":
                bad.append(f"{name}: 違規事件未被 DENY → {got!r} :: "
                           f"{ev['data'].get('arguments')}")
        for ev in vectors.get("allow", []):
            tested += 1
            got, err = _verdict(ev)
            if err:
                bad.append(f"{name}: 評估合法事件時拋例外 {err}")
            elif got and got.get("result") == "DENY":
                bad.append(f"{name}: 合法事件被 DENY → {ev['data'].get('arguments')}")
        # known_gap：已知擋不住、且**刻意不修**的向量（見 README §已知繞過向量）。
        # 不判 FAIL——它們記錄的是「我們知道這裡漏」。但若哪天變成 DENY，這裡會講出來，
        # 讓人把它升級成正式的 deny 向量。
        for ev in vectors.get("known_gap", []):
            tested += 1
            got, err = _verdict(ev)
            if err:
                bad.append(f"{name}: 評估 known_gap 事件時拋例外 {err}")
            elif got and got.get("result") == "DENY":
                gap_closed.append(f"{name}: {ev['data'].get('arguments', {}).get('command', '')[:60]}")
            else:
                gap_open += 1

    ok = not bad
    detail = (f"{len(exprs)} 支運算式 / {tested} 個事件符合預期"
              f"（其中 {gap_open} 個 known_gap 仍為已知漏洞）" if ok
              else f"{len(bad)} 個失敗: " + "; ".join(bad[:6]))
    if ok and gap_closed:
        detail += f"；⬆ {len(gap_closed)} 個 known_gap 現在已被擋，可升級成 deny 向量: {gap_closed[:3]}"
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


# ── 上傳路徑驗證（FIX ROUND 3）─────────────────────────────────────────────────
#
# 🔴 這一組是補最嚴重的驗收盲點：原本驗的是「原始樹 parse/validate」＋「materialize 後
#    parse/validate」，兩者都綠——但 `materialize_bundle()` **在使用者的實際啟動路徑上
#    根本不會被呼叫**（它只在 source 是單檔 YAML 或 `omnigent server --agent` preregister
#    時觸發）。實際路徑是 `omnigent run <目錄>` → `chat._bundle_agent()` → `cli._bundle()`，
#    那裡對目錄是 `rglob` ＋ `is_file()`，**不解 symlink**。所以驗的是一條不存在的路。
#
# 本組改成直接拿 server 端會用的那兩個函式（`cli._bundle` ＋
# `server.bundles.validate_agent_bundle`）模擬上傳，並把「原始樹必敗」也鎖成斷言——
# 那是現況的證據，哪天上游改了行為（變成會自動解引用）這一項會紅，提醒我們 launcher
# 可能不再需要。

#: 可啟動的 bundle root（目前只有 issue-leader；repo_leader 與 worker 都是 sub-agent）。
LAUNCHABLE_BUNDLES = [LEADER_DIR]


def _skill_map_of(spec) -> dict[str, frozenset[str]]:
    """{dotted agent name: frozenset(skill name)}。"""
    out: dict[str, frozenset[str]] = {}

    def walk(node, path: str = "") -> None:
        here = f"{path}.{node.name}" if path else str(node.name)
        out[here] = frozenset(sk.name for sk in node.skills)
        for child in node.sub_agents:
            walk(child, here)

    walk(spec)
    return out


def _upload_bits():
    """取得模擬上傳需要的三個上游函式（import 失敗轉成訊息，不讓整輪中斷）。"""
    sp = os.environ.get("OMNIGENT_SITE_PACKAGES", DEFAULT_SITE_PACKAGES)
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)
    from omnigent.cli import _bundle  # noqa: PLC0415
    from omnigent.server.bundles import validate_agent_bundle  # noqa: PLC0415
    from omnigent.spec import materialize_bundle, parse  # noqa: PLC0415

    return _bundle, validate_agent_bundle, materialize_bundle, parse


def _tar_members(data: bytes):
    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415

    return tarfile.open(fileobj=io.BytesIO(data)).getmembers()


def _check_upload_path() -> tuple[bool, str]:
    """模擬**實際啟動路徑**：原始樹直接打包上傳必須成功。

    🔴 FIX ROUND 4 把這一項從「原始樹必敗」翻成**正向斷言**。
    symlink 時代這一項鎖的是「現況就是壞的」（`tarball contains a link (not allowed)`，
    且 symlink 目錄 silent drop 讓 tar 內 SKILL.md 只剩 3 筆）。改成實體副本後，
    `omnigent run <目錄>` 可以直接跑 ⇒ 斷言方向反過來：**必須成功，而且內容必須完整**。

    驗的東西（全部對著 server 端真正會用的那兩個函式）：
      1. tar 內 link entry == 0
      2. 兩種 `enforce_handler_allowlist` 都通過（deployed server 走 True）
      3. tar 內 SKILL.md 筆數 == spec 樹的 skills 總數（抓整批 skills 消失）
      4. 直接上傳的 spec 與本機 `parse()` 的結果**逐 agent 等價**（skills 名稱集合相同）
    """
    bundle, validate_bundle, _materialize, parse_spec = _upload_bits()
    notes: list[str] = []
    bad: list[str] = []

    for root in LAUNCHABLE_BUNDLES:
        name = root.name
        data = bundle(root)                    # ← 未經 materialize，就是實際路徑用的打包器
        members = _tar_members(data)
        links = [m.name for m in members if m.issym() or m.islnk()]
        if links:
            bad.append(f"{name}: tar 內有 {len(links)} 個 link entry，server 會拒：{links[:3]}")
            continue

        specs = {}
        for strict in (True, False):
            try:
                specs[strict] = validate_bundle(data, enforce_handler_allowlist=strict)
            except Exception as exc:  # noqa: BLE001
                bad.append(
                    f"{name}: validate(enforce={strict}) 失敗 {type(exc).__name__}: {exc}"
                    + ("（deployed multi-user server 走的就是 True）" if strict else "")
                )
        if True not in specs:
            continue

        spec = specs[True]
        got = _skill_map_of(spec)
        want = _skill_map_of(parse_spec(root, expand_env=False))
        if got != want:
            diff = [f"{k}: 上傳後 {sorted(got.get(k, ()))} != 本機 {sorted(want.get(k, ()))}"
                    for k in sorted(set(got) | set(want)) if got.get(k) != want.get(k)]
            bad.append(f"{name}: 上傳後的 spec 與本機 parse 不等價 {diff[:3]}")
        total = sum(len(v) for v in want.values())
        # ⚠ 排除 `_templates/` 底下的 SKILL.md：那些是**產生器的模板來源**，會跟著 bundle
        #   一起上傳（worker 要在執行期從它們複製），但它們不是任何 agent 自己的 skills
        #   ⇒ 不該算進 spec 的 skills 總數。不排除的話這一項會永遠對不上。
        tar_skills = [m.name for m in members
                      if m.isfile() and m.name.endswith("SKILL.md")
                      and not m.name.startswith("_templates/")]
        if len(tar_skills) != total:
            bad.append(
                f"{name}: tar 內 SKILL.md {len(tar_skills)} 筆 != spec skills 總數 {total}"
                "（整批 skills 消失的徵兆）"
            )
        notes.append(
            f"{name}: 直接上傳 OK（link 0、enforce True/False 皆過）／"
            f"SKILL.md {len(tar_skills)} 筆 == skills 總數 {total}／"
            f"agent {len(got)} 個與本機 parse 等價"
        )

    return (not bad), ("；".join(notes) if not bad else "; ".join(bad[:4]))


# ── 廢止詞彙掃描（FIX ROUND 5）─────────────────────────────────────────────────
#
# 🔴 這是**第三次**出現「文件與實作矛盾」的 blocking（R0 的 prompt 與 policy 相反、
#    R2 的 README 自相矛盾、R5 的 symlink 殘留描述）。散文守不住，所以上 gate。
#    **過時的警告與錯的警告一樣危險**——讀者會照著做。
#
# 難點是「合法提及」：第 7／12 項**本身就在驗 symlink**，§已廢止的設計 章節也必須能談它。
# 純詞彙比對會誤報到不可用。所以分三層，每層規則不同：
#
#   L1  agent 會讀到的 prompt（AGENTS.md / SKILL.md）
#       → **零容忍**。agent 不需要知道某個設計曾經存在；過時描述會直接誤導它的行為。
#   L2  README.md（人讀的）
#       → 允許，但只在「歷史區」：標題含「已廢止」的章節，或帶 `<!-- legacy-ok -->` 標記。
#   L3  實作碼（build-bundles.py / mutation-tests.sh）
#       → 只掃**已移除的識別符**（見下方清單）。不掃 `symlink`——  # legacy-ok
#         那是它正在檢查的東西，掃了等於禁止實作本身。

#: 已廢止的設計詞彙：出現在 L1／L2 非歷史區就是錯的現行敘述。
RETIRED_TERMS = ["symlink", "mode 120000", "run-leader", "--rewrite-cwd",  # legacy-ok
                 "thin shell", "thin_shell", "tools.agents"]  # legacy-ok
#: 已移除的識別符：連實作碼裡都不該再出現（殘留＝指向不存在的東西）。
REMOVED_IDENTIFIERS = ["run-leader", "run_leader", "--rewrite-cwd", "LAUNCHER"]  # legacy-ok
#: L2 的歷史區標記（整章節放行）。
LEGACY_MARKER = "legacy-ok"
LEGACY_HEADING = "已廢止"
#: 刻意提及的區塊標記。用途：**規則本身就是「禁止 X」時，它必須能說出 X**——
#: 例如「版控裡一條 symlink 都不能有」。純詞彙比對無法區分「描述成現行做法」與
#: 「明文禁止」，所以要作者顯式標記；標記這個動作本身就迫使人停下來想一次
#: 「我是在描述現行做法嗎？」——那正是這道 gate 的價值。
#: 寫法：`<!-- retired-ok:start 理由 -->` … `<!-- retired-ok:end -->`（HTML 註解不渲染）。
RETIRED_OK_START = "retired-ok:start"
RETIRED_OK_END = "retired-ok:end"


def _prompt_files() -> list[Path]:
    """L1：agent 實際會讀到的 prompt（來源池 ＋ leader 自己的；產物是副本，不重複掃）。"""
    out: list[Path] = []
    for base in (TEMPLATES_DIR, SHARED_DIR, LEADER_DIR):
        for name in ("AGENTS.md", "SKILL.md"):
            out += [q for q in base.rglob(name)
                    if "__pycache__" not in q.parts
                    # 產物（thin shell 的副本）與來源內容相同，掃來源就夠
                    and "/agents/" not in str(q.relative_to(OMNIGENT_DIR))]
    return sorted(set(out))


def _check_retired_terms() -> tuple[bool, str]:
    """掃出「已廢止設計仍被當成現行做法描述」的地方。"""
    bad: list[str] = []
    scanned = 0

    # ── L1：prompt 零容忍 ──────────────────────────────────────────────────
    for f in _prompt_files():
        scanned += 1
        rel = f.relative_to(OMNIGENT_DIR)
        for n, line in enumerate(f.read_text().splitlines(), 1):
            for term in RETIRED_TERMS:
                if term in line:
                    bad.append(f"{rel}:{n} 出現廢止詞彙 {term!r}（prompt 層零容忍）")

    # ── L2：README 只在歷史區放行 ──────────────────────────────────────────
    readme = OMNIGENT_DIR / "README.md"
    if readme.is_file():
        scanned += 1
        in_legacy = False
        in_retired_ok = False
        for n, line in enumerate(readme.read_text().splitlines(), 1):
            if line.startswith("## "):
                # 章節切換：標題含「已廢止」才算歷史區
                in_legacy = LEGACY_HEADING in line
            if LEGACY_MARKER in line:
                in_legacy = True
            if RETIRED_OK_START in line:
                in_retired_ok = True
            elif RETIRED_OK_END in line:
                in_retired_ok = False
            if in_legacy or in_retired_ok:
                continue
            for term in RETIRED_TERMS:
                if term in line:
                    bad.append(
                        f"README.md:{n} 出現廢止詞彙 {term!r} 且不在歷史區"
                        "（要嘛刪掉，要嘛移進 §已廢止的設計）"
                    )

        if in_retired_ok:
            bad.append("README.md: 有 retired-ok:start 沒有對應的 retired-ok:end"
                       "（未關閉的區塊會放行整份檔案的其餘內容）")

    # ── L3：實作碼只掃已移除的識別符 ───────────────────────────────────────
    for f in sorted(OMNIGENT_DIR.glob("*.py")) + sorted(OMNIGENT_DIR.glob("*.sh")):
        scanned += 1
        rel = f.relative_to(OMNIGENT_DIR)
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if LEGACY_MARKER in line:
                continue
            for ident in REMOVED_IDENTIFIERS:
                if ident in line:
                    bad.append(f"{rel}:{n} 殘留已移除的識別符 {ident!r}")

    return (not bad), (f"掃 {scanned} 個檔案，無廢止詞彙殘留" if not bad
                       else f"{len(bad)} 處：" + "；".join(bad[:5]))


def _check_relative_cwd() -> tuple[bool, str]:
    """所有 config.yaml 的 `os_env.cwd` 必須是相對路徑。

    🔴 FIX ROUND 4 新增，取代上一輪那個「已知待拍板、不判 FAIL」的版本。
    裁決：一律寫 `.`。絕對路徑在「有 `OMNIGENT_RUNNER_WORKSPACE`」時無效（runner 覆寫
    它）、在「沒有 workspace」時會變成 agent environment root 與 copytree source 而暴露
    host filesystem（GHSA-p8rw-8qj3-hf33）、上 deployed server 時整個 bundle 被拒。
    三種情況沒有一種是好的 ⇒ 機器強制，防止下次又寫回去。

    ⚠ 本項比 server 端**更嚴**，刻意如此：`server/bundles.py` 只檢查 **root spec** 的
    `os_env.cwd`（`os_env = getattr(spec, "os_env", None)`，不遞迴 sub-agent），所以只有
    sub-agent 寫絕對路徑時 deployed server **不會**拒收——但那個 sub-agent 在沒有
    workspace 的 runner 上仍有 GHSA-p8rw-8qj3-hf33 的曝光面。實測：只改一個 thin shell
    的 cwd，第 10 項（模擬上傳）不會紅，只有本項會紅。
    """
    import re as _re2  # noqa: PLC0415

    pattern = _re2.compile(r"^\s*cwd:\s*(\S+)\s*$")
    bad: list[str] = []
    seen = 0
    for cfg in sorted(OMNIGENT_DIR.rglob("config.yaml")):
        if "__pycache__" in cfg.parts:
            continue
        for i, line in enumerate(cfg.read_text().splitlines(), 1):
            m = pattern.match(line)
            if not m:
                continue
            seen += 1
            value = m.group(1).strip("\"'")
            if value.startswith(("/", "~")) or ".." in Path(value).parts:
                bad.append(f"{cfg.relative_to(OMNIGENT_DIR)}:{i} cwd={value!r}")
    return (not bad), (f"{seen} 處 cwd 全部為相對路徑" if not bad
                       else f"絕對或逃逸路徑：{bad[:4]}")


def _check_no_tracked_symlinks() -> tuple[bool, str]:
    """git index 裡不得有 mode 120000（symlink）的項目。

    ⚠ 與第 7 項的差別：第 7 項看**工作目錄**，這一項看 **git index**——兩者可能不一致
    （例如 symlink 已在工作目錄刪掉但 index 還留著，或反之）。版控才是別人 clone 會拿到的。
    """
    proc = subprocess.run(
        ["git", "ls-files", "-s", "--", str(OMNIGENT_DIR.name)],
        cwd=OMNIGENT_DIR.parent, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        # 不在 git repo 內（例如突變測試把 .omnigent 複製到臨時目錄後跑 --check）⇒
        # 這一項無從判斷，跳過而不是假紅。⚠ 假紅會讓突變測試的基線變成「1 項 FAIL」，
        # 每個 case 都紅 ⇒ 整套突變測試失去鑑別力，比沒有更糟。
        return True, "不在 git repo 內（或無 tracked 檔案），跳過"
    modes: dict[str, int] = {}
    links: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        mode, _, rest = line.partition(" ")
        modes[mode] = modes.get(mode, 0) + 1
        if mode == "120000":
            links.append(rest.split("\t")[-1])
    if links:
        return False, f"git index 仍有 {len(links)} 個 symlink（mode 120000）：{links[:3]}"
    summary = "／".join(f"mode {m}: {n} 個" for m, n in sorted(modes.items()))
    return True, f"0 個 mode 120000（{summary}）"


def _check_dispatcher_copies() -> tuple[bool, str]:
    """第 16 項：所有 dispatcher 副本必須**逐字相同**。

    🔴 為什麼要獨立一項：第 0 項（產物同步）目前**恰好**也能抓到這件事，因為 `build()`
    把 `hooks/` 列進複製清單。但那是間接的——若有人改 `build()` 不再複製 `hooks/`，
    第 0 項不會抱怨，而兩份 dispatcher 就開始漂移。**一份補了守線、一份沒補，守線只在
    其中一條派工路徑生效，而且不會報錯**（靜默失效）。所以這一項直接對 hash，
    不管 `build()` 怎麼做。
    """
    copies = sorted(q for q in OMNIGENT_DIR.rglob("hooks/dispatch-hook.py")
                    if "__pycache__" not in q.parts)
    if len(copies) < 2:
        return False, (f"只找到 {len(copies)} 份 dispatcher（預期至少 2：issue-leader ＋ "
                       "每個 claude-native 的 repo-leader）")
    digests = {}
    for q in copies:
        digests.setdefault(hashlib.sha256(q.read_bytes()).hexdigest(), []).append(
            str(q.relative_to(OMNIGENT_DIR)))
    if len(digests) != 1:
        groups = "；".join(f"{d[:8]}…: {v}" for d, v in digests.items())
        return False, f"{len(copies)} 份 dispatcher 有 {len(digests)} 種內容（漂移了）：{groups}"
    # 每個掛 hooks/ 的 bundle 都要有對應的 dispatcher
    expect = {LEADER_DIR} | {LEADER_DIR / "agents" / k for k, v in LEADERS.items()
                             if v["harness"] == "claude-native"}
    missing = sorted(str(d.name) for d in expect
                     if not (d / "hooks" / "dispatch-hook.py").is_file())
    if missing:
        return False, f"這些 bundle 缺 dispatcher：{missing}"
    return True, f"{len(copies)} 份逐字相同（sha256 {next(iter(digests))[:12]}…）"


def _check_dispatcher() -> tuple[bool, str]:
    """第 14 項：dispatcher hook 端到端（委派給 test-dispatcher.py）。

    ⛔ 安全：該測試一律用 **fixture repo**，零副作用。歷史事故——上一輪驗證 dispatcher 時
    假 kubectl 沒在 harness 的 PATH 上生效，結果**真的對 woow-k3s 發出 delete pod**
    （回 NotFound，無損害，但那是運氣）。⛔ 不要為了「更真實」把真 repo／真 kubectl 接進來。
    """
    script = OMNIGENT_DIR / "test-dispatcher.py"
    if not script.is_file():
        return False, f"找不到 {script}"
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          timeout=300, check=False)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        # 截斷放寬：失敗訊息開頭是「所有失敗情境的標籤」，截太短會把關鍵情境切掉。
        return False, f"dispatcher 端到端失敗：{out[:900]}"
    return True, out.lstrip("✅ ").strip()


def _check_hook_wiring() -> tuple[bool, str]:
    """第 15 項：dispatcher 的接線前提——少一個 hook 就完全不生效。

    三個前提（全部實測）：
      1. `hooks/hooks.json` 必須同時註冊 **PreToolUse 與 PostToolUse**
         （repo 有 2 支 PostToolUse hook：repo-lint --hook、memory-links-guard）
      2. command 必須走 `python3 "${CLAUDE_PLUGIN_ROOT}/…"`，⛔ **不可靠 shebang**
         ——bundle 打包會掉 exec bit（755 → 644）
      3. 同層必須有 `skills/`，否則不會發 `--plugin-dir`，hooks.json 根本不被讀取
         （`inner/bundle_skills.py`）
    """
    bad: list[str] = []
    notes: list[str] = []
    hooks_json = LEADER_DIR / "hooks" / "hooks.json"
    if not hooks_json.is_file():
        return False, f"找不到 {hooks_json}"
    try:
        cfg = json.loads(hooks_json.read_text())
    except json.JSONDecodeError as exc:
        return False, f"hooks.json 不是合法 JSON：{exc}"
    events = cfg.get("hooks") or {}
    for required in ("PreToolUse", "PostToolUse"):
        if required not in events:
            bad.append(f"hooks.json 缺 {required}（repo 兩種事件都有註冊 hook）")
    commands = [h.get("command", "") for groups in events.values()
                for g in groups for h in (g.get("hooks") or [])]
    if not commands:
        bad.append("hooks.json 沒有任何 command")
    for cmd in commands:
        if not cmd.startswith("python3 "):
            bad.append(f"command 沒有顯式用 python3 開頭（靠 shebang 會因 exec bit 被打包"
                       f"掉而失效）：{cmd!r}")
        if "${CLAUDE_PLUGIN_ROOT}" not in cmd:
            bad.append(f"command 沒有用 ${{CLAUDE_PLUGIN_ROOT}}：{cmd!r}")
    notes.append(f"{len(commands)} 條 command、事件 {sorted(events)}")

    # 每個會掛 dispatcher 的 bundle 目錄都必須同時有 hooks/ 與 skills/
    for d in [LEADER_DIR] + [LEADER_DIR / "agents" / k for k, v in LEADERS.items()
                             if v["harness"] == "claude-native"]:
        if not (d / "hooks" / "hooks.json").is_file():
            bad.append(f"{d.name}: 缺 hooks/hooks.json")
        elif not (d / "skills").is_dir():
            bad.append(f"{d.name}: 有 hooks/ 但沒有 skills/ ⇒ 不會發 --plugin-dir，"
                       "hooks.json 根本不會被讀（inner/bundle_skills.py）")
    # pi 不該有 hooks/（pi 沒有 hook 機制，放了只是誤導）
    for k, v in LEADERS.items():
        if v["harness"] != "claude-native" and (LEADER_DIR / "agents" / k / "hooks").exists():
            bad.append(f"{k}: pi harness 不該有 hooks/（pi 沒有 hook 機制）")
    notes.append("claude-native 層皆有 hooks/＋skills/，pi 層無 hooks/")
    return (not bad), ("；".join(notes) if not bad else "；".join(bad[:4]))


def _check_top_leader_harness_pickable(spec) -> tuple[bool, str]:
    """第 17 項：頂層 leader 的 harness 不得是任何 `*-native`。

    🔴 為什麼要 gate：Omnigent Web UI 的 Agents → Custom agents 子選單**選不到**
    `*-native` harness 的 custom agent，而且**不報錯**——選單裡就是沒有那一列。
    `web/src/hooks/useAvailableAgents.ts` enrich harness 後，若 custom agent 解析出的
    native key 與清單中某個 builtin 相同就 `enriched.filter(a => a.id !== agent.id)`
    把自己剔除；`claude-native` → key `claude`，正好撞上 seeded builtin
    `claude-native-ui`（harness 也是 `claude-native`）⇒ 必然命中。

    實測對照（同一台 server，逐個 `sys_agent_get`）：`h1-probe-pi`(pi) ✅、
    `q3-probe`(pi) ✅、`claude`(claude-sdk) ✅／`h1-probe-cn`(claude-native) ❌、
    `h2-dispatch-bundle`(claude-native) ❌、`woow_issue_leader`(claude-native) ❌。
    `h1-probe-pi` 與 `h1-probe-cn` 是同一組實驗、只差 harness 一欄。

    ⚠ 只管**頂層** bundle。sub-agent（`repo_leader_cc`）與執行期產生的 worker 不出現在
    頂層 picker，不受這個 filter 影響，而且它們**需要** claude-native 才有 hook 機制
    （dispatcher 靠 claude CLI 讀 `--plugin-dir` 底下的 hooks.json）⇒ ⛔ 不要因為這一項
    去改它們的 harness。
    """
    harness = (getattr(spec.executor, "config", None) or {}).get("harness")
    if not harness:
        return False, "頂層 config.yaml 讀不到 executor.config.harness"
    if harness.endswith("-native"):
        return False, (
            f"頂層 leader 的 harness 是 {harness!r}——Omnigent Web UI 的 Custom agents "
            "選單會把 *-native 的 custom agent 過濾掉（useAvailableAgents.ts：native key "
            "撞上同 key 的 builtin 就自我剔除），使用者根本選不到這個 agent，而且不報錯。"
            "改用 claude-sdk（claude 家族、同樣吃 claude-* model）。"
            "⛔ 不要順手改 sub-agent／worker 的 harness——它們不在頂層 picker，"
            "且需要 claude-native 才有 dispatcher hook。")
    return True, f"harness={harness}（非 *-native，UI picker 選得到）"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="只驗收，不寫檔（產物不同步會判 FAIL）")
    args = ap.parse_args()
    if args.check:
        return run_checks()
    build(check=False)
    n_files = sum(1 for q in OMNIGENT_DIR.rglob("*")
                  if q.is_file() and "__pycache__" not in q.parts)
    purposes = [k for k, v in ROSTER.items() if v["template"] is not None]
    print(f"已產生 {1 + len(LEADERS)} 份靜態 config（issue-leader ＋ {len(LEADERS)} 份 "
          f"repo-leader）；worker 在**執行期**由 generate-worker.py 寫出。")
    print(f"（.omnigent/ 共 {n_files} 個檔案、0 條 symlink；"
          f"可產生的 purpose：{', '.join(purposes)}）")
    print("接著跑：python3 .omnigent/build-bundles.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())

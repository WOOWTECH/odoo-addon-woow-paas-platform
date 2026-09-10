#!/usr/bin/env python3
"""共同祖先 workspace 的 hook dispatcher。

## 它補的洞

跨 repo 統籌需要 workspace 抬到六個 repo 的共同祖先（`/home/elmo/woow-repos`）——
因為 top-level session 之間不能互相 `sys_session_send`，只有同一棵樹能派工。
代價是（實測）**子 repo 的 `.claude/` 三項全失**：

| 失去的 | 症狀 |
| --- | --- |
| hooks | worker `cd` 進 repo 後，repo 自己註冊的 PreToolUse / PostToolUse **不觸發** |
| CLAUDE.md | 不載入（on-demand 注入**只綁 `Read` 工具** ⇒ 用 `cat` / `grep` 連那點補償都沒有） |
| skills | 不註冊 |

本 hook 把 **hooks** 那一項拿回來：claude CLI 只載入 workspace 根那一份 settings，
所以這支 hook 讀 PreToolUse / PostToolUse 的 payload，判斷指令落在哪些子 repo，
**轉呼叫那些 repo 自己註冊的 hook command**，原樣傳遞 stdout / stderr / exit code。

## ⛔ 為什麼不改寫成 CEL policy

把 `kubectl-ns-guard` 的 namespace 白名單搬進 CEL，它會變成**第四份副本**，而且不在
repo-lint `kubectl-ns-whitelist-sync` 的對帳範圍內 ⇒ 必然漂移。dispatcher 跑的是 repo 裡
**原封不動**那一支，真相只有一個來源。

## 判定採聯集（寧可多跑、不可漏守）

  A. payload 的 `cwd` 落在某個 repo 底下（agent `cd` 進去後的常態）
  B. 指令文字出現該 repo 的路徑（相對於 workspace 根、絕對路徑、或目錄名）
     —— 補住 `cd` 在指令中間、`git -C <path>`、`pushd`、直接用絕對路徑等寫法。

## 兩種回傳協定都要能過

  - `kubectl-ns-guard.py`：**stdout JSON**（`hookSpecificOutput.permissionDecision: deny`），
    exit code 是 0 ⇒ 必須偵測 stdout 並**短路**，否則後面的 hook 會再吐一份 JSON，
    兩份混在 stdout 上 claude 解析不了。
  - 其餘三支：**exit 2** 代表 block。

## ⚠ 不要靠 shebang

bundle 打包會**掉 exec bit**（755 → 644），所以 `hooks.json` 的 command 一律寫
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/dispatch-hook.py"`。突變測試 T 實證這件事。

## 環境變數

  - `${CLAUDE_PLUGIN_ROOT}` = materialized bundle 路徑（claude 展開，hooks.json 用）
  - `$CLAUDE_PROJECT_DIR`   = workspace 根。⚠ 轉呼叫時**必須覆寫成該 repo 根**——
    repo 的 hook command 幾乎都寫成 `"$CLAUDE_PROJECT_DIR/.claude/hooks/x.py"`，
    不覆寫就找不到檔。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

WS_ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()
#: 從 workspace 根往下找 repo 的深度上限（本機佈局最深是 `WOOW/<repo>` = 2）。
MAX_DEPTH = 4
#: 轉呼叫單一 hook 的逾時上限（秒）。repo 的 repo-lint --hook 可能偏慢。
DEFAULT_TIMEOUT = 60
#: 掃 repo 時要跳過的目錄名（避免走進 worktree／快取／產生物）。
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".omnigent-runtime"}
#: 這些 token 的**下一個** token 是目錄（`cd x`／`pushd x`／`git -C x`／`env -C x`）。
DIR_ARG_TOKENS = {"cd", "pushd", "-C", "--directory"}
#: PreToolUse 的 hook 失敗時改為放行的逃生門（⛔ 只在 repo hook 自己壞掉時臨時用）。
FAIL_OPEN_ENV = "OMNIGENT_DISPATCH_FAIL_OPEN"
#: 會觸發「零命中告警」的工具（這些是真的會改動或執行東西的）。
WARN_TOOLS = {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"}


def guarded_repos() -> list[Path]:
    """列出 workspace 底下所有自帶 `.claude/settings.json` 的 repo。

    不含 workspace 根自己——根那一份（若有）claude 本來就會載入，再轉呼叫會重複執行。
    """
    out: list[Path] = []
    for depth in range(1, MAX_DEPTH + 1):
        pattern = "/".join(["*"] * depth) + "/.claude/settings.json"
        for s in WS_ROOT.glob(pattern):
            repo = s.parent.parent
            if repo == WS_ROOT:
                continue
            if SKIP_DIRS & set(repo.relative_to(WS_ROOT).parts):
                continue
            out.append(repo)
    return sorted(set(out))


def _command_of(payload: dict) -> str:
    """取出指令文字。Bash 用 `command`；Edit／Write 類用檔案路徑當判定依據。"""
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return ""
    parts = [ti.get("command"), ti.get("file_path"), ti.get("path"), ti.get("notebook_path")]
    return " ".join(str(p) for p in parts if p)


#: `-c` 之後是 shell payload（`bash -c "cd ../repoA && …"`）⇒ 遞迴解析一層。
SHELL_PAYLOAD_FLAGS = {"-c"}


def _path_tokens(text: str, _depth: int = 0) -> list[str]:
    """從指令文字抽出「看起來是路徑」的 token。

    兩種來源：
      1. token 本身含 `/` 或以 `.` 開頭（`../repoA`、`group/repoA`、`./x`）
      2. `cd` / `pushd` / `git -C` / `env -C` 的**下一個** token（即使它不含 `/`）

    ⚠ `shlex.split` 對未閉合的引號會丟 `ValueError` ⇒ 包起來並 fallback 成空列表，
    讓呼叫端還有字串比對那一層可用。**絕不能因為解析失敗就整個漏守。**
    """
    try:
        tokens = shlex.split(text, comments=False, posix=True)
    except ValueError:
        return []
    out: list[str] = []
    expect_dir = False
    expect_payload = False
    for tok in tokens:
        if expect_dir:
            out.append(tok)
            expect_dir = False
        if expect_payload:
            # `bash -c "cd ../repoA && …"`：payload 本身是一條指令 ⇒ 遞迴抽一層。
            # ⚠ 深度限 1：再深就該懷疑是刻意混淆，而本 hook 防的是手滑不是攻擊。
            if _depth < 1:
                out.extend(_path_tokens(tok, _depth + 1))
            expect_payload = False
        if tok in SHELL_PAYLOAD_FLAGS:
            expect_payload = True
            continue
        if tok in DIR_ARG_TOKENS:
            expect_dir = True
            continue
        # `git -C<path>` / `--directory=<path>` 這種黏在一起的寫法
        for flag in ("-C", "--directory="):
            if tok.startswith(flag) and len(tok) > len(flag):
                out.append(tok[len(flag):].lstrip("="))
        if "/" in tok or tok.startswith("."):
            out.append(tok)
    return out


def _resolved_candidates(payload_cwd: Path, text: str) -> set[Path]:
    """把 path token 以 **cwd 與 WS_ROOT 兩個基準**各 resolve 一次。

    兩個基準都要試，因為 agent 可能寫相對於當前位置的路徑（`cd ../repoA`），也可能寫
    相對於 workspace 根的路徑（`cd group/repoA`）——光靠單一基準會漏。

    `os.path.normpath` 順帶解掉連續斜線（`group//repoA`）與 `..`，不碰檔案系統
    （⛔ 不用 `Path.resolve()`：它會跟隨 symlink 並在路徑不存在時行為依版本而異，
    而我們只要語意上的正規化）。
    """
    out: set[Path] = set()
    for tok in _path_tokens(text):
        if not tok or tok.startswith("-"):
            continue
        for base in (payload_cwd, WS_ROOT):
            raw = tok if os.path.isabs(tok) else os.path.join(str(base), tok)
            out.add(Path(os.path.normpath(raw)))
    return out


def targets(payload: dict) -> list[Path]:
    """判斷這道呼叫可能作用到哪些 repo。

    三層聯集，**寧可多跑（最多多一次檢查）、不可漏守**：

      A. payload 的 `cwd` 落在某個 repo 底下（agent `cd` 進去後的常態）
      B. 指令文字出現該 repo 的路徑／目錄名（字面比對）
      C. **path-token 解析**：把 token 當路徑、以 cwd 與 WS_ROOT 兩個基準 resolve，
         落在某個 repo 內就算命中

    ⚠ C 是 FIX ROUND 6 補的。只有 A／B 時，**`cd ../repoA`（從 sibling repo 進入）
    會零命中**——cwd 不在 repoA 內、字串裡沒有 `group/repoA`、而裸 `repoA` 的 regex
    禁止前一字元是 `/` 所以 `../repoA` 不中。那是多 repo 共同祖先下**極常見**的寫法
    （不是對抗性規避），後面接 kubectl 就等於鐵律失效。
    """
    try:
        cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    except OSError:
        cwd = WS_ROOT
    text = _command_of(payload)
    candidates = _resolved_candidates(cwd, text)
    hits: list[Path] = []
    for repo in guarded_repos():
        if cwd == repo or repo in cwd.parents:          # A
            hits.append(repo)
            continue
        rel = repo.relative_to(WS_ROOT).as_posix()      # B
        needles = {rel, str(repo), repo.name}
        if any(re.search(r"(?<![\w/.-])" + re.escape(n) + r"(?![\w-])", text)
               for n in needles if n):
            hits.append(repo)
            continue
        if any(c == repo or repo in c.parents for c in candidates):   # C
            hits.append(repo)
    return hits


def matches(matcher: str, tool_name: str) -> bool:
    """比對 settings.json 的 matcher（claude 的 matcher 是 regex，支援 `A|B` 形式）。"""
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, tool_name) is not None
    except re.error:
        return matcher == tool_name


def _is_blocking_stdout(text: str) -> bool:
    """stdout JSON 是否表示 deny／block（kubectl-ns-guard 走這條，exit code 為 0）。"""
    if "permissionDecision" not in text and "decision" not in text:
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    specific = data.get("hookSpecificOutput")
    if isinstance(specific, dict) and specific.get("permissionDecision") == "deny":
        return True
    return data.get("decision") == "block"


def _hook_failure(event: str, repo: Path, command: str, kind: str, detail: str) -> int:
    """repo hook 自己失敗（timeout／執行不了）時的處置。

    🔴 **PreToolUse 一律 fail-closed（exit 2）**：它是安全 gate，放行一個沒被檢查過的
    指令，等於那道守線在這一次完全不存在。FIX ROUND 6 之前這裡只寫 stderr 就 `continue`，
    最後回 0 ⇒ **fail-OPEN**（覆核實跑：repo hook `timeout: 1` 實際 sleep 2，指令照樣放行）。

    **PostToolUse 保留 warning-only**：工具已經執行完了，擋也改變不了結果。

    ⚠ fail-closed 有誤擋風險：repo hook 自己有 bug 卡住的話，**該 repo 的所有操作都會被擋**。
    所以訊息必須可診斷（哪個 repo、哪支 hook、什麼原因、怎麼逆轉），並留一個明確的逃生門。
    """
    if event != "PreToolUse":
        sys.stderr.write(
            f"[dispatch-hook] {event} hook {kind}（已執行完的工具不擋）：{repo.name} / {command}\n"
            f"  {detail}\n")
        return 0
    if os.environ.get(FAIL_OPEN_ENV, "").strip() not in ("", "0", "false", "False"):
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {FAIL_OPEN_ENV} 已設，放行一個**未經檢查**的指令："
            f"{repo.name} / {command}（{kind}）\n")
        return 0
    sys.stderr.write(
        "[dispatch-hook] ⛔ 拒絕執行：轉呼叫 repo 的 hook 失敗，無法確認這道指令安全。\n"
        f"  repo      : {repo}\n"
        f"  hook      : {command}\n"
        f"  事件／原因: {event} / {kind}\n"
        f"  細節      : {detail}\n"
        "  為什麼擋 : PreToolUse 是安全 gate；hook 沒跑完就放行，等於那道守線這次不存在。\n"
        "  怎麼處理 : (1) 修那支 repo hook，或在該 repo 的 .claude/settings.json 調高它的\n"
        "             timeout；(2) 確認 hook 檔存在且 python3 跑得起來；\n"
        f"             (3) 真的要硬過：export {FAIL_OPEN_ENV}=1（⛔ 會放行未經檢查的指令）。\n")
    return 2


def main() -> int:  # noqa: PLR0911, PLR0912
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0  # dispatcher 自己絕不該變成故障點
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name") or "PreToolUse"
    tool_name = payload.get("tool_name") or ""

    matched_repos = targets(payload)
    executed = 0

    for repo_root in matched_repos:
        settings_path = repo_root / ".claude" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        groups = (settings.get("hooks") or {}).get(event) or []
        for group in groups:
            if not isinstance(group, dict) or not matches(group.get("matcher", ""), tool_name):
                continue
            for h in group.get("hooks") or []:
                if not isinstance(h, dict) or h.get("type") != "command":
                    continue
                command = h.get("command")
                if not command:
                    continue
                env = dict(os.environ)
                # repo 的 hook command 寫 "$CLAUDE_PROJECT_DIR/.claude/hooks/x.py"，
                # 而 claude 給的值是 workspace 根 ⇒ 必須改寫成該 repo 根。
                env["CLAUDE_PROJECT_DIR"] = str(repo_root)
                try:
                    proc = subprocess.run(
                        command, shell=True, input=raw, text=True, capture_output=True,
                        cwd=str(repo_root), env=env,
                        timeout=h.get("timeout", DEFAULT_TIMEOUT),
                    )
                except subprocess.TimeoutExpired as exc:
                    rc = _hook_failure(event, repo_root, command, "timeout",
                                       f"逾時 {exc.timeout} 秒未結束")
                    if rc:
                        return rc
                    continue
                except OSError as exc:
                    rc = _hook_failure(event, repo_root, command, "無法執行", str(exc))
                    if rc:
                        return rc
                    continue
                executed += 1
                if proc.stdout:
                    sys.stdout.write(proc.stdout)
                if proc.stderr:
                    sys.stderr.write(proc.stderr)
                # ⚠ 短路兩種 block 協定：exit 非 0，或 stdout JSON 判 deny。
                #   不短路的話後面的 hook 會再吐一份 JSON，兩份混在 stdout 上無法解析。
                if proc.returncode != 0:
                    return proc.returncode
                if _is_blocking_stdout(proc.stdout or ""):
                    return 0

    # ── 零命中告警 ────────────────────────────────────────────────────────────
    # 命中了 repo 卻沒有任何 hook 真的跑起來 ⇒ 接線可能壞了（matcher 改了、settings.json
    # 讀不到…）。只對真的會改動／執行東西的工具告警，避免噪音。
    #
    # ⚠ **這個告警抓不到「targets() 回空」那一類漏守**（FIX ROUND 6 的 BLOCKING 1 正是
    #   那一類：`cd ../repoA` 讓 targets() 回空）。它是 path-token 解析的**輔助**，
    #   ⛔ 不是替代品。真正的守線在 targets() 的三層聯集。
    if executed == 0 and matched_repos and tool_name in WARN_TOOLS:
        names = ", ".join(r.name for r in matched_repos)
        sys.stderr.write(
            f"[dispatch-hook] ⚠ 命中 repo（{names}）但沒有任何 {event} hook 跑起來"
            f"（tool={tool_name}）——接線可能壞了，請確認該 repo 的 .claude/settings.json。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

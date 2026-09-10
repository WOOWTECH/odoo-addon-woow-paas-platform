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


def targets(payload: dict) -> list[Path]:
    """判斷這道呼叫可能作用到哪些 repo。"""
    try:
        cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    except OSError:
        cwd = WS_ROOT
    text = _command_of(payload)
    hits: list[Path] = []
    for repo in guarded_repos():
        if cwd == repo or repo in cwd.parents:          # A：cwd 落在 repo 內
            hits.append(repo)
            continue
        rel = repo.relative_to(WS_ROOT).as_posix()      # B：指令文字提到該 repo
        needles = {rel, str(repo), repo.name}
        if any(re.search(r"(?<![\w/.-])" + re.escape(n) + r"(?![\w-])", text)
               for n in needles if n):
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


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0  # dispatcher 自己絕不該變成故障點
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name") or "PreToolUse"
    tool_name = payload.get("tool_name") or ""

    for repo_root in targets(payload):
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
                except subprocess.TimeoutExpired:
                    sys.stderr.write(f"[dispatch-hook] timeout: {command}\n")
                    continue
                except OSError as exc:
                    sys.stderr.write(f"[dispatch-hook] 無法執行 {command}: {exc}\n")
                    continue
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
    return 0


if __name__ == "__main__":
    sys.exit(main())

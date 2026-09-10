#!/usr/bin/env python3
"""dispatcher hook 的端到端測試（fixture repo，⛔ 零副作用）。

## ⛔ 安全約束

**絕不對任何真實 K8s 叢集發送指令，包括唯讀。** 歷史事故：上一輪驗證 dispatcher 時
假 kubectl 沒在 harness 的 PATH 上生效（runner 繼承 daemon PATH），結果**真的對 woow-k3s
發出了 `delete pod`**（回 NotFound，無損害，但那是運氣）。

所以本測試只用 fixture repo：自建一個假 workspace，裡面的 hook 只寫 marker 檔或回 JSON，
指令一律是 `echo`。⛔ 不要為了「更真實」把真 repo 或真 kubectl 接進來。

## 驗的東西

| # | 情境 | 為什麼要驗 |
| --- | --- | --- |
| 1 | cwd 落在 repo 內 | agent `cd` 進去後的常態 |
| 2 | cwd 在 workspace 根，指令開頭 `cd <repo>` | 最常見的寫法 |
| 3 | `cd` 在指令中間（`a && cd <repo> && b`） | 字面比對容易漏 |
| 4 | `git -C <repo>` | 不 cd 也會作用到 repo |
| 5 | `pushd <repo>` | 同上 |
| 6 | 絕對路徑、完全不 cd | 同上 |
| 7 | stdout JSON deny（exit 0） | `kubectl-ns-guard` 走這條協定 |
| 8 | exit 2 block | 其餘三支 repo hook 走這條 |
| 9 | PostToolUse | repo 有 2 支 PostToolUse hook（repo-lint、memory-links-guard） |
| 10 | Edit 工具用 `file_path` 判定 | Edit／Write 類沒有 `command` 欄位 |
| 11 | 與任何 repo 無關的指令 | ⛔ 不得誤觸發（否則每道指令都要跑全部 repo 的 hook） |
| 12 | `CLAUDE_PROJECT_DIR` 被覆寫成 repo 根 | repo 的 hook command 都寫 `"$CLAUDE_PROJECT_DIR/..."`，不覆寫就找不到檔 |
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DISPATCHER = Path(__file__).resolve().parent / "woow-issue-leader" / "hooks" / "dispatch-hook.py"


def _fixture(root: Path) -> None:
    """建一個假 workspace：repoA（marker／exit 2）、repoB（stdout JSON deny）。"""
    # ── repoA：PreToolUse 寫 marker（含 CLAUDE_PROJECT_DIR 值）、PostToolUse 也寫
    a = root / "repoA"
    (a / ".claude" / "hooks").mkdir(parents=True)
    (a / ".claude" / "hooks" / "marker.py").write_text(
        "import json,os,sys,pathlib\n"
        "p = json.load(sys.stdin)\n"
        "ev = p.get('hook_event_name','?')\n"
        # 把 CLAUDE_PROJECT_DIR 一起寫出來，驗它有被覆寫成 repo 根
        "pathlib.Path(os.environ['MARKER_FILE']).write_text(\n"
        "    f\"{ev}|{os.environ.get('CLAUDE_PROJECT_DIR','')}\")\n"
    )
    (a / ".claude" / "hooks" / "block.py").write_text(
        "import sys\nsys.stderr.write('repoA blocked\\n')\nsys.exit(2)\n")
    (a / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"matcher": "Bash|Edit|Write",
                        "hooks": [{"type": "command",
                                   "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/marker.py"'}]}],
        "PostToolUse": [{"matcher": "Edit|Write",
                         "hooks": [{"type": "command",
                                    "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/marker.py"'}]}],
    }}))
    # ── repoB：PreToolUse 走 stdout JSON deny（exit 0）——kubectl-ns-guard 的協定
    b = root / "nested" / "repoB"
    (b / ".claude" / "hooks").mkdir(parents=True)
    (b / ".claude" / "hooks" / "denyjson.py").write_text(
        "import json,sys\n"
        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',\n"
        "    'permissionDecision': 'deny', 'permissionDecisionReason': 'repoB says no'}}))\n"
    )
    (b / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/denyjson.py"'}]}],
    }}))


def _run(root: Path, payload: dict, marker: Path) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)      # claude 給的是 workspace 根
    env["MARKER_FILE"] = str(marker)
    if marker.exists():
        marker.unlink()
    proc = subprocess.run([sys.executable, str(DISPATCHER)], input=json.dumps(payload),
                          text=True, capture_output=True, env=env, timeout=120, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def run_tests() -> tuple[bool, str]:  # noqa: PLR0915
    if not DISPATCHER.is_file():
        return False, f"找不到 dispatcher {DISPATCHER}"
    results: list[str] = []
    bad: list[str] = []

    with tempfile.TemporaryDirectory(prefix="disp-e2e-") as td:
        root = Path(td).resolve()
        _fixture(root)
        a, b = root / "repoA", root / "nested" / "repoB"
        marker = root / "marker.txt"

        def bash(cmd: str, cwd: Path, tool: str = "Bash", event: str = "PreToolUse") -> dict:
            return {"hook_event_name": event, "tool_name": tool,
                    "cwd": str(cwd), "tool_input": {"command": cmd}}

        cases: list[tuple[str, dict, str]] = [
            # (說明, payload, 預期)  預期："marker" / "none" / "deny" / "block"
            ("1. cwd 落在 repoA 內", bash("echo hi", a), "marker"),
            ("2. cd 在開頭", bash("cd repoA && echo hi", root), "marker"),
            ("3. cd 在指令中間", bash("echo x && cd repoA && echo hi", root), "marker"),
            ("4. git -C", bash(f"git -C {a} log --oneline -1", root), "marker"),
            ("5. pushd", bash("pushd repoA && echo hi", root), "marker"),
            ("6. 絕對路徑不 cd", bash(f"echo hi > {a}/f.txt", root), "marker"),
            ("11. 與任何 repo 無關（不得誤觸發）", bash("echo unrelated", root), "none"),
        ]
        for label, payload, expect in cases:
            rc, out, err = _run(root, payload, marker)
            got = "marker" if marker.exists() else "none"
            if got != expect:
                bad.append(f"{label}：預期 {expect}，實得 {got}（rc={rc} err={err.strip()[:60]}）")
            else:
                results.append(label.split(".")[0])

        # 12. CLAUDE_PROJECT_DIR 必須被覆寫成 repo 根
        _run(root, bash("echo hi", a), marker)
        if marker.exists():
            ev, _, proj = marker.read_text().partition("|")
            if proj != str(a):
                bad.append(f"12. CLAUDE_PROJECT_DIR 未被覆寫成 repo 根：{proj!r} != {str(a)!r}")
            else:
                results.append("12")
        else:
            bad.append("12. marker 沒寫出來，無法驗 CLAUDE_PROJECT_DIR")

        # 7. stdout JSON deny（exit 0）必須原樣傳遞
        rc, out, _ = _run(root, bash("echo hi", b), marker)
        if rc != 0:
            bad.append(f"7. stdout JSON deny 的 exit code 應為 0，實得 {rc}")
        elif "permissionDecision" not in out or "deny" not in out:
            bad.append(f"7. stdout 沒有傳遞 deny JSON：{out.strip()[:80]!r}")
        else:
            try:
                parsed = json.loads(out)
                assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"
                results.append("7")
            except Exception as exc:  # noqa: BLE001
                bad.append(f"7. stdout 不是單一合法 JSON（短路失效？）：{exc}")

        # 8. exit 2 block 必須原樣傳遞
        sa = a / ".claude" / "settings.json"
        original = sa.read_text()
        cfg = json.loads(original)
        cfg["hooks"]["PreToolUse"][0]["hooks"].append(
            {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/block.py"'})
        sa.write_text(json.dumps(cfg))
        rc, _, err = _run(root, bash("echo hi", a), marker)
        if rc != 2:
            bad.append(f"8. exit 2 未被傳遞，實得 {rc}")
        elif "repoA blocked" not in err:
            bad.append("8. stderr 未被傳遞")
        else:
            results.append("8")
        sa.write_text(original)

        # 9. PostToolUse
        rc, _, _ = _run(root, {"hook_event_name": "PostToolUse", "tool_name": "Write",
                               "cwd": str(a), "tool_input": {"file_path": str(a / "x.py")}}, marker)
        if not marker.exists():
            bad.append("9. PostToolUse 未觸發")
        elif not marker.read_text().startswith("PostToolUse"):
            bad.append(f"9. PostToolUse 事件名沒傳對：{marker.read_text()[:40]!r}")
        else:
            results.append("9")

        # 10. Edit 工具用 file_path 判定（cwd 在 workspace 根）
        rc, _, _ = _run(root, {"hook_event_name": "PreToolUse", "tool_name": "Edit",
                               "cwd": str(root),
                               "tool_input": {"file_path": str(a / "src" / "m.py")}}, marker)
        if not marker.exists():
            bad.append("10. Edit 的 file_path 判定未觸發")
        else:
            results.append("10")

    ok = not bad
    return ok, (f"12 個情境全部符合預期（{','.join(sorted(set(results), key=len))}）" if ok
                else f"{len(bad)} 個情境不符：" + "；".join(bad[:4]))


if __name__ == "__main__":
    passed, detail = run_tests()
    print(("✅ " if passed else "✗ ") + detail)
    sys.exit(0 if passed else 1)

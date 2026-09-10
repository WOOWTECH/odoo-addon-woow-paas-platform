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
    # ── group/repoA：驗 sibling 相對路徑（`cd ../repoA`）；group/other 是無 hook 的 sibling
    g = root / "group" / "repoA"
    (g / ".claude" / "hooks").mkdir(parents=True)
    (g / ".claude" / "hooks" / "marker.py").write_text(
        "import json,os,sys,pathlib\n"
        "p = json.load(sys.stdin)\n"
        "pathlib.Path(os.environ['MARKER_FILE']).write_text(\n"
        "    f\"{p.get('hook_event_name','?')}|{os.environ.get('CLAUDE_PROJECT_DIR','')}\")\n")
    (g / ".claude" / "hooks" / "slow.py").write_text("import time\ntime.sleep(3)\n")
    (g / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
            "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/marker.py"'}]}],
    }}))
    (root / "group" / "other").mkdir(parents=True)
    # ── repoC：有 settings.json 但**沒有**任何 matching hook（驗零命中告警）
    c = root / "repoC"
    (c / ".claude").mkdir(parents=True)
    (c / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"matcher": "NotebookEdit", "hooks": [{"type": "command",
            "command": "true"}]}]}}))

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


def _run_raw(root: Path | None, raw: str, *, extra_env: dict | None = None,
             unset: tuple[str, ...] = ()) -> tuple[int, str, str]:
    """餵**原始字串**給 dispatcher（驗 stdin 不是合法 JSON 之類的情境）。

    `root=None` 代表刻意不設 `CLAUDE_PROJECT_DIR`。
    """
    env = dict(os.environ)
    env.pop("OMNIGENT_DISPATCH_FAIL_OPEN", None)
    env.pop("DISPATCH_HOOK", None)
    if root is not None:
        env["CLAUDE_PROJECT_DIR"] = str(root)
    env.update(extra_env or {})
    for key in unset:
        env.pop(key, None)
    proc = subprocess.run([sys.executable, str(DISPATCHER)], input=raw, text=True,
                          capture_output=True, env=env, timeout=120, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _run(root: Path, payload: dict, marker: Path,
         extra_env: dict | None = None) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)      # claude 給的是 workspace 根
    env["MARKER_FILE"] = str(marker)
    env.pop("OMNIGENT_DISPATCH_FAIL_OPEN", None)
    env.update(extra_env or {})
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
            # ── FIX ROUND 6：path-token 解析（BLOCKING 1／NON-BLOCKING 1）────────
            # 🔴 覆核者實跑證明的漏守：從 sibling repo `cd ../repoA` 時三種比對全不中
            #   （cwd 不在 repoA 內、字串沒有 `group/repoA`、裸 `repoA` 的 regex 禁止前一
            #   字元是 `/`）。這是多 repo 共同祖先下**極常見**的寫法，不是對抗性規避。
            ("13. cd ../repoA（從 sibling repo）",
             bash("cd ../repoA && echo hi", root / "group" / "other"), "marker"),
            ("14. cd ../../group/repoA（多層回彈）",
             bash("cd ../../group/repoA && echo hi", root / "group" / "repoA"), "marker"),
            ("15. group//repoA（重複 slash）",
             bash("cd group//repoA && echo hi", root), "marker"),
            ("16. env -C ../repoA（不 cd 也作用到 repo）",
             bash("env -C ../repoA echo hi", root / "group" / "other"), "marker"),
            ("17. bash -c 包一層的相對上跳（payload 遞迴解析）",
             bash('bash -c "cd ../repoA && echo hi"', root / "group" / "other"), "marker"),
            ("18. (cd repoA; …) 子 shell 寫法",
             bash("(cd group/repoA; echo hi)", root), "marker"),
            # 絕對路徑但落在 workspace 外 ⇒ 不得誤觸發
            ("19. 絕對路徑但在 workspace 外（不得誤觸發）",
             bash("cat /etc/hostname", root), "none"),
            # ── FIX ROUND 7 / BLOCKING 1：路徑形態補完 ─────────────────────────
            ("25. cd ../repoA/subdir（repo 的子目錄）",
             bash("cd ../repoA/subdir && echo hi", root / "group" / "other"), "marker"),
            ("26. ../repoA//x（路徑中間重複 slash）",
             bash("cat ../repoA//x", root / "group" / "other"), "marker"),
            ("27. ../repoA/（尾端 slash）",
             bash("cd ../repoA/ && echo hi", root / "group" / "other"), "marker"),
            ("28. cd repoA（裸相對名，cwd 在 group）",
             bash("cd repoA && echo hi", root / "group"), "marker"),
            # ── FIX ROUND 7 / BLOCKING 1：**必須不命中**（誤擋會阻斷日常工作）──
            # 🔴 覆核實測的誤命中：舊版把 repo.name 當裸字串比對，commit message 裡
            #    提到 repo 名就命中。而誤命中與同輪的 fail-closed 修正**相乘**——誤命中
            #    的 repo 其 hook 若因任何理由失敗，一條合法的 git commit 就被擋下。
            ('29. git commit -m "fix repoA bug"（不得誤觸發）',
             bash('git commit -m "fix repoA bug"', root), "none"),
            ("30. echo ../repoAX（相近但不同名，不得誤觸發）",
             bash("echo ../repoAX", root / "group" / "other"), "none"),
            ("31. cd /tmp && ls（workspace 外，不得誤觸發）",
             bash("cd /tmp && ls", root), "none"),
            ("32. pytest 參數含 tests/（不得誤觸發）",
             bash("python3 -m pytest tests/ -k foo --maxfail=1", root), "none"),
            # 解析失敗（引號不對稱）⇒ 必須倒向保守：fallback 回寬鬆比對而多命中
            ('33. 引號不對稱（解析失敗 ⇒ 必須倒向保守命中）',
             bash('git commit -m "fix repoA bug', root), "marker"),
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

        # ── FIX ROUND 6 / BLOCKING 2：hook 失敗的事件分級 ────────────────────
        g = root / "group" / "repoA"
        gs = g / ".claude" / "settings.json"   # 下面 FIX ROUND 7 的情境也用這個
        gs.write_text(json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/slow.py"',
                "timeout": 1}]}],
            "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command",
                "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/slow.py"',
                "timeout": 1}]}],
        }}))
        # 20. PreToolUse hook timeout ⇒ **必須 fail-closed（exit 2）**
        rc, _, err = _run(root, bash("echo hi", g), marker)
        if rc != 2:
            bad.append(f"20. PreToolUse hook timeout 應 fail-closed（exit 2），實得 {rc}"
                       "——放行一個沒被檢查過的指令＝那道守線這次不存在")
        elif "拒絕執行" not in err or "怎麼處理" not in err:
            bad.append(f"20. 拒絕訊息不可診斷（缺 repo／hook／原因／逆轉方式）：{err[:120]!r}")
        else:
            results.append("20")
        # 21. PostToolUse hook timeout ⇒ 仍 warning-only（工具已執行完，擋也沒用）
        rc, _, err = _run(root, {"hook_event_name": "PostToolUse", "tool_name": "Write",
                                 "cwd": str(g), "tool_input": {"file_path": str(g / "x.py")}},
                          marker)
        if rc != 0:
            bad.append(f"21. PostToolUse hook timeout 應 warning-only，實得 exit {rc}")
        elif "timeout" not in err:
            bad.append("21. PostToolUse timeout 沒有留下 warning")
        else:
            results.append("21")
        # 22. 逃生門：OMNIGENT_DISPATCH_FAIL_OPEN=1 讓 PreToolUse 放行
        env_extra = {"OMNIGENT_DISPATCH_FAIL_OPEN": "1"}
        rc, _, err = _run(root, bash("echo hi", g), marker, extra_env=env_extra)
        if rc != 0:
            bad.append(f"22. 逃生門未生效，實得 exit {rc}")
        elif "未經檢查" not in err:
            bad.append("22. 逃生門沒有警告這是未經檢查的指令")
        else:
            results.append("22")

        # ── FIX ROUND 7 / BLOCKING 2／3：PreToolUse 的**所有**失敗形態都要 fail-closed ──
        # ⚠ ROUND 6 只補了 timeout，其餘兩條仍 fail-open（覆核實跑：exits 7 → rc=7、
        #   invalid settings JSON → rc=0）。判準是「有沒有拿到判斷」，不是失敗長什麼樣。
        (g / ".claude" / "hooks" / "crash.py").write_text(
            "import sys\nsys.stderr.write('hook deliberate failure\\n')\nsys.exit(7)\n")
        (g / ".claude" / "hooks" / "block2.py").write_text(
            "import sys\nsys.stderr.write('repo 刻意 block 的理由\\n')\nsys.exit(2)\n")

        def _gs(hook_file: str, event: str = "PreToolUse",
                matcher: str = "Bash", timeout: int | None = None) -> None:
            entry: dict = {"type": "command",
                           "command": f'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/{hook_file}"'}
            if timeout is not None:
                entry["timeout"] = timeout
            gs.write_text(json.dumps({"hooks": {event: [
                {"matcher": matcher, "hooks": [entry]}]}}))

        # 34. 非 timeout 的失敗（exit 7 ＝ crash／exception）⇒ fail-closed
        _gs("crash.py")
        rc, _, err = _run(root, bash("echo hi", g), marker)
        if rc != 2:
            bad.append(f"34. PreToolUse hook exit 7 應 fail-closed（exit 2），實得 {rc}"
                       "——hook 協定裡只有 exit 2 代表 block，其餘非 0 會被視為非阻斷性"
                       "錯誤而照跑工具")
        elif "拒絕執行" not in err or "怎麼處理" not in err:
            bad.append(f"34. 拒絕訊息不可診斷：{err[:120]!r}")
        else:
            results.append("34")

        # 35. hook 檔不存在（shell 回 127）⇒ fail-closed
        _gs("does-not-exist.py")
        rc, _, err = _run(root, bash("echo hi", g), marker)
        if rc != 2:
            bad.append(f"35. hook 檔不存在應 fail-closed，實得 {rc}")
        else:
            results.append("35")

        # 36. settings.json 讀不到／解不開 ⇒ fail-closed（且要說出是哪個檔）
        gs.write_text("{ this is not json")
        rc, _, err = _run(root, bash("echo hi", g), marker)
        if rc != 2:
            bad.append(f"36. settings.json 解析失敗應 fail-closed，實得 {rc}")
        elif "settings.json" not in err:
            bad.append(f"36. 拒絕訊息沒說出是哪個檔：{err[:120]!r}")
        else:
            results.append("36")

        # 37. ⛔ exit 2 必須**原樣透傳**，理由不可被我們的診斷訊息蓋掉
        _gs("block2.py")
        rc, _, err = _run(root, bash("echo hi", g), marker)
        if rc != 2:
            bad.append(f"37. repo hook 的 exit 2 應原樣透傳，實得 {rc}")
        elif "repo 刻意 block 的理由" not in err:
            bad.append("37. repo hook 的 block 理由沒被傳遞")
        elif "拒絕執行" in err:
            bad.append("37. repo hook 刻意的 block 被我們的診斷訊息包裝掉了"
                       "（那段 stderr 就是給使用者看的理由）")
        else:
            results.append("37")

        # 38. PostToolUse 的非 timeout 失敗 ⇒ 仍 warning-only（與文件敘述一致）
        _gs("crash.py", event="PostToolUse", matcher="Write")
        rc, _, err = _run(root, {"hook_event_name": "PostToolUse", "tool_name": "Write",
                                 "cwd": str(g), "tool_input": {"file_path": str(g / "x.py")}},
                          marker)
        if rc != 0:
            bad.append(f"38. PostToolUse hook exit 7 應 warning-only（文件如此敘述），"
                       f"實得 exit {rc}")
        elif "不擋" not in err:
            bad.append("38. PostToolUse 失敗沒留下 warning")
        else:
            results.append("38")

        # 39. ⛔ 命中的 repo **根本沒有** settings.json ⇒ 正常情況，絕不可擋
        #     分界點：「宣稱有守線却載不起來」才擋，「本來就沒有守線」不擋。
        nb = root / "plainrepo"
        (nb / ".claude").mkdir(parents=True)
        rc, _, err = _run(root, bash("echo hi", nb), marker)
        if rc != 0:
            bad.append(f"39. 沒有 settings.json 的 repo 不該被擋，實得 exit {rc}")
        else:
            results.append("39")

        # ── FIX ROUND 8：**dispatcher 自己**拿不到判斷時也要 fail-closed ──────────
        # 🔴 這層比「repo hook 失敗」更危險：repo hook 失敗只影響那一個 repo，dispatcher
        #    自己壞掉（例如 CLAUDE_PROJECT_DIR 沒設）會讓**所有 repo 的守線一起靜默消失**，
        #    而唯一徵兆是一個「零命中告警」——那個告警區分不了「真的無關指令」與「守線全掉」。
        def _pl(event: str = "PreToolUse", **over) -> str:
            base: dict = {"hook_event_name": event, "tool_name": "Bash",
                          "cwd": str(a), "tool_input": {"command": "echo hi"}}
            base.update(over)
            return json.dumps({k: v for k, v in base.items() if v is not None})

        untrusted_cases: list[tuple[str, dict]] = [
            ("40. stdin 不是合法 JSON", {"raw": "{ this is not json"}),
            ("41. 缺 tool_name", {"raw": _pl(tool_name=None)}),
            ("42. 缺 cwd", {"raw": _pl(cwd=None)}),
            ("43. cwd 指向不存在的目錄（在 repo 底下）",
             {"raw": _pl(cwd=str(a / "nope" / "deep"))}),
            ("44. cwd 指向不存在的目錄（在 repo 外）", {"raw": _pl(cwd="/nonexistent/zzz")}),
            ("45. CLAUDE_PROJECT_DIR 未設", {"raw": _pl(), "root": None,
                                             "unset": ("CLAUDE_PROJECT_DIR",)}),
            ("46. CLAUDE_PROJECT_DIR 指向不存在的目錄",
             {"raw": _pl(), "extra_env": {"CLAUDE_PROJECT_DIR": "/nonexistent/ws"}}),
        ]
        for label, kw in untrusted_cases:
            rc, _, err = _run_raw(kw.pop("root", root), kw.pop("raw"), **kw)
            if rc != 2:
                bad.append(f"{label}：PreToolUse 下 dispatcher 自己拿不到判斷應 fail-closed"
                           f"（exit 2），實得 {rc}——靜默放行會讓所有 repo 的守線一起消失")
            elif "拒絕執行" not in err or "怎麼逆轉" not in err:
                bad.append(f"{label}：拒絕訊息不可診斷（缺原因或逆轉方式）：{err[:100]!r}")
            else:
                results.append(label.split(".")[0])

        # 47. dispatcher 自己拋出未預期例外 ⇒ 也要落到 exit 2（不能讓 Python 以非 2 收場）
        with tempfile.TemporaryDirectory(prefix="disp-boom-") as td2:
            boom = Path(td2) / "dispatch-hook.py"
            boom.write_text(DISPATCHER.read_text().replace(
                "    matched_repos = targets(payload)",
                "    raise AttributeError('刻意注入的未預期例外')", 1))
            env = dict(os.environ)
            env["CLAUDE_PROJECT_DIR"] = str(root)
            env.pop("DISPATCH_HOOK", None)
            env.pop("OMNIGENT_DISPATCH_FAIL_OPEN", None)
            pr = subprocess.run([sys.executable, str(boom)], input=_pl(), text=True,
                                capture_output=True, env=env, timeout=120, check=False)
            if pr.returncode != 2:
                bad.append(f"47. 未預期例外應落到 exit 2，實得 {pr.returncode}"
                           "（非 2 會被 Claude Code 視為非阻斷性錯誤而照跑工具）")
            elif "AttributeError" not in pr.stderr:
                bad.append("47. 未預期例外的 traceback 沒進 stderr（無法診斷）")
            else:
                results.append("47")

        # 48. 同樣情境在 PostToolUse 下一律 warning-only（工具已執行完，擋也沒用）
        post_ok = True
        for label, kw in [("缺 tool_name", {"raw": _pl("PostToolUse", tool_name=None)}),
                          ("缺 cwd", {"raw": _pl("PostToolUse", cwd=None)}),
                          ("CLAUDE_PROJECT_DIR 未設",
                           {"raw": _pl("PostToolUse"), "root": None,
                            "unset": ("CLAUDE_PROJECT_DIR",)})]:
            rc, _, _ = _run_raw(kw.pop("root", root), kw.pop("raw"), **kw)
            if rc != 0:
                bad.append(f"48. PostToolUse／{label} 應 warning-only，實得 exit {rc}")
                post_ok = False
        if post_ok:
            results.append("48")

        # ── ⛔ 以下兩種**不該擋**，擋了就是把整台機器 brick 掉 ───────────────────
        # 49. WS_ROOT 存在但底下沒有任何帶 .claude/settings.json 的 repo ⇒ 正常情況
        with tempfile.TemporaryDirectory(prefix="disp-empty-ws-") as td3:
            empty_ws = Path(td3)
            rc, _, err = _run_raw(empty_ws, json.dumps(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "cwd": str(empty_ws), "tool_input": {"command": "echo hi"}}))
            if rc != 0:
                bad.append(f"49. WS_ROOT 存在但無 repo 不該擋（那代表本來就沒有守線要轉），"
                           f"實得 exit {rc}")
            else:
                results.append("49")

        # 50／51. 緊急逆轉門（照 kubectl-ns-guard.py 的慣例：env 形式 ＋ 指令前綴形式）
        rc, _, err = _run_raw(None, _pl(), extra_env={"DISPATCH_HOOK": "off"},
                              unset=("CLAUDE_PROJECT_DIR",))
        if rc != 0:
            bad.append(f"50. export DISPATCH_HOOK=off 應放行，實得 exit {rc}")
        elif "關掉所有" not in err:
            bad.append("50. 逆轉門沒警告「這等於關掉所有 repo 的守線」")
        else:
            results.append("50")
        rc, _, err = _run_raw(None, json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(a),
             "tool_input": {"command": "DISPATCH_HOOK=off kubectl get pods"}}),
            unset=("CLAUDE_PROJECT_DIR",))
        if rc != 0:
            bad.append(f"51. 指令前綴 DISPATCH_HOOK=off（一次性）應放行，實得 exit {rc}")
        else:
            results.append("51")

        # ── X4：零命中告警 ───────────────────────────────────────────────────
        # 23. 命中 repo 但沒有 matching hook ⇒ 要有 warning
        rc, _, err = _run(root, bash("echo hi", root / "repoC"), marker)
        if "沒有任何" not in err:
            bad.append(f"23. 零命中但沒有告警：{err[:100]!r}")
        else:
            results.append("23")
        # 24. workspace 根的無關指令 ⇒ **不得**有告警（否則全是噪音）
        rc, _, err = _run(root, bash("echo unrelated", root), marker)
        if err.strip():
            bad.append(f"24. 無關指令竟然有告警（噪音）：{err[:100]!r}")
        else:
            results.append("24")

    ok = not bad
    n = len(set(results))
    if ok:
        return True, f"{n} 個情境全部符合預期"
    # ⚠ 先印**所有**失敗情境的精簡標籤再附細節：一個弱化改動可能同時弄壞十幾個情境，
    #   只印前幾筆細節會把真正該看的那個情境截掉（也讓突變測試的 EXPECT_KEY 失準）。
    labels = [b.split("：", 1)[0].strip() for b in bad]
    return False, (f"{len(bad)} 個情境不符 [{'; '.join(labels)}]"
                   f"｜細節：" + "；".join(bad[:3]))


if __name__ == "__main__":
    passed, detail = run_tests()
    print(("✅ " if passed else "✗ ") + detail)
    sys.exit(0 if passed else 1)

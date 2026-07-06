#!/usr/bin/env python3
"""PreToolUse pattern-guard：編輯守護路徑前的 read-first speed-bump。

機制：
- 讀 stdin 的 hook JSON（tool_input.file_path），對照 pattern-guard-map.json。
- 本 session 首次觸碰某類守護路徑 → deny 一次，訊息列出必讀 pattern/pitfall docs，
  並寫 marker；**重試同一編輯即放行**（marker 存在）。
- 這是 CLAUDE.md「改 X 前必看 Y」清單的機器強制版：不靠模型記得，靠 hook 餵到嘴邊。

Fail-open 原則：任何解析錯誤 / 缺檔 / 非本 repo 路徑 → exit 0 放行，絕不擋開發。
跳過整個守衛：export CLAUDE_PATTERN_GUARD=off
新增守護路徑：改 pattern-guard-map.json（不用動本檔）。
"""
import json
import os
import re
import sys
import tempfile


def allow():
    sys.exit(0)


def main():
    if os.environ.get("CLAUDE_PATTERN_GUARD", "").lower() in ("off", "0", "false"):
        allow()

    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()

    file_path = (payload.get("tool_input") or {}).get("file_path") or ""
    session_id = payload.get("session_id") or ""
    if not file_path or not session_id:
        allow()

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ""
    if not project_dir:
        allow()

    try:
        rel = os.path.relpath(os.path.abspath(file_path), os.path.abspath(project_dir))
    except ValueError:
        allow()
    if rel.startswith(".."):
        allow()  # 不在本 repo，不歸本守衛管
    rel = rel.replace(os.sep, "/")

    map_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pattern-guard-map.json")
    try:
        with open(map_path, encoding="utf-8") as f:
            guard_map = json.load(f)
    except Exception:
        allow()

    # 收集所有命中的 class（可多重命中，如 agent_template_default.xml 同時命中 seed-data 與 seed-default-template）
    hits = []
    for entry in guard_map.get("classes", []):
        try:
            if re.search(entry["pattern"], rel):
                hits.append(entry)
        except re.error:
            continue

    if not hits:
        allow()

    marker_dir = os.path.join(tempfile.gettempdir(), "claude-pattern-guard")
    os.makedirs(marker_dir, exist_ok=True)

    fresh = []
    for entry in hits:
        marker = os.path.join(marker_dir, "%s.%s" % (session_id, entry["class"]))
        if not os.path.exists(marker):
            fresh.append(entry)
            try:
                open(marker, "w").close()
            except OSError:
                pass

    if not fresh:
        allow()  # 本 session 全部類別都提醒過了

    lines = ["[pattern-guard] 你正在編輯守護路徑 %s，本 session 首次觸碰以下類別，先讀守線再動手：" % rel]
    for entry in fresh:
        lines.append("")
        lines.append("■ %s — %s" % (entry["class"], entry.get("why", "")))
        for doc in entry.get("docs", []):
            lines.append("  - %s" % doc)
    lines.append("")
    lines.append(
        "這不是使用者拒絕，是 read-first 守衛（CLAUDE.md「改 X 前必看」的機器強制版）。"
        "用 Read 讀完上列文件（或本 session 已讀過）後，**重試同一個編輯即會放行**。"
        "整段關閉：export CLAUDE_PATTERN_GUARD=off。"
    )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "\n".join(lines),
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        allow()

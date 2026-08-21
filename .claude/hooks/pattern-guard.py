#!/usr/bin/env python3
"""PreToolUse pattern-guard：編輯守護路徑前的 read-first speed-bump。

機制（兩套並存，**引用索引優先**）：

- 讀 stdin 的 hook JSON（tool_input.file_path），對照 pattern-guard-map.json。
- 命中守護路徑後分兩條路走：
  1. 被編輯的檔**逐字引用**了 pattern doc（`scripts/audit/doc_citation_index.py`）
     → 只推那幾份。改 `src/models/cloud_service.py` 從 15 份 / ~2,785 行收斂到 4 份，
     省下的是「每次改 code 前被強制讀 doc」的閱讀量（#585 的整個價值在這裡）。
  2. **沒有**引用 → 原封退回下面的 class 推播。
- 本 session 首次觸碰 → deny 一次並寫 marker；**重試同一編輯即放行**。
- 這是 CLAUDE.md「改 X 前必看 Y」清單的機器強制版：不靠模型記得，靠 hook 餵到嘴邊。

🔴 **class 推播不可關掉也不可降級。** `src/models/skill.py` 這類檔引用索引命中 0 份，
關掉等於零推播——漏推比多推貴得多。引用索引是**收斂器**，不是取代品。

Fail-open 原則：任何解析錯誤 / 缺檔 / 非本 repo 路徑 → exit 0 放行，絕不擋開發。
⚠ 但**引用索引這條新路徑失敗時會出聲**（訊息裡明講「引用索引無法使用」再退回 class
推播）。既有的四種 fail-open（JSON 壞／缺 pattern／regex 編不起來／docs 路徑不存在）
都是靜默的，這裡刻意不沿用：靜默失效會讓「收斂沒生效」長得跟「這個檔本來就沒引用」
一模一樣，而兩者該做的事完全不同。

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


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def cited_docs(project_dir, file_path):
    """回 `(slug 清單, 錯誤描述)`。索引不可用時回 `(None, 原因)` 讓呼叫端出聲。"""
    mod_dir = os.path.join(project_dir, "scripts", "audit")
    mod_path = os.path.join(mod_dir, "doc_citation_index.py")
    if not os.path.exists(mod_path):
        return None, "找不到 scripts/audit/doc_citation_index.py"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_pg_doc_citation_index", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        slugs = mod.pattern_doc_slugs(project_dir)
        if not slugs:
            return None, "pattern doc 目錄讀不到或是空的"
        hits = mod.cited_docs_for_file(file_path, slugs)
        if hits is None:
            return None, "讀不到被編輯的檔案內容"
        return hits, None
    except Exception as exc:  # noqa: BLE001 — 任何壞法都要能退回並出聲
        return None, "%s: %s" % (type(exc).__name__, exc)


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
        allow()  # 不在守護路徑上 —— 有沒有引用都不歸本守衛管

    marker_dir = os.path.join(tempfile.gettempdir(), "claude-pattern-guard")
    os.makedirs(marker_dir, exist_ok=True)

    # ── 路徑 1：本檔逐字引用了 pattern doc → 只推那幾份 ────────────────────
    cited, cite_error = cited_docs(os.path.abspath(project_dir), os.path.abspath(file_path))
    if cited:
        # marker 掛在檔案上：推的內容是 per-file 的，不是 per-class
        marker = os.path.join(
            marker_dir, "%s.cite.%s" % (session_id, rel.replace("/", "_")))
        if os.path.exists(marker):
            allow()
        try:
            open(marker, "w").close()
        except OSError:
            pass
        lines = [
            "[pattern-guard] 你正在編輯 %s。本檔的程式碼裡**逐字引用**了以下 pattern doc，"
            "先讀它們再動手（引用索引已把該讀的收斂到這幾份）：" % rel,
        ]
        for slug in cited:
            lines.append("  - docs/decisions/patterns/%s.md" % slug)
        lines.append("")
        lines.append(
            "若你的改動推翻了上列任何一份的主張，請在同一個 PR 裡更新那份 doc —— "
            "會引用它的人，正是此刻最有能力發現它過期的人。"
        )
        lines.append("")
        lines.append(
            "⚠ 收斂的代價：**未被任何 code 逐字引用的 pattern doc 不會出現在這裡**。"
            "要看是哪些：python3 scripts/audit/doc_citation_index.py --uncited"
        )
        lines.append(
            "這不是使用者拒絕，是 read-first 守衛。讀完後**重試同一個編輯即會放行**。"
            "整段關閉：export CLAUDE_PATTERN_GUARD=off。"
        )
        deny("\n".join(lines))

    # ── 路徑 2：沒有引用（或索引失效）→ 維持原本的整組 class 推播 ──────────
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
    if cite_error:
        lines.append(
            "⚠ 引用索引無法使用（%s），本次已退回完整 class 推播 —— "
            "推的份數會比預期多，不是你的改動造成的。" % cite_error
        )
        lines.append("")
    lines.append(
        "這不是使用者拒絕，是 read-first 守衛（CLAUDE.md「改 X 前必看」的機器強制版）。"
        "用 Read 讀完上列文件（或本 session 已讀過）後，**重試同一個編輯即會放行**。"
        "整段關閉：export CLAUDE_PATTERN_GUARD=off。"
    )

    deny("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        allow()

#!/usr/bin/env python3
"""PostToolUse memory-links-guard：memory 目錄被寫入後，立即跑 index-links 對帳。

觸發條件：被編輯的檔案位於任一 `…/.claude/projects/<slug>/memory/` 底下（含
MEMORY.md 與個別 memory 檔）。其他路徑一律 exit 0 快速放行，不跑任何子程序——
本 repo 的一般編輯只多付一次 stdin JSON 解析的成本。

為什麼掛在這裡而不是 CI（#683 的正反分析結論，見該 issue 留言）：

- `memory-integrity.py` 檢查的是**本機** `~/.claude/projects/<slug>/memory/`，
  CI runner 上不存在，結構性走不通。
- memory 的變更不走 PR，唯一與變更時點對齊的觸發點就是「寫入 memory 檔的那一刻」
  ——PR-triggered CI 即使拿得到資料也只是隨機抽查。
- 只掛 `--links`（斷鏈／孤兒集合對帳，機械可證偽、低誤報）。`--truncation` 誤報率
  高（#683 抽樣 5/5 誤報），刻意不掛：高誤報的自動警告會消耗信任額度、加速自我
  瓦解，等 #683 的清帳＋baseline 機制落地再議。

⚠ 這是提高執行頻率的 speed-bump，**不是強制 gate**（#683 提案 3 的誠實標示）：
  hook 失效與「沒有 finding」在輸出上相同，守住 wrapper 邏輯本身的是
  scripts/audit/test_memory_links_guard.py（CI 每 PR 跑，含注入紅燈的陽性對照）。

memory 目錄與 repo 基準的推導：

- memory 目錄**從被編輯檔案的路徑本身**取出（`…/.claude/projects/<slug>/memory/`
  的前綴），不經 `memory-integrity.py` 的 slug 推導——session 可能跑在
  `.maestri/roles/` 子目錄或 worktree，slug 推導在那些 checkout 會 fail-closed
  （見 memory-integrity.py 檔頭 ⚠ 第 1 點），而檔案路徑永遠指著真正被寫入的那個
  memory 目錄。
- repo 相對路徑基準用 `$CLAUDE_PROJECT_DIR`（settings.json 由哪個 checkout 載入，
  基準就是那棵樹），沒有時退回本檔所在 checkout 的 root。從 worktree 觸發時基準是
  worktree root，MEMORY.md 指向 `docs/…` 的連結會以該分支的樹解析——這正是
  memory-integrity.py 檔頭 ⚠ 第 2 點描述的行為，該腳本會無條件印出兩個基準，
  誤報時看輸出即可辨認。

Fail-open：stdin JSON 壞／找不到 memory-integrity.py／子程序逾時或炸掉 → exit 0，
絕不擋開發（與 repo-lint --hook 的既有慣例一致）。
"""
import json
import os
import re
import subprocess
import sys

# 被編輯檔案要匹配的 memory 目錄形態。錨定 `.claude/projects/<slug>/memory/`
# 這一段（slug 不含 /），前綴不限定 $HOME——測試用 fixture 目錄也要能觸發。
MEMORY_DIR_RE = re.compile(r"^(?P<mem>.*/\.claude/projects/[^/]+/memory)/.+")

# reason 最多帶回的輸出行數（斷鏈會逐條列「查過：」候選，全帶會灌爆 context）
MAX_REASON_LINES = 40


def project_root():
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return env
    # 本檔位於 <root>/.claude/hooks/，往上兩層就是 checkout root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    data = json.load(sys.stdin)
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        return
    match = MEMORY_DIR_RE.match(os.path.abspath(file_path))
    if not match:
        return
    mem_dir = match.group("mem")

    root = project_root()
    script = os.path.join(root, "scripts", "audit", "memory-integrity.py")
    if not os.path.isfile(script):
        return  # fail-open：settings.json 掛在沒有本 repo 內容的 checkout 上

    proc = subprocess.run(
        [sys.executable, script, "--links",
         "--memory-dir", mem_dir, "--repo-root", root],
        capture_output=True, text=True, timeout=8,
    )
    if proc.returncode == 0:
        return

    lines = (proc.stdout + proc.stderr).strip().splitlines()
    if len(lines) > MAX_REASON_LINES:
        lines = lines[:MAX_REASON_LINES] + [
            f"…（共 {len(lines)} 行，其餘截斷；完整輸出請跑 "
            f"python3 scripts/audit/memory-integrity.py --links）"]
    print(json.dumps({
        "decision": "block",
        "reason": (
            "剛才的 memory 寫入後，MEMORY.md 索引與 memory 目錄實體對帳有發現"
            "（斷鏈＝索引連到的檔不存在；孤兒＝檔案存在但索引沒連到、失去入口）。"
            "請立即修 MEMORY.md 或補索引行：\n" + "\n".join(lines)
        ),
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open：hook 絕不因自身故障擋開發
    sys.exit(0)

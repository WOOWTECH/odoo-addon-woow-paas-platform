#!/usr/bin/env python3
"""產生 docs/index.md —— docs 的 progressive disclosure 入口。

數字與目錄清單全部從 git 追蹤的檔案即時算出，不手寫，避免與實況漂移。
產生器與 --check 共用列檔口徑時，--check 只能驗證兩者一致，無法發現列檔偏差；
因此 docs/*.md 在 git add 前若未另行攔截，可能造成假綠。
用法：python3 scripts/audit/gen-docs-index.py [--check]
  --check 驗證現有 index.md 是否與 git 追蹤實況相符（供 CI / repo-lint 用），不寫檔。
"""
import subprocess, sys, collections
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs/index.md"

untracked_files = [f for f in subprocess.run(
    ["git", "ls-files", "--others", "--exclude-standard", "docs/"],
    cwd=REPO, capture_output=True, text=True
).stdout.splitlines() if f.endswith(".md")]
if untracked_files:
    print("❌ 偵測到未追蹤的 docs Markdown 檔案：", file=sys.stderr)
    for f in untracked_files:
        print(f"  - {f}", file=sys.stderr)
    print("請先 git add 再跑 gen-docs-index.py。", file=sys.stderr)
    sys.exit(1)

files = [f for f in subprocess.run(
    ["git", "ls-files", "docs/"], cwd=REPO, capture_output=True, text=True
).stdout.splitlines() if f.endswith(".md")]

by_dir = collections.Counter()
for f in files:
    parts = f.split("/")
    by_dir["/".join(parts[1:3]) if len(parts) > 3 else (parts[1] if len(parts) > 2 else "(頂層)")] += 1

def n(prefix):
    return sum(1 for f in files if f.startswith(f"docs/{prefix}"))

ROUTES = [
    ("我要照步驟做一件事", "guides/", [
        ("guides/deployment", "上 prod／stg、發版、升版、image 重釘"),
        ("guides/testing", "跑測試、E2E、本機沙盒"),
        ("guides/development", "開發期工具與架構"),
    ]),
    ("我要查一個事實或規格", "reference/", [
        ("reference/api", "HTTP / JSON API 端點"),
        ("reference/features", "功能規格與產品行為"),
        ("reference/gotchas", "框架與基礎設施的行為陷阱（踩過才知道的那種）"),
        ("reference/architecture", "生命週期與架構圖"),
    ]),
    ("我想知道為什麼這樣設計", "decisions/", [
        ("decisions/patterns", "可複用的設計 pattern（有 hook 強制閱讀）"),
        ("decisions/philosophy", "整體取向"),
        ("decisions/refactor", "重構決策"),
        ("decisions/reviews", "PR review 裁決"),
    ]),
    ("我要啟動一個 epic / 大功能", "plans/", [
        ("plans", "按季度分目錄，design + implementation 雙檔"),
    ]),
    ("我要查某張 issue 當初怎麼收的、或某個坑", "tracking/", [
        ("tracking/landings", "已收官 issue 的落地紀錄（檔名帶 issue 編號）"),
        ("tracking/pitfalls", "撞牆時對照：症狀 → 根因"),
        ("tracking/followups", "尚未收掉的追蹤項"),
        ("tracking/test-reports", "E2E / 測試報告"),
    ]),
]

L = ["# docs 索引", "",
     "> 這是 **routing 入口**：先問「我現在想做什麼」，再往下鑽。",
     "> 分類的定義與寫新檔該放哪，見 [`README.md`](README.md)。",
     "> 本檔由 `scripts/audit/gen-docs-index.py` 產生，數字即時算出——**不要手改**。", ""]
L.append(f"目前共 **{len(files)}** 份文件（含 `archived/` {n('archived/')} 份）。\n")

for q, top, subs in ROUTES:
    L.append(f"## {q}")
    L.append("")
    L.append("| 去哪 | 內容 | 篇數 |")
    L.append("|---|---|---|")
    for d, desc in subs:
        L.append(f"| [`{d}/`]({d.split('/',1)[1] if d.startswith('docs/') else d}/) | {desc} | {n(d + '/')} |")
    L.append("")

L.append("## 其他")
L.append("")
L.append("| 去哪 | 內容 | 篇數 |")
L.append("|---|---|---|")
L.append(f"| [`archived/`](archived/) | 不再使用但保留考古（含 `archived/memory/`：已被其他載體取代的 memory） | {n('archived/')} |")
L.append(f"| [`scout/`](scout/) | 決策前偵察，拍板後 promote 到 plans/ 或 decisions/ | {n('scout/')} |")
L.append(f"| [`agents/`](agents/) | mattpocock-skills 工具自管理區 | {n('agents/')} |")
L.append(f"| [`superpowers/`](superpowers/) | 已退役 obra/superpowers plugin 的舊產出區（只剩歷史） | {n('superpowers/')} |")
L.append("")
L.append("> `docs/meeting/` 是指向 repo 外錄音目錄的 symlink，不在版控內，也不屬上述分類。")
L.append("")

text = "\n".join(L)

if "--check" in sys.argv:
    cur = OUT.read_text() if OUT.exists() else ""
    if cur != text:
        print("❌ docs/index.md 與實況不符，請重跑 gen-docs-index.py")
        sys.exit(1)
    print("✓ docs/index.md 與實況相符")
    sys.exit(0)

OUT.write_text(text)
print(f"✓ 已產生 {OUT.relative_to(REPO)}（{len(files)} 份文件）")

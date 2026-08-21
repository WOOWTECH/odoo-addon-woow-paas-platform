#!/usr/bin/env python3
"""pattern doc 引用反向索引（#585）：從 code 裡逐字寫出的 slug 反查「該複查哪幾份」。

## 為什麼是反向索引而不是別的

`pattern-guard` 現行用 **class 路徑** 決定推哪些 doc：改 `src/models/cloud_service.py`
就把 `model-write-guard` 那一整組推出來——實測 15 份 / 2,785 行 / ~100k tokens。但那
15 份裡真正與這次改動相關的通常只有兩三份，其餘是「同一類路徑」這個粗粒度分類的副作用。

程式碼裡其實已經逐字寫著答案：改動者讀了某份 doc、把它的 slug 寫進 docstring 或註解。
「某檔引用某 doc」是**訊號最強的事件**——引用者此刻正在讀那份 doc，最有能力發現它過期。
索引每次現算，不落 baseline 檔（不製造第四個會漂的登記處）。

## 涵蓋的引用形態（先實查 repo 再定，不是先寫 regex 再看它抓到什麼）

🔴 **下面每個數字都是 `76243241`（2026-08-07 develop）的快照，會過期。**#585 自己就記錄
了「兩次量測相隔一天、六個指標動了五個」。它們在這裡的作用是**佐證「這個形態真的存在、
值得涵蓋」**，不是契約——要當下的值請跑 `--stats`，不要引用這段。

1. 裸 slug（`# 見 capability-gate-single-source-field：…`）—— 54 命中，最大宗
2. 完整路徑（`docs/decisions/patterns/xxx.md`）—— 30 命中
3. `[[wiki-link]]` —— 21 命中
4. 🔴 **slug 在連字號處換行續行** —— 4 處，**單行 `git grep -F` 結構上抓不到**：

       # of the same teardown contract ([[two-stage-remote-
       # provision-idempotency]]: every failure path keeps ...

   #585 的量法是純單行 grep，因此它給的「56 檔」是低估，實際 58 檔。其中兩處漏的正是
   #585 §一 拿來當核心案例的那份 doc。

5. 花括號 `xxx-{design,implementation}.md` —— repo 內確有 3 處，但**全部指向
   `docs/plans/`**，對 pattern doc 索引 0 適用。列在這裡是為了記錄「查過、不適用」，
   免得下次有人重查一遍。

做法是把換行與續行的註解前綴壓掉之後再比對，因此 1–4 全部涵蓋。**只壓縮、不插入
任何字元**——這樣「兩行各自無關」不會被接出一個不存在的 slug（測試有守）。

## 🔴 覆蓋邊界（不要把綠讀成「doc 是對的」）

- **不驗證引用內容的正確性。** #585 §一 那個案例裡，code 的引用句本身是對的、錯的是
  doc。本索引只能把 doc 推到人面前，**判斷仍然是人的**。
- **對零引用的 doc 完全沒有訊號。** 那些 doc 的唯一訊號來源仍是 class 推播。
  `--uncited` 會把它們列出來，`pattern-guard` 收斂時也會在訊息裡明講這個盲區。
- 🔴 **絕大多數守護路徑上的檔案根本沒有任何引用**（`76243241` 實測：467 個守護檔裡
  414 個零引用，89%）。`src/models/skill.py`、`security_access_template.py`、
  `cloud_service_secret.py` 都是這種。**這就是 class 推播不可關掉的理由**——引用索引
  是收斂器，不是取代品；關掉 class 推播會讓這 89% 變成零推播，而漏推比多推貴得多。
- ⚠ **收斂不保證行數一定變少。** `76243241` 實測 `src/models/project_project.py`：
  class 推 2 份 / 328 行，引用索引也是 2 份、但 562 行（它引用的兩份比較長）。本索引
  優先的是**精準度**（這個檔真的引用了什麼），不是行數最小化——把行數當唯一判準會導向
  「推最短的那幾份」這種明顯錯誤的機制。
- 只看 `src/` 與 `extra/`；`docs/` 內部的互相引用不算（那是 doc 治理，不是 code 訊號）。
"""
import argparse
import os
import re
import sys

PATTERNS_REL = "docs/decisions/patterns"
CODE_ROOTS = ("src", "extra")
# 可能承載引用的文字副檔名（其餘視為二進位／不相關，直接跳過）
TEXT_EXTS = (".py", ".js", ".xml", ".csv", ".yml", ".yaml", ".json",
             ".txt", ".sh", ".scss", ".css", ".cfg", ".toml", ".md")
# 掃描時要剪掉的目錄。`.claude/worktrees` 是整個 repo 的副本，不剪掉會讓統計自我污染。
PRUNE_DIRS = frozenset((
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
))
PRUNE_RELS = frozenset((".claude/worktrees",))

# 把「換行 + 續行的註解前綴 / 縮排」壓掉。只刪不加，見上方 docstring。
_CONTINUATION_RE = re.compile(r"\n[ \t]*(?:#|\*|//|--)?[ \t]*")


def pattern_doc_slugs(root):
    """`docs/decisions/patterns/*.md` 的 slug（排除 README）。目錄不存在則回空。"""
    d = os.path.join(root, PATTERNS_REL)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return sorted(n[:-3] for n in names
                  if n.endswith(".md") and n != "README.md")


def cited_docs(text, slugs):
    """`text` 逐字引用了哪些 slug（已涵蓋連字號斷行的續行形態）。"""
    if not text or not slugs:
        return []
    flat = _CONTINUATION_RE.sub("", text)
    return sorted(s for s in slugs if s in flat)


def cited_docs_for_file(path, slugs):
    """讀單一檔案並回傳它引用的 slug。讀不到 → 回 None（**與「沒有引用」不同**）。

    區分這兩者是刻意的：呼叫端要能分辨「這個檔真的沒引用」與「我沒讀到，別下結論」。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return cited_docs(raw.decode("utf-8", "replace"), slugs)


def _walk_code_files(root):
    for base in CODE_ROOTS:
        top = os.path.join(root, base)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            dirnames[:] = [x for x in dirnames
                           if x not in PRUNE_DIRS
                           and "%s/%s" % (rel_dir, x) not in PRUNE_RELS]
            for n in filenames:
                if n.endswith(TEXT_EXTS):
                    yield os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/")


def build_index(root):
    """回 `{相對路徑: [slug, ...]}`，只含至少有一個引用的檔。"""
    slugs = pattern_doc_slugs(root)
    index = {}
    for rel in _walk_code_files(root):
        hits = cited_docs_for_file(os.path.join(root, rel), slugs)
        if hits:
            index[rel] = hits
    return index


def uncited_slugs(root, index=None):
    """沒有被任何 code 檔逐字引用的 slug——本機制的盲區，必須能被列出來。"""
    slugs = pattern_doc_slugs(root)
    idx = build_index(root) if index is None else index
    cited = {s for v in idx.values() for s in v}
    return [s for s in slugs if s not in cited]


# --------------------------------------------------------------------- #
# CLI                                                                     #
# --------------------------------------------------------------------- #
def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description="pattern doc 引用反向索引（#585）")
    ap.add_argument("--root", default=None, help="repo 根目錄（預設：本檔往上兩層）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help="列出單一檔案引用了哪些 pattern doc")
    g.add_argument("--uncited", action="store_true", help="列出零引用的 doc（本機制的盲區）")
    g.add_argument("--stats", action="store_true", help="六個指標")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root or _repo_root())

    if args.file:
        slugs = pattern_doc_slugs(root)
        path = args.file if os.path.isabs(args.file) else os.path.join(root, args.file)
        hits = cited_docs_for_file(path, slugs)
        if hits is None:
            print("讀不到 %s" % args.file, file=sys.stderr)
            return 2
        if not hits:
            print("%s：沒有逐字引用任何 pattern doc（該檔仍由 class 推播覆蓋）" % args.file)
            return 0
        print("%s 逐字引用了 %d 份：" % (args.file, len(hits)))
        for s in hits:
            print("  - %s/%s.md" % (PATTERNS_REL, s))
        return 0

    index = build_index(root)
    if args.uncited:
        un = uncited_slugs(root, index)
        print("零引用的 pattern doc = %d 份（本機制對它們不出聲，只有 class 推播會）：" % len(un))
        for s in un:
            print("  - %s/%s.md" % (PATTERNS_REL, s))
        return 0

    slugs = pattern_doc_slugs(root)
    allf = list(_walk_code_files(root))
    cited = {s for v in index.values() for s in v}
    points = sum(len(v) for v in index.values())
    dist = {}
    for v in index.values():
        dist[len(v)] = dist.get(len(v), 0) + 1
    print("pattern doc 總數（排除 README） = %d" % len(slugs))
    print("被逐字引用的 doc            = %d" % len(cited))
    print("引用點（檔×doc 去重後）      = %d" % points)
    print("含至少一個引用的檔案         = %d / %d (%.1f%%)"
          % (len(index), len(allf), len(index) * 100.0 / max(len(allf), 1)))
    print("每檔最多要複查幾份            = %d" % max([len(v) for v in index.values()] or [0]))
    print("零引用的 doc                = %d" % (len(slugs) - len(cited)))
    print("每檔引用數分布               = %s" % dict(sorted(dist.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

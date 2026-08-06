#!/usr/bin/env python3
"""memory-integrity：Claude memory 目錄的完整性 gate。

檢查對象是**本機** `~/.claude/projects/<slug>/memory/`（依 repo 路徑推導 slug），
不是 repo 內容——但它守的東西與 repo 直接相關：memory 目錄的 103 篇已於 2026-08-05
搬進 `docs/`，那次搬移正是下面兩條 check 的來由。

兩個 check（各對應一次真實翻車，兩次都是零訊號）：

1. index-links     MEMORY.md 的連結與 memory 目錄實體對帳，雙向：
                   (a) 斷鏈——索引連到的檔不存在；
                   (b) 孤兒——檔案存在但索引沒連到，等於失去入口。
                   相對路徑會**同時**以兩個基準嘗試解析（memory 目錄優先、
                   再來 repo root），見 `resolve_link()`；
                   「算不算某個 memory 檔的入口」則一律看**解析結果落在哪**，
                   不看連結字面，見 `_is_memory_entry()` 的 🔴 註解。
                   2026-08-05 實測 60 斷鏈 / 27 孤兒：整併時把 103 篇搬進 docs/
                   且**檔名同時被改寫**（project_501_global_cap_landing.md →
                   501-global-cap-landing.md），但留在 MEMORY.md 的行沒跟著改。
                   當時的完備性檢查只覆蓋「搬走的檔」，沒覆蓋「留下的行對搬走的檔
                   的引用」——全程沒有任何錯誤訊息。其中 5 篇是檔案還在、入口全失，
                   不查就永遠讀不到。
                   ⚠ 斷鏈與孤兒是**不同機制**，只驗一邊會漏另一邊。

2. silent-truncation
                   用 `~/.claude/file-history/<session>/<hash>@vN`（Claude Code 在
                   編輯前存的舊版）比對每個 memory 檔的**所有歷史快照 vs 現況**，
                   找被整份 Write 靜默覆蓋掉的內容。多 session 對同一檔 append 時
                   每筆都落地、互不覆蓋，但**有人整份寫回就靜默蓋掉先前的 append**，
                   且改完的檔看起來完全正常。
                   依據 memory: reference_find_concurrent_file_writer_via_file_history

   silent-truncation 的四個坑（前三個都被實際踩過、都產出過看似真實的假數字）：
     - hash → 檔名**絕不能用猜的**，要讀快照 frontmatter 的 `name:` 認身分。
       曾有人把兩個 hash 配反、拿 MEMORY.md 的快照去 diff 某個 topic 檔，
       得出「117 行被吃掉」的假結論。
     - 同一個 hash 在不同 session 目錄下版本不同 ⇒ 必須跨全部 session 目錄收集，
       只看一個目錄會拿到舊版而虛報遺失。
     - 🔴 **不可只比對「最新快照」**——遺失發生後的每次編輯都會再產生快照，那些快照
       已經是缺失狀態。實例（feedback_absence_conclusion_scope_not_universe）：
       @v3 357 行 12:53 是遺失前、@v4 327 行 13:12 是遺失後，取最新就是拿 327 比 327、
       差 0、靜默放行。本 check 早期版本正是這樣寫的，對**唯一已知的真實遺失案例
       抓不到**；改成逐一比對所有快照取差異最大者後，同一案例亮 20 行。
     - 判準是**整段消失**不是「有差異」：參照寫法改變（wiki link → docs 路徑）、
       重新斷行都會產生差異但不是遺失，所以比對前要先 normalize 參照。

   ⚠ 本 check 刻意選擇**寧可誤報也不漏報**：比對所有歷史快照會把「歷史上存在過、
   後來有意改寫或清理」的內容也報出來（2026-08-05 實跑 5 筆 ≥3 行，逐一查全是章節
   改寫與有意清理）。判讀看輸出的快照日期與內容性質——近期快照＝可能真的剛被吃掉；
   久遠快照且標題措辭不同＝章節改寫。漏報是零訊號的，誤報只是多花一次人工確認。

用法：
    python3 scripts/audit/memory-integrity.py              # 兩個 check 都跑
    python3 scripts/audit/memory-integrity.py --links      # 只跑 index-links
    python3 scripts/audit/memory-integrity.py --truncation # 只跑 silent-truncation
    python3 scripts/audit/memory-integrity.py --threshold 3  # 整段消失的行數門檻
    python3 scripts/audit/memory-integrity.py --memory-dir X # 改指定目錄（測試用）
    python3 scripts/audit/memory-integrity.py --repo-root X  # 改 repo 相對路徑的基準

⚠ **只能從主 checkout 跑，不能從 worktree（或任何其他 clone）跑。** 兩個推導都吃
  腳本自身的絕對路徑，而且**兩者壞掉的方式相反**：

  1. memory 目錄的 slug 由 `repo_root()` → `memory_dir()` 推導，所以從
     `…/wt-xxx/scripts/audit/` 跑會去找 `…-wt-xxx/memory` 而得到：

         ✗ 找不到 memory 目錄：/Users/…/-Users-…-wt-memgate/memory

     這是 fail-closed、不是假綠（exit 1），但**基準跑不起來時後續每一組注入驗證都無
     意義**——實遇：有人在 worktree 拿到這行、照樣往下跑注入，整輪結論全部作廢。
     要在別的 checkout 驗證請一律帶 `--memory-dir <路徑>` 指向真實或副本目錄。

  2. 🔴 但 repo 相對路徑的基準（`--repo-root` 預設值）**不會 fail-closed**：它就是
     腳本所在的那個 checkout。帶了 `--memory-dir` 之後第 1 點的守門被繞過，基準就
     悄悄變成 **worktree root**——那棵樹是該分支的內容，不是 develop 的。分支上還沒
     有的 doc 會被報成斷鏈、分支上剛刪的 doc 會被報成存在，兩種都看不出是基準問題。
     ⇒ 所以 `check_index_links()` **一律把兩個基準印出來**（跑錯 checkout 唯一的跡象），
     要對別的樹對帳就明確帶 `--repo-root`。

  同理，本工具**不註冊進 `repo-lint.py` 的 `run_all`**：它檢查的不是 repo 內容，
  註冊進去會讓 repo-lint 在任何非主 checkout 的環境（含 CI runner）永遠紅。
  （純邏輯的部分另有 `test_memory_integrity.py`，全 fixture、不碰真實 memory 目錄，
  因此可以從任何 checkout 跑；但它目前**沒有被任何 CI 跑到**，見該檔說明。）

exit 1 = 有發現，exit 0 = 乾淨。輸出一律列「是哪一個」而不是「差幾個」——
數字沒有可證偽性，任何聽起來合理的解釋都會被接受；集合差立刻可證偽。

────────────────────────────────────────────────────────────────────────
已知限制（2026-08-05 盤點，修法追蹤於 issue #595）
────────────────────────────────────────────────────────────────────────

1. 🔴 **`name:` 與檔名對不上的 memory 檔，`silent-truncation` 完全不覆蓋——而且
   與「沒有快照」在輸出上不可區分。**

   `all_snapshots()` 以快照 frontmatter 的 `name:` + `.md` 去比對現況檔名集合，
   對不上就 `continue`。實測 124 檔中 **45 檔**如此（例
   `reference_kubectl_woow_k3s.md` 的 `name: kubectl-woow-k3s`——少了 `reference_`
   前綴、且 kebab vs snake 不一致）。

   本 check 只印得出「其中 72 檔有快照可比對」，**看的人會把差額歸因成「另外 52 檔
   只是還沒有快照」**（暫時沒檢查），而其中 45 檔是**結構性永久盲區**（永遠不會被
   檢查）。這兩件事的後果完全不同，輸出卻一模一樣。本檔的稽核記錄
   `docs/tracking/memory-integrity-audit-2026-08-05.md` 初版正是這樣寫錯的。

   ⇒ 在修好之前：**「其中 N 檔有快照可比對」不可讀成「其餘的暫時沒資料」。**

2. 🔴 **「整段消失」與「內容被搬到別的檔」在單檔比對下輸出完全相同。**

   本 check 逐檔比對，看不到跨檔搬移。判定某筆 finding 是不是真遺失，必須先問
   「這些行是不是在 memory 目錄的**別處**還活著」。實例：`任何聽起來合理的解釋都會
   被接受` 被報成消失，實際是搬到了
   `feedback_completeness_check_output_which_not_how_many.md`。

   逐字比對這一步是機械的（99 行中 2 行逐字存在於別的檔）；但「改寫後搬移」目前
   **不好機械化**——試過兩種關鍵片段探針，因門檻與 markdown 剝除方式不同而得出互相
   矛盾的數字。在做進 gate 之前，這一步只能人工，且**必須做**，否則會把搬移報成遺失。

3. MEMORY.md 本身沒有 `name:` frontmatter，`silent-truncation` 不涵蓋它
   （`all_snapshots()` 對讀不到 `name:` 的快照直接跳過）。索引檔的完整性只靠
   `index-links` 的斷鏈／孤兒對帳守住，內容遺失沒有守。
"""

import dataclasses
import os
import re
import time
import sys

HOME = os.path.expanduser("~")
FILE_HISTORY = os.path.join(HOME, ".claude", "file-history")

# 必然隨每次編輯變動的 frontmatter 欄位，比對時要排除，否則每個檔都會亮
VOLATILE_FIELD = re.compile(r"^\s*(modified|originSessionId|updated):")

LINK = re.compile(r"\]\(([^)]+\.md)\)")
NAME_FIELD = re.compile(r"^name:\s*(\S+)\s*$", re.M)

# 指向別筆記憶／文件的參照有三種寫法，2026-08-05 整併把大量 [[wiki-link]] 改寫成
# repo docs 路徑。比對前統一成同一個 token，否則**純粹的改寫會被報成內容消失**——
# 判準要問「內容有沒有消失」，不是「字面有沒有變」。
REFERENCE = re.compile(r"\[\[[^\]]+\]\]|`[^`]*\.md`|\]\([^)]+\)")


def repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def memory_dir(root):
    """由 repo 路徑推導 Claude projects slug：`/` 與 `_` 都換成 `-`。"""
    slug = re.sub(r"[/_]", "-", root)
    return os.path.join(HOME, ".claude", "projects", slug, "memory")


def content_lines(path, normalize_refs=True):
    """去掉空行與必然變動的 frontmatter 欄位後的行集合。

    normalize_refs=True 時把各種檔案參照寫法統一，讓「同一句話換了連結形式」
    不會被誤判成內容消失。
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = [
            line for line in fh.read().splitlines()
            if line.strip() and not VOLATILE_FIELD.match(line)
        ]
    if normalize_refs:
        lines = [REFERENCE.sub("@REF", line) for line in lines]
    return set(lines)


def resolve_link(target, mem, root):
    """把 MEMORY.md 的一條連結解析成實體檔路徑。

    回傳 `(resolved, tried)`：`resolved` 是命中的候選，全部沒命中則 None；
    `tried` 是實際試過的候選，讓斷鏈訊息能列出「查過哪裡」而不只是「找不到」。

    相對路徑**同時**試兩個基準，順序固定（memory 先）：

      1. memory 目錄——memory 檔互連（`reference_foo.md`），原本就有的形態。
      2. repo root ——指向 repo 內文件（`docs/tracking/landings/x.md`）。
         2026-08-06 新增；在此之前這種寫法一律被解析成 `memory/docs/…` ⇒ 必然
         斷鏈，所以 MEMORY.md 只能寫絕對路徑，65 條連結各背 70 bytes 的前綴、
         合計 4550 bytes，把索引檔頂到 Claude 讀取上限附近（22957 / 24400）。

    兩個基準同時命中時 memory 勝——這只影響「repo root 底下剛好有個同名裸檔名」
    這種情形，實測 0 筆，但順序仍必須是確定的，否則同一份索引在不同機器可能得到
    不同的孤兒集合。
    """
    if os.path.isabs(target):
        tried = [target]
    else:
        tried = [os.path.join(mem, target), os.path.join(root, target)]
    for candidate in tried:
        if os.path.isfile(candidate):
            return candidate, tried
    return None, tried


def _is_memory_entry(resolved, mem):
    """這條連結算不算「某個 memory 檔的入口」（＝可以把它從孤兒名單移除）。

    🔴 判準是**解析結果落在 memory 目錄裡**，不是「連結字面是不是相對路徑」。
    這個區別是本函式存在的唯一理由，它擋的是一種零訊號的假綠：

        MEMORY.md 有一條 `docs/tracking/landings/501-global-cap-landing.md`，
        memory 目錄裡剛好也有個 `501-global-cap-landing.md` 沒有任何索引行指向它。

    用「相對路徑就 `linked.add(basename(target))`」的寫法（2026-08-06 之前的版本，
    當時相對路徑只有 memory 內部一種形態，所以是對的），那個 memory 檔會被誤判成
    已連結 ⇒ **真正的孤兒被靜默放行**。孤兒的後果是「檔案還在、入口全失，不查就永遠
    讀不到」（2026-08-05 實測 5 篇），而且它跟其他零訊號故障一樣不會有任何錯誤訊息。

    ⚠ 這個碰撞**現在是 0 筆**（memory 檔名一律 `reference_/feedback_/project_/user_`
    前綴 + snake_case，repo docs 一律 kebab-case，123 檔對 repo 全部 675 個 .md 零重疊）。
    也就是說：寫錯的版本**今天完全沒有症狀**，只會在未來某次改名時才開始靜默漏報。
    而那條路徑並不遠——45 個 memory 檔的 frontmatter `name:` 是 kebab 且與檔名不一致
    （已知限制 1），哪天照 `name:` 正規化檔名，`tea-cli-write-gotchas.md` 就會與
    `docs/guides/development/tea-cli-write-gotchas.md` 撞上。守衛見
    test_memory_integrity.py::TestOrphanNotMaskedByRepoLink。
    """
    if resolved is None:
        return False
    parent = os.path.realpath(os.path.dirname(os.path.abspath(resolved)))
    return parent == os.path.realpath(mem)


@dataclasses.dataclass
class IndexLinkResult:
    total: int          # MEMORY.md 裡的連結條數
    on_disk: list       # memory 目錄下的個別檔（不含 MEMORY.md）
    dead: list          # 索引連到但兩個基準都找不到的 target（原字面、去重排序）
    orphan: list        # 檔案存在但沒有任何連結解析到它
    root: str           # 實際使用的 repo 相對路徑基準
    tried: dict         # target -> 試過的候選路徑

    @property
    def ok(self):
        return not self.dead and not self.orphan


def audit_index_links(mem, root):
    """MEMORY.md 連結 ↔ memory 目錄實體的雙向對帳（純邏輯，不印任何東西）。"""
    with open(os.path.join(mem, "MEMORY.md"), encoding="utf-8") as fh:
        targets = LINK.findall(fh.read())

    on_disk = {f for f in os.listdir(mem) if f.endswith(".md") and f != "MEMORY.md"}

    dead, linked, tried = set(), set(), {}
    for target in targets:
        resolved, candidates = resolve_link(target, mem, root)
        tried[target] = candidates
        if resolved is None:
            dead.add(target)
        elif _is_memory_entry(resolved, mem):
            linked.add(os.path.basename(resolved))

    return IndexLinkResult(
        total=len(targets),
        on_disk=sorted(on_disk),
        dead=sorted(dead),
        orphan=sorted(on_disk - linked),
        root=root,
        tried=tried,
    )


def check_index_links(mem, root):
    """MEMORY.md 連結 ↔ memory 目錄實體，雙向對帳。"""
    index = os.path.join(mem, "MEMORY.md")
    if not os.path.isfile(index):
        print(f"✗ [index-links] 找不到 {index}")
        return 1

    result = audit_index_links(mem, root)

    print(f"[index-links] 連結 {result.total} 條，memory 個別檔 {len(result.on_disk)} 個")
    # 相對路徑解析成什麼完全取決於這兩個基準，而且跑錯 checkout 時**沒有別的跡象**
    # （會表現成一批莫名其妙的斷鏈）。所以無條件印出來，不管有沒有發現。
    print(f"              相對路徑基準：memory = {mem}")
    print(f"                            repo   = {root}")
    for target in result.dead:
        print(f"  ✗ 斷鏈（索引連到但檔案不存在）：{target}")
        for candidate in result.tried[target]:
            print(f"        查過：{candidate}")
    for name in result.orphan:
        print(f"  ✗ 孤兒（檔案存在但索引沒連到）：{name}")
    if result.ok:
        print("  ✓ 斷鏈 0、孤兒 0")
    return 0 if result.ok else 1


def all_snapshots(mem):
    """跨全部 session 目錄，收集每個 memory 檔的**所有**快照。

    身分一律讀快照 frontmatter 的 `name:` 認定，絕不從 hash 反推。

    🔴 這裡曾經只取 mtime 最新的一份，對唯一已知的真實遺失案例**抓不到**：
    遺失發生後的每次編輯都會再產生快照，而那些快照已經是缺失狀態。實例
    （feedback_absence_conclusion_scope_not_universe，2026-08-05）：
        @v3 357 行 12:53:36  ← 遺失前
        @v4 327 行 13:12:21  ← 遺失後，但 mtime 更新
    取最新 ⇒ 拿 327 行比 327 行 ⇒ 差 0 ⇒ 靜默放行。
    正解是比對**每一個**快照、取差異最大者：只要某個歷史版本有一整段而現在沒有，
    就是遺失，與那個版本是不是最新無關。
    """
    current = {f for f in os.listdir(mem) if f.endswith(".md")}
    found = {}
    if not os.path.isdir(FILE_HISTORY):
        return found, current

    for root, _, files in os.walk(FILE_HISTORY):
        for filename in files:
            if "@v" not in filename:
                continue
            path = os.path.join(root, filename)
            try:
                if os.path.getsize(path) > 400_000:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(400)
            except OSError:
                continue

            match = NAME_FIELD.search(head)
            if not match:
                continue  # MEMORY.md 沒有 frontmatter，本 check 不涵蓋它
            target = match.group(1) + ".md"
            if target not in current:
                continue  # 已刪除的 memory 檔，不算遺失

            found.setdefault(target, []).append(path)
    return found, current


def check_silent_truncation(mem, threshold):
    """比對每個 memory 檔的所有歷史快照與現況，找疑似被整份 Write 吃掉的內容。"""
    found, current = all_snapshots(mem)
    total = sum(len(v) for v in found.values())
    print(f"[silent-truncation] 現況 {len(current)} 檔，"
          f"其中 {len(found)} 檔有快照可比對（共 {total} 份）")
    if not found:
        print("  ⚠ 沒有任何快照可比對——這不等於「沒有遺失」，是**沒有檢查**")
        return 0

    findings = []
    for name, paths in found.items():
        now = content_lines(os.path.join(mem, name))
        worst = None
        for snapshot in paths:              # 逐一比對，取差異最大者而非最新者
            lost = content_lines(snapshot) - now
            if worst is None or len(lost) > len(worst[0]):
                worst = (lost, snapshot)
        if worst and worst[0]:
            findings.append((len(worst[0]), name, worst[1], sorted(worst[0])))

    findings.sort(reverse=True)
    serious = [f for f in findings if f[0] >= threshold]

    # 低於門檻的差異幾乎都是改寫，列成一行讓人知道它們被看過、不是沒查
    minor = [f"{name}({count})" for count, name, _, _ in findings if count < threshold]
    if minor:
        print(f"  · 差異 <{threshold} 行（多為改寫，非遺失）：{', '.join(minor)}")

    for count, name, snapshot, lost in serious:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(snapshot)))
        print(f"  ✗ {name} 疑似整段消失 {count} 行")
        # 快照日期是判讀的關鍵：近期快照＝真的剛被吃掉；久遠快照＋標題措辭不同
        # ＝章節改寫的誤報。本 check 刻意選擇寧可誤報也不漏報。
        print(f"      快照 {when}  {snapshot}")
        for line in lost[:6]:
            print(f"      - {line[:140]}")

    if not serious:
        print(f"  ✓ 沒有 ≥{threshold} 行的整段消失")
    return 1 if serious else 0


def main(argv):
    root = repo_root()
    if "--repo-root" in argv:
        # 在別的 checkout／fixture 上對帳時明確指定；預設是腳本所在的那棵樹，
        # 從 worktree 跑時**那就是 worktree root**（見檔頭 ⚠ 第 2 點）。
        root = argv[argv.index("--repo-root") + 1]
    if "--memory-dir" in argv:
        mem = argv[argv.index("--memory-dir") + 1]
    else:
        mem = memory_dir(repo_root())
    if not os.path.isdir(mem):
        print(f"✗ 找不到 memory 目錄：{mem}")
        return 1

    threshold = 3
    if "--threshold" in argv:
        threshold = int(argv[argv.index("--threshold") + 1])

    only_links = "--links" in argv
    only_truncation = "--truncation" in argv
    run_all = not (only_links or only_truncation)

    status = 0
    if run_all or only_links:
        status |= check_index_links(mem, root)
    if run_all or only_truncation:
        status |= check_silent_truncation(mem, threshold)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

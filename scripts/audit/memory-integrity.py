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

   🔴 但「寧可誤報」有一個它自己會走到的失敗模式，那才是 #683 要修的東西：
   **誤報累積到某個量之後，這支工具會自我瓦解**——跑一次看到一串「疑似整段消失」、
   查完全是重構 ⇒ 下次沒有人會認真看下一筆，而真的資料遺失長得一模一樣。
   （2026-08-12 實測 11 筆 ≥3 行，抽驗 5 筆全為誤報，機制三種。）
   ⇒ 誤報不是免費的。本 check 因此有兩道降噪，順序固定、性質完全不同：

     1. `moved_elsewhere()`——**機械可判、每次重算**（逐字搬到別的 memory 檔）。
        零人工、且會自癒：目的檔哪天也把那行弄丟了，扣除自動失效、重新報。
     2. baseline 豁免——**人判定過的**（改寫、自我更正、狀態更新…）。這一道是靜音，
        所以受限制最多，見下方已知限制 4。

用法：
    python3 scripts/audit/memory-integrity.py              # 兩個 check 都跑
    python3 scripts/audit/memory-integrity.py --links      # 只跑 index-links
    python3 scripts/audit/memory-integrity.py --truncation # 只跑 silent-truncation
    python3 scripts/audit/memory-integrity.py --threshold 3  # 整段消失的行數門檻
    python3 scripts/audit/memory-integrity.py --memory-dir X # 改指定目錄（測試用）
    python3 scripts/audit/memory-integrity.py --repo-root X  # 改 repo 相對路徑的基準
    python3 scripts/audit/memory-integrity.py --file-history X # 改快照來源（測試用）
    python3 scripts/audit/memory-integrity.py --baseline X   # 改 baseline 檔位置
    python3 scripts/audit/memory-integrity.py --detail F     # 印出某檔**全部**疑似消失行
    python3 scripts/audit/memory-integrity.py --waive F --reason 分類  # 判定為誤報、寫進 baseline

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

2. 🟡 **「整段消失」與「內容被搬到別的檔」——逐字搬移已機械化，改寫後搬移仍不行。**

   `moved_elsewhere()`（#683）把「消失的行是不是在 memory 目錄**別處**逐字還活著」
   做成自動扣除，每次跑都重算，不需要 baseline。實例：`任何聽起來合理的解釋都會
   被接受` 曾被報成消失，實際是搬到了
   `feedback_completeness_check_output_which_not_how_many.md`。

   ⚠ 仍然只覆蓋**逐字**那層。「改寫後搬移」不好機械化——#595 試過兩種關鍵片段探針，
   因門檻與 markdown 剝除方式不同而得出互相矛盾的數字。所以改寫後搬移仍會被報出來，
   要靠人判定後走 baseline（見 4）。

3. MEMORY.md 本身沒有 `name:` frontmatter，`silent-truncation` 不涵蓋它
   （`all_snapshots()` 對讀不到 `name:` 的快照直接跳過）。索引檔的完整性只靠
   `index-links` 的斷鏈／孤兒對帳守住，內容遺失沒有守。

4. 🔴 **baseline 是本 check 唯一「人可以叫它閉嘴」的入口，因此它本身就是最可能的
   假綠來源。** 設計上做了四道限制，動它之前先讀懂為什麼：

   a. **豁免綁的是「行內容的指紋」，不是檔名／行數／整個檔。** 一筆豁免只讓
      `(memory 檔, 這一行的 sha256 前 16 hex)` 這個組合閉嘴。同一個檔明天再發生
      **別的**消失，那些行沒有指紋 ⇒ 照樣報。整檔靜音在本設計裡不可能發生。
      守衛：test_memory_integrity.py::TestBaselineWaiver::
      test_new_loss_in_waived_file_is_reported_again。

   b. **豁免的指紋算在 `content_lines()` 正規化後的行上**——參照寫法改變
      （wiki link → docs 路徑）不會讓既有豁免失效而整批重報。

   c. **寫入必須逐檔、且必須指定判定理由**（`--waive <檔> --reason <分類>`，分類限
      `WAIVER_CATEGORIES` 的封閉詞彙表）。刻意**不提供** repo-lint 那種
      `--update-baseline` 一次收全部：那個形狀的已知代價是「有人為了別的理由跑它，
      就把新冒出來的違規一起靜默吸收」（repo-lint.py 自己在 SAFETY 清單那段註解過），
      而這裡吸收掉的會是一次真實的資料遺失。
      `--waive` 會先把**每一行**印出來再寫，因為 check 的輸出只列前 6 行——不印全部
      就等於讓人對沒看過的內容按下靜音。

   d. **baseline 讀不到／不存在 ⇒ 全部照常報**（不是靜默通過），而且輸出**永遠**印出
      「這次扣掉了幾行、依據什麼」——包含乾淨那一行也印。「✓ 沒有整段消失」單獨出現
      會被讀成「memory 完整」，實際上它是「扣掉 N 行之後沒有」。

   baseline 檔位置：**memory 目錄的兄弟**（`<memory-dir>/../memory-integrity-baseline.tsv`），
   不進 repo。理由見 `baseline_path()`。
"""

import dataclasses
import hashlib
import os
import re
import time
import sys

HOME = os.path.expanduser("~")
FILE_HISTORY = os.path.join(HOME, ".claude", "file-history")

BASELINE_FILENAME = "memory-integrity-baseline.tsv"

# 判定理由的**封閉**詞彙表。刻意不收自由文字，兩個理由：
#   1. 自由文字會被寫成「查過了，沒事」這種不可覆核的東西；分類讓後人能問「這一類
#      的判定方式是什麼」並重驗。
#   2. baseline 檔可能被貼進 issue／PR，自由文字欄是最容易夾帶 memory 內文的地方。
#      封閉詞彙表把這個洩漏面**機械封死**，不靠人自律。
WAIVER_CATEGORIES = {
    "cross-file-move": "跨檔搬移，但改寫過 ⇒ 逐字比對抓不到（逐字那層已自動扣除）",
    "section-rewrite": "章節改寫擴充——同一批案例與結論在現況裡仍可逐一對應",
    "self-correction": "自我更正——作者主動撤回舊結論，新舊兩版都還在",
    "status-update": "狀態更新造成的改寫——問題後來修好，開頭補上狀態段",
    "format-evolution": "格式演進——壓縮備份區塊改寫成敘事、或表格欄位擴充",
    "intentional-cleanup": "有意清理——判定為刻意刪除，不是被覆蓋吃掉",
}

# 一行要能當「逐字搬到別的檔」的證據，必須先是**有辨識度的**。兩道門檻各擋一種
# 假扣除，缺任一道都會把真遺失的行悄悄扣掉：
#   * MOVE_MIN_CHARS 擋短行：`- ok`、`---` 這種在別的檔命中不證明任何事。
#   * MOVE_MAX_HOSTS 擋樣板行：`metadata:`、`type: reference`、`**Why:**` 長度夠但
#     每個 memory 檔都有一份，命中同樣不證明任何事。真的搬移落在 1 個檔（偶爾 2）。
MOVE_MIN_CHARS = 8
MOVE_MAX_HOSTS = 2
# 算「可辨識字元數」時要剝掉的 markdown 記號與標點（含全形）
_MARKUP = re.compile(r"[\s#*>`|_~\-–—:：。，、．.,!！?？()（）\[\]/／]+")

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


def line_fingerprint(line):
    """一行內容的指紋——baseline 裡**只存這個，不存原文**。

    memory 目錄是本機個人工作記憶。baseline 會被 diff、可能被貼進 issue／PR，所以
    它能記的只有「哪個檔的哪一行被判定過」這件事本身，不能記那一行說了什麼。

    ⚠ 指紋算在 `content_lines()` **正規化後**的行上（參照寫法已統一成 @REF），
    所以把 `[[wiki-link]]` 改寫成 docs 路徑不會讓既有豁免整批失效。
    """
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]


def baseline_path(mem):
    """baseline 檔的預設位置：**被檢查對象的兄弟**，不進 repo。

    三個候選都評估過（#683 設計風險二）：

      a. repo（`scripts/audit/baselines/`）——**否決**。這支 check 的對象是本機
         `~/.claude/projects/<slug>/memory/`，每個人內容不同、不在版控。放 repo 會
         有兩個後果：(1) 對別人是純噪音，他的 memory 目錄裡沒有那些檔，豁免永遠對不上；
         (2) 更糟的是**新增了洩漏面**——檔名已經在
         `docs/tracking/memory-inventory-2026-08-05.md` 裡不算新洩漏，但**行層級的
         內容指紋是新的**：hash 是驗證器，任何拿得到 repo 的人都能拿一句猜測去比對，
         確認某句話是否曾在別人的私人筆記裡。這個能力今天不存在，不該由本工具創造。
      b. memory 目錄**裡面**——否決。那個目錄的契約是「每個檔＝一則記憶」，把工具的
         產物混進被檢查的語料本身是典型污染；且工單紀律是「不要修改任何 memory 檔」，
         連新增檔案都不該碰它。
      c. memory 目錄的**兄弟**——採用。跟著被檢查的對象走（換機器／換 slug 就自然
         換一份，不會誤配到別人的資料），又不落在被檢查的語料裡。

    c 的已知代價：工具在 repo、baseline 在本機，兩者會漂——**baseline 檔不存在時
    的行為必須是「全部照常報」**（見 `read_baseline()`），這樣漂掉的方向是變吵，
    不是變綠。用 `--baseline` 可覆寫（測試 fixture 走這條）。
    """
    return os.path.join(os.path.dirname(os.path.abspath(mem)), BASELINE_FILENAME)


def read_baseline(path):
    """讀豁免表，回 `({memory 檔名: {指紋: 分類}}, 壞掉的行號清單)`。

    🔴 檔不存在 ⇒ 回空 dict ⇒ **全部照常報**。不是靜默通過——沒有豁免表的正確語意
    是「沒有人判定過任何一筆」，不是「都判定過了」。
    """
    waivers, malformed = {}, []
    if not os.path.isfile(path):
        return waivers, malformed
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 3 or parts[2] not in WAIVER_CATEGORIES:
                malformed.append(lineno)
                continue
            name, digest, category = parts
            waivers.setdefault(name, {})[digest] = category
    return waivers, malformed


def write_baseline(path, waivers):
    """寫回豁免表。欄位固定三個，沒有自由文字欄（見 WAIVER_CATEGORIES 的註解）。"""
    rows = sorted(
        (name, digest, category)
        for name, entries in waivers.items()
        for digest, category in entries.items()
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# memory-integrity silent-truncation 豁免表"
                 "（memory-integrity.py --waive 產生，勿手改欄位格式）\n")
        fh.write("# 格式：<memory 檔名>\\t<行內容指紋 sha256[:16]>\\t<判定分類>\n")
        fh.write("# 🔴 只存指紋不存原文，也不存任何本機絕對路徑——這個檔記的是"
                 "「哪一行被判定過」，不是那一行說了什麼。\n")
        fh.write("# 🔴 豁免綁的是「檔 + 這一行」。同一個檔再發生**別的**消失照樣會報；"
                 "整檔靜音在本設計裡不可能發生。\n")
        fh.write("# 分類詞彙表：%s\n" % "、".join(sorted(WAIVER_CATEGORIES)))
        for name, digest, category in rows:
            fh.write("%s\t%s\t%s\n" % (name, digest, category))


def current_line_hosts(mem, current):
    """memory 目錄現況的「這一行出現在哪些檔」反向索引（跨檔搬移判定的唯一資料源）。"""
    hosts = {}
    for name in sorted(current):
        for line in content_lines(os.path.join(mem, name)):
            hosts.setdefault(line, set()).add(name)
    return hosts


def moved_elsewhere(line, hosts):
    """這一行是不是「逐字搬到別的 memory 檔」？是就回目的檔清單，否則回 None。

    這是本 check 的**機械可判**降噪層：#595 的 3c 只做逐字那一層，因為它可證偽、
    零假陽性；「改寫後搬移」留給人判定走 baseline。

    🔴 兩道辨識度門檻不是防守過度，是防**假扣除**：命中本身不是證據，「一行有辨識度
    且只在別處出現一兩次」才是。少了它們，`---`／`metadata:` 這種每個檔都有的行會被
    當成搬移扣掉，於是一筆真遺失的行數被稀釋到門檻以下 ⇒ 靜默放行。
    """
    where = hosts.get(line)
    if not where:
        return None
    if len(_MARKUP.sub("", line)) < MOVE_MIN_CHARS:
        return None
    if len(where) > MOVE_MAX_HOSTS:
        return None
    return sorted(where)


def all_snapshots(mem, file_history=FILE_HISTORY):
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
    if not os.path.isdir(file_history):
        return found, current

    for root, _, files in os.walk(file_history):
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


@dataclasses.dataclass
class TruncationFinding:
    name: str        # memory 檔名
    snapshot: str    # 勝出快照的**本機路徑**——只用於終端輸出，絕不寫進 baseline
    lost: list       # 扣掉跨檔搬移與 baseline 豁免後，**仍**疑似消失的行（已排序）
    moved: dict      # line -> 目的檔清單（逐字搬移，機械扣除）
    waived: dict     # 分類 -> 行數（baseline 豁免，人判定過）
    raw: int         # 扣除前的差集大小，只用於決定哪份快照勝出（tie-break）

    @property
    def count(self):
        return len(self.lost)


def compute_truncation(mem, file_history=FILE_HISTORY, waivers=None):
    """算出每個 memory 檔的 finding——check 與 `--waive` 的**唯一**入口。

    兩邊共用同一個函式，是為了讓「baseline 寫進去的行」與「check 判定要報的行」
    不可能是兩組不同的東西（repo-lint 的同一課：症狀會是「跑完 --waive 之後
    check 仍然紅」，而且看起來像工具壞掉而不是判準不一致）。
    """
    waivers = waivers or {}
    found, current = all_snapshots(mem, file_history)
    hosts = current_line_hosts(mem, current)

    findings = []
    for name in sorted(found):
        now = content_lines(os.path.join(mem, name))
        file_waivers = waivers.get(name, {})
        best = None
        for snapshot in sorted(found[name]):   # 排序＝同分時的勝出者是確定的
            raw = content_lines(snapshot) - now
            moved, waived, residual = {}, {}, []
            for line in sorted(raw):
                dest = moved_elsewhere(line, hosts)
                if dest:
                    moved[line] = dest
                    continue
                category = file_waivers.get(line_fingerprint(line))
                if category:
                    waived[category] = waived.get(category, 0) + 1
                    continue
                residual.append(line)
            candidate = TruncationFinding(name, snapshot, residual, moved, waived, len(raw))
            # 🔴 勝出判準是**扣除後**的殘量，不是原始差集大小。用原始差集會讓「整份
            # 都被跨檔搬移解釋掉的舊快照」贏過「殘留一筆真消失的另一份快照」——那正是
            # 靜默放行的形狀。原始差集只當同分時的 tie-break。
            key = (candidate.count, candidate.raw)
            if best is None or key > (best.count, best.raw):
                best = candidate
        if best is not None and (best.lost or best.moved or best.waived):
            findings.append(best)

    findings.sort(key=lambda f: (-f.count, f.name))
    return findings, current, found


def _print_deduction_lines(findings):
    """把兩道扣除逐檔印出來。**不可省**：扣了什麼看不見的話，「✓ 乾淨」就是假綠。"""
    for finding in findings:
        if finding.moved:
            dests = sorted({d for hosts in finding.moved.values() for d in hosts})
            shown = ", ".join(dests[:3]) + ("…" if len(dests) > 3 else "")
            print(f"  · 跨檔逐字搬移，已扣除：{finding.name} {len(finding.moved)} 行 → {shown}")
    for finding in findings:
        if finding.waived:
            detail = ", ".join(f"{cat}×{n}" for cat, n in sorted(finding.waived.items()))
            print(f"  · baseline 已豁免：{finding.name} {detail}")


def check_silent_truncation(mem, threshold, file_history=FILE_HISTORY, baseline=None):
    """比對每個 memory 檔的所有歷史快照與現況，找疑似被整份 Write 吃掉的內容。"""
    baseline = baseline or baseline_path(mem)
    waivers, malformed = read_baseline(baseline)
    findings, current, found = compute_truncation(mem, file_history, waivers)

    total = sum(len(v) for v in found.values())
    print(f"[silent-truncation] 現況 {len(current)} 檔，"
          f"其中 {len(found)} 檔有快照可比對（共 {total} 份）")
    # 差額的成因有兩種、後果完全不同，而本 check 目前**分不出來**——不講明的話讀的人
    # 會一律歸因成「暫時還沒快照」，把一批永久盲區讀成暫時狀態（issue #595）。
    uncovered = len(current) - len(found)
    if uncovered:
        print(f"                    未涵蓋 {uncovered} 檔"
              f"——⚠ 不全是「暫時沒快照」，含 name: 與檔名對不上的**永久盲區**，"
              f"本 check 尚未區分兩者（#595）")
    if waivers:
        entries = sum(len(v) for v in waivers.values())
        print(f"                    baseline {baseline}"
              f"（{entries} 筆豁免 / {len(waivers)} 檔）")
    else:
        print(f"                    baseline {baseline}：不存在或無豁免 ⇒ 不靜音任何一筆")
    for lineno in malformed:
        print(f"  ⚠ baseline 第 {lineno} 行格式或分類無效，已忽略（該筆不生效 ⇒ 會照常報）")

    if not found:
        print("  ⚠ 沒有任何快照可比對——這不等於「沒有遺失」，是**沒有檢查**")
        return 0

    _print_deduction_lines(findings)

    serious = [f for f in findings if f.count >= threshold]

    # 低於門檻的差異幾乎都是改寫，列成一行讓人知道它們被看過、不是沒查
    minor = [f"{f.name}({f.count})" for f in findings if 0 < f.count < threshold]
    if minor:
        print(f"  · 差異 <{threshold} 行（多為改寫，非遺失）：{', '.join(minor)}")

    for finding in serious:
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(os.path.getmtime(finding.snapshot)))
        print(f"  ✗ {finding.name} 疑似整段消失 {finding.count} 行")
        # 快照日期是判讀的關鍵：近期快照＝真的剛被吃掉；久遠快照＋標題措辭不同
        # ＝章節改寫的誤報。本 check 刻意選擇寧可誤報也不漏報。
        print(f"      快照 {when}  {finding.snapshot}")
        for line in finding.lost[:6]:
            print(f"      - {line[:140]}")
        if finding.count > 6:
            print(f"      …另 {finding.count - 6} 行，"
                  f"用 --detail {finding.name} 看全部（判定前務必全看過）")

    moved_total = sum(len(f.moved) for f in findings)
    waived_total = sum(sum(f.waived.values()) for f in findings)
    if not serious:
        # 🔴 「乾淨」永遠要帶著它的前提一起講。單獨一行 ✓ 會被讀成「memory 完整」，
        # 實際語意是「**扣掉這些之後**沒有」——扣除本身才是最需要被覆核的部分。
        print(f"  ✓ 沒有 ≥{threshold} 行的整段消失"
              f"（已扣除：跨檔逐字搬移 {moved_total} 行、baseline 豁免 {waived_total} 行）")
    return 1 if serious else 0


def show_detail(mem, name, file_history=FILE_HISTORY, baseline=None):
    """印出某個檔**全部**待判定的疑似消失行（check 只列前 6 行）。"""
    baseline = baseline or baseline_path(mem)
    waivers, _ = read_baseline(baseline)
    findings, _, _ = compute_truncation(mem, file_history, waivers)
    match = next((f for f in findings if f.name == name), None)
    if match is None or not match.lost:
        print(f"✗ {name} 目前沒有待判定的疑似消失行（檔名打錯？或已全部豁免／扣除）")
        return 1
    print(f"{name}：待判定 {match.count} 行（快照 {match.snapshot}）")
    for line in match.lost:
        print(f"  - {line}")
    return 0


def waive(mem, name, reason, file_history=FILE_HISTORY, baseline=None):
    """把某個檔**目前**待判定的行寫進 baseline，附判定分類。

    刻意不做 repo-lint 那種「一次收全部現況」的 `--update-baseline`：那個形狀的已知
    代價是有人為了別的理由跑它就把新違規一起靜默吸收，而這裡被吸收掉的會是一次真實的
    資料遺失。逐檔 + 強制分類 + 寫入前印出全部內容，是為了讓靜音這個動作沒辦法在
    「沒看過」的狀態下發生。
    """
    if reason not in WAIVER_CATEGORIES:
        print(f"✗ 未知的判定分類 `{reason}`。可用分類：")
        for key, desc in sorted(WAIVER_CATEGORIES.items()):
            print(f"    {key:<20} {desc}")
        return 1

    baseline = baseline or baseline_path(mem)
    waivers, _ = read_baseline(baseline)
    findings, _, _ = compute_truncation(mem, file_history, waivers)
    match = next((f for f in findings if f.name == name), None)
    if match is None or not match.lost:
        # fail-loud：寫不出東西時不能安靜地成功，否則「我豁免過了」會變成假記憶
        print(f"✗ {name} 目前沒有待判定的疑似消失行——不寫入任何豁免。"
              f"（檔名打錯？或它的差異已經被跨檔搬移扣掉／先前已豁免）")
        return 1

    print(f"即將豁免 {name} 的 {match.count} 行，分類 {reason}"
          f"（{WAIVER_CATEGORIES[reason]}）：")
    for line in match.lost:      # 全部印出來——check 只列前 6 行，不補印等於盲簽
        print(f"  - {line}")

    entry = waivers.setdefault(name, {})
    for line in match.lost:
        entry[line_fingerprint(line)] = reason
    write_baseline(baseline, waivers)
    print(f"✓ 已寫入 {match.count} 筆指紋 → {baseline}")
    return 0


# 認得的旗標；True＝後面要跟一個值。未列出的一律拒絕，見 `validate_argv()`。
KNOWN_FLAGS = {
    "--links": False, "--truncation": False,
    "--repo-root": True, "--memory-dir": True, "--threshold": True,
    "--file-history": True, "--baseline": True,
    "--waive": True, "--reason": True, "--detail": True,
}


def validate_argv(argv):
    """不認得的旗標一律拒絕，回錯誤訊息 list（空 list＝通過）。

    🔴 原本的寫法是「`if "--x" in argv`」，打錯的旗標會被**靜默忽略**，然後跑完整套
    check 並印出一份看起來完全正常的報告。2026-08-12 實遇：拿舊版腳本跑 `--detail`，
    它安靜地忽略該旗標、改跑全套——輸出長得像成功，只是回答的是另一個問題。
    對 `--memory-dir` 打錯更糟：會退回推導路徑，於是**比對的是另一個語料庫**而毫無跡象。
    """
    errors, i = [], 0
    while i < len(argv):
        token = argv[i]
        if token not in KNOWN_FLAGS:
            errors.append(f"✗ 不認得的參數 `{token}`。可用：{' '.join(sorted(KNOWN_FLAGS))}")
            return errors          # 後面的位置語意已經不可信，不再往下猜
        if KNOWN_FLAGS[token]:
            if i + 1 >= len(argv):
                errors.append(f"✗ `{token}` 後面少了值")
                return errors
            i += 2
        else:
            i += 1
    return errors


def main(argv):
    errors = validate_argv(argv)
    for message in errors:
        print(message)
    if errors:
        return 1

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

    file_history = FILE_HISTORY
    if "--file-history" in argv:
        file_history = argv[argv.index("--file-history") + 1]
    baseline = None
    if "--baseline" in argv:
        baseline = argv[argv.index("--baseline") + 1]

    # 寫入模式與唯讀模式互斥，且都比 check 先處理——跑 --waive 的人要的是判定，
    # 不是再看一次同一份輸出。
    if "--waive" in argv:
        if "--reason" not in argv:
            print("✗ --waive 必須同時帶 --reason <分類>。判定理由不是選配："
                  "沒有它，後人只看得到一串指紋，無法覆核也無法推翻。")
            return 1
        return waive(mem, argv[argv.index("--waive") + 1],
                     argv[argv.index("--reason") + 1], file_history, baseline)
    if "--detail" in argv:
        return show_detail(mem, argv[argv.index("--detail") + 1], file_history, baseline)

    only_links = "--links" in argv
    only_truncation = "--truncation" in argv
    run_all = not (only_links or only_truncation)

    status = 0
    if run_all or only_links:
        status |= check_index_links(mem, root)
    if run_all or only_truncation:
        status |= check_silent_truncation(mem, threshold, file_history, baseline)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

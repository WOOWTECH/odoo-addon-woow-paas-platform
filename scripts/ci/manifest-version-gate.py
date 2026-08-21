#!/usr/bin/env python3
"""manifest 版號 gate（#538 建立，#691 改制為「發佈時才 bump」）。

擋的是同一家族的事——**版號關係錯了，但沒有任何東西會叫**。#691 之前的判準是
「每張 PR 都必須嚴格大於當下 origin/develop」，那條規則把「每張都 bump」制度化，
於是並行 PR 每張都改同一行 ⇒ merge train 每合一張、下一張就衝突一次（六張 PR 的
實戰紀錄見 docs/guides/development/merge-train-acceptance.md）。

🔴 現行三條規則（#691 裁決）
--------------------------
- **A. 一般 PR 不動版號**：`src/__manifest__.py` 的版號必須**等於**當下 origin/develop。
  版號 > develop 而既沒帶 migration、也不是 release PR ⇒ 紅。
- **B. 帶 migration 的 PR 才 bump**：版號嚴格大於 develop，且**本 PR 新增／變更的**
  `src/migrations/<X>/` 至少有一個 X 等於新的 manifest 版號（轉換規則沿用 Odoo 的
  `convert_version`）。目錄名與版號對不上 ⇒ 紅（Odoo 的執行視窗是
  `installed < 目錄 <= manifest`，名字取錯的那一側**靜默 no-op**）。
- **C. 發版才 bump**：`/woow:deploy` 以一張 `release/*` 分支的 PR 把版號嚴格升一號，
  merge 後打 tag。release PR 只要求嚴格大於。

不管哪一類，**版號低於 develop 一律紅**——包含「本 PR 沒動版號但 base 前進了」。
develop 的分支保護開著 `block_on_outdated_branch`，落後 base 本來就不准 merge，
所以這道紅燈與「你本來就得先 `git merge origin/develop`」完全同範圍，不是額外的門檻。

為什麼 release 訊號取**分支** `release/*` 而不是標題前綴 `release:`
------------------------------------------------------------------
本 repo 的 PR 標題一律被冠上「⛔ 勿代 merge｜待 Eugene 覆核 — 」前綴（實查：最近 20 張
PR，20/20 都有）⇒ `title.startswith("release:")` 對真正的 release PR 是**假陰性**，
gate 會把發版 PR 判紅。放寬成「標題**含** `release:`」則走向另一個極端：任何人在標題裡
打幾個字就能豁免規則 A，等於把閘門變成自助門。分支名由 `/woow:deploy` 產生、不被任何
前綴裝飾，是唯一結構性的訊號。

⚠ 訊號來源拿不到時**一律當非 release**（fail-closed），而且報告會回印實際收到的原始
輸入。少了這個回印，「workflow 傳參壞掉」與「這本來就不是 release PR」在輸出上完全
不可區分——兩者都是一句「不是 release PR」。

🔴 為什麼是 CI 而不是 `scripts/audit/repo-lint.py`
------------------------------------------------
repo-lint 是純靜態檢查；這條要**比對遠端當下的值**。判準是「相對於**當下的**
origin/develop」——由**排隊順序**決定，不是一次修掉就沒事：rebase 完到真正 merge 之間，
develop 可能又被別的 migration／release PR 動過。所以檢查時機必須是**每次 push 都重跑**，
這正是 CI 的性質。

🔴 為什麼一定要數值 tuple 比
--------------------------
    '18.0.1.0.100' > '18.0.1.0.99'   →  False   ← 字串比較（'1' < '9'）
    (18,0,1,0,100) > (18,0,1,0,99)   →  True    ← 數值 tuple

本 repo 已跨過百位，兩個失效方向都會實際發生：放行真正倒退的值（假綠），或把正當的
`.100` 判成「未大於 `.99`」（假紅）。下次撞到是 .199→.200、.999→.1000。

範圍（#538 留言 3，leader6）
--------------------------
- **只管 `src/**`**。`extra/paas-operator/` 是另一條獨立發版線（operator image，不吃 addon
  manifest 版號）；`docs/`、`.gitea/`、`scripts/` 更無關。沒動 `src/` 的 PR 連 manifest
  都碰不到（manifest 就在 `src/` 底下），版號關係只可能來自 base 前進 ⇒ 直接放行。
- ⚠ #691 起**沒有 tests-only 豁免**。舊制的豁免是「免除 bump 義務」，而新制根本沒有
  bump 義務可免；剩下的義務是「不得擅自 bump」與「別落後 develop」，純測試 PR 沒有比
  別人多的權利。守線隨它守的那條義務一起移除——留著一個沒有指涉對象、卻長得像守線的
  分支比刪掉更糟。

綠燈過期的缺口：**由 repo 設定收掉，不是由這支腳本或 workflow 收掉**
----------------------------------------------------------------
`pull_request` 事件只在 **PR 自己被 push** 時觸發，別人 merge 進 develop 不會讓本 PR 的
gate 自動重跑——這一點沒變。真正關掉它的是 develop 分支保護的
`block_on_outdated_branch`（2026-08-06 起；**開關現值一律看分支保護設定，此處刻意不複述**）：
開著時 PR 落後 base 就不准 merge ⇒ 必須先合併 ⇒ 那次 push 重跑本 gate
⇒ merge 當下的綠燈必然是對「當下 develop」驗出來的。

⚠ 這個閉合**依賴那個設定持續開著**；被關掉的話缺口原樣回來，且不會有任何訊號。

另：本 gate 的 workflow **刻意沒有 paths 白名單**，每個 PR 都無條件跑——因為它要當
required status check，而 paths 沒命中時該 context 不存在（不是紅也不是綠），required
判準永遠不滿足 ⇒ 純 docs PR 會卡死。理由與實證見
`.gitea/workflows/manifest-version-gate.yml` 的 `on.pull_request` 註解。

跑法：
    python3 scripts/ci/manifest-version-gate.py                      # 預設比 origin/develop
    python3 scripts/ci/manifest-version-gate.py --base-ref origin/main
    python3 scripts/ci/manifest-version-gate.py --head-ref release/18.0.1.0.176
自測（CI 每次先跑）：
    python3 scripts/ci/test_manifest_version_gate.py
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import re
import subprocess
import sys

MANIFEST_PATH = "src/__manifest__.py"
MIGRATIONS_DIR = "src/migrations"
SRC_PREFIX = "src/"
MIGRATIONS_PREFIX = "src/migrations/"

# 規則 C 的訊號：分支名以此為前綴（目錄邊界，`release-notes` / `released` 不算）。
RELEASE_BRANCH_PREFIX = "release/"

# Odoo 的 `release.major_version`。migration 目錄名點數 <= 2 時要補上這個前綴，
# 見 odoo/modules/migration.py :: MigrationManager.migrate_module 的 convert_version()。
ODOO_MAJOR_VERSION = "18.0"

# 逐字照抄 odoo/modules/migration.py :: VERSION_RE。不符的目錄 Odoo 根本不看
# （只留一行 warning），所以本 gate 也不拿它們比大小——但**必須列出來**，
# 「跳過幾個」不夠，要說出是哪幾個。
VERSION_RE = re.compile(
    r"""^
        ((
            6\.1|
            [6-9]\.0|
            [1-9]\d+\.0|
            (7|8|9|10)\.saas~[1-9]\d*|
            saas~(1[1-9]|[2-9]\d+)\.[1-9]
        )\.)?
        \d+\.\d+(\.\d+)?
    $""",
    re.VERBOSE | re.ASCII,
)

_MANIFEST_VERSION_RE = re.compile(r"""["']version["']\s*:\s*["']([^"']+)["']""")


class GateError(RuntimeError):
    """環境／輸入本身有問題——fail-loud，不猜。"""


# --------------------------------------------------------------------------
# 版號：一律數值 tuple
# --------------------------------------------------------------------------
def parse_version_tuple(version: str) -> tuple[int, ...]:
    """'18.0.1.0.116' → (18, 0, 1, 0, 116)。

    不做「盡力而為」的容錯：猜錯的方向正好是假綠，寧可 fail-loud。
    """
    if not version or not version.strip():
        raise ValueError("版號是空的，無法比較")
    parts = version.strip().split(".")
    if not all(p.isdigit() for p in parts):
        raise ValueError(
            f"版號 {version!r} 含非純數字段，無法做數值比較。"
            f"本 gate 刻意不退回字串比較——那正是 .100 < .99 的來源。"
        )
    return tuple(int(p) for p in parts)


def compare_versions(a: str, b: str) -> int:
    """數值比較，短的一方補 0。回傳 -1 / 0 / 1。"""
    ta, tb = parse_version_tuple(a), parse_version_tuple(b)
    width = max(len(ta), len(tb))
    ta += (0,) * (width - len(ta))
    tb += (0,) * (width - len(tb))
    return (ta > tb) - (ta < tb)


def next_version(version: str) -> str:
    """給出一個嚴格大於 `version` 的最小值（末段 +1）。"""
    parts = list(parse_version_tuple(version))
    parts[-1] += 1
    return ".".join(str(p) for p in parts)


def read_manifest_version(text: str) -> str:
    m = _MANIFEST_VERSION_RE.search(text)
    if not m:
        raise ValueError(f"在 {MANIFEST_PATH} 內容中找不到 version 欄位")
    return m.group(1)


# --------------------------------------------------------------------------
# 範圍
# --------------------------------------------------------------------------
@dataclasses.dataclass
class ScopeVerdict:
    in_scope: bool
    reason: str  # out-of-scope | in-scope
    src_paths: list[str]


def _under(path: str, prefix: str) -> bool:
    """目錄邊界比對，不是字串前綴。

    `srcfoo/bar.py` 不在 `src/` 底下；`src/migrations_old/x` 不在 `src/migrations/` 底下。
    少了這個邊界，範圍判準會被檔名拼字繞過。
    """
    return path == prefix.rstrip("/") or path.startswith(prefix)


def classify_scope(changed_paths: list[str]) -> ScopeVerdict:
    src_paths = sorted(p for p in changed_paths if _under(p, SRC_PREFIX))
    if not src_paths:
        return ScopeVerdict(False, "out-of-scope", [])
    return ScopeVerdict(True, "in-scope", src_paths)


def collect_pr_migration_dirs(changed_paths: list[str]) -> list[str]:
    """**本 PR 新增／變更**的 `src/migrations/<X>/` 目錄名（規則 B 的輸入）。

    🔴 這裡吃的必須是 merge-base 三點 diff 的 changed，**不是工作目錄整份 listing**。
       整份 listing 含所有既有目錄（本 repo 現有 41 個）⇒ 任何 PR 都會被判成「帶
       migration」⇒ 規則 B 恆真、規則 A 的紅燈永遠不會出現。那是假綠，而且輸出與真通過
       一模一樣。吃整份 listing 的是另一支 `evaluate_migrations`（目錄 > manifest ⇒ 紅），
       兩者刻意不共用輸入。
    """
    dirs = set()
    for path in changed_paths:
        if not _under(path, MIGRATIONS_PREFIX):
            continue
        rest = path[len(MIGRATIONS_PREFIX):]
        # `src/migrations/README.md` 沒有目錄段可取，不算一個 migration 目錄。
        if "/" not in rest:
            continue
        name = rest.split("/", 1)[0]
        if name:
            dirs.add(name)
    return sorted(dirs)


# --------------------------------------------------------------------------
# release 訊號（規則 C）
# --------------------------------------------------------------------------
@dataclasses.dataclass
class ReleaseSignal:
    is_release: bool
    usable: bool          # 有沒有拿到「看得懂的分支名」——與「不是 release 分支」不同
    branch: str | None
    source: str | None    # 值是從哪個輸入來的
    raw_inputs: dict      # 實際收到的原始值，原樣回印
    detail: str


def normalise_branch_ref(value: str | None) -> str | None:
    """把 ref 正規化成分支名；不是分支名就回 None（**不猜**）。

    ⚠ `tea api` 的 pulls listing 回的是 `refs/pull/<n>/head`——那是 PR 的 head ref，
       不是分支名。Gitea Actions 的 event payload 給什麼是另一回事，必須實測，所以
       這裡對「不認得的 refs/*」回 None 並讓上層講出來，而不是硬切最後一段當分支名。
    """
    if not value or not value.strip():
        return None
    ref = value.strip()
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/"):]
    elif ref.startswith("refs/"):
        return None
    return ref or None


def detect_release_signal(
    head_ref: str | None = None, env_head_ref: str | None = None
) -> ReleaseSignal:
    raw = {"--head-ref": head_ref or "", "GITHUB_HEAD_REF": env_head_ref or ""}

    for source, value in (("--head-ref", head_ref), ("GITHUB_HEAD_REF", env_head_ref)):
        branch = normalise_branch_ref(value)
        if branch is None:
            continue
        # ⚠ 這裡刻意**不用** `_under()`：它的目錄語意會讓 `path == prefix.rstrip("/")`
        #   成立（`src` 算在 `src/` 底下，對路徑是對的），套到分支名上就變成分支叫
        #   `release` 也被當成 release PR。要的是嚴格前綴 `release/<something>`。
        is_release = branch.startswith(RELEASE_BRANCH_PREFIX)
        detail = (
            f"release 訊號來源 `{source}` = `{value}` → 分支名 `{branch}` → "
            + ("**是** release PR（規則 C）。" if is_release
               else f"不是 release PR（分支名未以 `{RELEASE_BRANCH_PREFIX}` 開頭）。")
        )
        return ReleaseSignal(is_release, True, branch, source, raw, detail)

    # 走到這裡代表兩個來源都拿不到分支名。分兩種講法，因為修法不同。
    provided = [f"`{k}` = `{v}`" for k, v in raw.items() if v]
    if provided:
        detail = (
            "⚠ 收到了 release 訊號輸入，但**不是分支名**，無法據以判定 release PR："
            + "、".join(provided)
            + "\n（`refs/pull/<n>/head` 是 PR 的 head ref，不是分支名；本 gate 刻意不硬切"
            "最後一段來猜——猜錯的方向正好是「一般 PR 被當成 release PR 放行」。）\n"
            "⇒ 一律當**非** release PR 處理（fail-closed）。"
            "修法：workflow 改傳真正的分支名（見 .gitea/workflows/manifest-version-gate.yml）。"
        )
    else:
        detail = (
            "⚠ **未收到**任何 release 訊號來源（`--head-ref` 與 `$GITHUB_HEAD_REF` 皆空）"
            "⇒ 一律當**非** release PR 處理（fail-closed）。\n"
            "本機手跑時這是正常的；在 CI 裡看到這行代表 workflow 傳參壞了。"
        )
    return ReleaseSignal(False, False, None, None, raw, detail)


# --------------------------------------------------------------------------
# 判準 1：三條規則的裁決
# --------------------------------------------------------------------------
@dataclasses.dataclass
class Verdict:
    ok: bool
    # out-of-scope | unchanged | migration-bump | release-bump
    # | unexpected-bump | migration-mismatch | behind
    reason: str
    detail: str
    suggested_version: str | None = None
    scope: ScopeVerdict | None = None
    release: ReleaseSignal | None = None
    pr_migration_dirs: list[str] = dataclasses.field(default_factory=list)


_SILENT_SAME_VALUE = """🔴 為什麼「照抄 develop 的版號」對會 bump 的 PR 特別危險（#538 的原始成因）：
   兩張 PR 各自把同一行改成**同一個值**時，git 認定無衝突——那行 diff 是空白的，
   衝突不會出現，review 看 diff 也是空白。後 merge 的那個**靜默地沒有 bump**。
   模組不升版 ⇒ 下次發版 `-u` 不會跑到它，migration 也不會執行。同日曾發生三次
   （#352 / PR#517 / #522）。所以會 bump 的 PR（migration／release）一律要求
   **嚴格大於當下 develop**，不是「大於等於」、也不是「照抄」。"""

_UNEXPECTED_BUMP_DETAIL = """PR 的 manifest 版號 {head} **高於** origin/{base} 的 {version}，
但本 PR 既**沒有新增／變更任何 `src/migrations/<新版號>/`**，分支名也不是 `release/*`。

🔴 **一般 PR 不得動版號**（#691 規則 A）。版號只由兩類 PR 帶：
   1. **帶 migration 的 PR** —— 新增 `src/migrations/{head}/`（目錄名必須等於新版號）；
   2. **發版 PR** —— `/woow:deploy` 開的 `release/*` 分支。

為什麼改成這樣：舊制要求「每張 PR 都嚴格大於 develop」，於是每張 PR 都改同一行 ⇒
merge train 每合一張、剩下每一張都在同一位置重新產生同一個衝突（2026-08-20/21 六張 PR
的實戰紀錄見 docs/guides/development/merge-train-acceptance.md）。

正解：把版號**改回 {version}**（＝當下 develop 的值，也就是別動那一行）。
   本 PR 若確實需要跑 migration，改走上面第 1 條：連同 `src/migrations/{head}/` 一起提交。"""

_MIGRATION_MISMATCH_DETAIL = """PR 把 manifest 版號 bump 到 {head}（origin/{base} 是 {version}），
本 PR 也動到了 migration 目錄，但**沒有任何一個目錄名等於新版號**。

本 PR 新增／變更的 migration 目錄：
{dirs}

🔴 Odoo 的執行視窗是 `installed < 目錄 <= manifest`
   （odoo/modules/migration.py :: MigrationManager.migrate_module 的 compare()）。
   目錄名 < 新版號時，已升到該版之後的 DB **完全不會執行它**——不會報錯、不會有訊息，
   純粹靜默 no-op。這正是 #691 規則 B 要求「目錄名 == 新 manifest 版號」的理由。

正解二選一：
- 把 migration 目錄改名成 **{head}**（`src/migrations/{head}/`）；或
- 把 manifest 版號改成目錄實際的版號。
（目錄名點數 <= 2 時 Odoo 會自動補上 `{major}.` 前綴，本 gate 沿用同一條轉換規則。）"""

_BEHIND_DETAIL = """PR 的 manifest 版號 {head} **低於** origin/{base} 的 {version}。

這一種**會吵**——三方相異，合併時 git 會真的報衝突。問題不是沒訊號，是
**訊號出現時人會解錯**：順手 take theirs 或 take ours 都沒有重想。

- take ours（保留自己的 {head}）⇒ 把 develop **降版**，比沒 bump 更糟：版號倒退，
  升級邏輯判成不需要升。
- take theirs（照抄 develop 的 {version}）⇒ 對一般 PR 是對的；對會 bump 的 PR 則變成
  「同值」，一樣靜默不升版。

{resolution}"""

_BEHIND_ORDINARY_RESOLUTION = """正解：`git merge origin/develop` 把 develop 併進來，版號回到 **{version}**。
一般 PR 不動版號（#691 規則 A），所以這一行不會有語意衝突——只有一邊改過它。

⚠ develop 的分支保護開著 `block_on_outdated_branch`，落後 base 本來就不准 merge，
   這道紅燈與「你本來就得先合併 develop」同範圍，不是額外的門檻。"""

_BEHIND_BUMPING_RESOLUTION = """本 PR 屬於**會 bump 的那一類**（帶 migration 或 release 分支），
所以正解不是照抄 develop，而是**重新取號**：改成 **{suggested}**（嚴格大於當下 develop）；
帶 migration 的話**目錄名要一起改成同一個值**。

這正是 #691 預期會發生、而且**看得見**的那種競爭：release PR 追過待合的 migration PR 時，
後者的版號不再嚴格大於 develop ⇒ 在這裡紅一次，重 bump ＋ 改目錄名即可。舊制底下這件事
是靜默的（見下段）。

""" + _SILENT_SAME_VALUE


def evaluate_bump(
    base_version: str,
    head_version: str,
    changed_paths: list[str],
    release: ReleaseSignal | None = None,
) -> Verdict:
    scope = classify_scope(changed_paths)
    release = release or detect_release_signal()
    pr_migration_dirs = collect_pr_migration_dirs(changed_paths)

    if not scope.in_scope:
        detail = (
            "本 PR 沒有動到 src/ 底下任何檔案，不在本 gate 範圍內"
            "（extra/paas-operator/ 是另一條獨立發版線；docs/、.gitea/、scripts/ 無關）。\n"
            f"沒動 src/ 就碰不到 {MANIFEST_PATH}（它就在 src/ 底下）⇒ 版號關係只可能來自 "
            "base 前進，那是分支保護 block_on_outdated_branch 的事，不是本 gate 的事。"
        )
        return Verdict(True, "out-of-scope", detail, None, scope, release, pr_migration_dirs)

    cmp = compare_versions(head_version, base_version)
    fmt = dict(
        base="develop",
        version=base_version,
        head=head_version,
        suggested=next_version(base_version),
        major=ODOO_MAJOR_VERSION,
    )

    # --- 版號低於 develop：不分類別一律紅 ---------------------------------
    if cmp < 0:
        bumping = bool(pr_migration_dirs) or release.is_release
        resolution = (
            _BEHIND_BUMPING_RESOLUTION if bumping else _BEHIND_ORDINARY_RESOLUTION
        ).format(**fmt)
        detail = _BEHIND_DETAIL.format(resolution=resolution, **fmt)
        suggested = next_version(base_version) if bumping else base_version
        return Verdict(
            False, "behind", detail, suggested, scope, release, pr_migration_dirs
        )

    # --- 版號等於 develop：規則 A 的常態 ----------------------------------
    if cmp == 0:
        detail = (
            f"版號與 origin/develop 相同（都是 {base_version}）——**這是一般 PR 的常態**"
            "（#691 規則 A：版號由帶 migration 的 PR 或發版 PR 帶，一般 PR 不動那一行）。"
        )
        return Verdict(
            True, "unchanged", detail, None, scope, release, pr_migration_dirs
        )

    # --- 版號高於 develop：只有兩類 PR 可以 -------------------------------
    if release.is_release:
        detail = (
            f"{head_version} 嚴格大於 origin/develop 的 {base_version}，"
            f"且本 PR 是 release PR（#691 規則 C）。\n{release.detail}"
        )
        return Verdict(
            True, "release-bump", detail, head_version, scope, release, pr_migration_dirs
        )

    if pr_migration_dirs:
        matched = [
            d
            for d in pr_migration_dirs
            if compare_versions(convert_migration_version(d), head_version) == 0
        ]
        if matched:
            detail = (
                f"{head_version} 嚴格大於 origin/develop 的 {base_version}，"
                f"且本 PR 帶了對應版號的 migration（#691 規則 B）：\n"
                + "\n".join(f"  - {MIGRATIONS_DIR}/{d}" for d in matched)
            )
            if len(matched) != len(pr_migration_dirs):
                others = [d for d in pr_migration_dirs if d not in matched]
                detail += (
                    "\n\n本 PR 另外動到的 migration 目錄（不影響判定，列出以免靜默截斷）："
                    + "、".join(others)
                )
            return Verdict(
                True,
                "migration-bump",
                detail,
                head_version,
                scope,
                release,
                pr_migration_dirs,
            )
        detail = _MIGRATION_MISMATCH_DETAIL.format(
            dirs="\n".join(f"  - {MIGRATIONS_DIR}/{d}" for d in pr_migration_dirs),
            **fmt,
        )
        return Verdict(
            False,
            "migration-mismatch",
            detail,
            head_version,
            scope,
            release,
            pr_migration_dirs,
        )

    detail = _UNEXPECTED_BUMP_DETAIL.format(**fmt)
    detail += "\n\n" + release.detail
    detail += "\n\n觸發本檢查的 src/ 改動：\n" + "\n".join(
        f"  - {p}" for p in scope.src_paths
    )
    return Verdict(
        False, "unexpected-bump", detail, base_version, scope, release, pr_migration_dirs
    )


# --------------------------------------------------------------------------
# 判準 2：migration 目錄版號不得高於 manifest
#
# ⚠ 這一支吃的是**工作目錄整份 listing**（所有既有目錄），與規則 B 的輸入
#   （本 PR 動到的目錄）刻意不同——#691 明寫保留，行為不變。
# --------------------------------------------------------------------------
@dataclasses.dataclass
class MigrationVerdict:
    ok: bool
    detail: str
    offenders: list[str] = dataclasses.field(default_factory=list)
    compared_dirs: list[str] = dataclasses.field(default_factory=list)
    ignored_dirs: list[str] = dataclasses.field(default_factory=list)


def convert_migration_version(dirname: str) -> str:
    """照搬 Odoo 的 convert_version()：點數 <= 2 才補 server major 前綴。"""
    if dirname == "0.0.0":
        return dirname
    if dirname.count(".") > 2:
        return dirname
    return f"{ODOO_MAJOR_VERSION}.{dirname}"


def evaluate_migrations(manifest_version: str, dirnames: list[str]) -> MigrationVerdict:
    offenders: list[str] = []
    compared: list[str] = []
    ignored: list[str] = []

    for name in sorted(dirnames):
        # `0.0.0` 在 Odoo 是「任何版本變更都會跑」的特例，不參與大小比較。
        if name == "0.0.0":
            compared.append(name)
            continue
        if name == "tests" or not VERSION_RE.match(name):
            ignored.append(name)
            continue
        compared.append(name)
        if compare_versions(convert_migration_version(name), manifest_version) > 0:
            offenders.append(name)

    lines = []
    if offenders:
        lines.append(
            f"以下 migration 目錄版號**高於** manifest 的 {manifest_version}，"
            f"Odoo 會**靜默 no-op**（執行視窗是 installed < 目錄 <= manifest，"
            f"高於 manifest 那一側不會被執行、也不會有任何訊息）：\n"
            + "\n".join(f"  - {MIGRATIONS_DIR}/{n}" for n in offenders)
            + f"\n\n正解：把 manifest 版號升到 >= 這些目錄的最大值，"
            f"或把目錄改名成 <= {manifest_version}。"
        )
    else:
        lines.append(
            f"{len(compared)} 個 migration 目錄版號皆 <= manifest 的 {manifest_version}。"
            if compared
            else "沒有 migration 目錄需要比對。"
        )
    if ignored:
        # 「跳過幾個」不夠，要說出是哪幾個——否則就是靜默截斷。
        lines.append(
            "Odoo 不會執行、故未納入比較的目錄（不符 VERSION_RE 或名為 tests）："
            + "、".join(ignored)
        )
    return MigrationVerdict(not offenders, "\n\n".join(lines), offenders, compared, ignored)


# --------------------------------------------------------------------------
# git 層
# --------------------------------------------------------------------------
def _git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise GateError(
            f"`git {' '.join(args)}` 失敗（exit {proc.returncode}）：\n{proc.stderr.strip()}"
        )
    return proc.stdout


_SHALLOW_HINT = (
    "這是 shallow clone（`git rev-parse --is-shallow-repository` 回 true），"
    "算不出 merge-base ⇒ 無法得知本 PR 到底動了哪些檔案。\n"
    "正解：checkout 步驟加上 **fetch-depth: 0**。\n"
    "（若不修，gate 只會在此 fail-loud——刻意不退回「猜一個檔案清單」，"
    "猜錯的方向正好是假綠。）"
)


def collect_facts(repo: pathlib.Path, base_ref: str) -> dict:
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise GateError(_SHALLOW_HINT)

    try:
        base_sha = _git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}").strip()
    except GateError as exc:
        raise GateError(
            f"取不到 base ref {base_ref!r}：\n{exc}\n"
            "CI 內請先 `git fetch origin <base branch>`；本機請確認 remote 已同步。"
        ) from exc

    head_sha = _git(repo, "rev-parse", "HEAD").strip()
    try:
        merge_base = _git(repo, "merge-base", base_sha, head_sha).strip()
    except GateError as exc:
        raise GateError(f"算不出 merge-base（{base_ref} vs HEAD）：\n{exc}\n{_SHALLOW_HINT}") from exc

    # 🔴 三點語意（merge-base..HEAD）：只算**本 PR 自己**動的檔案。
    # 兩點 diff 會把 base 分支自己的改動也算進來 ⇒「只改 docs 的 PR」被誤判成動了 src/ ⇒ 假紅。
    changed = [
        line for line in _git(repo, "diff", "--name-only", merge_base, head_sha).splitlines() if line
    ]

    try:
        base_manifest = _git(repo, "show", f"{base_sha}:{MANIFEST_PATH}")
    except GateError as exc:
        raise GateError(f"讀不到 {base_ref}:{MANIFEST_PATH}：\n{exc}") from exc

    head_manifest_path = repo / MANIFEST_PATH
    if not head_manifest_path.is_file():
        raise GateError(f"工作目錄找不到 {MANIFEST_PATH}")

    migrations_root = repo / MIGRATIONS_DIR
    migration_dirs = (
        sorted(p.name for p in migrations_root.iterdir() if p.is_dir())
        if migrations_root.is_dir()
        else []
    )

    return {
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "changed": changed,
        "base_version": read_manifest_version(base_manifest),
        "head_version": read_manifest_version(
            head_manifest_path.read_text(encoding="utf-8")
        ),
        # 整份 listing：只給 evaluate_migrations 用（目錄 > manifest ⇒ 紅）。
        "migration_dirs": migration_dirs,
        # 本 PR 自己動到的：規則 B 的輸入。兩者刻意分開，見 collect_pr_migration_dirs。
        "pr_migration_dirs": collect_pr_migration_dirs(changed),
    }


# --------------------------------------------------------------------------
# 報告
# --------------------------------------------------------------------------
def render_report(facts: dict, bump: Verdict, migrations: MigrationVerdict) -> str:
    ok = bump.ok and migrations.ok
    release = bump.release or detect_release_signal()
    # 🔴 實際收到的原始輸入原樣回印。少了這行，「傳參壞掉」與「這不是 release PR」
    #    在輸出上完全不可區分——兩者都只會是一句「不是 release PR」。
    echoed = "、".join(f"`{k}` = `{v}`" for k, v in release.raw_inputs.items())
    lines = [
        "## manifest 版號 gate（#538／#691）",
        "",
        f"- 結果：{'✅ PASS' if ok else '❌ FAIL'}",
        f"- base：`{facts['base_ref']}` @ `{facts['base_sha'][:12]}` → 版號 `{facts['base_version']}`",
        f"- head：`{facts['head_sha'][:12]}` → 版號 `{facts['head_version']}`",
        f"- merge-base：`{facts['merge_base'][:12]}`",
        f"- 本 PR 動到 {len(facts['changed'])} 個檔案，其中 src/ 底下 "
        f"{len(bump.scope.src_paths) if bump.scope else 0} 個",
        f"- 本 PR 動到的 migration 目錄："
        + (
            "、".join(f"`{d}`" for d in facts.get("pr_migration_dirs") or [])
            or "（無）"
        ),
        f"- release 訊號輸入（原樣回印）：{echoed}",
        "",
        release.detail,
        "",
        f"### 1. 版號規則（#691 A／B／C） — {'✅' if bump.ok else '❌'} `{bump.reason}`",
        "",
        bump.detail,
        "",
        f"### 2. migration 目錄版號 <= manifest — {'✅' if migrations.ok else '❌'}",
        "",
        migrations.detail,
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    default_base = os.environ.get("GITHUB_BASE_REF") or "develop"
    parser = argparse.ArgumentParser(description="manifest 版號 gate（#538／#691）")
    parser.add_argument(
        "--base-ref",
        default=f"origin/{default_base}" if os.environ.get("GITHUB_BASE_REF") else "origin/develop",
        help="要比對的 base ref（預設 origin/$GITHUB_BASE_REF，否則 origin/develop）",
    )
    parser.add_argument(
        "--head-ref",
        default="",
        help=(
            "本 PR 的 head 分支名（規則 C 的 release 訊號來源）。"
            "拿不到時退回 $GITHUB_HEAD_REF；兩者皆無 ⇒ 一律當非 release PR。"
        ),
    )
    parser.add_argument("--repo-root", default=".", help="repo 根目錄（預設當前目錄）")
    args = parser.parse_args(argv)

    release = detect_release_signal(
        head_ref=args.head_ref, env_head_ref=os.environ.get("GITHUB_HEAD_REF")
    )

    repo = pathlib.Path(args.repo_root).resolve()
    try:
        facts = collect_facts(repo, args.base_ref)
        bump = evaluate_bump(
            facts["base_version"],
            facts["head_version"],
            facts["changed"],
            release=release,
        )
        migrations = evaluate_migrations(facts["head_version"], facts["migration_dirs"])
    except (GateError, ValueError) as exc:
        report = (
            "## manifest 版號 gate（#538／#691）\n\n"
            "- 結果：❌ FAIL（gate 無法完成檢查）\n\n"
            f"{exc}"
        )
        print(report)
        _write_step_summary(report)
        return 1

    report = render_report(facts, bump, migrations)
    print(report)
    _write_step_summary(report)
    return 0 if (bump.ok and migrations.ok) else 1


def _write_step_summary(report: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(report + "\n")


if __name__ == "__main__":
    sys.exit(main())

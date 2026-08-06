#!/usr/bin/env python3
"""manifest 版號嚴格遞增 gate（#538）。

擋兩件同一家族的事——**版號關係錯了，但沒有任何東西會叫**：

1. **PR 的 `src/__manifest__.py` 版號未嚴格大於當下 `origin/develop`。**
   版號是 CD 的 module-version-gate 判斷要不要跑 `-u woow_paas_platform` 的唯一訊號
   （docs/guides/deployment/prod-release-sop.md）。沒升版 ⇒ 模組不更新、migration 不執行。
2. **`src/migrations/` 的目錄版號高於 manifest。** Odoo 的執行視窗是
   `installed < 目錄 <= manifest`（odoo/modules/migration.py :: MigrationManager.migrate_module
   的 `compare()`），高於 manifest 的那一側**靜默 no-op**。

🔴 為什麼是 CI 而不是 `scripts/audit/repo-lint.py`
------------------------------------------------
repo-lint 是純靜態檢查；這條要**比對遠端當下的值**。而且判準是「嚴格大於**當下的**
origin/develop」——由**排隊順序**決定，不是一次修掉就沒事：rebase 完到真正 merge 之間，
develop 可能又被別人 bump 過。所以檢查時機必須是**每次 push 都重跑**，這正是 CI 的性質。

🔴 為什麼人工檢查必然會漏
------------------------
兩邊 bump 到**同一個值**時，git 認定無衝突——那行 diff 是空白的，**衝突根本不會出現**。
所有以 conflict 為訊號的檢查都不會響，review 看 diff 也是空白。後 merge 的那個
**靜默地沒有 bump**。同日曾發生三次（#352 / PR#517 / #522）。

🔴 為什麼一定要數值 tuple 比
--------------------------
    '18.0.1.0.100' > '18.0.1.0.99'   →  False   ← 字串比較（'1' < '9'）
    (18,0,1,0,100) > (18,0,1,0,99)   →  True    ← 數值 tuple

本 repo 已跨過百位，兩個失效方向都會實際發生：放行真正倒退的值（假綠），或把正當的
`.100` 判成「未大於 `.99`」（假紅）。下次撞到是 .199→.200、.999→.1000。

範圍與豁免（#538 留言 3，leader6）
--------------------------------
- **只管 `src/**`**。`extra/paas-operator/` 是另一條獨立發版線（operator image，不吃 addon
  manifest 版號）；`docs/`、`.gitea/`、`scripts/` 更無關。這三類若也要求 bump，
  **製造的假紅會比它防的問題還頻繁**。
- **豁免是 universal 而非 existence**：只有當 PR 動到的 `src/` 檔案**全部落在** `src/tests/`
  內才免 bump。寫成「有動 `src/tests/` 就免」的話，把一行實作塞進測試 PR 就能繞過；
  universal 判準讓**繞路動作本身成為觸發條件**。

已知殘留缺口（誠實標明，不宣稱已覆蓋）
------------------------------------
`pull_request` 事件只在 **PR 自己被 push** 時觸發。**別人 merge 進 develop 不會讓本 PR 的
gate 自動重跑** ⇒ 綠燈可能在 merge 前就過期。收掉它需要 merge queue 或「合併前要求分支
up-to-date」的分支保護設定，不在本腳本能力範圍內。

跑法：
    python3 scripts/ci/manifest-version-gate.py                      # 預設比 origin/develop
    python3 scripts/ci/manifest-version-gate.py --base-ref origin/main
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
TESTS_PREFIX = "src/tests/"

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
# 範圍與豁免
# --------------------------------------------------------------------------
@dataclasses.dataclass
class BumpRequirement:
    required: bool
    reason: str  # out-of-scope | tests-only | bump-required
    src_paths: list[str]
    offending_paths: list[str]


def _under(path: str, prefix: str) -> bool:
    """目錄邊界比對，不是字串前綴。

    `src/tests_helpers.py` 不在 `src/tests/` 底下；`srcfoo/bar.py` 不在 `src/` 底下。
    少了這個邊界，兩個豁免都會被檔名拼字繞過。
    """
    return path == prefix.rstrip("/") or path.startswith(prefix)


def classify_bump_requirement(changed_paths: list[str]) -> BumpRequirement:
    src_paths = sorted(p for p in changed_paths if _under(p, SRC_PREFIX))
    if not src_paths:
        return BumpRequirement(False, "out-of-scope", [], [])

    offending = [p for p in src_paths if not _under(p, TESTS_PREFIX)]
    if not offending:
        return BumpRequirement(False, "tests-only", src_paths, [])

    return BumpRequirement(True, "bump-required", src_paths, offending)


# --------------------------------------------------------------------------
# 判準 1：manifest 版號嚴格大於 base
# --------------------------------------------------------------------------
@dataclasses.dataclass
class Verdict:
    ok: bool
    reason: str  # out-of-scope | tests-only | ok | equal | behind
    detail: str
    suggested_version: str | None = None
    requirement: BumpRequirement | None = None


_EQUAL_DETAIL = """PR 的 manifest 版號與 origin/{base} 完全相同（都是 {version}）。

🔴 這是**零訊號**的那一種：同一行改成同樣的內容，**git 認定無衝突**——rebase 後那行
   diff 是空白的，衝突不會出現，review 看 diff 也是空白。後 merge 的那個**靜默地沒有
   bump**，你的 bump 會被靜默吃掉。模組不升版 ⇒ 下次發版 `-u` 不會跑到它，migration
   也不會執行。

正解：把版號改成 **{suggested}**（嚴格大於當下 develop 的新值）。"""

_BEHIND_DETAIL = """PR 的 manifest 版號 {head} **低於** origin/{base} 的 {version}。

這一種**會吵**——三方相異，rebase/merge 時 git 會真的報衝突。問題不是沒訊號，是
**訊號出現時人會解錯**：順手 take theirs 或 take ours 都沒有重想。

- take ours（保留自己的 {head}）⇒ 把 develop **降版**，比沒 bump 更糟：版號倒退，
  升級邏輯判成不需要升。
- take theirs（照抄 develop 的 {version}）⇒ 變成上面那種「同值」情形，一樣靜默不升版。

正解：改成 **{suggested}**——**嚴格大於當下 develop 的新值，不是保留自己的、也不是
照抄 develop 的**。"""


def evaluate_bump(base_version: str, head_version: str, changed_paths: list[str]) -> Verdict:
    req = classify_bump_requirement(changed_paths)
    if not req.required:
        if req.reason == "out-of-scope":
            detail = (
                "本 PR 沒有動到 src/ 底下任何檔案，不在本 gate 範圍內"
                "（extra/paas-operator/ 是另一條獨立發版線；docs/、.gitea/、scripts/ 無關）。"
            )
        else:
            detail = (
                "本 PR 動到的 src/ 檔案**全部**落在 src/tests/ 內（純測試 PR），"
                "依慣例免 bump：\n"
                + "\n".join(f"  - {p}" for p in req.src_paths)
            )
        return Verdict(True, req.reason, detail, requirement=req)

    cmp = compare_versions(head_version, base_version)
    suggested = next_version(base_version)
    if cmp > 0:
        return Verdict(
            True,
            "ok",
            f"{head_version} 嚴格大於 origin/develop 的 {base_version}。",
            suggested,
            req,
        )

    fmt = dict(
        base="develop", version=base_version, head=head_version, suggested=suggested
    )
    detail = (_EQUAL_DETAIL if cmp == 0 else _BEHIND_DETAIL).format(**fmt)
    detail += "\n\n觸發本檢查的 src/ 改動（不在 src/tests/ 內）：\n" + "\n".join(
        f"  - {p}" for p in req.offending_paths
    )
    return Verdict(False, "equal" if cmp == 0 else "behind", detail, suggested, req)


# --------------------------------------------------------------------------
# 判準 2：migration 目錄版號不得高於 manifest
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
        "migration_dirs": migration_dirs,
    }


# --------------------------------------------------------------------------
# 報告
# --------------------------------------------------------------------------
def render_report(facts: dict, bump: Verdict, migrations: MigrationVerdict) -> str:
    ok = bump.ok and migrations.ok
    lines = [
        "## manifest 版號 gate（#538）",
        "",
        f"- 結果：{'✅ PASS' if ok else '❌ FAIL'}",
        f"- base：`{facts['base_ref']}` @ `{facts['base_sha'][:12]}` → 版號 `{facts['base_version']}`",
        f"- head：`{facts['head_sha'][:12]}` → 版號 `{facts['head_version']}`",
        f"- merge-base：`{facts['merge_base'][:12]}`",
        f"- 本 PR 動到 {len(facts['changed'])} 個檔案，其中 src/ 底下 "
        f"{len(bump.requirement.src_paths) if bump.requirement else 0} 個",
        "",
        f"### 1. 版號嚴格遞增 — {'✅' if bump.ok else '❌'} `{bump.reason}`",
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
    parser = argparse.ArgumentParser(description="manifest 版號嚴格遞增 gate（#538）")
    parser.add_argument(
        "--base-ref",
        default=f"origin/{default_base}" if os.environ.get("GITHUB_BASE_REF") else "origin/develop",
        help="要比對的 base ref（預設 origin/$GITHUB_BASE_REF，否則 origin/develop）",
    )
    parser.add_argument("--repo-root", default=".", help="repo 根目錄（預設當前目錄）")
    args = parser.parse_args(argv)

    repo = pathlib.Path(args.repo_root).resolve()
    try:
        facts = collect_facts(repo, args.base_ref)
        bump = evaluate_bump(facts["base_version"], facts["head_version"], facts["changed"])
        migrations = evaluate_migrations(facts["head_version"], facts["migration_dirs"])
    except (GateError, ValueError) as exc:
        report = f"## manifest 版號 gate（#538）\n\n- 結果：❌ FAIL（gate 無法完成檢查）\n\n{exc}"
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

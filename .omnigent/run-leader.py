#!/usr/bin/env python3
"""啟動 woow multi-agent bundle 的唯一正確入口。

⛔ **不要直接跑 `omnigent run .omnigent/woow-issue-leader`**——會壞，而且其中一種壞法
   是靜默的。理由見 `.omnigent/README.md` §啟動：為什麼一定要走 launcher。

用法::

    .omnigent/run-leader.py                      # 啟動 issue-leader
    .omnigent/run-leader.py --check-only         # 只做 materialize ＋自檢，不啟動
    .omnigent/run-leader.py --rewrite-cwd        # 額外把 os_env.cwd 改寫成相對路徑
    .omnigent/run-leader.py -- -p "盤點 #823"    # `--` 之後的參數原樣透傳給 omnigent run

本 script 做四件事：

1. `omnigent.spec.materialize_bundle()` 把整棵樹複製到臨時目錄，**解引用所有 symlink**。
2. 對解引用後的樹跑 `cli._bundle()` ＋ `server.bundles.validate_agent_bundle()` 自檢
   （模擬 server 端實際會看到的東西）。**任一項不符就 fail loud 並停下**，不啟動。
3. 把那棵臨時樹（不是版控的原始樹）指給 `omnigent run`。
4. `omnigent run` 結束後刪掉臨時樹。

── 為什麼需要這一層（實測根因）────────────────────────────────────────────────

`omnigent run <目錄>` 最終會走 `chat._bundle_agent()` → `cli._bundle()`，而 `_bundle()`
對**目錄**來源是 `source.rglob("*")` ＋ `if file_path.is_file()`，**不解 symlink**
（`cli.py:5529-5538`；只有 source 是**單檔 YAML** 時才會先 materialize，見 `cli.py:5508`）。
於是兩種 symlink 各有各的壞法：

| 型態 | 行為 |
| --- | --- |
| symlink **檔**（16 份 thin shell ＋ 2 份 repo_leader 的 `AGENTS.md`） | **loud fail**：tar 裡留下 link entry，server 端 `extract_safe` 直接拒（`spec/tar_utils.py:126`） |
| symlink **目錄**（16 份 thin shell 的 `skills/`） | ⛔ **silent drop**：`rglob` 不遞迴進去，tar 裡一筆 `SKILL.md` 都沒有，但 `validate` 照樣回 valid、`skills` 變成 `[]` |

⚠ **local server 不能規避**：`_chat_local()`（「Start a local server with the agent」）與
`_run_local_headless_prompt()` 跟 remote 的 `_chat_via_daemon()` 一樣都呼叫
`_bundle_agent(spec_path)`。三條路徑共用同一個打包器 ⇒ 症狀相同。

⚠ 驗收上的教訓：`materialize_bundle()` **在這條實際路徑上不會被呼叫**，它只在 source 是
單檔 YAML 或 `omnigent server --agent` preregister 時觸發。所以「對 materialize 後的樹
parse/validate 全綠」證明不了這條路會通——那是另一條路。本 script 的存在就是把實際路徑
**真的**變成 materialize 過的那一條。
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

OMNIGENT_DIR = Path(__file__).resolve().parent
DEFAULT_BUNDLE = OMNIGENT_DIR / "woow-issue-leader"
DEFAULT_SITE_PACKAGES = (
    "/home/elmo/.local/share/uv/tools/omnigent/lib/python3.12/site-packages"
)


# ── interpreter 自舉 ─────────────────────────────────────────────────────────
# 本 script 需要 import omnigent（含 click / httpx 等相依），那些只裝在 omnigent 自己的
# venv 裡。所以先確認能不能 import；不能就找到那個 venv 的 python 並 re-exec 自己。
# ⛔ 不要改成硬編 shebang 指向該 venv——換機器或 uv 重裝路徑就會變。

def _omnigent_python() -> str | None:
    """找出裝了 omnigent 的 python 解譯器路徑。"""
    env = os.environ.get("OMNIGENT_PYTHON")
    if env and Path(env).exists():
        return env
    # uv tool 裝的 `omnigent` 進入點與其 venv python 同目錄
    exe = shutil.which("omnigent")
    if exe:
        candidate = Path(exe).resolve().parent / "python"
        if candidate.exists():
            return str(candidate)
    fallback = Path(DEFAULT_SITE_PACKAGES).parent.parent.parent / "bin" / "python"
    return str(fallback) if fallback.exists() else None


def _ensure_omnigent_importable() -> None:
    try:
        import omnigent.spec  # noqa: F401, PLC0415
    except ImportError:
        pass
    else:
        return
    if os.environ.get("_RUN_LEADER_REEXEC") == "1":
        sys.exit("✗ 無法 import omnigent，且 re-exec 後仍然失敗。請設 OMNIGENT_PYTHON。")
    py = _omnigent_python()
    if py is None:
        sys.exit("✗ 找不到裝了 omnigent 的 python。請設 OMNIGENT_PYTHON=<path>。")
    os.execve(py, [py, str(Path(__file__).resolve()), *sys.argv[1:]],
              {**os.environ, "_RUN_LEADER_REEXEC": "1"})


_ensure_omnigent_importable()

from omnigent.cli import _bundle  # noqa: E402
from omnigent.server.bundles import validate_agent_bundle  # noqa: E402
from omnigent.spec import materialize_bundle, parse  # noqa: E402


# ── 自檢 ─────────────────────────────────────────────────────────────────────

class SelfCheckFailure(RuntimeError):
    """自檢不通過。⛔ 絕不降級成 warning——這支 script 的全部價值就在於會停下來。"""


def _skill_map(spec) -> dict[str, frozenset[str]]:
    """走訪整棵 spec 樹，收集 {dotted agent name: frozenset(skill name)}。

    比對 materialize 前後的這張表，是抓 symlink 目錄 silent drop 的唯一辦法——
    數量與名稱都要比，只比總數會讓「A 少一個、B 多一個」互相抵銷。
    """
    out: dict[str, frozenset[str]] = {}

    def walk(node, path: str = "") -> None:
        here = f"{path}.{node.name}" if path else str(node.name)
        out[here] = frozenset(sk.name for sk in node.skills)
        for child in node.sub_agents:
            walk(child, here)

    walk(spec)
    return out


def _rewrite_absolute_cwd(tree: Path) -> list[str]:
    """把臨時樹裡 `os_env.cwd` 的絕對路徑改寫成 `.`（相對）。

    ⚠ 只動**臨時樹**，版控的 config.yaml 一律不碰。

    為什麼需要這個選項：deployed（multi-user）server 以
    `enforce_handler_allowlist=not local_single_user_enabled()` 呼叫 validate，
    而該檢查會拒絕絕對路徑的 `os_env.cwd`
    （"agent os_env.cwd must be a relative path within the workspace"）。
    本 repo 的 bundle 目前宣告 `cwd: /home/elmo/woow-repos`，所以上 remote 會被擋。
    這是**待使用者拍板的設計問題**（見 README §待拍板），不是本 script 要替他決定的，
    所以預設 **off**，要用得自己加 `--rewrite-cwd`。
    """
    changed: list[str] = []
    pattern = re.compile(r"^(\s*cwd:\s*)(/\S+)\s*$")
    for cfg in sorted(tree.rglob("config.yaml")):
        lines = cfg.read_text().splitlines(keepends=True)
        hit = False
        for i, line in enumerate(lines):
            m = pattern.match(line.rstrip("\n"))
            if m:
                lines[i] = f"{m.group(1)}.\n"
                hit = True
        if hit:
            cfg.write_text("".join(lines))
            changed.append(str(cfg.relative_to(tree)))
    return changed


def self_check(source: Path, tree: Path, *, strict_upload: bool) -> dict[str, object]:
    """對 materialize 後的樹做全套自檢。不通過就 raise SelfCheckFailure。

    :param source: 版控的原始樹（含 symlink），作為 skills 的正確答案。
    :param tree: materialize 後的臨時樹。
    :param strict_upload: 是否要求 `enforce_handler_allowlist=True` 也通過。
        deployed server 走的就是 True ⇒ 預設必須通過；只有明確指定
        `--server local` 時才放寬（local single-user server 用 False）。
    """
    results: list[str] = []

    # (1) 樹內不得殘留任何 symlink
    links = [p for p in tree.rglob("*") if p.is_symlink()]
    if links:
        raise SelfCheckFailure(
            f"materialize 後樹內仍有 {len(links)} 條 symlink（預期 0）：{links[:3]}"
        )
    results.append("樹內 symlink 0")

    # (2) 打包後 tar 內不得有 link entry（server 端 extract_safe 會拒）
    data = _bundle(tree)
    members = tarfile.open(fileobj=io.BytesIO(data)).getmembers()
    tar_links = [m.name for m in members if m.issym() or m.islnk()]
    if tar_links:
        raise SelfCheckFailure(
            f"tar 內有 {len(tar_links)} 個 link entry（預期 0）：{tar_links[:3]}"
        )
    tar_files = [m for m in members if m.isfile()]
    results.append(f"tar link entry 0／files {len(tar_files)}")

    # (3) server 端 validate：寬鬆模式（local single-user server 走這條）
    try:
        spec_lax = validate_agent_bundle(data, enforce_handler_allowlist=False)
    except Exception as exc:  # noqa: BLE001
        raise SelfCheckFailure(
            f"validate_agent_bundle(enforce=False) 失敗：{type(exc).__name__}: {exc}"
        ) from exc
    results.append(f"validate(寬鬆) OK → {spec_lax.name}")

    # (4) server 端 validate：嚴格模式（deployed multi-user server 走這條）
    strict_err: str | None = None
    try:
        validate_agent_bundle(data, enforce_handler_allowlist=True)
    except Exception as exc:  # noqa: BLE001
        strict_err = f"{type(exc).__name__}: {exc}"
    if strict_err is None:
        results.append("validate(嚴格) OK")
    elif strict_upload:
        raise SelfCheckFailure(
            "validate_agent_bundle(enforce=True) 失敗，deployed（multi-user）server 會拒收：\n"
            f"    {strict_err}\n"
            "  → 若這是 os_env.cwd 絕對路徑問題：加 --rewrite-cwd 可在臨時樹內改寫成相對\n"
            "    路徑（不動版控內容）；或改用 --server local（local server 用寬鬆模式）。\n"
            "    這是待拍板的設計問題，見 README §待拍板。"
        )
    else:
        results.append(f"validate(嚴格) 不通過但已放寬：{strict_err}")

    # (5) skills 比對：materialize 前後的每個 agent，skill 名稱集合必須完全相等
    #     這是唯一抓得住 symlink 目錄 silent drop 的檢查。
    before = _skill_map(parse(source, expand_env=False))
    after = _skill_map(spec_lax)
    if before != after:
        diff = [
            f"{k}: 前 {sorted(before.get(k, ()))} != 後 {sorted(after.get(k, ()))}"
            for k in sorted(set(before) | set(after))
            if before.get(k) != after.get(k)
        ]
        raise SelfCheckFailure(
            "materialize 前後 skills 不一致（symlink 目錄被 silent drop 的徵兆）：\n    "
            + "\n    ".join(diff[:6])
        )
    total_skills = sum(len(v) for v in after.values())
    results.append(f"skills 前後一致（{len(after)} 個 agent／共 {total_skills} 支）")

    # (6) tar 內 SKILL.md 實體筆數必須等於 spec 樹的 skills 總數
    #     （(5) 比的是 parse 結果，這一項比的是 tar 裡真的有幾個檔）
    tar_skills = [m.name for m in tar_files if m.name.endswith("SKILL.md")]
    if len(tar_skills) != total_skills:
        raise SelfCheckFailure(
            f"tar 內 SKILL.md {len(tar_skills)} 筆 != spec 樹 skills 總數 {total_skills}"
        )
    results.append(f"tar 內 SKILL.md {len(tar_skills)} 筆")

    # (7) agent 數量（materialize 不得少掉 sub-agent）
    results.append(f"agent {len(after)} 個")

    return {"lines": results, "bytes": len(data), "spec": spec_lax}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="materialize ＋自檢後才啟動 woow bundle（直接 omnigent run 會壞）",
        epilog="`--` 之後的參數原樣透傳給 omnigent run，例如： -- -p '盤點 #823'",
    )
    ap.add_argument("--bundle", default=str(DEFAULT_BUNDLE),
                    help=f"bundle 根目錄（預設 {DEFAULT_BUNDLE.name}）")
    ap.add_argument("--check-only", action="store_true",
                    help="只 materialize ＋自檢，不啟動 omnigent run")
    ap.add_argument("--rewrite-cwd", action="store_true",
                    help="在臨時樹內把 os_env.cwd 的絕對路徑改寫成 '.'（不動版控內容）")
    ap.add_argument("--keep-tree", action="store_true",
                    help="結束後保留臨時樹並印出路徑（debug 用）")
    args, passthrough = ap.parse_known_args()

    source = Path(args.bundle).resolve()
    if not (source / "config.yaml").exists():
        print(f"✗ {source} 裡沒有 config.yaml", file=sys.stderr)
        return 2

    # `--server local` ⇒ local single-user server，validate 走寬鬆模式，嚴格不通過可放行。
    strict_upload = "local" not in _server_values(passthrough)

    tmp_root = Path(tempfile.mkdtemp(prefix="omnigent-run-leader-"))
    tree = tmp_root / source.name
    try:
        materialize_bundle(source, tree)
        rewritten = _rewrite_absolute_cwd(tree) if args.rewrite_cwd else []

        print(f"● 來源        {source}", flush=True)
        print(f"● 臨時樹      {tree}", flush=True)
        if rewritten:
            print(f"● 已改寫 cwd  {len(rewritten)} 份 config.yaml（僅臨時樹）", flush=True)
        print(f"● 上傳嚴格度  enforce_handler_allowlist={strict_upload}"
              f"{'（--server local ⇒ 放寬）' if not strict_upload else ''}", flush=True)

        try:
            info = self_check(source, tree, strict_upload=strict_upload)
        except SelfCheckFailure as exc:
            print(f"\n✗ 自檢失敗，**不啟動**：\n  {exc}", file=sys.stderr)
            return 1

        for line in info["lines"]:
            print(f"  ✓ {line}")
        print(f"✅ 自檢通過（bundle {info['bytes']} bytes）")

        if args.check_only:
            print("（--check-only：不啟動 omnigent run）")
            return 0

        cmd = ["omnigent", "run", str(tree), *passthrough]
        print(f"\n▶ {' '.join(cmd)}\n")
        return subprocess.run(cmd, check=False).returncode
    finally:
        if args.keep_tree:
            print(f"\n（--keep-tree：臨時樹保留在 {tmp_root}）")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


def _server_values(argv: list[str]) -> set[str]:
    """抓出透傳參數裡 --server 的值（支援 `--server x` 與 `--server=x`）。"""
    vals: set[str] = set()
    for i, tok in enumerate(argv):
        if tok == "--server" and i + 1 < len(argv):
            vals.add(argv[i + 1])
        elif tok.startswith("--server="):
            vals.add(tok.split("=", 1)[1])
    return vals


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""woow-security-sweep 的收集階段：合併設定、決定掃描範圍、跑掃描工具、正規化 findings。

子命令::

    collect.py run --repo-path <abs> --run-root <dir> [--mode incremental|full] [--state-dir <dir>]
    collect.py commit --run-dir <dir> [--state-dir <dir>]   # 本輪處理完才推進 last_sha

run 的產出（<run-root>/<repo 名>/）：

    config.json     合併後設定（public .security/sweep.yaml ＋ private targets/<repo>.yaml）
    findings.jsonl  正規化 findings（每行一筆）
    tools.json      每個工具的狀態：ok／unavailable／skipped／error（缺工具要被看見，不靜默略過）
    meta.json       repo、head／last SHA、模式、變動檔案、.security/ 是否變動
    src/            預設分支 HEAD 的乾淨快照（git archive；不建 worktree、不碰工作目錄）

設計重點：

- 設定一律讀 **origin 預設分支**上的 `.security/sweep.yaml`（`git show`），PR 分支或工作目錄
  裡的修改不生效。repo 沒有這個檔 ⇒ 保守預設（不自動修、high 以上轉人工）。
- 掃描對象是 git archive 出來的快照，tar 以 `filter="data"` 解開，擋掉絕對路徑與指向
  快照外的連結（repo 內容不可信）。
- gitleaks 一律 `--redact`，findings 不帶原始秘密值。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import yaml

SWEEP_DIR = Path(__file__).resolve().parents[3]
TARGETS_DIR = SWEEP_DIR / "targets"
TOOL_TIMEOUT = 900
PRIVATE_KEYS = ("context", "review_focus", "history", "baseline", "disclosure")

#: repo 沒有 .security/sweep.yaml 時的保守預設
DEFAULT_PUBLIC: dict[str, Any] = {
    "version": 1,
    "scan": {
        "exclude": ["tests/", "i18n/", "**/__pycache__/"],
        "tools": {"semgrep": {"rulesets": ["p/python", "p/secrets"]}, "bandit": {"skip": ["B101"]},
                  "gitleaks": {}},
    },
    "always_human": {"paths": [".security/", "security/", "migrations/", "__manifest__.py"],
                     "severities": ["critical", "high"], "topics": []},
    "autofix": {"enabled": False, "max_prs_per_run": 0},
    "verify": {},
}

SEMGREP_SEVERITY = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
BANDIT_SEVERITY = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}


class CollectError(Exception):
    pass


# ── git ───────────────────────────────────────────────────────────────────────

def git(repo: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise CollectError(f"git {' '.join(args)} 失敗：{res.stderr.strip()[:300]}")
    return res.stdout.strip() if res.returncode == 0 else ""


def default_branch(repo: Path) -> str:
    ref = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    return ref.split("/", 1)[1] if ref.startswith("origin/") else "main"


def show_file(repo: Path, rev: str, path: str) -> str | None:
    res = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else None


def snapshot(repo: Path, rev: str, dest: Path) -> list[str]:
    """git archive → 解到 dest，回傳被擋下的成員。

    以 tarfile 的 data_filter 逐一檢查：絕對路徑、`..`、指向快照外的 symlink 一律不解，
    並回報出來（之後變成 high finding 交人工）——不讓單一惡意成員中斷整輪。
    """
    # 舊版 Python（< 3.11.4）沒有 data_filter：寧可中止也不在無檢查下解開
    if not hasattr(tarfile, "data_filter"):
        raise CollectError(f"Python {sys.version.split()[0]} 沒有 tarfile.data_filter，請改用 python3.12 執行")
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", rev],
                          capture_output=True, check=True).stdout
    dest.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []

    def _filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        try:
            return tarfile.data_filter(member, path)
        except tarfile.FilterError as exc:
            skipped.append(f"{member.name}（{type(exc).__name__}）")
            return None

    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter=_filter)
    return skipped


# ── 設定 ──────────────────────────────────────────────────────────────────────

def load_config(repo: Path, rev: str, targets_dir: Path = TARGETS_DIR) -> dict[str, Any]:
    raw = show_file(repo, rev, ".security/sweep.yaml")
    if raw is None:
        public, source = json.loads(json.dumps(DEFAULT_PUBLIC)), "default"
    else:
        public, source = yaml.safe_load(raw) or {}, f"{rev}:.security/sweep.yaml"
    name = repo.name
    private_path = targets_dir / f"{name}.yaml"
    private: dict[str, Any] = {}
    if private_path.exists():
        private = yaml.safe_load(private_path.read_text(encoding="utf-8")) or {}
    cfg = dict(public)
    for key in PRIVATE_KEYS:
        if key in private:
            cfg[key] = private[key]
    cfg.setdefault("repo", {})
    if not cfg["repo"].get("full_name"):
        url = git(repo, "remote", "get-url", "origin", check=False)
        cfg["repo"]["full_name"] = url.rstrip("/").removesuffix(".git").split(":")[-1].split("github.com/")[-1]
    cfg["_sources"] = {"public": source, "private": str(private_path) if private else None}
    return cfg


def is_excluded(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        pat = pat.removeprefix("**/")
        if pat.endswith("/"):
            if f"/{path}".find(f"/{pat}") >= 0:
                return True
        elif fnmatch.fnmatch(path, pat):
            return True
    return False


# ── 範圍 ──────────────────────────────────────────────────────────────────────

def load_state(state_dir: Path, name: str) -> dict[str, Any]:
    path = state_dir / "repos" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def scope(repo: Path, head: str, last: str | None, mode: str) -> dict[str, Any]:
    """決定掃描範圍。last 不存在、不是 head 的祖先、或 mode=full ⇒ 全量。"""
    if mode == "full" or not last:
        return {"mode": "full", "changed": None, "reason": "full" if mode == "full" else "no previous run"}
    is_ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", last, head],
                                 capture_output=True).returncode == 0
    if not is_ancestor:
        return {"mode": "full", "changed": None, "reason": f"last {last[:8]} 不是 head 的祖先（force-push？）"}
    changed = [p for p in git(repo, "diff", "--name-only", last, head).splitlines() if p]
    return {"mode": "incremental", "changed": changed, "reason": f"{last[:8]}..{head[:8]}"}


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _run_tool(cmd: list[str], cwd: Path, ok_codes: tuple[int, ...] = (0,)) -> tuple[int, str, str]:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TOOL_TIMEOUT)
    if res.returncode not in ok_codes:
        raise CollectError(f"exit {res.returncode}：{(res.stderr or res.stdout).strip()[-500:]}")
    return res.returncode, res.stdout, res.stderr


def excerpt(src: Path, path: str, line: int | None, radius: int = 3) -> str:
    try:
        lines = (src / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if not line:
        return ""
    lo, hi = max(0, line - 1 - radius), min(len(lines), line + radius)
    return "\n".join(f"{i + 1}: {lines[i]}" for i in range(lo, hi))


def line_text(src: Path, path: str, line: int | None) -> str:
    try:
        lines = (src / path).read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[line - 1].strip() if line and 0 < line <= len(lines) else ""
    except OSError:
        return ""


def make_finding(src: Path, *, tool: str, rule: str, path: str, line: int | None, severity: str,
                 message: str, with_snippet: bool = True) -> dict[str, Any]:
    """fingerprint 用「該行內容」而不是行號：上方插行不會讓同一筆 finding 變成新的。"""
    anchor = hashlib.sha1(line_text(src, path, line).encode("utf-8")).hexdigest()[:10]
    fp = f"{tool}:{rule}:{path}:{anchor}"
    return {
        "id": hashlib.sha1(fp.encode("utf-8")).hexdigest()[:12], "fingerprint": fp, "tool": tool,
        "rule": rule, "path": path, "line": line, "severity_tool": severity, "message": message,
        "snippet": excerpt(src, path, line) if with_snippet else "",
    }


def run_semgrep(src: Path, files: list[str], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rulesets = cfg.get("rulesets") or ["p/python"]
    cmd = ["uvx", "--from", "semgrep", "semgrep", "scan", "--json", "--quiet", "--metrics=off",
           "--disable-version-check"]
    for rs in rulesets:
        cmd += ["--config", rs]
    _, out, _ = _run_tool(cmd + files, src, ok_codes=(0, 1))
    results = json.loads(out).get("results", [])
    return [make_finding(src, tool="semgrep", rule=r["check_id"], path=r["path"],
                         line=r.get("start", {}).get("line"),
                         severity=SEMGREP_SEVERITY.get(r.get("extra", {}).get("severity", ""), "medium"),
                         message=r.get("extra", {}).get("message", ""))
            for r in results]


def run_bandit(src: Path, files: list[str], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    py = [f for f in files if f.endswith(".py")]
    if not py:
        return []
    cmd = ["uvx", "bandit", "-f", "json", "-q"]
    if cfg.get("skip"):
        cmd += ["-s", ",".join(cfg["skip"])]
    _, out, _ = _run_tool(cmd + py, src, ok_codes=(0, 1))
    return [make_finding(src, tool="bandit", rule=r["test_id"], path=r["filename"].removeprefix("./"),
                         line=r.get("line_number"), severity=BANDIT_SEVERITY.get(r.get("issue_severity"), "low"),
                         message=f"{r.get('issue_text', '')}（confidence {r.get('issue_confidence')}）")
            for r in json.loads(out).get("results", [])]


def run_gitleaks(src: Path, files: list[str], cfg: dict[str, Any], workdir: Path) -> list[dict[str, Any]]:
    report = workdir / "gitleaks.json"
    base = ["gitleaks", "--no-banner", "--redact", "--report-format", "json", "--report-path", str(report),
            "--exit-code", "0"]
    try:
        _run_tool([base[0], "dir", *base[1:], str(src)], src)
    except CollectError:
        # 舊版沒有 `dir` 子命令
        _run_tool([base[0], "detect", "--no-git", "--source", str(src), *base[1:]], src)
    wanted = set(files)
    out = []
    for r in json.loads(report.read_text(encoding="utf-8") or "[]"):
        path = os.path.relpath(r.get("File", ""), src)
        if path in wanted:
            out.append(make_finding(src, tool="gitleaks", rule=r.get("RuleID", "secret"), path=path,
                                    line=r.get("StartLine"), severity="high",
                                    message=r.get("Description", "possible secret"), with_snippet=False))
    return out


def collect_tools(src: Path, files: list[str], scan_cfg: dict[str, Any], workdir: Path
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tools_cfg = scan_cfg.get("tools") or {}
    findings: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    runners = {
        "semgrep": (lambda c: run_semgrep(src, files, c), "uvx"),
        "bandit": (lambda c: run_bandit(src, files, c), "uvx"),
        "gitleaks": (lambda c: run_gitleaks(src, files, c, workdir), "gitleaks"),
    }
    for name, tcfg in tools_cfg.items():
        tcfg = tcfg or {}
        if name == "pip_audit":
            unpinned = [p for p in tcfg.get("packages", []) if "==" not in p]
            status[name] = {"status": "skipped",
                            "detail": f"套件未釘版本（{unpinned}），無法得知部署版本；需要時改在 image 層掃"}
            continue
        if name not in runners:
            status[name] = {"status": "skipped", "detail": "collect.py 不支援這個工具"}
            continue
        fn, binary = runners[name]
        if not shutil.which(binary):
            status[name] = {"status": "unavailable", "detail": f"runner 上找不到 {binary}"}
            continue
        if not files:
            status[name] = {"status": "skipped", "detail": "範圍內沒有檔案"}
            continue
        t0 = time.monotonic()
        try:
            got = fn(tcfg)
            findings += got
            status[name] = {"status": "ok", "count": len(got), "seconds": round(time.monotonic() - t0, 1)}
        except (CollectError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            status[name] = {"status": "error", "detail": str(exc)[:500]}
    # 同一行被多個 ruleset 命中時以 fingerprint 去重
    uniq: dict[str, dict[str, Any]] = {}
    for f in findings:
        uniq.setdefault(f["fingerprint"], f)
    return list(uniq.values()), status


# ── 子命令 ────────────────────────────────────────────────────────────────────

def resolve_state_dir(explicit: str | None) -> Path:
    if explicit:
        base = Path(explicit)
    elif os.environ.get("SWEEP_STATE_DIR"):
        base = Path(os.environ["SWEEP_STATE_DIR"])
    elif os.environ.get("OMNIGENT_RUNNER_WORKSPACE"):
        base = Path(os.environ["OMNIGENT_RUNNER_WORKSPACE"]) / ".omnigent-runtime" / "woow-security-sweep"
    else:
        raise CollectError("找不到 state 目錄：請給 --state-dir，或設 SWEEP_STATE_DIR／OMNIGENT_RUNNER_WORKSPACE")
    base.mkdir(parents=True, exist_ok=True)
    return base


def cmd_run(repo: Path, run_root: Path, mode: str, state_dir: Path, *, fetch: bool = True,
            targets_dir: Path = TARGETS_DIR) -> dict[str, Any]:
    repo = repo.resolve()
    if not (repo / ".git").exists():
        raise CollectError(f"不是 git repo：{repo}")
    fetch_error = None
    if fetch:
        res = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"], capture_output=True, text=True)
        fetch_error = res.stderr.strip()[:300] if res.returncode else None
    branch = default_branch(repo)
    rev = f"origin/{branch}"
    head = git(repo, "rev-parse", rev)
    run_dir = run_root / repo.name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    cfg = load_config(repo, rev, targets_dir)
    last = load_state(state_dir, repo.name).get("last_sha")
    sc = scope(repo, head, last, mode)
    src = run_dir / "src"
    unsafe_members = snapshot(repo, head, src)

    excludes = (cfg.get("scan") or {}).get("exclude") or []
    if sc["changed"] is None:
        files = sorted(str(p.relative_to(src)) for p in src.rglob("*") if p.is_file())
    else:
        files = [p for p in sc["changed"] if (src / p).is_file()]
    files = [f for f in files if not is_excluded(f, excludes)]
    config_changed = any(p.startswith(".security/") for p in (sc["changed"] or []))

    if sc["mode"] == "incremental" and not sc["changed"]:
        findings, tools = [], {"_all": {"status": "skipped", "detail": "沒有新 commit"}}
    else:
        findings, tools = collect_tools(src, files, cfg.get("scan") or {}, run_dir)
    # 指向快照外的 symlink 等成員：fixer 透過它寫檔可能寫到 worktree 外，一律 high 交人工
    for member in unsafe_members:
        path = member.split("（", 1)[0]
        findings.append(make_finding(src, tool="collect", rule="unsafe-archive-member", path=path, line=None,
                                     severity="high", message=f"快照時被擋下的成員：{member}",
                                     with_snippet=False))
    # .security/ 變動（例如有人往 baseline 加豁免）一律交人工：jev_gate 的硬規則會擋
    if config_changed:
        findings.append(make_finding(src, tool="collect", rule="security-config-changed",
                                     path=".security/sweep.yaml", line=None, severity="high",
                                     message=f"{sc['reason']} 之間 .security/ 有變動，需人工確認",
                                     with_snippet=False))

    meta = {
        "repo": cfg["repo"]["full_name"], "repo_path": str(repo), "branch": branch, "head_sha": head,
        "last_sha": last, "scope": sc, "files_scanned": len(files), "scanned_files": files,
        "config_changed": config_changed,
        "unsafe_members": unsafe_members,
        "config_sources": cfg.pop("_sources"), "fetch_error": fetch_error, "findings": len(findings),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    (run_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2, default=str),
                                         encoding="utf-8")
    with open(run_dir / "findings.jsonl", "w", encoding="utf-8") as fh:
        for f in findings:
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")
    (run_dir / "tools.json").write_text(json.dumps(tools, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta | {"run_dir": str(run_dir), "tools": tools}


def cmd_commit(run_dir: Path, state_dir: Path) -> dict[str, Any]:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    name = Path(meta["repo_path"]).name
    path = state_dir / "repos" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"last_sha": meta["head_sha"], "repo": meta["repo"],
             "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


VALID_SEVERITIES = ("info", "low", "medium", "high", "critical")


def cmd_import(run_dir: Path, source: Path, tool: str = "review") -> dict[str, Any]:
    """把 deep review worker 回報的 finding（JSON lines）正規化後併入 findings.jsonl。

    每行至少要有 rule、path、message；line、severity 選填。path 必須在快照內——
    worker 回報的內容不可信，越界路徑與格式錯的行一律丟棄並回報數量。
    """
    src = (run_dir / "src").resolve()
    existing = {json.loads(x)["fingerprint"] for x in (run_dir / "findings.jsonl").read_text(
        encoding="utf-8").splitlines() if x.strip()}
    added, rejected = [], []
    for n, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            path = str(row["path"]).removeprefix("./")
            target = (src / path).resolve()
            if not target.is_relative_to(src) or not target.is_file():
                raise ValueError(f"path 不在快照內：{path}")
            sev = str(row.get("severity", "medium")).lower()
            f = make_finding(src, tool=tool, rule=str(row["rule"])[:120], path=path,
                             line=int(row["line"]) if row.get("line") else None,
                             severity=sev if sev in VALID_SEVERITIES else "medium",
                             message=str(row["message"])[:2000])
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append(f"第 {n} 行：{exc}")
            continue
        if f["fingerprint"] not in existing:
            existing.add(f["fingerprint"])
            added.append(f)
    with open(run_dir / "findings.jsonl", "a", encoding="utf-8") as fh:
        for f in added:
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")
    return {"added": len(added), "rejected": rejected}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--repo-path", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    p.add_argument("--no-fetch", action="store_true")
    p = sub.add_parser("commit")
    p.add_argument("--run-dir", required=True)
    p = sub.add_parser("import", help="併入 deep review worker 回報的 finding")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--input", required=True, help="JSON lines：rule、path、message，選填 line、severity")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "import":
            print(json.dumps(cmd_import(Path(args.run_dir), Path(args.input)), ensure_ascii=False, indent=2))
            return 0
        sdir = resolve_state_dir(args.state_dir)
        if args.cmd == "run":
            out = cmd_run(Path(args.repo_path), Path(args.run_root), args.mode, sdir, fetch=not args.no_fetch)
        else:
            out = cmd_commit(Path(args.run_dir), sdir)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except CollectError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

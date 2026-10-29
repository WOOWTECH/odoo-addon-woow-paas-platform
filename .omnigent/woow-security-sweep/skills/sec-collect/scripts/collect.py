#!/usr/bin/env python3
"""woow-security-sweep 的收集階段：合併設定、決定掃描範圍、跑掃描工具、正規化 findings。

子命令::

    collect.py run --repo-path <abs> --run-root <dir> [--mode incremental|full] [--units a,b] [--state-dir <dir>]
    collect.py commit --run-dir <dir> [--state-dir <dir>]   # 本輪處理完才推進 last_sha（與分塊輪審進度）
    collect.py preset --name <名稱> [--state-dir <dir>]    # 讀 <state>/presets/<名稱>.yaml，驗證後印出本輪輸入
    collect.py import --run-dir <dir> --input <jsonl> [--unit <塊>]   # 併入 deep review 回報（finding／hardening／coverage）
    collect.py verdict --run-dir <dir> --finding-id <id> --input <json>   # 套用驗證者的結論
    collect.py promote --run-dir <dir> --hardening-id <id>  # 人決定把某條 hardening 升級成 finding

run 的產出（<run-root>/<repo 名>/）：

    config.json     合併後設定（public .security/sweep.yaml ＋ private targets/<repo>.yaml）
    findings.jsonl  正規化 findings（每行一筆）
    tools.json      每個工具的狀態：ok／unavailable／skipped／error（缺工具要被看見，不靜默略過）
    meta.json       repo、head／last SHA、模式、變動與已刪除檔案、.security/ 是否變動、upstream 快照狀態
    src/            預設分支 HEAD 的乾淨快照（git archive；不建 worktree、不碰工作目錄）
    upstream/<名>/  context.upstream 列的上游 repo 快照（只給 review worker 讀，不跑掃描工具、不產生 finding）

meta.json 的 `review`：targets 有 review_units（大 repo 分塊輪審）時，本輪 deep review 審哪幾塊、各塊的檔案與
entry point（ast 抽出的 route）、沒歸到任何一塊的檔案與 route；沒有 review_units 時是 null（照舊審整個範圍）。

import 另外產生 hardening.jsonl（不構成 finding 的建議）與 coverage.jsonl（各 entry point 審過與否）；
verdict 判 rejected 的 finding 移到 rejected.jsonl，下一輪的工單會帶上它，避免重報同一個被推翻的說法。

設計重點：

- 設定一律讀 **origin 預設分支**上的 `.security/sweep.yaml`（`git show`），PR 分支或工作目錄
  裡的修改不生效。repo 沒有這個檔 ⇒ 保守預設（不自動修、high 以上轉人工）。
- 掃描對象是 git archive 出來的快照，tar 以 `filter="data"` 解開，擋掉絕對路徑與指向
  快照外的連結（repo 內容不可信）。
- gitleaks 一律 `--redact`，findings 不帶原始秘密值。
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import fnmatch
import hashlib
import io
import json
import os
import re
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
STACKS_DIR = SWEEP_DIR / "stacks"
PRIVATE_KEYS = ("context", "review_focus", "review_classes", "history", "baseline", "disclosure",
                "branch", "since_upstream", "origin", "review_units")
UPSTREAM_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
STACK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
#: since_upstream 只接受 https 的 git remote（擋 file://、ext::、本機路徑這類會讓 git 執行或讀本機的寫法）
REMOTE_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_./-]+?(?:\.git)?$")
FETCH_TIMEOUT = 600

#: repo 沒有 .security/sweep.yaml 時的保守預設；scan／always_human 由 stack profile 的 defaults 補上
DEFAULT_PUBLIC: dict[str, Any] = {
    "version": 1,
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


def _origin_slug(repo: Path) -> str:
    url = git(repo, "remote", "get-url", "origin", check=False).strip().rstrip("/").removesuffix(".git")
    return url.lower()


def locate_upstream(repo: Path, full_name: str) -> Path | None:
    """在目標 repo 的同層目錄找上游 clone：目錄名＝repo 名（不分大小寫），origin 指向同一個 owner/repo。

    只認 origin 對得上的 clone：同名但指向別處（fork、mirror 以外的 repo）不當成上游。
    Gitea mirror-internal 的鏡像與 GitHub 正本視為同一份（只比對 repo 名與 owner 尾段）。
    """
    name = full_name.partition("/")[2]
    want = full_name.lower()
    try:
        candidates = sorted(p for p in repo.parent.iterdir() if p.is_dir() and p.name.lower() == name.lower())
    except OSError:
        return None
    for cand in candidates:
        if cand.resolve() == repo.resolve() or not (cand / ".git").exists():
            continue
        slug = _origin_slug(cand)
        if slug.endswith("/" + want) or slug.endswith(":" + want) or slug.endswith(f"/mirror-internal/{name.lower()}"):
            return cand
    return None


def snapshot_upstreams(repo: Path, run_dir: Path, cfg: dict[str, Any], *, fetch: bool) -> list[dict[str, Any]]:
    """依 context.upstream 為每個上游 repo 做唯讀快照。找不到或失敗都記在回傳值，不中斷本輪。"""
    wanted = (cfg.get("context") or {}).get("upstream") or []
    out: list[dict[str, Any]] = []
    for full in wanted:
        entry: dict[str, Any] = {"repo": full, "status": "missing", "path": None, "head_sha": None, "detail": None}
        out.append(entry)
        if not isinstance(full, str) or not UPSTREAM_RE.match(full):
            entry.update(status="error", detail="格式要是 owner/repo")
            continue
        local = locate_upstream(repo, full)
        if local is None:
            entry["detail"] = f"在 {repo.parent} 找不到 origin 指向 {full} 的 clone"
            continue
        try:
            if fetch:
                subprocess.run(["git", "-C", str(local), "fetch", "--quiet", "origin"], capture_output=True, text=True)
            branch = default_branch(local)
            head = git(local, "rev-parse", f"origin/{branch}")
            dest = run_dir / "upstream" / local.name
            skipped = snapshot(local, head, dest)
        except (CollectError, subprocess.CalledProcessError, OSError) as exc:
            entry.update(status="error", detail=str(exc)[:300])
            continue
        entry.update(status="ok", path=str(dest.relative_to(run_dir)), head_sha=head, branch=branch,
                     local_path=str(local), unsafe_members=skipped)
    return out


# ── 設定 ──────────────────────────────────────────────────────────────────────

def parse_remote(url: str) -> tuple[str | None, str | None]:
    """git remote URL → (host, owner/name)。支援 https、`git@host:owner/name`、`ssh://git@host/owner/name`。"""
    url = (url or "").strip().rstrip("/").removesuffix(".git")
    m = (re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
         or re.match(r"^(?:[^@/]+@)?([^/:]+):(.+)$", url))
    if not m:
        return None, None
    host, path = m.group(1).lower(), m.group(2).strip("/")
    return (host, path) if UPSTREAM_RE.match(path) else (host, None)


def web_url(host: str | None, full_name: str) -> str:
    return f"https://{host or 'github.com'}/{full_name}"


def read_target(name: str, targets_dir: Path = TARGETS_DIR) -> tuple[dict[str, Any], Path | None]:
    path = targets_dir / f"{name}.yaml"
    if not path.exists():
        return {}, None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise CollectError(f"{path} 不是 YAML mapping")
    return data, path


def load_stack(name: str | None, stacks_dir: Path = STACKS_DIR) -> dict[str, Any]:
    """讀 stacks/<名>.yaml；沒指定用 generic。名稱不合法或檔案不存在 ⇒ 中止（設定錯不能靜默退回預設）。"""
    name = name or "generic"
    if not STACK_RE.match(str(name)):
        raise CollectError(f"stack 名稱不合法：{name!r}")
    path = stacks_dir / f"{name}.yaml"
    if not path.is_file():
        raise CollectError(f"找不到 stack profile：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if data.get("name") != name:
        raise CollectError(f"{path} 的 name 與檔名不符")
    return data


def target_branch(repo: Path, private: dict[str, Any]) -> str:
    branch = private.get("branch")
    if branch is None:
        return default_branch(repo)
    branch = str(branch)
    if not BRANCH_RE.match(branch) or ".." in branch:
        raise CollectError(f"targets 的 branch 不合法：{branch!r}")
    return branch


def load_config(repo: Path, rev: str, targets_dir: Path = TARGETS_DIR,
                stacks_dir: Path = STACKS_DIR) -> dict[str, Any]:
    name = repo.name
    private, private_path = read_target(name, targets_dir)
    profile = load_stack(private.get("stack"), stacks_dir)
    raw = show_file(repo, rev, ".security/sweep.yaml")
    if raw is None:
        public = json.loads(json.dumps(DEFAULT_PUBLIC)) | json.loads(json.dumps(profile.get("defaults") or {}))
        source = f"default（stack {profile['name']}）"
        # 只有在 repo 沒有自己的 .security/sweep.yaml 時，targets 才能追加排除（repo 有就以 repo 的為準）
        extra = [str(p) for p in (private.get("scan_exclude") or []) if str(p).strip()]
        if extra:
            scan = public.setdefault("scan", {})
            scan["exclude"] = list(scan.get("exclude") or []) + extra
            source += f"＋targets scan_exclude {len(extra)} 條"
    else:
        public, source = yaml.safe_load(raw) or {}, f"{rev}:.security/sweep.yaml"
    cfg = dict(public)
    for key in PRIVATE_KEYS:
        if key in private:
            cfg[key] = private[key]
    if not cfg.get("review_classes"):
        cfg["review_classes"] = list(profile.get("review_classes") or [])
    cfg["stack"] = {k: profile.get(k) for k in ("name", "version_files", "fix_verify")}
    cfg["repo"] = dict(cfg.get("repo") or {})
    host, full = parse_remote(git(repo, "remote", "get-url", "origin", check=False))
    if not cfg["repo"].get("full_name"):
        cfg["repo"]["full_name"] = full or name
    cfg["repo"].setdefault("host", host or "github.com")
    cfg["repo"]["web_url"] = web_url(cfg["repo"]["host"], cfg["repo"]["full_name"])
    cfg["_sources"] = {"public": source, "private": str(private_path) if private_path else None,
                       "stack": profile["name"]}
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


# ── fork：只掃相對上游改過的部分（targets 的 since_upstream） ──────────────────────

def fork_scope(repo: Path, head: str, conf: Any, *, fetch: bool) -> dict[str, Any]:
    """把上游 branch fetch 到 `refs/sweep/upstream/<branch>`，算出 fork 相對 merge-base 改過、仍存在的檔案。

    fetch 失敗時沿用本機已有的 ref（記在 fetch_error）；連一次都沒有成功過 ⇒ 中止這個 target，
    不退回全量掃描（全量會把整個上游當成我們的程式碼來審，人已決定只掃 WOOWTECH 的改動）。
    """
    if not isinstance(conf, dict):
        raise CollectError("since_upstream 要是 mapping（remote、branch）")
    remote, branch = str(conf.get("remote") or ""), str(conf.get("branch") or "main")
    if not REMOTE_RE.match(remote) or ".." in remote:
        raise CollectError(f"since_upstream.remote 只接受 https URL：{remote!r}")
    if not BRANCH_RE.match(branch) or ".." in branch:
        raise CollectError(f"since_upstream.branch 不合法：{branch!r}")
    ref = f"refs/sweep/upstream/{branch}"
    fetch_error = None
    if fetch:
        try:
            res = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "--no-tags", remote,
                                  f"+refs/heads/{branch}:{ref}"], capture_output=True, text=True,
                                 timeout=FETCH_TIMEOUT)
            fetch_error = res.stderr.strip()[:300] if res.returncode else None
        except subprocess.TimeoutExpired:
            fetch_error = f"fetch 超過 {FETCH_TIMEOUT} 秒"
    if not git(repo, "rev-parse", "--verify", "--quiet", ref, check=False):
        raise CollectError(f"上游 {remote} {branch} 從未 fetch 成功：{fetch_error or '本機沒有 ' + ref}")
    base = git(repo, "merge-base", head, ref)
    files = [p for p in git(repo, "diff", "--name-only", "--diff-filter=d", base, head).splitlines() if p]
    return {
        "remote": remote, "branch": branch, "ref": ref, "merge_base": base,
        "fork_point": git(repo, "log", "-1", "--format=%cI", base),
        "ahead": int(git(repo, "rev-list", "--count", f"{ref}..{head}") or 0),
        "behind": int(git(repo, "rev-list", "--count", f"{head}..{ref}") or 0),
        "files": files, "fetch_error": fetch_error,
    }


# ── 分塊輪審（targets 的 review_units；大 repo 一個 worker 讀不完） ──────────────────

UNIT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
UNITS_KEYS = frozenset({"per_run", "incremental_max_files", "cover", "units"})
UNIT_KEYS = frozenset({"name", "focus", "paths", "exclude_paths", "entry_points", "review_classes"})
UNIT_EP_KEYS = frozenset({"extract", "auth", "routes", "exclude_routes", "extra"})
#: entry point 抽取器：只 parse decorator（ast），不 import、不執行目標程式碼
EXTRACTORS = ("odoo-route", "fastapi-route")
FASTAPI_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "api_route", "websocket"})
DEFAULT_PER_RUN = 2
DEFAULT_INCREMENTAL_MAX = 20
#: 報告裡列出的清單上限（未歸塊的檔案、route 等），其餘只給數量
LIST_LIMIT = 50


def _str_list(value: Any, where: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value) \
            or (required and not value):
        raise CollectError(f"review_units：{where} 要是非空字串清單")
    return [x.strip() for x in value]


def _int(value: Any, where: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CollectError(f"review_units：{where} 要是正整數")
    return value


def validate_units(raw: Any) -> dict[str, Any] | None:
    """驗證 targets 的 review_units 並補預設值。格式錯一律中止（設定錯不能靜默退回全量）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - UNITS_KEYS:
        raise CollectError(f"review_units 只接受 {sorted(UNITS_KEYS)}")
    units_raw = raw.get("units")
    if not isinstance(units_raw, list) or not units_raw:
        raise CollectError("review_units.units 要是非空清單")
    units: list[dict[str, Any]] = []
    for i, u in enumerate(units_raw, 1):
        if not isinstance(u, dict) or set(u) - UNIT_KEYS:
            raise CollectError(f"review_units 第 {i} 塊只接受 {sorted(UNIT_KEYS)}")
        name = str(u.get("name") or "")
        if not UNIT_NAME_RE.match(name):
            raise CollectError(f"review_units 第 {i} 塊的 name 不合法：{name!r}（小寫英數與 -）")
        if any(x["name"] == name for x in units):
            raise CollectError(f"review_units 的 name 重複：{name}")
        ep = u.get("entry_points") or {}
        if not isinstance(ep, dict) or set(ep) - UNIT_EP_KEYS:
            raise CollectError(f"review_units {name}.entry_points 只接受 {sorted(UNIT_EP_KEYS)}")
        extract = ep.get("extract")
        if extract is not None and extract not in EXTRACTORS:
            raise CollectError(f"review_units {name}.entry_points.extract 只能是 {EXTRACTORS}")
        extra = ep.get("extra") or []
        if not isinstance(extra, list) or not all(isinstance(e, dict) and isinstance(e.get("route"), str)
                                                  and e["route"].strip() for e in extra):
            raise CollectError(f"review_units {name}.entry_points.extra 每項都要有 route")
        units.append({
            "name": name, "focus": str(u.get("focus") or "").strip(),
            "paths": _str_list(u.get("paths"), f"{name}.paths", required=True),
            "exclude_paths": _str_list(u.get("exclude_paths"), f"{name}.exclude_paths"),
            "entry_points": {"extract": extract, "auth": _str_list(ep.get("auth"), f"{name}.entry_points.auth"),
                             "routes": _str_list(ep.get("routes"), f"{name}.entry_points.routes"),
                             "exclude_routes": _str_list(ep.get("exclude_routes"),
                                                         f"{name}.entry_points.exclude_routes"),
                             "extra": [{k: e.get(k) for k in ("route", "file", "auth", "note") if e.get(k)}
                                       for e in extra]},
            "review_classes": _str_list(u.get("review_classes"), f"{name}.review_classes"),
        })
    return {"per_run": min(_int(raw.get("per_run"), "per_run", DEFAULT_PER_RUN), len(units)),
            "incremental_max_files": _int(raw.get("incremental_max_files"), "incremental_max_files",
                                          DEFAULT_INCREMENTAL_MAX),
            "cover": _str_list(raw.get("cover"), "cover"), "units": units}


def _glob_match(path: str, patterns: list[str]) -> bool:
    """fnmatch（`*` 也吃 `/`）；結尾 `/` 的 pattern 視為目錄前綴。"""
    for pat in patterns:
        if pat.endswith("/"):
            if path.startswith(pat):
                return True
        elif fnmatch.fnmatchcase(path, pat):
            return True
    return False


def _const(node: ast.AST | None) -> Any:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) for e in node.elts):
        return [e.value for e in node.elts]
    return f"<{ast.unparse(node)[:80]}>"  # 不是字面值（變數、運算式）：照原樣標示，不猜


def extract_routes(src: Path, files: list[str], kind: str) -> tuple[list[dict[str, Any]], list[str]]:
    """從 Python 原始碼的 decorator 抽出 route。

    - `odoo-route`：`@http.route(...)`／`@route(...)`；auth 省略時是 Odoo 預設的 user、type 預設 http。
      沒寫路徑的 `@route()` 是覆寫父類 controller：URL、auth、type 都沿用父類（常是 Odoo core 或上游 addon 的
      auth=none／public 入口），這個檔案看不出來，標成 `<繼承父類 route：函式名>`、auth／type 為 `inherited`
    - `fastapi-route`：`@<router>.get/post/…(path)`；router 的 prefix 不展開（只記 router 名）
    只用 ast.parse 讀結構，不 import、不執行目標程式碼。parse 失敗的檔案列在第二個回傳值。
    """
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for rel in files:
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse((src / rel).read_text(encoding="utf-8", errors="replace"), filename=rel)
        except (SyntaxError, ValueError, OSError) as exc:
            errors.append(f"{rel}：{type(exc).__name__}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                attr = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
                kw = {k.arg: k.value for k in dec.keywords if k.arg}
                first = dec.args[0] if dec.args else None
                if kind == "odoo-route":
                    if attr != "route":
                        continue
                    paths = _const(first if first is not None else kw.get("route"))
                    inherited = paths is None
                    if inherited:
                        paths = f"<繼承父類 route：{node.name}>"
                    default = "inherited" if inherited else None
                    info = {"auth": _const(kw.get("auth")) or default or "user",
                            "type": _const(kw.get("type")) or default or "http"}
                    if "csrf" in kw:
                        info["csrf"] = _const(kw["csrf"])
                    if "methods" in kw:
                        info["methods"] = _const(kw["methods"])
                else:
                    if not isinstance(fn, ast.Attribute) or attr not in FASTAPI_METHODS:
                        continue
                    paths = _const(first if first is not None else kw.get("path"))
                    info = {"methods": [attr.upper()], "router": ast.unparse(fn.value)[:40]}
                for p in (paths if isinstance(paths, list) else [paths]):
                    out.append({"route": str(p if p is not None else "<無路徑>"), "file": rel, "line": node.lineno,
                                "func": node.name, **info})
    return out, errors


def _route_filter(routes: list[dict[str, Any]], ep: dict[str, Any]) -> list[dict[str, Any]]:
    auth = set(ep.get("auth") or [])
    inc, exc = ep.get("routes") or [], ep.get("exclude_routes") or []
    return [r for r in routes
            if (not auth or str(r.get("auth")) in auth)
            and (not inc or _glob_match(r["route"], inc))
            and not (exc and _glob_match(r["route"], exc))]


def unit_entry_points(src: Path, files: list[str], unit: dict[str, Any]
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """回傳 (這塊的 entry point, 這些檔案裡抽到的全部 route, parse 錯誤)。"""
    ep = unit["entry_points"]
    found, errors = extract_routes(src, files, ep["extract"]) if ep.get("extract") else ([], [])
    return _route_filter(found, ep) + [dict(e) for e in ep.get("extra") or []], found, errors


def select_units(units: list[dict[str, Any]], history: dict[str, Any], per_run: int) -> list[str]:
    """最久沒審的塊優先（從沒審過的排最前面），同分依 targets 的順序（越前面風險越高）。"""
    order = sorted(range(len(units)),
                   key=lambda i: (str((history.get(units[i]["name"]) or {}).get("at") or ""), i))
    return [units[i]["name"] for i in order[:per_run]]


def _size(src: Path, files: list[str]) -> int:
    return sum((src / f).stat().st_size for f in files if (src / f).is_file())


def plan_review(src: Path, conf: dict[str, Any], all_files: list[str], sc: dict[str, Any],
                history: dict[str, Any], forced: list[str] | None) -> dict[str, Any]:
    """依 review_units 決定本輪 deep review 審什麼，結果寫進 meta.review（ticket.py 照它產工單）。

    - 全量：挑 per_run 塊（或 --units 指定），各給檔案清單與 entry point；同時對全部塊找漂移：
      `cover` 裡沒有歸到任何一塊的檔案、抽得到卻沒有任何一塊收的 route、對不到檔案的 paths
    - 增量：只審落在某一塊裡的變動檔案，依塊的順序最多取 incremental_max_files 個，其餘列為 overflow
    """
    units = conf["units"]
    by_name = {u["name"]: u for u in units}
    owned = {u["name"]: sorted(f for f in all_files if _glob_match(f, u["paths"])
                               and not _glob_match(f, u["exclude_paths"])) for u in units}
    schedule = [{"name": u["name"], "last_reviewed": history.get(u["name"])} for u in units]
    if sc.get("changed") is not None:
        present = set(all_files)
        changed = [f for f in sc["changed"] if f in present]
        picked: list[str] = []
        touched: list[str] = []
        for u in units:  # 依塊的順序排變動檔案；同一個檔案可以屬於多塊（例如同檔不同 route）
            mine_all = set(owned[u["name"]])
            hit = [f for f in changed if f in mine_all]
            if hit:
                touched.append(u["name"])
                picked += [f for f in hit if f not in picked]
        limit = conf["incremental_max_files"]
        files, overflow = picked[:limit], picked[limit:]
        eps: list[dict[str, Any]] = []
        errors: list[str] = []
        reviewed: list[str] = []
        for name in touched:
            mine = [f for f in files if f in set(owned[name])]
            if not mine:
                continue  # 這塊的變動檔案都落在 overflow
            reviewed.append(name)
            got, _, err = unit_entry_points(src, mine, by_name[name])
            eps += [e for e in got if e not in eps and (not e.get("file") or e["file"] in mine)]
            errors += err
        done = set(picked)
        outside = [f for f in changed if f not in done]
        return {"mode": "changed", "files": files, "overflow": overflow, "max_files": limit,
                "units": reviewed, "touched_units": touched, "entry_points": eps,
                "outside_units": outside[:LIST_LIMIT], "outside_count": len(outside),
                "extract_errors": errors, "schedule": schedule}
    if forced:
        unknown = [n for n in forced if n not in by_name]
        if unknown:
            raise CollectError(f"--units 有不存在的塊：{unknown}（可用：{list(by_name)}）")
        chosen = list(dict.fromkeys(forced))
    else:
        chosen = select_units(units, history, conf["per_run"])
    out_units: list[dict[str, Any]] = []
    selected_routes: set[tuple[str, str, int]] = set()
    all_routes: dict[tuple[str, str, int], dict[str, Any]] = {}
    errors = []
    for idx, u in enumerate(units, 1):
        files = owned[u["name"]]
        eps, found, err = unit_entry_points(src, files, u)
        errors += [e for e in err if e not in errors]
        for r in found:
            all_routes.setdefault((r["route"], r["file"], r["line"]), r)
        selected_routes |= {(e["route"], e.get("file") or "", e.get("line") or 0) for e in eps}
        if u["name"] not in chosen:
            continue
        # 對不到快照裡任何檔案的 pattern（設定過期）；被 exclude_paths 扣掉的不算
        missing = [p for p in u["paths"] if not any(_glob_match(f, [p]) for f in all_files)]
        out_units.append({"name": u["name"], "order": idx, "focus": u["focus"], "files": files,
                          "bytes": _size(src, files), "entry_points": eps, "missing_paths": missing,
                          "review_classes": u["review_classes"], "last_reviewed": history.get(u["name"])})
    out_units.sort(key=lambda x: chosen.index(x["name"]))
    in_units = {f for fs in owned.values() for f in fs}
    unassigned = [f for f in all_files if conf["cover"] and _glob_match(f, conf["cover"]) and f not in in_units]
    stray = [r for k, r in sorted(all_routes.items()) if k not in selected_routes]
    nxt = [n for n in select_units(units, history | {n: {"at": "9999"} for n in chosen}, conf["per_run"])]
    return {"mode": "units", "per_run": conf["per_run"], "total": len(units), "forced": bool(forced),
            "units": out_units, "next_due": nxt, "schedule": schedule,
            "unassigned_files": unassigned[:LIST_LIMIT], "unassigned_count": len(unassigned),
            "unassigned_routes": [{k: r.get(k) for k in ("route", "file", "line", "auth")} for r in stray][:LIST_LIMIT],
            "unassigned_route_count": len(stray), "extract_errors": errors}


# ── 上游 advisory（since_upstream.advisories） ─────────────────────────────────

_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "preview": 3, "rc": 3, "c": 3}
_CONSTRAINT_RE = re.compile(r"^\s*(<=|>=|==|=|<|>)?\s*v?([0-9][0-9A-Za-z.+-]*)\s*$")


def version_key(raw: str) -> tuple[int, ...] | None:
    """PEP 440／semver 的簡化排序鍵：數字段＋預發行（dev < a < b < rc < 正式）。認不出來回 None。"""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)(?:[.-]?(dev|alpha|beta|preview|pre|rc|a|b|c)\.?(\d*))?", raw or "", re.I)
    if not m:
        return None
    nums = [int(x) for x in m.group(1).split(".")][:6]
    nums += [0] * (6 - len(nums))
    tag = (m.group(2) or "").lower()
    pre = (_PRE_RANK[tag], int(m.group(3) or 0)) if tag else (9, 0)
    return (*nums, *pre)


def in_range(version: str, spec: str) -> bool | None:
    """GitHub advisory 的 vulnerable_version_range（例：`>= 1.0, < 2.0`、`<= 8.7.0`）。無法判斷回 None。"""
    v = version_key(version)
    if v is None or not (spec or "").strip():
        return None
    for part in spec.split(","):
        m = _CONSTRAINT_RE.match(part)
        bound = version_key(m.group(2)) if m else None
        if bound is None:
            return None
        op = m.group(1) or "="
        ok = {"<": v < bound, "<=": v <= bound, ">": v > bound, ">=": v >= bound,
              "=": v == bound, "==": v == bound}[op]
        if not ok:
            return False
    return True


def read_version(src: Path, conf: dict[str, Any]) -> tuple[str | None, str | None, int | None]:
    """從快照讀 fork 的版號：回傳 (版號, 檔案, 行號)。"""
    path = str(conf.get("version_file") or "")
    pattern = str(conf.get("version_regex") or r'^\s*version\s*=\s*"([^"]+)"')
    target = (src / path).resolve()
    if not path or not target.is_relative_to(src.resolve()) or not target.is_file():
        return None, path or None, None
    rx = re.compile(pattern)
    for n, line in enumerate(target.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = rx.search(line)
        if m:
            return m.group(1), path, n
    return None, path, None


def fetch_advisories(full_name: str) -> list[dict[str, Any]]:
    """`gh api` 只讀上游 repo 已公開的 security advisories（GET；不帶任何寫入參數）。"""
    if not UPSTREAM_RE.match(full_name):
        raise CollectError(f"advisories.github 要是 owner/repo：{full_name!r}")
    _, out, _ = _run_tool(["gh", "api", "--paginate",
                           f"repos/{full_name}/security-advisories?state=published&per_page=100"], Path.cwd())
    data = json.loads(out or "[]")
    return data if isinstance(data, list) else []


def advisory_findings(src: Path, conf: dict[str, Any], advisories: list[dict[str, Any]]
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """上游已公告、而 fork 目前版號落在受影響範圍（或無法判斷）的 advisory ⇒ finding。

    fingerprint 固定為 `advisory:<GHSA id>`：fork 升版但仍受影響時是同一筆，升到修正版就不再偵測到、由 carryover 結案。
    """
    version, path, line = read_version(src, conf)
    package = conf.get("package")
    out: list[dict[str, Any]] = []
    checked = 0
    for adv in advisories:
        ghsa = str(adv.get("ghsa_id") or "")
        if not re.fullmatch(r"GHSA(-[0-9a-z]{4}){3}", ghsa):
            continue
        for vuln in adv.get("vulnerabilities") or []:
            pkg = (vuln.get("package") or {}).get("name")
            if package and pkg != package:
                continue
            checked += 1
            spec = str(vuln.get("vulnerable_version_range") or "")
            hit = in_range(version or "", spec)
            if hit is False:
                continue
            sev = str(adv.get("severity") or "medium").lower()
            sev = sev if sev in VALID_SEVERITIES else "medium"
            state = "受影響" if hit else "無法比對版本，請人確認"
            msg = (f"上游 {conf.get('github')} 已公告 {ghsa}（{sev}）：{_text(adv.get('summary'), 300)}。"
                   f"受影響範圍 `{spec or '?'}`，修正版 `{vuln.get('patched_versions') or '?'}`；"
                   f"fork 目前 {package or ''} {version or '（讀不到版號）'} ⇒ {state}。")
            f = make_finding(src, tool="advisory", rule=ghsa, path=path or "?", line=line, severity=sev, message=msg,
                             with_snippet=bool(path and line))
            fp = f"advisory:{ghsa}"
            f.update(id=hashlib.sha1(fp.encode("utf-8")).hexdigest()[:12], fingerprint=fp,
                     advisory={"ghsa_id": ghsa, "published_at": adv.get("published_at"), "range": spec,
                               "patched": vuln.get("patched_versions"), "package": pkg, "fork_version": version,
                               "affected": hit, "url": adv.get("html_url")})
            out.append(f)
            break  # 同一個 advisory 只算一筆
    status = {"status": "ok", "count": len(out), "checked": checked, "version": version, "version_file": path}
    if version is None:
        status["detail"] = f"讀不到 fork 版號（{path or '未設定 version_file'}）"
    return out, status


def collect_advisories(src: Path, conf: Any) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """回傳 (findings, tools.json 狀態, 這個工具掃過的路徑)。"""
    if not isinstance(conf, dict) or not conf.get("github"):
        return [], {"status": "skipped", "detail": "since_upstream 沒有設定 advisories"}, []
    if not shutil.which("gh"):
        return [], {"status": "unavailable", "detail": "runner 上找不到 gh"}, []
    try:
        found, status = advisory_findings(src, conf, fetch_advisories(str(conf["github"])))
    except (CollectError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        return [], {"status": "error", "detail": str(exc)[:500]}, []
    return found, status, [status["version_file"]] if status.get("version_file") else []


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


# ── 本輪輸入（preset） ──────────────────────────────────────────────────────────
# 排程 prompt 只放一行 `preset: <名稱>`：Omnigent 0.16.0 會把長 prompt 收合成 [Pasted text]
# 後偵測不到草稿、只盲按一次 Enter，訊息就停在輸入框不送出（#1989）。

PRESET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
INPUT_KEYS = ("targets", "mode", "deep_review", "roster")
INPUT_MODES = ("incremental", "full")
DEEP_REVIEW = ("auto", "always", "never")
ROSTER_HARNESSES = ("claude-native", "pi")


def validate_input(data: Any, workspace: Path) -> dict[str, Any]:
    """驗證本輪輸入並補上預設值。targets 必須是 workspace 底下既有的 git repo。"""
    if not isinstance(data, dict):
        raise CollectError("輸入必須是 YAML mapping")
    unknown = sorted(set(data) - set(INPUT_KEYS))
    if unknown:
        raise CollectError(f"不認得的欄位：{unknown}（只接受 {list(INPUT_KEYS)}）")
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets or not all(isinstance(x, str) for x in targets):
        raise CollectError("targets 必須是非空的 repo 絕對路徑清單")
    ws = workspace.resolve()
    out: list[str] = []
    for raw in targets:
        path = Path(raw)
        if not path.is_absolute():
            raise CollectError(f"target 必須是絕對路徑：{raw}")
        real = path.resolve()
        if not real.is_relative_to(ws):
            raise CollectError(f"target 不在 workspace {ws} 底下：{raw}")
        if not (real / ".git").exists():
            raise CollectError(f"target 不是 git repo：{raw}")
        if str(real) not in out:
            out.append(str(real))
    mode = data.get("mode", "incremental")
    if mode not in INPUT_MODES:
        raise CollectError(f"mode 只能是 {INPUT_MODES}")
    deep = data.get("deep_review", "auto")
    if deep not in DEEP_REVIEW:
        raise CollectError(f"deep_review 只能是 {DEEP_REVIEW}")
    roster = data.get("roster") or {}
    if not isinstance(roster, dict) or set(roster) - {"review", "fix"} \
            or any(v not in ROSTER_HARNESSES for v in roster.values()):
        raise CollectError(f"roster 只接受 review／fix，值為 {ROSTER_HARNESSES}")
    return {"targets": out, "mode": mode, "deep_review": deep, "roster": roster}


def preset_path(state_dir: Path, name: str) -> Path:
    if not PRESET_NAME_RE.match(name or ""):
        raise CollectError(f"preset 名稱不合法：{name!r}（小寫英數與 -，最多 32 字）")
    return state_dir / "presets" / f"{name}.yaml"


def cmd_preset(name: str, state_dir: Path) -> dict[str, Any]:
    """讀 <state>/presets/<名稱>.yaml 並驗證。workspace 取 state 目錄往上兩層（<WS>/.omnigent-runtime/…）。"""
    path = preset_path(state_dir, name)
    if not path.is_file():
        raise CollectError(f"找不到 preset 檔：{path}（用 setup skill 的 preset 子命令建立）")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CollectError(f"preset 檔不是合法 YAML：{path}") from exc
    workspace = state_dir.resolve().parents[1]
    return {"preset": name, "path": str(path), **validate_input(data, workspace)}


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
            targets_dir: Path = TARGETS_DIR, stacks_dir: Path = STACKS_DIR,
            units: list[str] | None = None) -> dict[str, Any]:
    repo = repo.resolve()
    if not (repo / ".git").exists():
        raise CollectError(f"不是 git repo：{repo}")
    if units and mode != "full":
        raise CollectError("--units 只能搭配 --mode full（指定本輪要審的塊）")
    fetch_error = None
    if fetch:
        res = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"], capture_output=True, text=True)
        fetch_error = res.stderr.strip()[:300] if res.returncode else None
    private, _ = read_target(repo.name, targets_dir)
    branch = target_branch(repo, private)
    rev = f"origin/{branch}"
    head = git(repo, "rev-parse", "--verify", rev, check=False)
    if not head:
        raise CollectError(f"origin 沒有 branch {branch}（targets 的 branch 寫錯，或還沒 fetch）")
    run_dir = run_root / repo.name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    cfg = load_config(repo, rev, targets_dir, stacks_dir)
    units_conf = validate_units(cfg.get("review_units"))
    if units and not units_conf:
        raise CollectError(f"targets/{repo.name}.yaml 沒有 review_units，不能用 --units")
    if units_conf:
        cfg["review_units"] = units_conf
    state = load_state(state_dir, repo.name)
    last = state.get("last_sha")
    sc = scope(repo, head, last, mode)
    src = run_dir / "src"
    unsafe_members = snapshot(repo, head, src)

    upstreams = snapshot_upstreams(repo, run_dir, cfg, fetch=fetch)
    fork = fork_scope(repo, head, cfg["since_upstream"], fetch=fetch) if cfg.get("since_upstream") else None

    excludes = (cfg.get("scan") or {}).get("exclude") or []
    if sc["changed"] is None:
        files = sorted(str(p.relative_to(src)) for p in src.rglob("*") if p.is_file())
        deleted: list[str] = []
    else:
        files = [p for p in sc["changed"] if (src / p).is_file()]
        # 增量範圍裡已不在 HEAD 的檔案：工單要分開列，不能叫 worker 去審不存在的檔（10-27 實例）
        deleted = [p for p in sc["changed"] if not (src / p).exists()]
    files = [f for f in files if not is_excluded(f, excludes)]
    if fork is not None:
        # 只掃 fork 相對上游改過的檔案（人決定只掃 WOOWTECH 的改動，上游程式碼只當上下文）
        owned = set(fork["files"])
        files = [f for f in files if f in owned]
    config_changed = any(p.startswith(".security/") for p in (sc["changed"] or []))
    review = None
    if units_conf:
        if sc["changed"] is None:
            universe = files
        else:
            universe = sorted(f for f in (str(p.relative_to(src)) for p in src.rglob("*") if p.is_file())
                              if not is_excluded(f, excludes))
            if fork is not None:
                fork_files = set(fork["files"])
                universe = [f for f in universe if f in fork_files]
        review = plan_review(src, units_conf, universe, sc, state.get("units") or {}, units)

    if sc["mode"] == "incremental" and not sc["changed"]:
        findings, tools = [], {"_all": {"status": "skipped", "detail": "沒有新 commit"}}
    else:
        findings, tools = collect_tools(src, files, cfg.get("scan") or {}, run_dir)
    tool_scanned: dict[str, list[str]] = {}
    adv_conf = (cfg.get("since_upstream") or {}).get("advisories") if fork is not None else None
    if adv_conf:
        # 上游公告與我們有沒有新 commit 無關，每輪都查
        adv, tools["advisory"], tool_scanned["advisory"] = collect_advisories(src, adv_conf)
        findings += adv
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
        "deleted_files": deleted, "upstream": upstreams, "fork": fork, "tool_scanned": tool_scanned,
        "stack": (cfg.get("stack") or {}).get("name"), "review": review,
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
    """推進 last_sha；分塊輪審的輪次另外記下哪幾塊審完（有併入回報的才算，worker 沒回報的下輪再排）。"""
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    name = Path(meta["repo_path"]).name
    path = state_dir / "repos" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    state = load_state(state_dir, name) | {"last_sha": meta["head_sha"], "repo": meta["repo"], "updated_at": now}
    review = meta.get("review") or {}
    done: list[str] = []
    if review.get("mode") == "units":
        planned = {u["name"] for u in review.get("units") or []}
        imported = {r.get("unit") for r in read_jsonl(run_dir / "review_imports.jsonl") if r.get("accepted")}
        done = sorted(planned & imported)
        history = dict(state.get("units") or {})
        for unit in done:
            history[unit] = {"at": now, "head_sha": meta["head_sha"], "run": run_dir.parent.name}
        state["units"] = history
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state | {"units_reviewed": done} if review.get("mode") == "units" else state


VALID_SEVERITIES = ("info", "low", "medium", "high", "critical")
#: 根因 fingerprint：不含行號、嚴重度、輪次（同一根因在每輪、每個狀態都用同一個）
FP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")
VERDICTS = ("confirmed", "needs_validation")
TRACE_KINDS = ("entrypoint", "propagation", "sink")
COVERAGE_STATUS = ("reviewed", "not_reviewed", "blocked")
PRIOR_STATUS = ("fixed", "not_checked")
MAX_TEXT = 2000


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _text_list(value: Any, limit: int = 20) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("要是字串清單")
    return [_text(v, 1000) for v in value[:limit] if _text(v)]


def snapshot_file(run_dir: Path, raw: Any, *, allow_upstream: bool) -> str:
    """驗證 worker 回報的路徑：目標快照內（相對 src/），或 `upstream/<名>/…`（只有 trace 可以）。"""
    path = str(raw or "").strip().removeprefix("./")
    if not path:
        raise ValueError("缺少路徑")
    if path.startswith("upstream/"):
        if not allow_upstream:
            raise ValueError(f"finding 的主要位置必須在目標 repo，不能在上游：{path}")
        base = (run_dir / "upstream").resolve()
        target = (run_dir / path).resolve()
    else:
        base = (run_dir / "src").resolve()
        target = (base / path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise ValueError(f"路徑不在快照內：{path}")
    return path


def _line(value: Any) -> int | None:
    if value in (None, ""):
        return None
    n = int(value)
    if n <= 0:
        raise ValueError(f"行號要是正整數：{value}")
    return n


def normalize_trace(run_dir: Path, raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """trace 至少一步；多步時第一步是 entrypoint、最後一步是 sink。中間誤標的 sink 改成 propagation 並記警告。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("trace 要是非空清單")
    warnings: list[str] = []
    steps = []
    for i, step in enumerate(raw[:30]):
        if not isinstance(step, dict):
            raise ValueError(f"trace[{i}] 不是物件")
        kind = str(step.get("kind") or "")
        if kind not in TRACE_KINDS:
            raise ValueError(f"trace[{i}].kind 只能是 {TRACE_KINDS}")
        steps.append({"kind": kind, "file": snapshot_file(run_dir, step.get("file"), allow_upstream=True),
                      "line": _line(step.get("line")), "description": _text(step.get("description"), 1000)})
    if len(steps) > 1:
        if steps[0]["kind"] != "entrypoint" or steps[-1]["kind"] != "sink":
            raise ValueError("多步 trace 要以 entrypoint 開頭、sink 結尾")
        for i, s in enumerate(steps[1:-1], 1):
            if s["kind"] != "propagation":
                warnings.append(f"trace[{i}] 的 {s['kind']} 改標 propagation（只有最後一步能是 sink）")
                s["kind"] = "propagation"
    return steps, warnings


def compose_message(rec: dict[str, Any]) -> str:
    """給 Jev、報告、issue 用的一段文字（結構化欄位另存在 finding 裡）。"""
    lines = [rec["title"], f"根因：{rec['root_cause']}"]
    lines.append("路徑：" + " → ".join(f"{s['file']}:{s['line'] or '?'}" for s in rec["trace"]))
    if rec.get("conditions"):
        lines.append("條件：" + "；".join(rec["conditions"]))
    if rec.get("impact"):
        lines.append(f"影響：{rec['impact']}")
    if rec["verdict"] == "confirmed":
        lines.append(f"修法：{rec.get('remediation') or '-'}")
    else:
        lines.append("待確認：" + "；".join(rec.get("blockers") or []))
    return "\n".join(lines)[:MAX_TEXT]


def normalize_record(run_dir: Path, row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """驗證新格式的 finding（confirmed／needs_validation），回傳正規化後的結構化欄位與警告。

    sweep 不執行目標程式碼，所以這裡的 confirmed＝原始碼上路徑完整、條件都看得到、沒有阻擋層
    （source-confirmed），不是 Cloudflare 原義的「本機實測」。
    """
    verdict = str(row.get("verdict") or "")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 只能是 {VERDICTS}")
    title = _text(row.get("title"), 200)
    root = _text(row.get("root_cause") or row.get("claimed_root_cause"))
    if not title or not root:
        raise ValueError("缺少 title 或 root_cause")
    trace, warnings = normalize_trace(run_dir, row.get("trace"))
    rec: dict[str, Any] = {
        "verdict": verdict, "title": title, "root_cause": root, "trace": trace,
        "conditions": _text_list(row.get("conditions")), "impact": _text(row.get("impact"), 1000),
    }
    if verdict == "confirmed":
        sev = str(row.get("severity") or "").lower()
        if sev not in VALID_SEVERITIES:
            raise ValueError(f"confirmed 要有 severity（{VALID_SEVERITIES}）")
        rec["severity"] = sev
        rec["remediation"] = _text(row.get("remediation"))
        if not rec["remediation"]:
            raise ValueError("confirmed 要有 remediation")
    else:
        blockers = _text_list(row.get("blockers"))
        plan = row.get("validation_plan") or {}
        if not isinstance(plan, dict):
            raise ValueError("validation_plan 要是物件")
        plan = {k: _text(plan.get(k)) for k in ("local", "deployment") if _text(plan.get(k))}
        if not blockers or not plan:
            raise ValueError("needs_validation 要有 blockers 與 validation_plan（local 或 deployment）")
        rec["blockers"], rec["validation_plan"] = blockers, plan
        if row.get("severity"):
            warnings.append("needs_validation 不給嚴重度，已移除 severity")
    return rec, warnings


def prior_fingerprints(run_dir: Path) -> set[str]:
    """ticket.py 寫給 worker 的「之前的 finding」清單；worker 沿用其中的 fingerprint 時原樣採用。"""
    path = run_dir / "prior_findings.json"
    if not path.exists():
        return set()
    return {str(p.get("fingerprint")) for p in json.loads(path.read_text(encoding="utf-8")) if p.get("fingerprint")}


def resolve_fingerprint(raw: Any, tool: str, prior: set[str]) -> str:
    fp = str(raw or "").strip()
    if fp in prior:
        return fp  # 沿用前幾輪的 fingerprint（含舊制 review:<rule>:<path>:<hash>）
    fp = fp.removeprefix(f"{tool}:")
    if not FP_RE.match(fp):
        raise ValueError(f"fingerprint 格式不合（{FP_RE.pattern}）：{fp!r}")
    return f"{tool}:{fp}"


def build_finding(run_dir: Path, row: dict[str, Any], tool: str, prior: set[str]
                  ) -> tuple[dict[str, Any], list[str]]:
    src = (run_dir / "src").resolve()
    rec, warnings = normalize_record(run_dir, row)
    fp = resolve_fingerprint(row.get("fingerprint"), tool, prior)
    path = snapshot_file(run_dir, row.get("path") or rec["trace"][-1]["file"], allow_upstream=False)
    line = _line(row.get("line"))
    finding = {
        "id": hashlib.sha1(fp.encode("utf-8")).hexdigest()[:12], "fingerprint": fp, "tool": tool,
        "rule": fp.removeprefix(f"{tool}:")[:120], "path": path, "line": line,
        "severity_tool": rec.get("severity"), "message": compose_message(rec),
        "snippet": excerpt(src, path, line), **rec,
    }
    return finding, warnings


def _import_unit(run_dir: Path, unit: str | None) -> str | None:
    """分塊輪審的回報一定要帶 --unit（commit 依它記下哪塊審完）；不是分塊輪次就不能帶。"""
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    review = meta.get("review") or {}
    if review.get("mode") != "units":
        if unit:
            raise CollectError("本輪不是分塊輪審（meta.review.mode 不是 units），不用帶 --unit")
        return None
    names = [u["name"] for u in review.get("units") or []]
    if unit not in names:
        raise CollectError(f"分塊輪審的回報要帶 --unit，值是本輪的塊之一：{names}")
    return unit


def cmd_import(run_dir: Path, source: Path, tool: str = "review", unit: str | None = None) -> dict[str, Any]:
    """把 deep review worker 回報（JSON lines）正規化後併入本輪。

    每行依 `type` 分流：
    - `finding`（或沒寫 type 但有 verdict）：新格式，見 normalize_record；fingerprint 用根因
    - `hardening`：不構成 finding 的建議 → hardening.jsonl（人可以 promote）
    - `coverage`：entry point 審過與否 → coverage.jsonl
    - `prior`：之前的 finding 本輪狀態（fixed／not_checked）→ prior_status.jsonl，只供報告
    - 舊格式（rule、path、message；選填 line、severity）照收，fingerprint 仍用行內容

    worker 回報的內容不可信：路徑必須在快照內，格式錯的行一律丟棄並列在 rejected。
    分塊輪審（meta.review.mode=units）一定要帶 unit：finding／hardening／coverage 都標上塊名，
    並在 review_imports.jsonl 記一筆，`commit` 才會把這塊記為審完。
    """
    unit = _import_unit(run_dir, unit)
    src = (run_dir / "src").resolve()
    existing = {f["fingerprint"] for f in read_jsonl(run_dir / "findings.jsonl")}
    prior = prior_fingerprints(run_dir)
    added: list[dict[str, Any]] = []
    extra: dict[str, list[dict[str, Any]]] = {"hardening": [], "coverage": [], "prior": []}
    rejected, warnings = [], []
    for n, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError("不是 JSON 物件")
            kind = row.get("type") or ("finding" if "verdict" in row else "legacy")
            if kind == "finding":
                f, warn = build_finding(run_dir, row, tool, prior)
                warnings += [f"第 {n} 行：{w}" for w in warn]
            elif kind == "legacy":
                path = snapshot_file(run_dir, row["path"], allow_upstream=False)
                sev = str(row.get("severity", "medium")).lower()
                f = make_finding(src, tool=tool, rule=str(row["rule"])[:120], path=path, line=_line(row.get("line")),
                                 severity=sev if sev in VALID_SEVERITIES else "medium",
                                 message=_text(row["message"]))
            elif kind == "hardening":
                path = snapshot_file(run_dir, row["path"], allow_upstream=False) if row.get("path") else None
                note = _text(row.get("note"), 1000)
                if not note:
                    raise ValueError("hardening 缺少 note")
                hid = hashlib.sha1(f"{path}:{note}".encode("utf-8")).hexdigest()[:12]
                extra["hardening"].append({"id": hid, "path": path, "line": _line(row.get("line")),
                                           "note": note, "source": tool, **({"unit": unit} if unit else {})})
                continue
            elif kind == "coverage":
                status = str(row.get("status") or "")
                if status not in COVERAGE_STATUS or not _text(row.get("entry")):
                    raise ValueError(f"coverage 要有 entry 與 status（{COVERAGE_STATUS}）")
                extra["coverage"].append({"entry": _text(row["entry"], 300), "status": status,
                                          "note": _text(row.get("note"), 500), **({"unit": unit} if unit else {})})
                continue
            elif kind == "prior":
                status = str(row.get("status") or "")
                if status not in PRIOR_STATUS or str(row.get("fingerprint")) not in prior:
                    raise ValueError(f"prior 要帶工單列過的 fingerprint 與 status（{PRIOR_STATUS}）")
                extra["prior"].append({"fingerprint": row["fingerprint"], "status": status,
                                       "note": _text(row.get("note"), 500)})
                continue
            else:
                raise ValueError(f"不認得的 type：{kind!r}")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            rejected.append(f"第 {n} 行：{exc}")
            continue
        if unit:
            f["unit"] = unit
        if f["fingerprint"] not in existing:
            existing.add(f["fingerprint"])
            added.append(f)
        else:
            warnings.append(f"第 {n} 行：fingerprint 重複，略過（{f['fingerprint']}）")
    with open(run_dir / "findings.jsonl", "a", encoding="utf-8") as fh:
        for f in added:
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")
    for name, rows in (("hardening.jsonl", extra["hardening"]), ("coverage.jsonl", extra["coverage"]),
                       ("prior_status.jsonl", extra["prior"])):
        if rows:
            seen = {r.get("id") or json.dumps(r, sort_keys=True) for r in read_jsonl(run_dir / name)}
            new = [r for r in rows if (r.get("id") or json.dumps(r, sort_keys=True)) not in seen]
            with open(run_dir / name, "a", encoding="utf-8") as fh:
                for r in new:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    out = {"added": len(added), "hardening": len(extra["hardening"]), "coverage": len(extra["coverage"]),
           "prior": len(extra["prior"]), "rejected": rejected, "warnings": warnings}
    if unit:
        accepted = len(added) + len(extra["hardening"]) + len(extra["coverage"]) + len(extra["prior"])
        with open(run_dir / "review_imports.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"unit": unit, "accepted": accepted, "rejected": len(rejected),
                                 "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")},
                                ensure_ascii=False) + "\n")
        out["unit"] = unit
    return out


def cmd_verdict(run_dir: Path, finding_id: str, source: Path) -> dict[str, Any]:
    """套用驗證者的結論：`{"decision": confirmed|needs_validation|rejected, "record": {...}}`。

    - rejected ⇒ 從 findings.jsonl 移到 rejected.jsonl（帶 reason），不進閘門 A
    - 其餘 ⇒ 以驗證者修正後的 record 取代結構化欄位，id／fingerprint 不變，標 verified
    - 驗證者改了 fingerprint（不同根因）⇒ 不套用，回報給人
    必須在 jev_gate triage 之前執行。
    """
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"decision", "record"}:
        raise CollectError("驗證結果要是恰好含 decision 與 record 的 JSON 物件")
    decision, rec = data["decision"], data["record"]
    if decision not in (*VERDICTS, "rejected") or not isinstance(rec, dict):
        raise CollectError(f"decision 只能是 {(*VERDICTS, 'rejected')}")
    findings = read_jsonl(run_dir / "findings.jsonl")
    idx = next((i for i, f in enumerate(findings) if f.get("id") == finding_id), None)
    if idx is None:
        raise CollectError(f"本輪 findings.jsonl 沒有 id={finding_id}")
    f = findings[idx]
    fp_in = str(rec.get("fingerprint") or "")
    if fp_in and fp_in not in (f["fingerprint"], f["fingerprint"].removeprefix(f"{f['tool']}:")):
        raise CollectError(f"驗證者把 fingerprint 改成 {fp_in}（原 {f['fingerprint']}）：視為不同根因，請人判斷")
    stamp = {"decision": decision, "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if decision == "rejected":
        reason = _text(rec.get("reason"))
        if not reason:
            raise CollectError("rejected 要有 reason")
        findings.pop(idx)
        write_jsonl(run_dir / "findings.jsonl", findings)
        with open(run_dir / "rejected.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"finding": f, "reason": reason, "verified": stamp}, ensure_ascii=False) + "\n")
        return {"finding_id": finding_id, "decision": decision, "moved_to": "rejected.jsonl"}
    try:
        norm, warnings = normalize_record(run_dir, rec | {"verdict": decision})
    except (ValueError, KeyError, TypeError) as exc:
        raise CollectError(f"驗證者的 record 格式不合：{exc}") from exc
    for key in ("severity", "remediation", "blockers", "validation_plan"):
        f.pop(key, None)
    f.update(norm)
    f["severity_tool"] = norm.get("severity")
    f["message"] = compose_message(norm)
    f["verified"] = stamp
    findings[idx] = f
    write_jsonl(run_dir / "findings.jsonl", findings)
    return {"finding_id": finding_id, "decision": decision, "warnings": warnings}


def cmd_promote(run_dir: Path, hardening_id: str) -> dict[str, Any]:
    """人在 Inbox 決定把某條 hardening 升級成 finding：併入 findings.jsonl，之後跑 `jev_gate triage --only`。"""
    path = run_dir / "hardening.jsonl"
    rows = read_jsonl(path)
    item = next((r for r in rows if r.get("id") == hardening_id), None)
    if item is None:
        raise CollectError(f"本輪 hardening.jsonl 沒有 id={hardening_id}")
    if item.get("promoted_to"):
        return {"hardening_id": hardening_id, "finding_id": item["promoted_to"], "already": True}
    src = (run_dir / "src").resolve()
    tool = item.get("source") or "review"
    fp = f"{tool}:hardening/{hardening_id}"
    finding = {
        "id": hashlib.sha1(fp.encode("utf-8")).hexdigest()[:12], "fingerprint": fp, "tool": tool,
        "rule": f"hardening/{hardening_id}", "path": item.get("path"), "line": item.get("line"),
        "severity_tool": None, "message": item["note"],
        "snippet": excerpt(src, item["path"], item.get("line")) if item.get("path") else "",
        "verdict": "promoted_hardening", "title": item["note"][:200],
    }
    with open(run_dir / "findings.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(finding, ensure_ascii=False) + "\n")
    item["promoted_to"] = finding["id"]
    write_jsonl(path, rows)
    return {"hardening_id": hardening_id, "finding_id": finding["id"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--repo-path", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--units", help="分塊輪審：本輪指定要審的塊（逗號分隔，只能搭配 --mode full；手動試跑用）")
    p = sub.add_parser("commit")
    p.add_argument("--run-dir", required=True)
    p = sub.add_parser("import", help="併入 deep review worker 回報（finding／hardening／coverage／prior）")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--input", required=True, help="JSON lines；格式見 skills/sec-dispatch/SKILL.md")
    p.add_argument("--unit", help="分塊輪審：這份回報屬於哪一塊（ticket review 輸出的 unit）")
    p = sub.add_parser("verdict", help="套用驗證者的結論（triage 之前）")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--finding-id", required=True)
    p.add_argument("--input", required=True, help='JSON：{"decision": ..., "record": {...}}')
    p = sub.add_parser("promote", help="把某條 hardening 升級成 finding")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--hardening-id", required=True)
    p = sub.add_parser("preset", help="讀 <state>/presets/<名稱>.yaml，驗證後印出本輪輸入")
    p.add_argument("--name", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd in ("import", "verdict", "promote"):
            run_dir = Path(args.run_dir)
            if args.cmd == "import":
                out = cmd_import(run_dir, Path(args.input), unit=args.unit)
            elif args.cmd == "verdict":
                out = cmd_verdict(run_dir, args.finding_id, Path(args.input))
            else:
                out = cmd_promote(run_dir, args.hardening_id)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        sdir = resolve_state_dir(args.state_dir)
        if args.cmd == "preset":
            out = cmd_preset(args.name, sdir)
        elif args.cmd == "run":
            units = [u.strip() for u in (args.units or "").split(",") if u.strip()] or None
            out = cmd_run(Path(args.repo_path), Path(args.run_root), args.mode, sdir, fetch=not args.no_fetch,
                          units=units)
        else:
            out = cmd_commit(Path(args.run_dir), sdir)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except CollectError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

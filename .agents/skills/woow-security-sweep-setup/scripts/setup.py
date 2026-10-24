#!/usr/bin/env python3
"""woow-security-sweep 的安裝與更新（#1978）。

子命令（`--workspace` 預設 `$OMNIGENT_RUNNER_WORKSPACE`）::

    setup.py doctor [--targets <repo 路徑> ...] [--check-api]   # 環境與權限檢查
    setup.py record-agent --session <session id>                # 註冊或接手共用 agent 後記下 anchor
    setup.py bundle-plan                                        # 打包本機 checkout，與 server 上的版本比較
    setup.py bundle-apply --plan <plan 目錄>                    # 依核准的計畫原地更新（PUT）

設計原則：

- 「能不能用」一律由檢查結果決定，不問人。規則來自 Omnigent server 的程式碼：
  排程只能建在呼叫者自己的主機（別人的 host 回 403）、session-scoped agent 對 anchor
  session 有 READ 權限即可使用、原地更新需要 anchor session 的 OWNER。
- agent 紀錄存在 `<state>/agent.json`（每台主機各自一份，不進 repo）。
- Jev key 只用 stat 檢查存在與權限，**不讀內容**；連線測試交給 jev_gate.py（它自己讀 key）。
- Omnigent 認證沿用 omnigent CLI 的 `_remote_headers`（private API）；header 不印、不寫檔。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[4]
BUNDLE_REL = ".omnigent/woow-security-sweep"
AGENT_NAME = "woow_security_sweep"
DEFAULT_TARGETS = ("WOOW/woow_line_base", "WOOW/Woow_odoo_line_liff", "WOOW/woow_odoo_livechat_line")
ISSUE_OWNER = "mirror-internal"
#: omnigent/server/auth.py：READ=1、EDIT=2、MANAGE=3、OWNER=4。PUT bundle 需要 OWNER。
LEVEL_READ, LEVEL_OWNER = 1, 4
SITE_PACKAGES_CANDIDATES = (
    "/home/elmo/.local/share/uv/tools/omnigent/lib/python3.12/site-packages",  # 59mqj（uv tool）
    "/opt/omnigent/venv/lib/python3.12/site-packages",                        # pi3（omnigent-host kit）
)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


class SetupError(Exception):
    """設定或前置條件錯誤；訊息不含 token。"""


# ── 環境 ──────────────────────────────────────────────────────────────────────

def workspace(explicit: str | None) -> Path:
    ws = explicit or os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
    if not ws:
        raise SetupError("找不到 workspace：請給 --workspace，或在 Omnigent session 內執行")
    return Path(ws).resolve()


def state_dir(ws: Path) -> Path:
    return ws / ".omnigent-runtime" / "woow-security-sweep"


def omnigent_config() -> dict[str, Any]:
    """讀 omnigent 的 host 設定（server 網址、host_id）。只取這兩個欄位。"""
    import yaml

    home = Path(os.environ.get("OMNIGENT_CONFIG_HOME") or Path.home() / ".omnigent")
    data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    return {"server": data.get("server"), "host_id": (data.get("host") or {}).get("host_id"),
            "host_name": (data.get("host") or {}).get("name")}


def load_agent_record(sdir: Path) -> dict[str, Any] | None:
    path = sdir / "agent.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_agent_record(sdir: Path, record: dict[str, Any]) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "agent.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── Omnigent API ─────────────────────────────────────────────────────────────

class Api:
    """最小的 Omnigent REST client。測試以假物件替換。"""

    def __init__(self, server: str, host_id: str | None):
        sp = os.environ.get("OMNIGENT_SITE_PACKAGES") or next(
            (p for p in SITE_PACKAGES_CANDIDATES if os.path.isdir(p)), None)
        if sp and sp not in sys.path:
            sys.path.insert(0, sp)
        try:
            import httpx
            from omnigent.chat import _remote_headers
        except ImportError as exc:
            raise SetupError(f"載入 omnigent 失敗（{exc.name}）：設 OMNIGENT_SITE_PACKAGES") from exc
        self._client = httpx.Client(base_url=server, timeout=60,
                                    headers=_remote_headers(server_url=server, host_id=host_id))

    def request(self, method: str, path: str, **kw: Any) -> tuple[int, Any]:
        resp = self._client.request(method, path, **kw)
        ctype = resp.headers.get("content-type", "")
        body = resp.json() if "json" in ctype else resp.content
        return resp.status_code, body

    def get(self, path: str) -> tuple[int, Any]:
        return self.request("GET", path)

    def put_bundle(self, path: str, data: bytes) -> tuple[int, Any]:
        return self.request("PUT", path, files={"bundle": ("bundle.tar.gz", data, "application/gzip")})


# ── 檢查 ──────────────────────────────────────────────────────────────────────

def check(cid: str, status: str, detail: str, fix: str | None = None) -> dict[str, Any]:
    return {"id": cid, "status": status, "detail": detail, **({"fix": fix} if fix else {})}


def run(cmd: list[str], *, timeout: int = 120, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def check_python() -> dict[str, Any]:
    if not shutil.which("python3.12"):
        return check("python", FAIL, "找不到 python3.12", "安裝 python3.12（collect.py 需要 tarfile.data_filter）")
    # 不加 -I：sweep 的腳本以 `python3.12 <script>` 執行，PyYAML 可能裝在使用者 site-packages。
    # 改在空的暫存目錄執行，避免 cwd 裡的同名模組被 import。
    with tempfile.TemporaryDirectory() as tmp:
        res = run(["python3.12", "-c", "import tarfile, yaml; assert hasattr(tarfile, 'data_filter')"], cwd=Path(tmp))
    if res.returncode:
        return check("python", FAIL, "python3.12 缺 PyYAML 或 tarfile.data_filter",
                     "python3.12 -m pip install --user --break-system-packages PyYAML==6.0.2")
    return check("python", OK, "python3.12＋PyYAML＋tarfile.data_filter")


def check_tools() -> list[dict[str, Any]]:
    out = []
    gl = shutil.which("gitleaks")
    if not gl:
        out.append(check("gitleaks", FAIL, "找不到 gitleaks", "安裝 gitleaks 8.30.1（對過官方 checksums）到 ~/.local/bin"))
    else:
        ver = run(["gitleaks", "version"]).stdout.strip()
        out.append(check("gitleaks", OK if ver else WARN, f"gitleaks {ver or '版本不明'}"))
    out.append(check("uvx", OK, "uvx 可用（semgrep／bandit 由它拉）") if shutil.which("uvx")
               else check("uvx", FAIL, "找不到 uvx", "安裝 uv（semgrep／bandit 由 uvx 執行）"))
    out.append(check("tea", OK, "tea 可用") if shutil.which("tea")
               else check("tea", FAIL, "找不到 tea", "安裝 tea 並 tea login（開 issue 用）"))
    return out


def check_jev_key(ws: Path, *, call_api: bool) -> list[dict[str, Any]]:
    key = Path(os.environ.get("TYPESAFE_API_KEY_FILE") or Path.home() / ".config/typesafe/api_key")
    try:
        st = key.stat()
    except FileNotFoundError:
        return [check("jev_key", FAIL, f"找不到 {key}",
                      "把 Jev key 放到 ~/.config/typesafe/api_key 並 chmod 600（檔案對檔案複製，不要貼進對話）")]
    mode = stat.S_IMODE(st.st_mode)
    out = [check("jev_key", OK if mode & 0o077 == 0 and st.st_size > 0 else FAIL,
                 f"{key}：權限 {mode:o}、{st.st_size} bytes（未讀內容）",
                 None if mode & 0o077 == 0 and st.st_size > 0 else f"chmod 600 {key}")]
    if call_api:
        jg = ws / "paas/woow_paas_platform" / BUNDLE_REL / "skills/sec-gate/scripts/jev_gate.py"
        res = run(["python3.12", str(jg), "--state-dir", str(state_dir(ws)), "models"], timeout=90)
        out.append(check("jev_api", OK if res.returncode == 0 else FAIL,
                         "jev_gate.py models 成功" if res.returncode == 0 else f"jev_gate.py models exit {res.returncode}",
                         None if res.returncode == 0 else "確認 key 有效、主機連得到 api.typesafe.ai"))
    return out


def check_targets(ws: Path, targets: list[Path], tea_login: str) -> list[dict[str, Any]]:
    out = []
    for repo in targets:
        name = repo.name
        if not (repo / ".git").exists():
            out.append(check(f"target:{name}", FAIL, f"{repo} 不是 git repo", f"git clone https://github.com/WOOWTECH/{name}.git {repo}"))
            continue
        url = run(["git", "-C", str(repo), "remote", "get-url", "origin"]).stdout.strip()
        good = re.search(rf"github\.com[:/]WOOWTECH/{re.escape(name)}(\.git)?$", url) is not None
        out.append(check(f"target:{name}", OK if good else WARN, f"origin {url or '（無）'}",
                         None if good else "origin 應指向 GitHub WOOWTECH 正本"))
        res = run(["tea", "api", "-i", "--login", tea_login, f"repos/{ISSUE_OWNER}/{name}"], timeout=60)
        m = re.search(r"^HTTP/\S+ (\d{3})", res.stderr, re.M)
        code = int(m.group(1)) if m else 0
        push = False
        if code == 200:
            try:
                push = bool((json.loads(res.stdout).get("permissions") or {}).get("push"))
            except json.JSONDecodeError:
                pass
        out.append(check(f"issues:{name}", OK if push else FAIL,
                         f"tea（{tea_login}）對 {ISSUE_OWNER}/{name}：HTTP {code or '?'}、push={push}",
                         None if push else f"tea login 一個對 {ISSUE_OWNER}/{name} 有寫入權限的帳號"))
    return out


def check_identity_and_host(api: Any, host_id: str | None) -> tuple[list[dict[str, Any]], str | None]:
    code, me = api.get("/v1/me")
    if code != 200:
        return [check("identity", FAIL, f"/v1/me HTTP {code}", "omnigent login")], None
    user = me.get("user_id")
    out = [check("identity", OK, f"Omnigent 使用者 {user}")]
    if not host_id:
        out.append(check("host", FAIL, "config.yaml 沒有 host.host_id", "在這台主機執行 omnigent host 註冊"))
        return out, user
    code, host = api.get(f"/v1/hosts/{host_id}")
    owner = host.get("owner") if code == 200 else None
    if code == 403 or (owner and owner != user):
        out.append(check("host", FAIL, f"主機 {host_id} 不屬於 {user}",
                         "排程只能建在自己的主機（server 回 403）：改在自己的主機執行 setup"))
    elif code != 200:
        out.append(check("host", FAIL, f"/v1/hosts/{host_id} HTTP {code}"))
    else:
        out.append(check("host", OK if host.get("status") == "online" else WARN,
                         f"主機 {host.get('name')}（{host_id}）屬於 {user}，狀態 {host.get('status')}"))
    return out, user


def config_path_for_session(ws: Path) -> str:
    """sys_session_create 的 config_path 相對於「呼叫它的 session」的 workspace，不一定是 <WS>。"""
    bundle = ws / "paas/woow_paas_platform" / BUNDLE_REL
    base = Path(os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or ws).resolve()
    try:
        return str(bundle.relative_to(base))
    except ValueError:
        return str(bundle)


def check_agent(api: Any, sdir: Path, user: str | None, ws: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    record = load_agent_record(sdir)
    cfg = config_path_for_session(ws)
    if not record:
        return [check("agent", FAIL, "這台主機還沒有 sweep agent 紀錄",
                      f"有人共用給你就 record-agent --session <anchor>；否則 sys_session_create(config_path=\"{cfg}\") "
                      "後 record-agent --session <回傳的 conversation_id>")], None
    code, agent = api.get(f"/v1/sessions/{record['anchor_session']}/agent")
    if code != 200:
        return [check("agent", FAIL, f"讀不到 anchor {record['anchor_session']} 的 agent（HTTP {code}）",
                      "共用者沒分享 anchor session 給你，或 session 已刪：請對方分享，或自己重新註冊")], record
    if agent.get("id") != record.get("agent_id") or agent.get("name") != AGENT_NAME:
        return [check("agent", FAIL, f"anchor 上的 agent 是 {agent.get('name')}／{agent.get('id')}，與紀錄不符",
                      "重新 record-agent")], record
    _, conv = api.get(f"/v1/sessions/{record['anchor_session']}")
    level = (conv or {}).get("permission_level", 0) if isinstance(conv, dict) else 0
    owner = level >= LEVEL_OWNER
    return [check("agent", OK, f"sweep agent {agent['id']}（v{agent.get('version')}，harness {agent.get('harness')}）"),
            check("agent_owner", OK if owner else WARN,
                  "你是 anchor 擁有者，可以發新版（bundle-plan／bundle-apply）" if owner
                  else f"共用 {record.get('recorded_by')} 註冊的 agent（權限等級 {level}）：可使用、不能更新（PUT 需要 OWNER）")], record


def check_schedules(api: Any, record: dict[str, Any] | None, host_id: str | None, ws: Path) -> list[dict[str, Any]]:
    code, body = api.get("/v1/scheduled-tasks")
    if code != 200:
        return [check("schedules", WARN, f"讀不到排程（HTTP {code}）")]
    rows = (body.get("scheduled_tasks") or body.get("data")) if isinstance(body, dict) else body
    tasks = [t for t in rows or [] if re.search(r"woow[-_]security[-_]sweep", str(t.get("name", ""))) or
             (record and t.get("agent_id") == record.get("agent_id"))]
    if not tasks:
        return [check("schedules", SKIP, "沒有 woow-security-sweep 排程")]
    out = []
    claimed: dict[str, list[str]] = {}
    for t in tasks:
        problems = []
        if record and t.get("agent_id") != record.get("agent_id"):
            problems.append(f"agent_id 是舊的 {t.get('agent_id')}")
        if t.get("host_id") != host_id:
            problems.append(f"host 是 {t.get('host_id')}，不是本機")
        if t.get("workspace") not in (None, str(ws)):
            problems.append(f"workspace 是 {t.get('workspace')}")
        if t.get("permission_mode") != "auto":
            problems.append(f"permission_mode 是 {t.get('permission_mode')}")
        if not t.get("max_cost_usd"):
            problems.append("沒有費用上限（create 不會存 max_cost_usd，要再 update）")
        days = re.search(r"BYDAY=([A-Z,]+)", t.get("rrule") or "")
        for day in (days.group(1).split(",") if days else ["*"]):
            for tgt in re.findall(r"^\s*-\s*(\S+)\s*$", t.get("prompt") or "", re.M):
                claimed.setdefault(f"{day}|{tgt}", []).append(t.get("name", t.get("id")))
        out.append(check(f"schedule:{t.get('id')}", FAIL if problems else OK,
                         f"{t.get('name')}（{t.get('state')}）" + (f"：{'；'.join(problems)}" if problems else ""),
                         "sys_scheduled_task_update 修正上述欄位（換 agent 要一併重送 permission_mode 與 max_cost_usd）"
                         if problems else None))
    overlap = sorted({f"{k.split('|')[1]}（{k.split('|')[0]}）：{'、'.join(v)}" for k, v in claimed.items() if len(v) > 1})
    if overlap:
        out.append(check("schedule_overlap", FAIL, "同一天有多個排程掃同一個 repo：" + "；".join(overlap),
                         "錯開日期（例如每週全量那天不跑每日），避免兩輪同時推進同一份 state"))
    return out


def doctor(ws: Path, targets: list[Path], *, call_api: bool, api_factory: Callable[[str, str | None], Any] = Api,
           tea_login: str = "git-prod") -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    bundle = ws / "paas/woow_paas_platform" / BUNDLE_REL
    checks.append(check("workspace", OK if bundle.is_dir() else FAIL, f"workspace {ws}",
                        None if bundle.is_dir() else f"把 woow_paas_platform clone 到 {ws}/paas/woow_paas_platform"))
    sdir = state_dir(ws)
    checks.append(check("state", OK if sdir.is_dir() else WARN,
                        f"state {sdir}" + ("" if sdir.is_dir() else "（不存在：第一次跑會從頭掃，人工決定不會帶過來）")))
    checks.append(check_python())
    checks += check_tools()
    checks += check_jev_key(ws, call_api=call_api)
    checks += check_targets(ws, targets, tea_login)
    conf = omnigent_config()
    if not conf.get("server"):
        checks.append(check("server", FAIL, "config.yaml 沒有 server", "omnigent host <server url>"))
    else:
        api = api_factory(conf["server"], conf.get("host_id"))
        part, user = check_identity_and_host(api, conf.get("host_id"))
        checks += part
        part, record = check_agent(api, sdir, user, ws)
        checks += part
        checks += check_schedules(api, record, conf.get("host_id"), ws)
    fails = [c["id"] for c in checks if c["status"] == FAIL]
    return {"workspace": str(ws), "host_id": conf.get("host_id"), "host_name": conf.get("host_name"),
            "ready": not fails, "failed": fails, "checks": checks}


# ── agent 紀錄 ────────────────────────────────────────────────────────────────

def record_agent(api: Any, ws: Path, session: str, user: str | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", session):
        raise SetupError("session id 格式不對（32 位 hex）")
    code, agent = api.get(f"/v1/sessions/{session}/agent")
    if code != 200:
        raise SetupError(f"讀不到 session {session} 的 agent（HTTP {code}）：沒有 READ 權限或 session 不存在")
    if agent.get("name") != AGENT_NAME:
        raise SetupError(f"session {session} 的 agent 是 {agent.get('name')!r}，不是 {AGENT_NAME}")
    if user is None:
        _, me = api.get("/v1/me")
        user = me.get("user_id")
    _, conv = api.get(f"/v1/sessions/{session}")
    level = (conv or {}).get("permission_level", 0) if isinstance(conv, dict) else 0
    record = {"anchor_session": session, "agent_id": agent["id"], "name": AGENT_NAME,
              "version": agent.get("version"), "harness": agent.get("harness"),
              "can_update": level >= LEVEL_OWNER, "recorded_by": user,
              "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    save_agent_record(state_dir(ws), record)
    return record


# ── bundle 打包與比對 ─────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    res = run(["git", "-C", str(repo), *args])
    if res.returncode:
        raise SetupError(f"git {' '.join(args)} 失敗：{res.stderr.strip()[:200]}")
    return res.stdout.strip()


def source_snapshot(repo: Path, dest: Path, integration: str = "develop") -> str:
    """確認 checkout 乾淨且等於已合併的 origin/<integration>，再用 git archive 取 tracked 檔。"""
    _git(repo, "fetch", "-q", "origin", integration)
    head, merged = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", f"origin/{integration}")
    if head != merged:
        raise SetupError(f"checkout 在 {head[:8]}，不是 origin/{integration}（{merged[:8]}）：先 merge --ff-only")
    if _git(repo, "status", "--porcelain", "--", ".omnigent"):
        raise SetupError(".omnigent 有未 commit 的變更：只能發已合併的版本")
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", head, ".omnigent"],
                          capture_output=True, check=True).stdout
    old = os.umask(0o022)  # 執行位元要和 git 的 100755／100644 一致
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")
    finally:
        os.umask(old)
    res = run(["python3.12", str(dest / BUNDLE_REL / "build.py"), "--check"], timeout=300, cwd=dest)
    if res.returncode or "❌" in res.stdout:
        raise SetupError("build.py --check 沒通過：回原 PR 修正，不在部署時重產")
    return head


def pack(bundle_dir: Path) -> bytes:
    """沿用 omnigent CLI 的打包（private API，版本變動時要重新核對）。"""
    from omnigent.cli import _bundle

    return _bundle(bundle_dir)


def manifest(tar_bytes: bytes) -> dict[str, str]:
    """路徑 → sha256＋執行位元。用來比對兩份 bundle 的實際內容（不比 tar 本身的 hash）。"""
    out: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            data = tar.extractfile(m).read()
            out[m.name.removeprefix("./")] = f"{hashlib.sha256(data).hexdigest()}:{'x' if m.mode & 0o111 else '-'}"
    return out


def diff_manifest(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {"added": sorted(after.keys() - before.keys()), "removed": sorted(before.keys() - after.keys()),
            "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}


def bundle_plan(api: Any, ws: Path, record: dict[str, Any], *, packer: Callable = pack,
                snapshot: Callable = source_snapshot) -> dict[str, Any]:
    _, conv = api.get(f"/v1/sessions/{record['anchor_session']}")
    if not isinstance(conv, dict) or conv.get("permission_level", 0) < LEVEL_OWNER:
        raise SetupError("你不是 anchor session 的擁有者：只有擁有者能更新（PUT 需要 OWNER）")
    plans = state_dir(ws) / "setup"
    plans.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="plan-", dir=plans))
    os.chmod(out, 0o700)
    src = out / "src"
    sha = snapshot(ws / "paas/woow_paas_platform", src)
    candidate = packer(src / BUNDLE_REL)
    code, stored = api.get(f"/v1/sessions/{record['anchor_session']}/agent/contents")
    if code != 200:
        raise SetupError(f"讀不到 server 上的 bundle（HTTP {code}）")
    code, meta = api.get(f"/v1/sessions/{record['anchor_session']}/agent")
    diff = diff_manifest(manifest(stored), manifest(candidate))
    (out / "candidate.tar.gz").write_bytes(candidate)
    plan = {"source_sha": sha, "anchor_session": record["anchor_session"], "agent_id": record["agent_id"],
            "before_version": meta.get("version"), "before_sha256": hashlib.sha256(stored).hexdigest(),
            "candidate_sha256": hashlib.sha256(candidate).hexdigest(), "diff": diff,
            "noop": not any(diff.values()), "plan_dir": str(out)}
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(src)
    plan["plan_digest"] = hashlib.sha256((out / "plan.json").read_bytes()).hexdigest()
    return plan


def bundle_apply(api: Any, ws: Path, plan_dir: Path, digest: str) -> dict[str, Any]:
    raw = (plan_dir / "plan.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise SetupError("plan digest 不符：計畫在核准後被改過，重新 bundle-plan")
    plan = json.loads(raw)
    if plan["noop"]:
        return {"result": "NO-OP", "agent_id": plan["agent_id"]}
    candidate = (plan_dir / "candidate.tar.gz").read_bytes()
    if hashlib.sha256(candidate).hexdigest() != plan["candidate_sha256"]:
        raise SetupError("candidate.tar.gz 與計畫不符")
    anchor = plan["anchor_session"]
    code, stored = api.get(f"/v1/sessions/{anchor}/agent/contents")
    if code != 200 or hashlib.sha256(stored).hexdigest() != plan["before_sha256"]:
        raise SetupError("server 上的 bundle 在計畫之後變了：重新 bundle-plan")
    code, body = api.put_bundle(f"/v1/sessions/{anchor}/agent", candidate)
    if code != 200:
        raise SetupError(f"PUT 失敗（HTTP {code}）：先 GET 對帳，⛔ 不要直接重送")
    code, meta = api.get(f"/v1/sessions/{anchor}/agent")
    _, after = api.get(f"/v1/sessions/{anchor}/agent/contents")
    same_content = manifest(after) == manifest(candidate) if isinstance(after, bytes) else False
    if meta.get("id") != plan["agent_id"] or not same_content:
        raise SetupError(f"PUT 後驗證失敗：agent_id {meta.get('id')}、內容一致 {same_content}")
    record = load_agent_record(state_dir(ws)) or {}
    record.update({"version": meta.get("version"), "source_sha": plan["source_sha"],
                   "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    save_agent_record(state_dir(ws), record)
    return {"result": "DEPLOYED", "agent_id": meta["id"], "version": meta.get("version"),
            "source_sha": plan["source_sha"]}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _api() -> tuple[Any, str]:
    conf = omnigent_config()
    if not conf.get("server"):
        raise SetupError("config.yaml 沒有 server")
    api = Api(conf["server"], conf.get("host_id"))
    code, me = api.get("/v1/me")
    if code != 200:
        raise SetupError(f"/v1/me HTTP {code}：omnigent login")
    return api, me["user_id"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor")
    p.add_argument("--targets", nargs="*", help="repo 絕對路徑；預設 <WS>/WOOW 下的 LINE 三件組")
    p.add_argument("--check-api", action="store_true", help="另外打一次 Jev API（jev_gate.py models）")
    p.add_argument("--tea-login", default="git-prod")
    p = sub.add_parser("record-agent")
    p.add_argument("--session", required=True)
    sub.add_parser("bundle-plan")
    p = sub.add_parser("bundle-apply")
    p.add_argument("--plan", required=True)
    p.add_argument("--digest", required=True, help="bundle-plan 印出的 plan_digest（核准時看到的那個）")
    args = ap.parse_args(argv)
    try:
        ws = workspace(args.workspace)
        if args.cmd == "doctor":
            targets = [Path(t) for t in args.targets] if args.targets else [ws / t for t in DEFAULT_TARGETS]
            out = doctor(ws, targets, call_api=args.check_api, tea_login=args.tea_login)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out["ready"] else 1
        api, user = _api()
        if args.cmd == "record-agent":
            out = record_agent(api, ws, args.session, user)
        elif args.cmd == "bundle-plan":
            record = load_agent_record(state_dir(ws))
            if not record:
                raise SetupError("沒有 agent 紀錄：先 record-agent")
            out = bundle_plan(api, ws, record)
        else:
            out = bundle_apply(api, ws, Path(args.plan), args.digest)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except SetupError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

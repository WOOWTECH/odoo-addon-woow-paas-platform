#!/usr/bin/env python3
"""woow-security-sweep 的安裝與更新（#1978）。

子命令（`--workspace` 預設 `$OMNIGENT_RUNNER_WORKSPACE`）::

    setup.py doctor [--targets <repo 路徑> ...] [--check-api]   # 環境與權限檢查
    setup.py record-agent [--agent sweep|launcher] --session <id>   # 註冊或接手 agent 後記下 anchor
    setup.py preset --name <名稱> --targets <repo> ... [--mode …]  # 寫 <state>/presets/<名稱>.yaml（排程 prompt 用）
    setup.py bundle-plan [--agent sweep|launcher]               # 打包本機 checkout，與 server 上的版本比較
    setup.py bundle-apply [--agent …] --plan <plan 目錄> --digest <d>  # 依核准的計畫原地更新（PUT）

設計原則：

- 「能不能用」一律由檢查結果決定，不問人。規則來自 Omnigent server 的程式碼：
  排程只能建在呼叫者自己的主機（別人的 host 回 403）、session-scoped agent 對 anchor
  session 有 READ 權限即可使用、原地更新需要 anchor session 的 OWNER。
- 兩個 agent（`--agent`）：sweep（排程跑的本體）與 launcher（前台，出現在 UI picker，#1978 階段 B）。
  紀錄分別存在 `<state>/agent.json`、`<state>/launcher-agent.json`（每台主機各自一份，不進 repo）。
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
import time
from pathlib import Path
from typing import Any, Callable

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[4]
BUNDLE_REL = ".omnigent/woow-security-sweep"
AGENT_NAME = "woow_security_sweep"
#: 兩個由本 skill 註冊／更新的 agent。key 是 CLI 的 `--agent` 值。
AGENTS: dict[str, dict[str, str]] = {
    "sweep": {"name": AGENT_NAME, "bundle": BUNDLE_REL, "record": "agent.json"},
    "launcher": {"name": "woow_security_sweep_launcher", "bundle": ".omnigent/woow-security-sweep-launcher",
                 "record": "launcher-agent.json"},
}
#: snapshot 要 archive 的目錄：launcher 的 build.py --check 會比對 .agents/ 裡 launch skill 的正本
SNAPSHOT_DIRS = (".omnigent", ".agents")
DEFAULT_TARGETS = ("WOOW/woow_line_base", "WOOW/Woow_odoo_line_liff", "WOOW/woow_odoo_livechat_line")
ISSUE_OWNER = "mirror-internal"
#: omnigent/server/auth.py：READ=1、EDIT=2、MANAGE=3、OWNER=4。PUT bundle 需要 OWNER。
LEVEL_READ, LEVEL_OWNER = 1, 4
SITE_PACKAGES_CANDIDATES = (
    "/home/elmo/.local/share/uv/tools/omnigent/lib/python3.12/site-packages",  # 59mqj（uv tool）
    "/opt/omnigent/venv/lib/python3.12/site-packages",                        # pi3（omnigent-host kit）
)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

#: 排程 prompt 上限。Omnigent 0.16.0 會把長 prompt 收合成 [Pasted text] 後偵測不到草稿、
#: 只盲按一次 Enter，訊息停在輸入框不送出（#1989）。實測 `ping` 正常；`preset: <名稱>` 遠低於上限。
PROMPT_MAX = 40
PRESET_PROMPT_RE = re.compile(r"preset:\s*(\S+)")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
#: 觸發後超過這個秒數，session 仍沒有任何訊息 ⇒ 判定 prompt 沒送出
SUBMIT_GRACE_S = 600


def _load_collect():
    """preset 的驗證與 sweep 共用同一份程式（collect.py），避免兩邊規則漂移。"""
    import importlib.util

    path = REPO / BUNDLE_REL / "skills/sec-collect/scripts/collect.py"
    spec = importlib.util.spec_from_file_location("_sweep_collect", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def _agent_spec(agent: str) -> dict[str, str]:
    if agent not in AGENTS:
        raise SetupError(f"--agent 只能是 {sorted(AGENTS)}")
    return AGENTS[agent]


def load_agent_record(sdir: Path, agent: str = "sweep") -> dict[str, Any] | None:
    path = sdir / _agent_spec(agent)["record"]
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_agent_record(sdir: Path, record: dict[str, Any], agent: str = "sweep") -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / _agent_spec(agent)["record"]).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                                    encoding="utf-8")


def is_native(harness: Any) -> bool:
    """UI picker 會濾掉頂層 *-native 的 custom agent（.omnigent/README.md §UI picker）。"""
    return str(harness or "").endswith("-native")


def rrule_days(rrule: str | None) -> set[str]:
    """排程會在星期幾觸發。沒有 BYDAY、或不是 WEEKLY ⇒ 保守當成每天（重疊檢查寧可多擋）。"""
    rule = rrule or ""
    days = re.search(r"BYDAY=([A-Z0-9,+-]+)", rule)
    if not days or not re.search(r"FREQ=WEEKLY", rule):
        return set(WEEKDAYS)
    return {d[-2:] for d in days.group(1).split(",") if d[-2:] in WEEKDAYS} or set(WEEKDAYS)


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

    def post(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        return self.request("POST", path, json=body or {})

    def patch(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return self.request("PATCH", path, json=body)


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
    out.append(check("gh", OK, "gh 可用（since_upstream 的上游 advisory 用它唯讀查詢）") if shutil.which("gh")
               else check("gh", WARN, "找不到 gh", "安裝 gh 並 gh auth login；沒有它 advisory 標 unavailable"))
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


def read_target_conf(ws: Path, name: str) -> dict[str, Any]:
    """主 checkout 上 bundle 的 targets/<repo 目錄名>.yaml（沒有就是空的）。"""
    import yaml
    path = ws / "paas/woow_paas_platform" / BUNDLE_REL / "targets" / f"{name}.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _norm_remote(url: str) -> str:
    """比對用：去掉 .git、結尾斜線、帳號，ssh 寫法換成 host/path，小寫。"""
    u = url.strip().rstrip("/").removesuffix(".git").lower()
    u = re.sub(r"^[a-z+]+://(?:[^@/]+@)?", "", u)
    u = re.sub(r"^(?:[^@/]+@)?([^/:]+):(?!\d)", r"\1/", u)
    return u


def check_targets(ws: Path, targets: list[Path], tea_login: str) -> list[dict[str, Any]]:
    out = []
    for repo in targets:
        name = repo.name
        conf = read_target_conf(ws, name)
        want = str(conf.get("origin") or f"https://github.com/WOOWTECH/{name}")
        if not (repo / ".git").exists():
            out.append(check(f"target:{name}", FAIL, f"{repo} 不是 git repo", f"git clone {want}.git {repo}"))
            continue
        url = run(["git", "-C", str(repo), "remote", "get-url", "origin"]).stdout.strip()
        good = bool(url) and _norm_remote(url) == _norm_remote(want)
        out.append(check(f"target:{name}", OK if good else WARN, f"origin {url or '（無）'}",
                         None if good else f"origin 應指向正本 {want}（targets 的 origin 欄位）"))
        issues_to = str((conf.get("disclosure") or {}).get("issues_to") or f"{ISSUE_OWNER}/{name}")
        res = run(["tea", "api", "-i", "--login", tea_login, f"repos/{issues_to}"], timeout=60)
        m = re.search(r"^HTTP/\S+ (\d{3})", res.stderr, re.M)
        code = int(m.group(1)) if m else 0
        info: dict[str, Any] = {}
        if code == 200:
            try:
                info = json.loads(res.stdout)
            except json.JSONDecodeError:
                pass
        push = bool((info.get("permissions") or {}).get("push"))
        private = bool(info.get("private"))
        org_vis = None
        if code == 200 and not private:
            owner = issues_to.split("/", 1)[0]
            r2 = run(["tea", "api", "--login", tea_login, f"orgs/{owner}"], timeout=60)
            try:
                org_vis = (json.loads(r2.stdout) or {}).get("visibility")
            except json.JSONDecodeError:
                org_vis = None
        hidden = private or org_vis in ("limited", "private")
        status = OK if push and hidden else FAIL
        detail = f"tea（{tea_login}）對 {issues_to}：HTTP {code or '?'}、push={push}、private={private}、org={org_vis or '-'}"
        fix = None
        if code != 200:
            fix = f"建立 {issues_to}（targets 的 disclosure.issues_to）或修正該欄位"
        elif not hidden:
            fix = f"{issues_to} 是 public：改成 private，或改開在 limited／private 的 org（issue_sync 會拒絕開單）"
        elif not push:
            fix = f"tea login 一個對 {issues_to} 有寫入權限的帳號"
        out.append(check(f"issues:{name}", status, detail, fix))
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


def check_launcher(api: Any, sdir: Path) -> list[dict[str, Any]]:
    """前台 agent（#1978 階段 B）：紀錄存在、讀得到、頂層 harness 不是 *-native。"""
    spec = AGENTS["launcher"]
    record = load_agent_record(sdir, "launcher")
    if not record:
        return [check("launcher", FAIL, "這台主機還沒有前台 agent 紀錄",
                      f"在 <WS> 執行 `omnigent run paas/woow_paas_platform/{spec['bundle']} --server <url>` 註冊"
                      "（出現在 UI picker），再 record-agent --agent launcher --session <該 session id>")]
    code, agent = api.get(f"/v1/sessions/{record['anchor_session']}/agent")
    if code != 200:
        return [check("launcher", FAIL, f"讀不到前台 anchor {record['anchor_session']} 的 agent（HTTP {code}）",
                      "anchor session 已刪或沒分享給你：重新註冊前台 agent")]
    if agent.get("id") != record.get("agent_id") or agent.get("name") != spec["name"]:
        return [check("launcher", FAIL, f"前台 anchor 上的 agent 是 {agent.get('name')}／{agent.get('id')}，與紀錄不符",
                      "重新 record-agent --agent launcher")]
    if is_native(agent.get("harness")):
        return [check("launcher", FAIL, f"前台 agent 的 harness 是 {agent.get('harness')}：UI picker 會把它濾掉",
                      "前台 bundle 的頂層 harness 必須是 claude-sdk（build.py --check 會擋），修正後 bundle-plan／bundle-apply")]
    return [check("launcher", OK, f"前台 agent {agent['id']}（v{agent.get('version')}，harness {agent.get('harness')}）")]


def check_last_run(api: Any, task: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """最近一次觸發有沒有真的開始跑。run 紀錄的 succeeded 只代表「已觸發」，不代表 prompt 有送出。"""
    cid = f"schedule_run:{task.get('id')}"
    code, body = api.get(f"/v1/scheduled-tasks/{task.get('id')}/runs?limit=1")
    rows = (body.get("data") or body.get("runs") or body.get("scheduled_task_runs") or []) if code == 200 and isinstance(body, dict) else []
    if not rows or not rows[0].get("conversation_id"):
        return check(cid, SKIP, "還沒有觸發紀錄")
    run, sid = rows[0], rows[0]["conversation_id"]
    age = (now or time.time()) - float(run.get("fired_at") or 0)
    if age < SUBMIT_GRACE_S:
        return check(cid, SKIP, f"session {sid} 剛觸發（{int(age)} 秒前），稍後再檢查")
    code, items = api.get(f"/v1/sessions/{sid}/items?limit=20&order=asc")
    rows = (items.get("data") or items.get("items") or []) if code == 200 and isinstance(items, dict) else []
    started = any(x.get("type") in ("message", "function_call") for x in rows)
    if code != 200:
        return check(cid, WARN, f"讀不到 session {sid} 的內容（HTTP {code}）")
    return check(cid, OK if started else FAIL,
                 f"最近一次觸發 session {sid}：" + ("已開始執行" if started else "prompt 沒有送出（session 沒有任何訊息）"),
                 None if started else "打開該 session，輸入框卡在 [Pasted text] 就按一次 Enter；排程 prompt 改用 `preset: <名稱>`")


def check_schedules(api: Any, record: dict[str, Any] | None, host_id: str | None, ws: Path) -> list[dict[str, Any]]:
    sdir = state_dir(ws)
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
        prompt = str(t.get("prompt") or "").strip()
        targets: list[str] = []
        m = PRESET_PROMPT_RE.fullmatch(prompt)
        if m:
            try:
                targets = _load_collect().cmd_preset(m.group(1), sdir)["targets"]
            except Exception as exc:  # CollectError 或檔案問題，一律當成設定錯誤回報
                problems.append(f"preset 無效：{exc}")
        elif "\n" in prompt or len(prompt) > PROMPT_MAX:
            problems.append(f"prompt 太長（{len(prompt)} 字元、{prompt.count(chr(10)) + 1} 行）："
                            "Omnigent 0.16.0 會卡在 [Pasted text] 不送出，改用 `preset: <名稱>`")
        # 暫停中的排程不會自己觸發（手動排程平常就是暫停的，只靠「立即執行」）⇒ 不算重疊
        if t.get("state") != "paused":
            for day in sorted(rrule_days(t.get("rrule"))):
                for tgt in targets:
                    claimed.setdefault(f"{day}|{tgt}", []).append(t.get("name", t.get("id")))
        out.append(check(f"schedule:{t.get('id')}", FAIL if problems else OK,
                         f"{t.get('name')}（{t.get('state')}）" + (f"：{'；'.join(problems)}" if problems else ""),
                         "sys_scheduled_task_update 修正上述欄位（換 agent 要一併重送 permission_mode 與 max_cost_usd；"
                         "preset 用 setup.py preset 建立）" if problems else None))
        out.append(check_last_run(api, t))
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
        checks += check_launcher(api, sdir)
        checks += check_schedules(api, record, conf.get("host_id"), ws)
    fails = [c["id"] for c in checks if c["status"] == FAIL]
    return {"workspace": str(ws), "host_id": conf.get("host_id"), "host_name": conf.get("host_name"),
            "ready": not fails, "failed": fails, "checks": checks}


# ── agent 紀錄 ────────────────────────────────────────────────────────────────

def record_agent(api: Any, ws: Path, session: str, user: str | None = None, agent_kind: str = "sweep") -> dict[str, Any]:
    spec = _agent_spec(agent_kind)
    if not re.fullmatch(r"[0-9a-f]{32}", session):
        raise SetupError("session id 格式不對（32 位 hex）")
    code, agent = api.get(f"/v1/sessions/{session}/agent")
    if code != 200:
        raise SetupError(f"讀不到 session {session} 的 agent（HTTP {code}）：沒有 READ 權限或 session 不存在")
    if agent.get("name") != spec["name"]:
        raise SetupError(f"session {session} 的 agent 是 {agent.get('name')!r}，不是 {spec['name']}")
    if agent_kind == "launcher" and is_native(agent.get("harness")):
        raise SetupError(f"前台 agent 的 harness 是 {agent.get('harness')}：UI picker 會把它濾掉，不能當前台")
    if user is None:
        _, me = api.get("/v1/me")
        user = me.get("user_id")
    _, conv = api.get(f"/v1/sessions/{session}")
    level = (conv or {}).get("permission_level", 0) if isinstance(conv, dict) else 0
    record = {"anchor_session": session, "agent_id": agent["id"], "name": spec["name"], "agent": agent_kind,
              "version": agent.get("version"), "harness": agent.get("harness"),
              "can_update": level >= LEVEL_OWNER, "recorded_by": user,
              "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    save_agent_record(state_dir(ws), record, agent_kind)
    return record


# ── preset ───────────────────────────────────────────────────────────────────

def write_preset(ws: Path, name: str, targets: list[str], mode: str, deep_review: str,
                 review: str | None, fix: str | None) -> dict[str, Any]:
    import yaml

    collect = _load_collect()
    roster = {k: v for k, v in (("review", review), ("fix", fix)) if v}
    data = {"targets": targets, "mode": mode, "deep_review": deep_review, **({"roster": roster} if roster else {})}
    try:
        normalized = collect.validate_input(data, ws)
        path = collect.preset_path(state_dir(ws), name)
    except collect.CollectError as exc:
        raise SetupError(str(exc)) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in normalized.items() if v or k != "roster"}
    path.write_text("# 由 woow-security-sweep-setup 的 preset 子命令產生；排程 prompt 寫 `preset: "
                    f"{name}`\n" + yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"path": str(path), "schedule_prompt": f"preset: {name}", **normalized}


# ── bundle 打包與比對 ─────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    res = run(["git", "-C", str(repo), *args])
    if res.returncode:
        raise SetupError(f"git {' '.join(args)} 失敗：{res.stderr.strip()[:200]}")
    return res.stdout.strip()


def source_snapshot(repo: Path, dest: Path, bundle_rel: str = BUNDLE_REL, integration: str = "develop") -> str:
    """確認 checkout 乾淨且等於已合併的 origin/<integration>，再用 git archive 取 tracked 檔。

    archive `.omnigent` 與 `.agents` 兩個目錄（launcher 的 build.py 要比對 launch skill 的正本），
    再跑**該 bundle 自己的** build.py --check。
    """
    _git(repo, "fetch", "-q", "origin", integration)
    head, merged = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", f"origin/{integration}")
    if head != merged:
        raise SetupError(f"checkout 在 {head[:8]}，不是 origin/{integration}（{merged[:8]}）：先 merge --ff-only")
    # 只看 git 追蹤中的檔案（含已 stage 的新檔）：未追蹤的檔案不會進 git archive，不影響發出去的內容
    if _git(repo, "status", "--porcelain", "--untracked-files=no", "--", *SNAPSHOT_DIRS):
        raise SetupError(f"{'／'.join(SNAPSHOT_DIRS)} 有未 commit 的變更：只能發已合併的版本")
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", head, *SNAPSHOT_DIRS],
                          capture_output=True, check=True).stdout
    old = os.umask(0o022)  # 執行位元要和 git 的 100755／100644 一致
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")
    finally:
        os.umask(old)
    res = run(["python3.12", str(dest / bundle_rel / "build.py"), "--check"], timeout=300, cwd=dest)
    if res.returncode or "❌" in res.stdout:
        raise SetupError(f"{bundle_rel}/build.py --check 沒通過：回原 PR 修正，不在部署時重產")
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


def bundle_plan(api: Any, ws: Path, record: dict[str, Any], *, agent_kind: str = "sweep", packer: Callable = pack,
                snapshot: Callable = source_snapshot) -> dict[str, Any]:
    bundle_rel = _agent_spec(agent_kind)["bundle"]
    _, conv = api.get(f"/v1/sessions/{record['anchor_session']}")
    if not isinstance(conv, dict) or conv.get("permission_level", 0) < LEVEL_OWNER:
        raise SetupError("你不是 anchor session 的擁有者：只有擁有者能更新（PUT 需要 OWNER）")
    plans = state_dir(ws) / "setup"
    plans.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="plan-", dir=plans))
    os.chmod(out, 0o700)
    src = out / "src"
    sha = snapshot(ws / "paas/woow_paas_platform", src, bundle_rel)
    candidate = packer(src / bundle_rel)
    code, stored = api.get(f"/v1/sessions/{record['anchor_session']}/agent/contents")
    if code != 200:
        raise SetupError(f"讀不到 server 上的 bundle（HTTP {code}）")
    code, meta = api.get(f"/v1/sessions/{record['anchor_session']}/agent")
    diff = diff_manifest(manifest(stored), manifest(candidate))
    (out / "candidate.tar.gz").write_bytes(candidate)
    plan = {"agent": agent_kind, "bundle": bundle_rel,
            "source_sha": sha, "anchor_session": record["anchor_session"], "agent_id": record["agent_id"],
            "before_version": meta.get("version"), "before_sha256": hashlib.sha256(stored).hexdigest(),
            "candidate_sha256": hashlib.sha256(candidate).hexdigest(), "diff": diff,
            "noop": not any(diff.values()), "plan_dir": str(out)}
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(src)
    plan["plan_digest"] = hashlib.sha256((out / "plan.json").read_bytes()).hexdigest()
    return plan


def bundle_apply(api: Any, ws: Path, plan_dir: Path, digest: str, agent_kind: str | None = None) -> dict[str, Any]:
    raw = (plan_dir / "plan.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise SetupError("plan digest 不符：計畫在核准後被改過，重新 bundle-plan")
    plan = json.loads(raw)
    planned = plan.get("agent", "sweep")
    if agent_kind is not None and agent_kind != planned:
        raise SetupError(f"這份計畫是給 {planned} 的，不是 {agent_kind}：重新 bundle-plan --agent {agent_kind}")
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
    record = load_agent_record(state_dir(ws), planned) or {}
    record.update({"version": meta.get("version"), "source_sha": plan["source_sha"],
                   "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    save_agent_record(state_dir(ws), record, planned)
    return {"result": "DEPLOYED", "agent": planned, "agent_id": meta["id"], "version": meta.get("version"),
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
    agent_help = "sweep（排程跑的本體，預設）或 launcher（前台，出現在 UI picker）"
    p = sub.add_parser("record-agent")
    p.add_argument("--agent", choices=sorted(AGENTS), default="sweep", help=agent_help)
    p.add_argument("--session", required=True)
    p = sub.add_parser("preset", help="寫 <state>/presets/<名稱>.yaml；排程 prompt 只寫 `preset: <名稱>`")
    p.add_argument("--name", required=True)
    p.add_argument("--targets", nargs="+", required=True, help="repo 絕對路徑（必須在 <WS> 底下）")
    p.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    p.add_argument("--deep-review", choices=("auto", "always", "never"), default="auto")
    p.add_argument("--review", choices=("claude-native", "pi"))
    p.add_argument("--fix", choices=("claude-native", "pi"))
    p = sub.add_parser("bundle-plan")
    p.add_argument("--agent", choices=sorted(AGENTS), default="sweep", help=agent_help)
    p = sub.add_parser("bundle-apply")
    p.add_argument("--agent", choices=sorted(AGENTS), help="給了就核對計畫的 agent 一致（預設照計畫）")
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
        if args.cmd == "preset":
            out = write_preset(ws, args.name, args.targets, args.mode, args.deep_review, args.review, args.fix)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        api, user = _api()
        if args.cmd == "record-agent":
            out = record_agent(api, ws, args.session, user, args.agent)
        elif args.cmd == "bundle-plan":
            record = load_agent_record(state_dir(ws), args.agent)
            if not record:
                raise SetupError(f"沒有 {args.agent} 的 agent 紀錄：先 record-agent --agent {args.agent}")
            out = bundle_plan(api, ws, record, agent_kind=args.agent)
        else:
            out = bundle_apply(api, ws, Path(args.plan), args.digest, args.agent)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except SetupError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

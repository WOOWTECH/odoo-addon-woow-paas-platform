#!/usr/bin/env python3
"""woow-security-sweep 的啟動：跑一次、建排程、查狀態（#1978 階段 B）。

前台 agent（.omnigent/woow-security-sweep-launcher）與 CLI 的 Claude Code／Pi session 共用。
腳本一律從主 checkout 執行：`python3.12 <WS>/paas/woow_paas_platform/.agents/skills/
woow-security-sweep-launch/scripts/launch.py --workspace <WS> <子命令>`。

子命令::

    launch.py check                                     # 能不能啟動（doctor ＋ 每個 preset 有沒有手動排程）
    launch.py presets                                   # 列出 preset、內容、哪些排程在用
    launch.py preset --name <名稱> --targets <repo> ...  # 建立或覆寫 preset（與 setup skill 同一份程式）
    launch.py run --preset <名稱>                        # 跑一次：觸發該 preset 的暫停中手動排程「立即執行」
    launch.py schedule --preset <名稱> (--rrule <RRULE> | --manual) [--name …] [--max-cost 30]
    launch.py status                                    # 排程、進行中的 sweep session、最近幾輪

設計：

- **跑一次**走「暫停中的手動排程 ＋ POST /v1/scheduled-tasks/{id}/run」。這跟每日排程是同一條
  fire 路徑（session 建立、owner grant、prompt 送出），判斷題進得了 Inbox。暫停中的排程可以
  手動觸發（server 明文允許），平常不會自己跑。
- 觸發前檢查有沒有 targets 重疊、還在進行中的 sweep session：兩輪同時推進同一份 state 會重複出題。
- **建排程**：同一天已有 targets 重疊的排程（略過暫停中的）就拒絕。建好後補設 max_cost_usd
  （部分版本的 create 不存），再讀回來逐欄核對。prompt 一律是 `preset: <名稱>`（#1989）。
- 前台 agent 不直接用排程的 MCP 工具、不直接寫 `<state>/`：守線擋，一律經過本腳本。
- Omnigent REST client、agent 紀錄、preset 驗證全部沿用 setup skill 的 setup.py（單一真相）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[4]
SETUP_PY = REPO / ".agents/skills/woow-security-sweep-setup/scripts/setup.py"


def _load_setup():
    spec = importlib.util.spec_from_file_location("_sweep_setup", SETUP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


su = _load_setup()
SetupError = su.SetupError

TIMEZONE = "Asia/Taipei"
DEFAULT_MAX_COST = 30.0
#: 手動排程的命名：`<前綴><preset>`。run 先找這個名字，找不到才退回「唯一一個暫停中、同 preset」的排程。
MANUAL_PREFIX = "woow-security-sweep 手動 "
#: 手動排程的 rrule：建立後立刻暫停，只靠「立即執行」觸發。server 不收只觸發一次的 rule，
#: 所以給一年一次、離現在最遠的時段。
MANUAL_RRULE = "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1;BYHOUR=4;BYMINUTE=0"
#: idle 的 sweep session 在這段時間內有活動、而且重疊的 target 還沒 commit ⇒ 視為本輪還沒結束
#: （例如派完 deep review worker、正在等完成通知）。同 server 對 running 的 6 小時上限。
ACTIVE_WINDOW_S = 6 * 3600
#: 觸發後等 run 紀錄出現的秒數（fire 在背景進行）
RUN_POLL_S = 30
#: 建好後讀回核對的欄位
VERIFY_FIELDS = ("name", "prompt", "rrule", "agent_id", "timezone", "permission_mode", "max_cost_usd",
                 "workspace", "host_id", "state")


# ── 共用小工具 ────────────────────────────────────────────────────────────────

def _epoch(value: Any) -> float:
    """server 的時間戳可能是秒或毫秒（或 ISO 字串）；一律轉成秒。"""
    if value is None:
        return 0.0
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    num = float(value)
    return num / 1000 if num > 1e11 else num


def _rows(body: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in keys:
            if isinstance(body.get(key), list):
                return body[key]
    return []


def preset_of(task: dict[str, Any]) -> str | None:
    m = su.PRESET_PROMPT_RE.fullmatch(str(task.get("prompt") or "").strip())
    return m.group(1) if m else None


def load_preset(sdir: Path, name: str) -> dict[str, Any]:
    collect = su._load_collect()
    try:
        return collect.cmd_preset(name, sdir)
    except collect.CollectError as exc:
        raise SetupError(str(exc)) from exc


def preset_targets(sdir: Path, name: str | None) -> list[str] | None:
    """preset 的 targets；讀不到就回 None（呼叫端一律當成「可能重疊」處理）。"""
    if not name:
        return None
    try:
        return load_preset(sdir, name)["targets"]
    except Exception:  # noqa: BLE001 —— preset 壞掉也只是「不知道」，交給呼叫端保守處理
        return None


def sweep_record(sdir: Path) -> dict[str, Any]:
    record = su.load_agent_record(sdir, "sweep")
    if not record:
        raise SetupError("這台主機沒有 sweep agent 紀錄：先照 setup skill 註冊或接手（record-agent）")
    return record


def sweep_tasks(api: Any, agent_id: str) -> list[dict[str, Any]]:
    code, body = api.get("/v1/scheduled-tasks")
    if code != 200:
        raise SetupError(f"讀不到排程（HTTP {code}）")
    return [t for t in _rows(body, "scheduled_tasks", "data")
            if t.get("agent_id") == agent_id or re.search(r"woow[-_]security[-_]sweep", str(t.get("name", "")))]


def find_manual(tasks: list[dict[str, Any]], preset: str, agent_id: str) -> dict[str, Any] | None:
    """preset 的手動排程：先找固定名稱；沒有就找「唯一一個暫停中、同 preset、同 agent」的排程。"""
    named = [t for t in tasks if t.get("name") == MANUAL_PREFIX + preset]
    if len(named) > 1:
        raise SetupError(f"有 {len(named)} 個同名的手動排程（{MANUAL_PREFIX + preset}）：刪到剩一個")
    if named:
        return named[0]
    paused = [t for t in tasks if t.get("state") == "paused" and preset_of(t) == preset
              and t.get("agent_id") == agent_id]
    if len(paused) > 1:
        raise SetupError(f"preset {preset} 有 {len(paused)} 個暫停中的排程，分不出哪個是手動排程："
                         f"把其中一個改名為 `{MANUAL_PREFIX + preset}`")
    return paused[0] if paused else None


# ── 進行中的 sweep session ────────────────────────────────────────────────────

def _committed_since(sdir: Path, target: str, since: float) -> bool:
    """`collect commit` 在 since 之後推進過這個 target ⇒ 那一輪對它已經結束。"""
    path = sdir / "repos" / f"{Path(target).name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return _epoch(data.get("updated_at")) >= since


def session_targets(api: Any, tasks: list[dict[str, Any]], sdir: Path) -> dict[str, list[str] | None]:
    """排程觸發出來的 session → 該排程 preset 的 targets（查不到 preset 的是 None）。"""
    out: dict[str, list[str] | None] = {}
    for t in tasks:
        targets = preset_targets(sdir, preset_of(t))
        if t.get("last_run_conversation_id"):
            out[t["last_run_conversation_id"]] = targets
        code, body = api.get(f"/v1/scheduled-tasks/{t.get('id')}/runs?limit=10")
        for run in _rows(body, "runs", "data") if code == 200 else []:
            if run.get("conversation_id"):
                out.setdefault(run["conversation_id"], targets)
    return out


def busy_sessions(api: Any, sdir: Path, agent_id: str, tasks: list[dict[str, Any]], want: list[str],
                  now: float | None = None) -> list[dict[str, Any]]:
    """和 want（targets）重疊、還沒結束的 sweep session。

    判定（任一成立）：status 是 running／waiting、Inbox 有待答題目、或 idle 但 6 小時內有活動且
    重疊的 target 在 session 建立後還沒 commit（例如派完 worker 正在等完成通知）。
    查不到 targets 的 session（不是排程觸發的）一律當成重疊。
    """
    now = now or time.time()
    code, body = api.get(f"/v1/sessions?agent_id={agent_id}&kind=any&limit=20&sort_by=updated_at&order=desc")
    if code != 200:
        raise SetupError(f"讀不到 sweep 的 session 清單（HTTP {code}）：無法確認有沒有一輪正在跑，不觸發")
    mapping = session_targets(api, tasks, sdir)
    want_set = set(want)
    out = []
    for s in _rows(body, "data", "sessions"):
        if s.get("agent_id") not in (None, agent_id) or s.get("archived"):
            continue
        sid = s.get("id")
        targets = mapping.get(sid)
        overlap = sorted(want_set if targets is None else want_set & set(targets))
        if not overlap:
            continue
        status = s.get("status")
        pending = int(s.get("pending_elicitations_count") or len(s.get("pending_elicitations") or []))
        reason = None
        if status in ("running", "waiting"):
            reason = f"狀態 {status}"
        elif pending:
            reason = f"Inbox 有 {pending} 題待答"
        elif status == "idle" and now - _epoch(s.get("updated_at")) < ACTIVE_WINDOW_S:
            since = _epoch(s.get("created_at"))
            pending_targets = [t for t in overlap if not _committed_since(sdir, t, since)]
            if pending_targets:
                overlap = pending_targets
                reason = "本輪還沒 commit（可能在等 worker 完成通知）"
        if reason:
            out.append({"session": sid, "title": s.get("title"), "reason": reason, "overlap": overlap,
                        "targets_known": targets is not None})
    return out


# ── 子命令 ────────────────────────────────────────────────────────────────────

def list_presets(ws: Path, tasks: list[dict[str, Any]] | None = None, agent_id: str | None = None) -> list[dict[str, Any]]:
    sdir = su.state_dir(ws)
    out = []
    for path in sorted((sdir / "presets").glob("*.yaml")):
        name = path.stem
        entry: dict[str, Any] = {"name": name, "schedule_prompt": f"preset: {name}"}
        try:
            data = load_preset(sdir, name)
            entry.update({k: data[k] for k in ("targets", "mode", "deep_review", "roster")}, valid=True)
        except SetupError as exc:
            entry.update(valid=False, error=str(exc))
        if tasks is not None:
            entry["schedules"] = [{"id": t.get("id"), "name": t.get("name"), "state": t.get("state")}
                                  for t in tasks if preset_of(t) == name]
            try:
                manual = find_manual(tasks, name, agent_id or "")
            except SetupError as exc:
                manual, entry["manual_error"] = None, str(exc)
            entry["manual_schedule"] = manual.get("id") if manual else None
        out.append(entry)
    return out


def check(ws: Path, api_factory: Callable = su.Api, tea_login: str = "git-prod") -> dict[str, Any]:
    """doctor（不打 Jev API）＋ 每個 preset 有沒有手動排程。前台 agent 沒註冊不影響啟動。"""
    targets, targets_from = su.doctor_targets(ws)
    doc = su.doctor(ws, targets, call_api=False, api_factory=api_factory, tea_login=tea_login, targets_from=targets_from)
    blocking = [c for c in doc["failed"] if c != "launcher"]
    out: dict[str, Any] = {"ready": not blocking, "failed": blocking, "doctor": doc}
    record = su.load_agent_record(su.state_dir(ws), "sweep")
    conf = su.omnigent_config()
    if record and conf.get("server"):
        api = api_factory(conf["server"], conf.get("host_id"))
        try:
            tasks = sweep_tasks(api, record["agent_id"])
            out["presets"] = list_presets(ws, tasks, record["agent_id"])
        except SetupError as exc:
            out["presets_error"] = str(exc)
    return out


def run_once(api: Any, ws: Path, preset: str, *, now: Callable[[], float] = time.time,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    sdir = su.state_dir(ws)
    data = load_preset(sdir, preset)
    record = sweep_record(sdir)
    tasks = sweep_tasks(api, record["agent_id"])
    task = find_manual(tasks, preset, record["agent_id"])
    if not task:
        raise SetupError(f"preset {preset} 沒有手動排程：先 `schedule --preset {preset} --manual` 建一個（暫停中，只供立即執行）")
    if task.get("state") != "paused":
        raise SetupError(f"手動排程 {task.get('id')} 不是暫停中（{task.get('state')}）：它會自己觸發，先暫停或改名")
    if task.get("agent_id") != record["agent_id"]:
        raise SetupError(f"手動排程 {task.get('id')} 的 agent_id 是舊的 {task.get('agent_id')}：先用 setup skill 的 doctor 修正")
    busy = busy_sessions(api, sdir, record["agent_id"], tasks, data["targets"], now=now())
    if busy:
        detail = "；".join(f"{b['session']}（{b['reason']}，重疊 {', '.join(Path(t).name for t in b['overlap'])}）"
                          for b in busy)
        raise SetupError(f"有 targets 重疊的 sweep 還在進行，不觸發：{detail}。等它結束（Inbox 題目答完、本輪 commit）再跑")
    started = now()
    code, body = api.post(f"/v1/scheduled-tasks/{task['id']}/run")
    if code == 409:
        raise SetupError("這個手動排程已經有一輪在觸發中（HTTP 409）：用 status 查看")
    if code not in (200, 202):
        detail = body.get("detail") if isinstance(body, dict) else ""
        raise SetupError(f"立即執行失敗（HTTP {code}）{detail or ''}")
    conversation = None
    deadline = started + RUN_POLL_S
    while conversation is None and now() < deadline:
        sleep(3)
        c, runs = api.get(f"/v1/scheduled-tasks/{task['id']}/runs?limit=1")
        for r in _rows(runs, "runs", "data") if c == 200 else []:
            if _epoch(r.get("fired_at") or r.get("scheduled_at")) >= started - 5 and r.get("conversation_id"):
                conversation = r["conversation_id"]
    return {"result": "TRIGGERED", "preset": preset, "task_id": task["id"], "targets": data["targets"],
            "mode": data["mode"], "deep_review": data["deep_review"], "conversation_id": conversation,
            "next": "判斷題會進 Inbox；session 還沒出現就稍後跑 status" if conversation is None
                    else f"session {conversation} 已建立；判斷題會進 Inbox"}


def create_schedule(api: Any, ws: Path, preset: str, *, host_id: str | None, rrule: str | None = None,
                    manual: bool = False, name: str | None = None,
                    max_cost: float = DEFAULT_MAX_COST) -> dict[str, Any]:
    if manual == bool(rrule):
        raise SetupError("--rrule 與 --manual 二選一")
    if not host_id:
        raise SetupError("omnigent config 沒有 host_id：排程要釘在本機（有給 workspace 就必填）")
    if max_cost <= 0:
        raise SetupError("--max-cost 必須大於 0")
    sdir = su.state_dir(ws)
    data = load_preset(sdir, preset)
    record = sweep_record(sdir)
    tasks = sweep_tasks(api, record["agent_id"])
    name = name or (MANUAL_PREFIX + preset if manual else f"woow-security-sweep {preset}")
    if any(t.get("name") == name for t in tasks):
        raise SetupError(f"已經有同名排程 {name!r}")
    if manual:
        existing = find_manual(tasks, preset, record["agent_id"])
        if existing:
            raise SetupError(f"preset {preset} 已經有手動排程 {existing.get('id')}（{existing.get('name')}）")
        rrule = MANUAL_RRULE
    else:
        days = su.rrule_days(rrule)
        want = set(data["targets"])
        clash = []
        for t in tasks:
            if t.get("state") == "paused":
                continue  # 暫停中的不會自己觸發（手動排程就是這種）
            targets = preset_targets(sdir, preset_of(t))
            same_day = sorted(days & su.rrule_days(t.get("rrule")), key=su.WEEKDAYS.index)
            overlap = want if targets is None else want & set(targets)
            if same_day and overlap:
                clash.append(f"{t.get('name')}（{','.join(same_day)}；{', '.join(sorted(Path(x).name for x in overlap))}）")
        if clash:
            raise SetupError("同一天已有 targets 重疊的排程，拒絕建立：" + "；".join(clash)
                             + "。錯開日期（例如每週全量那天不跑每日）")
    body = {"name": name, "prompt": f"preset: {preset}", "rrule": rrule, "agent_id": record["agent_id"],
            "timezone": TIMEZONE, "permission_mode": "auto", "max_cost_usd": max_cost,
            "workspace": str(ws), "host_id": host_id}
    code, created = api.post("/v1/scheduled-tasks", body)
    if code not in (200, 201) or not isinstance(created, dict) or not created.get("id"):
        detail = created.get("detail") if isinstance(created, dict) else ""
        raise SetupError(f"建立排程失敗（HTTP {code}）{detail or ''}")
    tid = created["id"]
    patch: dict[str, Any] = {"max_cost_usd": max_cost}  # 部分版本的 create 不存 max_cost_usd
    if manual:
        patch["state"] = "paused"
    code, _ = api.patch(f"/v1/scheduled-tasks/{tid}", patch)
    if code != 200:
        raise SetupError(f"排程 {tid} 已建立，但補設 {sorted(patch)} 失敗（HTTP {code}）：用 status 檢查後修正"
                         + ("；⚠ 手動排程還沒暫停" if manual else ""))
    code, got = api.get(f"/v1/scheduled-tasks/{tid}")
    if code != 200 or not isinstance(got, dict):
        raise SetupError(f"排程 {tid} 已建立，但讀不回來（HTTP {code}）：用 status 檢查")
    want = dict(body, state="paused" if manual else "active")
    bad = []
    for key in VERIFY_FIELDS:
        a, b = got.get(key), want[key]
        same = (Path(str(a)).resolve() == Path(str(b)).resolve() if key == "workspace" and a
                else float(a or 0) == float(b) if key == "max_cost_usd" else a == b)
        if not same:
            bad.append(f"{key}：期望 {b!r}，讀回 {a!r}")
    if bad:
        raise SetupError(f"排程 {tid} 已建立，但讀回與預期不符：{'；'.join(bad)}。用 status 檢查後修正")
    return {"result": "CREATED", "id": tid, "manual": manual, "targets": data["targets"],
            **{k: got.get(k) for k in VERIFY_FIELDS}, "next_run_at": got.get("next_run_at")}


def status(api: Any, ws: Path, now: float | None = None) -> dict[str, Any]:
    sdir = su.state_dir(ws)
    record = sweep_record(sdir)
    tasks = sweep_tasks(api, record["agent_id"])
    every = sorted({t for p in list_presets(ws) for t in p.get("targets", [])})
    try:
        busy = busy_sessions(api, sdir, record["agent_id"], tasks, every, now=now) if every else []
    except SetupError as exc:
        busy = [{"error": str(exc)}]
    runs = sorted((p for p in (sdir / "runs").glob("*") if p.is_dir()), reverse=True)[:3] \
        if (sdir / "runs").is_dir() else []
    return {
        "agent_id": record["agent_id"],
        "schedules": [{k: t.get(k) for k in ("id", "name", "state", "rrule", "prompt", "next_run_at",
                                             "last_run_at", "last_run_status", "last_run_conversation_id")}
                      | {"manual": str(t.get("name") or "").startswith(MANUAL_PREFIX)} for t in tasks],
        "in_progress": busy,
        "recent_runs": [{"run": p.name, "targets": sorted(x.name for x in p.iterdir() if x.is_dir()),
                         "report": (p / "report.md").is_file()} for p in runs],
        "presets": list_presets(ws, tasks, record["agent_id"]),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", help="<WS>；省略時取 $OMNIGENT_RUNNER_WORKSPACE（互動 session 常常不是 <WS>，明給比較安全）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("--tea-login", default="git-prod")
    sub.add_parser("presets")
    p = sub.add_parser("preset", help="建立或覆寫 <state>/presets/<名稱>.yaml")
    p.add_argument("--name", required=True)
    p.add_argument("--targets", nargs="+", required=True, help="repo 絕對路徑（必須在 <WS> 底下）")
    p.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    p.add_argument("--deep-review", choices=("auto", "always", "never"), default="auto")
    p.add_argument("--review", choices=("claude-native", "pi"))
    p.add_argument("--fix", choices=("claude-native", "pi"))
    p = sub.add_parser("run", help="跑一次：觸發 preset 的手動排程「立即執行」")
    p.add_argument("--preset", required=True)
    p = sub.add_parser("schedule", help="建排程（prompt 固定是 `preset: <名稱>`）")
    p.add_argument("--preset", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--rrule", help="例：FREQ=WEEKLY;BYDAY=TU,WE,TH,FR,SA,SU;BYHOUR=8;BYMINUTE=30")
    g.add_argument("--manual", action="store_true", help="建暫停中的手動排程（run 用）")
    p.add_argument("--name")
    p.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST)
    sub.add_parser("status")
    args = ap.parse_args(argv)
    try:
        ws = su.workspace(args.workspace)
        if args.cmd == "check":
            out = check(ws, tea_login=args.tea_login)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out["ready"] else 1
        if args.cmd == "preset":
            out = su.write_preset(ws, args.name, args.targets, args.mode, args.deep_review, args.review, args.fix)
        elif args.cmd == "presets" and not su.load_agent_record(su.state_dir(ws), "sweep"):
            out = list_presets(ws)
        else:
            api, _ = su._api()
            if args.cmd == "presets":
                rec = sweep_record(su.state_dir(ws))
                out = list_presets(ws, sweep_tasks(api, rec["agent_id"]), rec["agent_id"])
            elif args.cmd == "run":
                out = run_once(api, ws, args.preset)
            elif args.cmd == "schedule":
                out = create_schedule(api, ws, args.preset, host_id=su.omnigent_config().get("host_id"),
                                      rrule=args.rrule, manual=args.manual, name=args.name, max_cost=args.max_cost)
            else:
                out = status(api, ws)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except SetupError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

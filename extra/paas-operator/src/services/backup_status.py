"""備份狀態的唯讀彙整（#1190 operator 端；商用驗收 X-BK-006-1／DATA-ODOO-005-2／AI-LLM-009-1）。

``GET /api/releases/{ns}/{name}/backup-status`` 的本體。只讀 K8s 物件（CronJob／Job／Pod）
與 LiteLLM db-agent 已寫好的狀態列，**不建 Job、不改任何東西**。

兩種來源，依 release 實際有的物件判定（不看 template slug）：

``odoo-cronjob``（woow-odoo chart）
    ``<release>-backup`` CronJob 產生的 Job（含 operator 以同一個 jobTemplate 自組的
    pre-upgrade Job）。selector 一律帶 ``woow.paas/task=backup``——housekeeping 與
    restore 共用 ``component=db-backup``／``db-restore``，不可把回收或還原的 Complete
    當成備份成功（charts README〈備份／還原〉）。
    每個 Job 的結果以容器 terminationMessage 為準（chart 0.2.14 起失敗也寫）：

    - ``{"schema_version":1,"backups":[…]}`` ＝ 成功；``last_success_at`` 取 index 最大
      ``timestamp``（備份開始時間，UTC），不用 Job completionTime——partial_upgrade
      skip 也是 exit 0。
    - ``{"skipped_reason":…}`` ＝ 略過（不算成功、也不算失敗）。
    - ``{"status":"failed","reason":…}`` ＝ 失敗原因代碼。
    - 舊 chart 失敗時沒有 message：退回 Pod／Job 的 reason（``oom_killed``／
      ``deadline_exceeded``／``job_failed``）。

``litellm-db-agent``（litellm chart）
    PostgreSQL pod 的 db-agent sidecar 把每次 dump 的結果寫進
    ``woow_platform.settings`` 的 ``backup_last_*``（chart 0.3.9 起）；這裡在 db-agent
    容器內以 unix socket 唯讀 ``SELECT`` 那幾個固定 key。

回應只有事實，不做「健康」判定——門檻（多久沒成功算 stale）屬平台政策。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: woow-odoo 例行備份 Job 的 selector（{name}＝release）。
ODOO_BACKUP_SELECTOR = (
    "app.kubernetes.io/instance={name},"
    "app.kubernetes.io/component=db-backup,"
    "woow.paas/task=backup"
)
#: 有 db-agent sidecar 的 PostgreSQL pod（litellm chart）；container 名再過濾一次。
DB_AGENT_POD_SELECTOR = "app.kubernetes.io/instance={name},app.kubernetes.io/component=postgresql"
DB_AGENT_CONTAINER = "db-agent"
#: db-agent 寫的固定 key（litellm chart templates/scripts-configmap.yaml backup_status()）。
DB_AGENT_KEYS = (
    "backup_last_ok_at",
    "backup_last_ok_file",
    "backup_last_error",
    "backup_last_error_kind",
    "backup_last_error_detail",
    "backup_last_error_at",
)
DB_AGENT_EXEC_TIMEOUT_SECONDS = 15
#: 回應裡最近幾次嘗試的上限（CronJob 只留 3 成功＋3 失敗，pre-upgrade 另計）。
RECENT_LIMIT = 10
_TEXT_LIMIT = 300
_SAFE_CODE = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _code(value: Any) -> Optional[str]:
    """reason 代碼只放行小寫英數＋底線／連字號，其餘一律 unknown（不轉送任意文字）。"""
    if not isinstance(value, str) or not value:
        return None
    value = value.strip().lower()
    if len(value) > 64 or not set(value) <= _SAFE_CODE:
        return "unknown"
    return value


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return "".join(ch for ch in value if ch.isprintable())[:_TEXT_LIMIT]


def _condition(job: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    for cond in (job.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind and cond.get("status") == "True":
            return cond
    return None


def _job_kind(job: Dict[str, Any]) -> str:
    containers = (((job.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    for container in containers:
        for env in container.get("env") or []:
            if env.get("name") == "BACKUP_KIND" and isinstance(env.get("value"), str):
                return _code(env["value"]) or "daily"
    return "daily"


def _pods_by_job(pods: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        labels = meta.get("labels") or {}
        job = labels.get("batch.kubernetes.io/job-name") or labels.get("job-name")
        if not job:
            for owner in meta.get("ownerReferences") or []:
                if owner.get("kind") == "Job":
                    job = owner.get("name")
                    break
        if job:
            grouped.setdefault(job, []).append(pod)
    for items in grouped.values():
        items.sort(key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "", reverse=True)
    return grouped


def _terminated(pod: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for status in (pod.get("status") or {}).get("containerStatuses") or []:
        for key in ("state", "lastState"):
            terminated = (status.get(key) or {}).get("terminated")
            if terminated:
                return terminated
    return None


def _termination_message(pods: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """最新一顆有 message 的 Pod 的 JSON terminationMessage（解析不了就當沒有）。"""
    for pod in pods:
        terminated = _terminated(pod) or {}
        raw = terminated.get("message")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _fallback_failure_reason(job: Dict[str, Any], pods: List[Dict[str, Any]]) -> str:
    for pod in pods:
        reason = (_terminated(pod) or {}).get("reason")
        if reason == "OOMKilled":
            return "oom_killed"
    cond = _condition(job, "Failed") or {}
    if cond.get("reason") == "DeadlineExceeded":
        return "deadline_exceeded"
    return "job_failed"


def summarize_job(job: Dict[str, Any], pods: List[Dict[str, Any]]) -> Dict[str, Any]:
    """單一備份 Job → ``{job, kind, status, started_at, finished_at, backup_at, reason, message}``。"""
    meta = job.get("metadata") or {}
    status_obj = job.get("status") or {}
    started = _parse_ts(status_obj.get("startTime")) or _parse_ts(meta.get("creationTimestamp"))
    message = _termination_message(pods)
    entry: Dict[str, Any] = {
        "job": meta.get("name"),
        "kind": _job_kind(job),
        "status": "running",
        "started_at": started,
        "finished_at": None,
        "backup_at": None,
        "reason": None,
        "message": None,
    }
    complete = _condition(job, "Complete")
    failed = _condition(job, "Failed")
    if complete:
        entry["finished_at"] = _parse_ts(status_obj.get("completionTime")) or _parse_ts(
            complete.get("lastTransitionTime"))
        if message and message.get("skipped_reason"):
            entry["status"] = "skipped"
            entry["reason"] = _code(message.get("skipped_reason"))
        else:
            entry["status"] = "succeeded"
            stamps = []
            if message and isinstance(message.get("backups"), list):
                stamps = [ts for ts in (_parse_ts((b or {}).get("timestamp"))
                                        for b in message["backups"] if isinstance(b, dict)) if ts]
            # index 最大 timestamp＝這次（或更新）的備份開始時間；舊 Pod 被回收、讀不到
            # message 時才退回 Job 完成時間。
            entry["backup_at"] = max(stamps) if stamps else entry["finished_at"]
            entry["backup_at_source"] = "index" if stamps else "job_completion"
    elif failed:
        entry["status"] = "failed"
        entry["finished_at"] = _parse_ts(failed.get("lastTransitionTime"))
        if message and message.get("status") == "failed":
            entry["reason"] = _code(message.get("reason")) or "unknown"
            entry["message"] = _text(message.get("message"))
        else:
            entry["reason"] = _fallback_failure_reason(job, pods)
    return entry


def summarize_odoo(
    cronjobs: List[Dict[str, Any]],
    jobs: List[Dict[str, Any]],
    pods: List[Dict[str, Any]],
) -> Dict[str, Any]:
    cronjob = cronjobs[0] if cronjobs else {}
    cj_spec = cronjob.get("spec") or {}
    cj_status = cronjob.get("status") or {}
    grouped = _pods_by_job(pods)
    entries = [summarize_job(job, grouped.get((job.get("metadata") or {}).get("name"), []))
               for job in jobs]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    entries.sort(key=lambda e: e["started_at"] or epoch, reverse=True)

    finished = [e for e in entries if e["status"] in ("succeeded", "failed", "skipped")]
    successes = [e for e in entries if e["status"] == "succeeded" and e["backup_at"]]
    failures = [e for e in entries if e["status"] == "failed"]

    last_success_at, last_success_source = None, None
    if successes:
        best = max(successes, key=lambda e: e["backup_at"])
        last_success_at, last_success_source = best["backup_at"], best.get("backup_at_source")
    cj_success = _parse_ts(cj_status.get("lastSuccessfulTime"))
    if cj_success and not last_success_at:
        # 成功 Job 已被回收（ttl 3 天／history 3）：CronJob 自己記得最近一次 Complete。
        # 這包含 partial_upgrade skip，所以只當最後手段、並標出來源。
        last_success_at, last_success_source = cj_success, "cronjob_status"

    consecutive = 0
    for entry in finished:
        if entry["status"] == "succeeded":
            break
        if entry["status"] == "failed":
            consecutive += 1

    latest_failure = failures[0] if failures else None
    if entries and entries[0]["status"] == "running":
        last_status = "running"
    elif finished:
        last_status = finished[0]["status"]
    else:
        last_status = "none"

    return {
        "supported": True,
        "source": "odoo-cronjob",
        "enabled": not bool(cj_spec.get("suspend")),
        "schedule": cj_spec.get("schedule") if isinstance(cj_spec.get("schedule"), str) else None,
        "last_schedule_at": _iso(_parse_ts(cj_status.get("lastScheduleTime"))),
        "running": any(e["status"] == "running" for e in entries),
        "last_status": last_status,
        "last_success_at": _iso(last_success_at),
        "last_success_source": last_success_source,
        "last_failure_at": _iso(latest_failure["finished_at"]) if latest_failure else None,
        "last_failure_reason": latest_failure["reason"] if latest_failure else None,
        "last_failure_message": latest_failure["message"] if latest_failure else None,
        "consecutive_failures": consecutive,
        "status_recorded": True,
        "recent": [
            {
                "job": e["job"], "kind": e["kind"], "status": e["status"],
                "started_at": _iso(e["started_at"]), "finished_at": _iso(e["finished_at"]),
                "backup_at": _iso(e["backup_at"]), "reason": e["reason"],
            }
            for e in entries[:RECENT_LIMIT]
        ],
    }


def parse_db_agent_settings(stdout: str) -> Dict[str, str]:
    """``psql -tA -F<TAB>`` 的 ``key<TAB>value`` 列 → dict（只收已知 key）。"""
    values: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        key, sep, value = line.partition("\t")
        if sep and key in DB_AGENT_KEYS:
            values[key] = value
    return values


def summarize_db_agent(settings: Dict[str, str], enabled: bool) -> Dict[str, Any]:
    ok_at = _parse_ts(settings.get("backup_last_ok_at"))
    error = _code(settings.get("backup_last_error"))
    error_at = _parse_ts(settings.get("backup_last_error_at"))
    recorded = any(key in settings for key in DB_AGENT_KEYS)
    failing = bool(error) and (ok_at is None or (error_at is not None and error_at >= ok_at))
    if not recorded:
        last_status = "unknown"
    elif failing:
        last_status = "failed"
    elif ok_at:
        last_status = "succeeded"
    else:
        last_status = "none"
    return {
        "supported": True,
        "source": "litellm-db-agent",
        "enabled": enabled,
        "schedule": None,
        "last_schedule_at": None,
        "running": False,
        "last_status": last_status,
        "last_success_at": _iso(ok_at),
        "last_success_source": "db_agent" if ok_at else None,
        "last_failure_at": _iso(error_at) if error else None,
        "last_failure_reason": error if error else None,
        "last_failure_message": _text(settings.get("backup_last_error_detail")) if error else None,
        # db-agent 只記最近一次；失敗中至少 1 次，連續幾次不可知。
        "consecutive_failures": 1 if failing else 0,
        # False ＝ chart < 0.3.9：db-agent 還不寫狀態，平台應顯示「無法得知」而不是「沒備份」。
        "status_recorded": recorded,
        "recent": [],
    }


def unsupported() -> Dict[str, Any]:
    return {"supported": False, "source": None, "status_recorded": False, "recent": []}


def _db_agent_pod(pods: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for pod in pods:
        spec = pod.get("spec") or {}
        if (pod.get("status") or {}).get("phase") != "Running":
            continue
        if any(c.get("name") == DB_AGENT_CONTAINER for c in spec.get("containers") or []):
            return pod
    return None


def _db_agent_enabled(pod: Dict[str, Any]) -> bool:
    for container in (pod.get("spec") or {}).get("containers") or []:
        if container.get("name") != DB_AGENT_CONTAINER:
            continue
        for env in container.get("env") or []:
            if env.get("name") == "WOOW_BACKUP_ENABLED":
                return env.get("value") == "true"
    return False


def _db_agent_has_container(pods: List[Dict[str, Any]]) -> bool:
    return any(
        c.get("name") == DB_AGENT_CONTAINER
        for pod in pods for c in (pod.get("spec") or {}).get("containers") or []
    )


#: 唯讀查詢；key 是常數，不吃任何外部輸入。表不存在（chart 太舊或 db-prepare 還沒跑）時
#: psql 會失敗，呼叫端當成 status_recorded=False。
_DB_AGENT_QUERY = (
    "SELECT key, value FROM woow_platform.settings WHERE key IN ("
    + ", ".join("'%s'" % key for key in DB_AGENT_KEYS)
    + ") ORDER BY key"
)
DB_AGENT_COMMAND = [
    "sh", "-c",
    'exec psql -X -q -tA -F "$(printf \'\\t\')" -h /var/run/postgresql '
    '-U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "$0"',
    _DB_AGENT_QUERY,
]


def read_backup_status(k8s, namespace: str, name: str) -> Dict[str, Any]:
    """依 release 現有物件讀出備份狀態。kubectl 失敗（非「找不到」）照樣往上丟。"""
    cronjobs = [
        cj for cj in k8s.get_items("cronjobs", namespace, ODOO_BACKUP_SELECTOR.format(name=name))
        if ((cj.get("metadata") or {}).get("name") or "").endswith("-backup")
    ]
    if cronjobs:
        selector = ODOO_BACKUP_SELECTOR.format(name=name)
        jobs = k8s.get_items("jobs", namespace, selector)
        pods = k8s.get_items("pods", namespace, selector)
        result = summarize_odoo(cronjobs, jobs, pods)
    else:
        pods = k8s.get_items("pods", namespace, DB_AGENT_POD_SELECTOR.format(name=name))
        if not _db_agent_has_container(pods):
            result = unsupported()
        else:
            pod = _db_agent_pod(pods)
            if pod is None:
                # 資料庫 pod 沒在跑：狀態列讀不到，但這個 release 確實有備份機制。
                result = summarize_db_agent({}, enabled=True)
                result["status_recorded"] = False
                result["unavailable_reason"] = "database_not_running"
            else:
                enabled = _db_agent_enabled(pod)
                settings: Dict[str, str] = {}
                recorded = True
                try:
                    proc = k8s.exec_in_pod(
                        namespace, pod["metadata"]["name"], DB_AGENT_CONTAINER,
                        DB_AGENT_COMMAND, timeout=DB_AGENT_EXEC_TIMEOUT_SECONDS,
                    )
                    settings = parse_db_agent_settings(proc.stdout or "")
                except Exception as exc:  # noqa: BLE001 — 舊 chart 沒有表也走這裡
                    logger.warning(
                        "backup-status: db-agent settings unreadable for %s/%s (%s)",
                        namespace, name, type(exc).__name__,
                    )
                    recorded = False
                result = summarize_db_agent(settings, enabled=enabled)
                if not recorded:
                    result["status_recorded"] = False
                    result["unavailable_reason"] = "status_unreadable"
    result["observed_at"] = _iso(datetime.now(timezone.utc))
    return result

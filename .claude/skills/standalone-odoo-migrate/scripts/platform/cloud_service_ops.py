"""cloud_service_ops.py — 在平台 Odoo 的 odoo shell 裡操作一台 cloud_service（standalone-odoo-migrate skill，#1604）。

由 cloud-service.sh 把本檔內容加上一行 `main(env, <參數>)` 從 stdin 餵給 `odoo shell`，不要單獨執行。
動作與前置條件見 cloud-service.sh 檔頭。輸出夾在 ---JSON-START--- 與 ---JSON-END--- 兩行之間（odoo shell 的 log 也在
同一個輸出裡）。conf 只回 CONF_KEYS 三個鍵——helm_values 的 conf 裡還有管理員密碼之類的值，不能印出來。
"""
import datetime
import json

MARK_START = "---JSON-START---"
MARK_END = "---JSON-END---"
CONF_KEYS = ("limitTimeCpu", "limitTimeReal", "serverWideModules")
# 動作 → 允許的前一個 state。限制的是搬遷流程會用到的情況；其他情況（例如欠費停權中的服務）要先查清楚，不要用本腳本硬做
ALLOWED_STATE = {
    "status": None,
    "suspend": ("running",),
    "resume": ("suspended",),
    "redeploy": ("running",),
    "conf": ("running",),
}


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def _conf(service):
    conf = service._helm_values_dict().get("conf") or {}
    return {k: conf.get(k) for k in CONF_KEYS}


def _snapshot(env, service):
    cron = env.ref("woow_paas_platform.ir_cron_subscription_lifecycle", raise_if_not_found=False)
    return {
        "state": service.state,
        "error_message": service.error_message or None,
        "subscription_state": service.subscription_id.state or None,
        "conf": _conf(service),
        "now_utc": _now_utc(),
        # 訂閱正常（含試用中）時，這支每日排程會把停權中的服務開回來：停權到復權不要跨過它
        "lifecycle_nextcall_utc": cron.nextcall if cron else None,
    }


def _load(env, args):
    service = env["woow_paas_platform.cloud_service"].sudo().browse(int(args["id"]))
    if not service.exists():
        raise LookupError(f"cloud_service {args['id']} not found")
    got = {"name": service.name, "release": service.helm_release_name}
    mismatch = {k: {"expected": args[k], "actual": got[k]} for k in got if args[k] != got[k]}
    if mismatch:
        raise ValueError(f"cloud_service {args['id']} does not match: {json.dumps(mismatch, ensure_ascii=False)}")
    return service


def run(env, args):
    action = args["action"]
    if action not in ALLOWED_STATE:
        raise ValueError(f"unknown action: {action}")
    service = _load(env, args)
    out = {"id": service.id, "name": service.name, "release": service.helm_release_name,
           "workspace_id": service.workspace_id.id, "state_before": service.state}
    allowed = ALLOWED_STATE[action]
    if allowed and service.state not in allowed:
        raise RuntimeError(f"{action} needs state in {allowed}, got {service.state!r}")
    if action == "suspend":
        service._do_suspend()
    elif action == "resume":
        service._do_resume()
    elif action == "redeploy":
        service.action_redeploy()
    elif action == "conf":
        updates = {k: v for k, v in (args.get("conf") or {}).items() if k in CONF_KEYS}
        if not updates:
            raise ValueError(f"conf needs at least one of {CONF_KEYS}")
        out["conf_before"] = _conf(service)
        values = service._helm_values_dict()
        values.setdefault("conf", {}).update(updates)
        service._apply_helm_values(values)
        # 先把 helm_values 寫定，redeploy 失敗時設定也還在，修好後單獨 redeploy 即可
        env.cr.commit()
        service.action_redeploy()
    if action != "status":
        env.cr.commit()
    out.update(_snapshot(env, service))
    return out


def main(env, args):
    try:
        out = {"action": args.get("action"), "ok": True, **run(env, args)}
        if out.get("state") == "error":
            out["ok"] = False
    except Exception as exc:  # noqa: BLE001 — 任何失敗都要回成 JSON，讓 cloud-service.sh 判斷 exit code
        env.cr.rollback()
        out = {"action": args.get("action"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(MARK_START)
    print(json.dumps(out, ensure_ascii=False, default=str))
    print(MARK_END)
    return out

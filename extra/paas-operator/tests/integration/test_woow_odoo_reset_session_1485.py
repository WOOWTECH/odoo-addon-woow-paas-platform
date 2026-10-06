"""#1485：重設 Odoo 管理員密碼後，重設前的 session 與舊密碼 RPC 必須在**常駐 worker** 上失效。

#1370／#1477 的 integration 只在「新連線」上驗新舊密碼（``/xmlrpc/2/common``
``authenticate`` 每次都讀 DB），蓋不到快取路徑——prod service 167（#1419 第 5 步）重設
成功後，重設前登入的瀏覽器 session 仍然有效。根因是兩個 per-worker 的 ormcache：

- ``res.users._compute_session_token``（``@ormcache('sid')``）：每個 request 的 session 檢查
- ``res.users.check``（``@ormcache('uid', 'passwd')``）：``/xmlrpc/2/object``、``/jsonrpc``
  的 ``execute_kw`` 密碼檢查

重設腳本在另一個 process（``kubectl exec python3 -c``）改密碼；不送 cache signaling，
常駐 worker 就不知道要清。本測試用**真 woow-odoo chart**：

1. 登入取得 session A；以並行 request 對每個 HTTP worker 暖 session 與舊密碼 RPC 快取，
   並從 access log 的 pid 確認每個 worker 都真的被暖到（前提不成立就 fail，不是 skip）
2. 經 operator 端點重設
3. session A 必須 SessionExpired；舊密碼 ``execute_kw``（JSON-RPC 與 XML-RPC）在每個
   worker 都被拒；舊密碼 ``authenticate`` 被拒；新密碼可登入、可 RPC

``workers`` 參數化：2＝prod 租戶現況（service 167 的 plan overlay）、0＝seed 預設
（threaded，單 process 也一樣會快取）。

需要的環境變數同 ``test_woow_odoo_exec_contract_1477.py``（沒設 ``WOOW_ODOO_CHART`` 就
skip——**skip 不是通過**）::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- \\
    WOOW_ODOO_CHART=<chart 目錄> HELM_BINARY=<helm 3.13.3> \\
    OPERATOR_TEST_PULL_SECRET_SOURCE=<ns>/<secret> \\
    pytest -m integration tests/integration/test_woow_odoo_reset_session_1485.py -v -s
"""
import json
import os
import secrets
import subprocess
import time

import pytest

from tests.integration.test_woow_odoo_exec_contract_1477 import (
    ODOO_IMAGE_DIGEST,
    ODOO_IMAGE_TAG,
    _copy_pull_secret,
    _kubectl,
)

pytestmark = pytest.mark.integration

_DB = "odoo"  # chart 預設 postgres.database
_ADMIN_LOGIN = "admin-1485@example.com"
# 每輪並行 request 數：遠大於 worker 數，逼 prefork 的每個 worker 都接到。
_FANOUT = 24

# 在 odoo 容器內打 localhost:8069（不經 nginx／Service）。argv[1]＝動作；stdin＝JSON 參數
# （密碼與 session id 不上 argv）。最後一行印 JSON。
_PROBE = r"""
import http.client, json, sys, xmlrpc.client
from concurrent.futures import ThreadPoolExecutor

action = sys.argv[1]
p = json.loads(sys.stdin.read())


def post(path, payload, sid=None):
    conn = http.client.HTTPConnection("localhost", 8069, timeout=60)
    headers = {"Content-Type": "application/json"}
    if sid:
        headers["Cookie"] = "session_id=" + sid
    conn.request("POST", path, json.dumps(payload), headers)
    resp = conn.getresponse()
    body = json.loads(resp.read() or b"null")
    cookies = [v for k, v in resp.getheaders() if k.lower() == "set-cookie"]
    conn.close()
    return body, cookies


def rpc_error(body):
    err = (body or {}).get("error")
    return (err.get("data") or {}).get("name") or err.get("message") if err else None


def session_uid(_):
    body, _ = post("/web/session/get_session_info", {"jsonrpc": "2.0", "params": {}}, p["sid"])
    return {"uid": ((body or {}).get("result") or {}).get("uid"), "error": rpc_error(body)}


def jsonrpc_object(_):
    body, _ = post("/jsonrpc", {"jsonrpc": "2.0", "method": "call", "params": {
        "service": "object", "method": "execute_kw",
        "args": [p["db"], p["uid"], p["password"], "res.users", "search_count", [[]]]}})
    return {"ok": "result" in (body or {}), "error": rpc_error(body)}


def xmlrpc_object(_):
    proxy = xmlrpc.client.ServerProxy("http://localhost:8069/xmlrpc/2/object")
    try:
        proxy.execute_kw(p["db"], p["uid"], p["password"], "res.users", "search_count", [[]])
        return {"ok": True, "error": None}
    except xmlrpc.client.Fault as exc:
        return {"ok": False, "error": exc.faultString.strip().splitlines()[-1][:120]}


def fan_out(fn):
    with ThreadPoolExecutor(max_workers=p.get("fanout", 1)) as pool:
        return list(pool.map(fn, range(p.get("fanout", 1))))


if action == "login":
    body, cookies = post("/web/session/authenticate", {"jsonrpc": "2.0", "params": {
        "db": p["db"], "login": p["login"], "password": p["password"]}})
    sid = None
    for cookie in cookies:
        if cookie.startswith("session_id="):
            sid = cookie.split(";", 1)[0].split("=", 1)[1]
    print(json.dumps({"uid": ((body or {}).get("result") or {}).get("uid"), "sid": sid,
                      "error": rpc_error(body)}))
elif action == "session":
    print(json.dumps(fan_out(session_uid)))
elif action == "rpc":
    print(json.dumps({"jsonrpc": fan_out(jsonrpc_object), "xmlrpc": fan_out(xmlrpc_object)}))
elif action == "authenticate":
    uid = xmlrpc.client.ServerProxy("http://localhost:8069/xmlrpc/2/common").authenticate(
        p["db"], p["login"], p["password"], {})
    print(json.dumps({"uid": uid}))
"""


def _release(workers):
    return f"svc-1485-w{workers}"


def _server_selector(release):
    return f"app.kubernetes.io/instance={release},app.kubernetes.io/component=server"


def _install_chart(namespace, release, admin_password, workers):
    values = {
        "image": {"tag": ODOO_IMAGE_TAG, "digest": ODOO_IMAGE_DIGEST},
        "conf": {"adminEmail": _ADMIN_LOGIN, "adminPassword": admin_password, "workers": workers},
        "resources": {"requests": {"cpu": "50m", "memory": "256Mi"}},
        "postgres": {"resources": {"requests": {"cpu": "10m", "memory": "64Mi"}}},
        "backup": {"housekeepingEnabled": False},
    }
    storage_class = os.environ.get("OPERATOR_TEST_STORAGE_CLASS")
    if storage_class:
        values["persistence"] = {"storageClass": storage_class}
        values["postgres"]["persistence"] = {"storageClass": storage_class}
        values["backup"]["persistence"] = {"storageClass": storage_class}
    pull_secret = _copy_pull_secret(namespace)
    if pull_secret:
        values["imagePullSecrets"] = [{"name": pull_secret}]
    # values 走 stdin：初始管理員密碼不進 argv。
    subprocess.run(
        [os.environ["HELM_BINARY"], "install", release, os.environ["WOOW_ODOO_CHART"],
         "--namespace", namespace, "-f", "-"],
        check=True, capture_output=True, text=True, input=json.dumps(values), timeout=120,
    )


def _wait_server_ready(namespace, release, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = json.loads(_kubectl(
            "get", "pods", "-n", namespace, "-l", _server_selector(release), "-o", "json"))["items"]
        for pod in items:
            conditions = pod.get("status", {}).get("conditions", [])
            if any(c["type"] == "Ready" and c["status"] == "True" for c in conditions):
                return pod["metadata"]["name"]
        time.sleep(5)
    raise AssertionError(f"{timeout}s 內 woow-odoo server 沒有 Ready")


def _probe(namespace, pod, action, **params):
    out = _kubectl("exec", pod, "-n", namespace, "-c", "odoo", "-i", "--",
                   "python3", "-c", _PROBE, action, stdin=json.dumps(params), timeout=300)
    return json.loads(out.strip().splitlines()[-1])


def _worker_pids(namespace, pod, since, path):
    """access log（werkzeug）裡服務過 ``path`` 的 pid 集合。

    Odoo log 格式 ``<date> <time> <pid> <LEVEL> <db> <logger>: <msg>``；只算 ``since``
    之後的行（``--since-time``），把 probe 啟動前的 health check 排除在外。
    """
    logs = _kubectl("logs", pod, "-n", namespace, "-c", "odoo", f"--since-time={since}")
    return {line.split()[2] for line in logs.splitlines()
            if f'"POST {path} ' in line and len(line.split()) > 2}


def _now_rfc3339():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@pytest.mark.parametrize("workers", [2, 0])
def test_reset_revokes_cached_sessions_and_rpc_credentials(app_client, test_namespace, workers):
    if not os.environ.get("WOOW_ODOO_CHART"):
        pytest.skip("WOOW_ODOO_CHART 未設定：需要真 woow-odoo chart 目錄")

    release = _release(workers)
    old_password = secrets.token_urlsafe(18)
    _install_chart(test_namespace, release, old_password, workers)
    pod = _wait_server_ready(test_namespace, release)
    db_login = {"db": _DB, "login": _ADMIN_LOGIN}

    # --- 1. 重設前：session A＋舊密碼 RPC，並暖每個 worker 的快取 -------------------
    login = _probe(test_namespace, pod, "login", password=old_password, **db_login)
    assert login["uid"] and login["sid"], login
    uid, sid = login["uid"], login["sid"]
    # access log 的時間戳精度到秒：等過一秒再開始，免得把 login 以前的行算進來。
    time.sleep(1.1)
    warm_since = _now_rfc3339()
    for _ in range(3):
        warm = _probe(test_namespace, pod, "session", sid=sid, fanout=_FANOUT)
        assert all(r["uid"] == uid for r in warm), warm
        rpc = _probe(test_namespace, pod, "rpc", db=_DB, uid=uid, password=old_password, fanout=_FANOUT)
        assert all(r["ok"] for r in rpc["jsonrpc"] + rpc["xmlrpc"]), rpc
    # 前提：每個 HTTP worker 都真的服務過（＝快取過）session A 與舊密碼 RPC。
    # 沒暖到所有 worker，修正前也可能「碰巧通過」——那不是證據。
    expected_processes = max(workers, 1)
    for path in ("/web/session/get_session_info", "/jsonrpc", "/xmlrpc/2/object"):
        pids = _worker_pids(test_namespace, pod, warm_since, path)
        print(f"\n[#1485 workers={workers}] {path} served by pids {sorted(pids)}")
        assert len(pids) >= expected_processes, (path, pids)

    # --- 2. 重設 -------------------------------------------------------------------
    new_password = secrets.token_urlsafe(18)
    resp = app_client.post(f"/api/releases/{test_namespace}/{release}/odoo/admin-password",
                           json={"new_password": new_password})
    body = resp.json()
    assert resp.status_code == 200 and body["success"] is True, resp.text
    assert new_password not in resp.text

    # --- 3. 重設後：同一顆 pod、同一批常駐 worker ----------------------------------
    assert _wait_server_ready(test_namespace, release) == pod
    # 第一個 request 最關鍵：失效的 session 一被任何 worker 判定過期就會被 logout，
    # 之後就算 worker 還快取著舊 token 也看不出來——所以先單發一次再 fan-out。
    first = _probe(test_namespace, pod, "session", sid=sid, fanout=1)
    stale = _probe(test_namespace, pod, "rpc", db=_DB, uid=uid, password=old_password, fanout=_FANOUT)
    # 先全部觀測完再斷言：修正前一次看到 session 與 RPC 兩條快取路徑各自的狀態。
    print(f"\n[#1485 workers={workers}] after reset: first session probe={first}; "
          f"old-password jsonrpc ok={sum(r['ok'] for r in stale['jsonrpc'])}/{_FANOUT}, "
          f"xmlrpc ok={sum(r['ok'] for r in stale['xmlrpc'])}/{_FANOUT}")
    assert first[0]["uid"] is None and "SessionExpired" in (first[0]["error"] or ""), first
    after = _probe(test_namespace, pod, "session", sid=sid, fanout=_FANOUT)
    assert all(r["uid"] is None for r in after), after
    assert not any(r["ok"] for r in stale["jsonrpc"]), stale["jsonrpc"]
    assert not any(r["ok"] for r in stale["xmlrpc"]), stale["xmlrpc"]
    assert _probe(test_namespace, pod, "authenticate", password=old_password, **db_login)["uid"] is False

    assert _probe(test_namespace, pod, "authenticate", password=new_password, **db_login)["uid"] == uid
    fresh = _probe(test_namespace, pod, "rpc", db=_DB, uid=uid, password=new_password, fanout=_FANOUT)
    assert all(r["ok"] for r in fresh["jsonrpc"] + fresh["xmlrpc"]), fresh
    relogin = _probe(test_namespace, pod, "login", password=new_password, **db_login)
    assert relogin["uid"] == uid, relogin

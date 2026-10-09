"""#1616 integration：n8n 還在啟動（暫時 server）時送 ``/init/n8n``，要等到真的 API 起來再做。

n8n 2.x 一啟動先開暫時的 HTTP server：DB migration 跑完之前，除了 ``/healthz/readiness`` 之外
任何路徑（含 ``GET /rest/settings``、``POST /rest/owner/setup``）都回 200＋純文字「n8n is starting
up. Please wait」。chart 的 readinessProbe 打 ``/healthz``，這段時間 pod 已經 Ready。舊的
``_wait_for_ready`` 只看 200 ⇒ owner setup 拿到 200＋非 JSON ⇒ init 500（JSONDecodeError）。

空窗本來只有幾秒（2.35.3 實測 postgres 約 1–6 秒），測試要穩定重現就得把它撐開：先建好
TypeORM 的 ``migrations`` 表、在另一個連線以 ACCESS EXCLUSIVE 鎖住 ``_HOLD_SECONDS`` 秒，n8n 讀
migration 紀錄時就卡在暫時 server 階段；鎖放開後 migration 照常從頭跑完。init 的 ready 等待上限
是 ``MAX_READY_RETRIES × READY_RETRY_DELAY``（60 秒），要大於鎖住的時間加上 migration 時間。

本地跑（無 CI runner；與其他 integration 同一套 recipe）::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- \\
    OPERATOR_TEST_NODE_NAME=<避開正式站的 node> \\
    pytest -m integration tests/integration/test_n8n_init_during_startup_1616.py -v
"""
import subprocess
import time

import pytest

from tests.integration import test_app_admin_password_reset as reset_it
from tests.integration.test_n8n_init_owner_changed_1534 import _make_app_secret, _secret_key

pytestmark = pytest.mark.integration

_HOLD_SECONDS = 25
_PLACEHOLDER = "n8n is starting up"
_SETTINGS_PROBE = (
    'const http=require("http");http.get({hostname:"localhost",port:5678,path:"/rest/settings"},'
    'r=>{let b="";r.on("data",c=>b+=c).on("end",()=>console.log(r.statusCode+" "+b.slice(0,40)))})'
    '.on("error",()=>console.log("0"))'
)


def _psql(namespace, pod, sql, db="n8n"):
    out = reset_it._exec(namespace, pod, "postgres", ["psql", "-U", "postgres", "-d", db, "-qc", sql])
    assert out.returncode == 0, out.stderr


def test_init_sent_while_n8n_is_starting_waits_for_the_real_api(app_client, test_namespace):
    release = "n8n-st"
    pg = f"{release}-pg"
    reset_it._apply(f"""
apiVersion: v1
kind: Pod
metadata:
  name: {pg}
  namespace: {test_namespace}
  labels: {{app: {pg}}}
spec:
{reset_it._node_selector_yaml(2)}  containers:
  - name: postgres
    image: {reset_it.POSTGRES_IMAGE}
    env:
    - {{name: POSTGRES_PASSWORD, value: n8n-int-1616}}
    - {{name: POSTGRES_DB, value: n8n}}
    readinessProbe:
      exec: {{command: [pg_isready, -U, postgres]}}
      periodSeconds: 3
---
apiVersion: v1
kind: Service
metadata: {{name: {pg}, namespace: {test_namespace}}}
spec:
  selector: {{app: {pg}}}
  ports: [{{port: 5432}}]
""")
    reset_it._wait_ready(test_namespace, f"app={pg}", timeout=300)
    pg_pod = reset_it._pod_name(test_namespace, f"app={pg}")
    # TypeORM（n8n）的 migration 紀錄表，形狀與它自己建的一樣；鎖住期間 n8n 停在暫時 server。
    _psql(test_namespace, pg_pod, 'CREATE TABLE "migrations" ("id" SERIAL NOT NULL PRIMARY KEY, '
                                  '"timestamp" bigint NOT NULL, "name" character varying NOT NULL)')
    # n8n 容器先停在閘門前（image 拉多久都不影響鎖的時間），鎖拿到之後才放行啟動。
    _make_app_secret(test_namespace, release)
    reset_it._apply(f"""
apiVersion: v1
kind: Pod
metadata:
  name: {release}
  namespace: {test_namespace}
  labels:
    app.kubernetes.io/instance: {release}
    app.kubernetes.io/name: n8n
spec:
{reset_it._node_selector_yaml(2)}  containers:
  - name: n8n
    image: {reset_it.N8N_IMAGE}
    command: [/bin/sh, -c, "until [ -f /tmp/go ]; do sleep 0.2; done; exec /docker-entrypoint.sh"]
    env:
    - {{name: DB_TYPE, value: postgresdb}}
    - {{name: DB_POSTGRESDB_HOST, value: {pg}}}
    - {{name: DB_POSTGRESDB_DATABASE, value: n8n}}
    - {{name: DB_POSTGRESDB_USER, value: postgres}}
    - {{name: DB_POSTGRESDB_PASSWORD, value: n8n-int-1616}}
""")
    deadline = time.monotonic() + 600
    while reset_it._exec(test_namespace, release, "n8n", ["true"]).returncode != 0:
        assert time.monotonic() < deadline, "n8n container never started"
        time.sleep(2)

    holder = subprocess.Popen(
        ["kubectl", "exec", pg_pod, "-n", test_namespace, "-c", "postgres", "--",
         "psql", "-U", "postgres", "-d", "n8n", "-qc",
         f'BEGIN; LOCK TABLE "migrations" IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep({_HOLD_SECONDS}); COMMIT;'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(2)  # 讓鎖先拿到
        assert reset_it._exec(test_namespace, release, "n8n", ["touch", "/tmp/go"]).returncode == 0
        # 先確認真的在暫時 server 階段（不然這支測不到東西）——鎖住期間它一直在。
        deadline = time.monotonic() + 240
        while True:
            out = reset_it._exec(test_namespace, release, "n8n", ["node", "-e", _SETTINGS_PROBE])
            last = out.stdout.strip()
            if last.startswith("200 " + _PLACEHOLDER):
                break
            assert holder.poll() is None, f"lock released before n8n reached its startup server ({last})"
            assert time.monotonic() < deadline, f"n8n never served its startup page (last {last!r})"
            time.sleep(1)

        started = time.monotonic()
        resp = app_client.post(f"/api/releases/{test_namespace}/{release}/init/n8n", json={
            "owner_email": reset_it.OWNER_EMAIL, "owner_password": reset_it._password()})
        elapsed = time.monotonic() - started
    finally:
        holder.wait(timeout=_HOLD_SECONDS + 60)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True, body
    assert body["secret_patched"] is True
    assert _secret_key(test_namespace, release) == body["api_key"]
    # 真的有等：init 回來時鎖已經放開過（不是剛好在空窗之後才送出）。
    assert holder.returncode == 0, holder.stderr.read() if holder.stderr else ""
    assert elapsed > 5, elapsed

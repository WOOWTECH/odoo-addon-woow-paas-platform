"""#1370 integration：Odoo／n8n 管理員密碼重設打真的 app 容器（真 kubectl、真 DB）。

unit 測試只驗得到「operator 有沒有把密碼經 stdin 交出去」與失敗分類；它驗不到
真正會壞的地方——全部都綁 app 的內部契約，只在真容器裡現形：

- n8n：image 內 bcryptjs／sqlite3／pg 的位置、``user`` 表與 ``roleSlug`` 欄位、
  MFA 欄位的序列化（simple-array 空陣列＝''）、改完不重啟就能登入。
  sqlite（預設）與 postgres（開通表單可選）兩種 DB 各跑一次。
- Odoo：``base.user_admin`` xmlid、``auth_totp`` 的 ``totp_secret``、
  ``/etc/odoo/odoo.conf`` 的 db_name、改完不重啟就能經 XML-RPC 登入；
  封存的管理員要 fail-loud 而且密碼不得被動到。

本地跑（無 CI runner；與其他 integration 同一套 recipe）::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- \\
    OPERATOR_TEST_NODE_NAME=<避開正式站的 node> \\
    pytest -m integration tests/integration/test_app_admin_password_reset.py -v

image 可用 ``OPERATOR_TEST_N8N_IMAGE``／``OPERATOR_TEST_ODOO_IMAGE``／
``OPERATOR_TEST_POSTGRES_IMAGE`` 覆寫（預設即平台 seed 釘的版本）。n8n 升版時必跑。
"""
import json
import os
import secrets
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

N8N_IMAGE = os.environ.get("OPERATOR_TEST_N8N_IMAGE", "docker.io/n8nio/n8n:1.122.4")
ODOO_IMAGE = os.environ.get(
    "OPERATOR_TEST_ODOO_IMAGE",
    "jcr-prod.woowtech.io/woow-paas-docker-local/cloud-odoo@sha256:"
    "8f984434b810c0ebeeea54c460167b8f487ece5ff87286f334c4fd47f43e5cd3",
)
POSTGRES_IMAGE = os.environ.get("OPERATOR_TEST_POSTGRES_IMAGE", "docker.io/library/postgres:16-alpine")
OWNER_EMAIL = "owner-1370@example.com"


def _node_selector_yaml(indent):
    node = os.environ.get("OPERATOR_TEST_NODE_NAME")
    if not node:
        return ""
    pad = " " * indent
    return f"{pad}nodeSelector:\n{pad}  kubernetes.io/hostname: {json.dumps(node)}\n"


def _apply(manifest):
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest.encode(), check=True, capture_output=True)


def _wait_ready(namespace, selector, timeout=600):
    subprocess.run(
        ["kubectl", "wait", "--for=condition=Ready", "pod", "-l", selector,
         "-n", namespace, f"--timeout={timeout}s"],
        check=True, capture_output=True,
    )


def _exec(namespace, pod, container, command, stdin=None, timeout=120):
    args = ["kubectl", "exec", pod, "-n", namespace, "-c", container]
    if stdin is not None:
        args.append("-i")
    return subprocess.run(args + ["--"] + command, input=stdin, capture_output=True, text=True, timeout=timeout)


def _pod_name(namespace, selector):
    return subprocess.check_output(
        ["kubectl", "get", "pod", "-n", namespace, "-l", selector, "-o", "jsonpath={.items[0].metadata.name}"],
        text=True,
    ).strip()


def _password():
    return "Pw1370-" + secrets.token_urlsafe(18)


# --- n8n ---------------------------------------------------------------------

_N8N_HTTP = """
const http=require("http");let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{
const {method,path,body}=JSON.parse(s);const d=body?JSON.stringify(body):"";
const r=http.request({hostname:"localhost",port:5678,path,method,headers:{"Content-Type":"application/json",
"Content-Length":Buffer.byteLength(d)}},res=>{let b="";res.on("data",c=>b+=c).on("end",()=>
console.log(JSON.stringify({status:res.statusCode})))});r.on("error",e=>console.log(JSON.stringify({status:0})));
r.write(d);r.end();});
"""


def _n8n_http(namespace, pod, method, path, body=None):
    out = _exec(namespace, pod, "n8n", ["node", "-e", _N8N_HTTP],
                stdin=json.dumps({"method": method, "path": path, "body": body}))
    return json.loads(out.stdout.strip().splitlines()[-1])["status"]


def _n8n_login(namespace, pod, password):
    return _n8n_http(namespace, pod, "POST", "/rest/login",
                     {"emailOrLdapLoginId": OWNER_EMAIL, "password": password})


def _deploy_n8n(namespace, release, db):
    env = ""
    if db == "postgres":
        _apply(f"""
apiVersion: v1
kind: Pod
metadata:
  name: {release}-pg
  namespace: {namespace}
  labels: {{app: {release}-pg}}
spec:
{_node_selector_yaml(2)}  containers:
  - name: postgres
    image: {POSTGRES_IMAGE}
    env:
    - {{name: POSTGRES_PASSWORD, value: n8n-int-1370}}
    - {{name: POSTGRES_DB, value: n8n}}
    readinessProbe:
      exec: {{command: [pg_isready, -U, postgres]}}
      periodSeconds: 3
---
apiVersion: v1
kind: Service
metadata: {{name: {release}-pg, namespace: {namespace}}}
spec:
  selector: {{app: {release}-pg}}
  ports: [{{port: 5432}}]
""")
        _wait_ready(namespace, f"app={release}-pg", timeout=300)
        env = f"""    env:
    - {{name: DB_TYPE, value: postgresdb}}
    - {{name: DB_POSTGRESDB_HOST, value: {release}-pg}}
    - {{name: DB_POSTGRESDB_DATABASE, value: n8n}}
    - {{name: DB_POSTGRESDB_USER, value: postgres}}
    - {{name: DB_POSTGRESDB_PASSWORD, value: n8n-int-1370}}
"""
    _apply(f"""
apiVersion: v1
kind: Pod
metadata:
  name: {release}
  namespace: {namespace}
  labels:
    app.kubernetes.io/instance: {release}
    app.kubernetes.io/name: n8n
spec:
{_node_selector_yaml(2)}  containers:
  - name: n8n
    image: {N8N_IMAGE}
{env}    readinessProbe:
      httpGet: {{path: /healthz, port: 5678}}
      periodSeconds: 3
""")
    _wait_ready(namespace, f"app.kubernetes.io/instance={release}", timeout=600)
    pod = _pod_name(namespace, f"app.kubernetes.io/instance={release}")
    # /healthz 比 REST 路由早起來（實測 owner/setup 先回 404）；比照 init/n8n 等 /rest/settings。
    deadline = time.monotonic() + 300
    while _n8n_http(namespace, pod, "GET", "/rest/settings") != 200:
        assert time.monotonic() < deadline, "n8n REST API never became ready"
        time.sleep(3)
    return pod


def _enable_fake_n8n_mfa(namespace, pod, db):
    """把 owner 標成已開 MFA（假 secret）——不關掉的話新密碼也登不進去。"""
    if db == "sqlite":
        script = ('const {createRequire}=require("module");const fs=require("fs");'
                  'const r=createRequire(fs.realpathSync("/usr/local/bin/n8n"));const s=r("sqlite3");'
                  'const db=new s.Database(process.env.HOME+"/.n8n/database.sqlite");'
                  'db.run("UPDATE user SET mfaEnabled=1, mfaSecret=\'x\', mfaRecoveryCodes=\'a,b\'",'
                  'function(e){console.log(e?"ERR":this.changes)})')
    else:
        script = ('const {createRequire}=require("module");const fs=require("fs");'
                  'const r=createRequire(fs.realpathSync("/usr/local/bin/n8n"));const pg=r("pg");'
                  'const c=new pg.Client({host:process.env.DB_POSTGRESDB_HOST,database:"n8n",user:"postgres",'
                  'password:process.env.DB_POSTGRESDB_PASSWORD});c.connect().then(()=>c.query('
                  '"UPDATE \\"user\\" SET \\"mfaEnabled\\"=true, \\"mfaSecret\\"=\'x\', \\"mfaRecoveryCodes\\"=\'a,b\'"))'
                  '.then(r=>{console.log(r.rowCount);return c.end()})')
    out = _exec(namespace, pod, "n8n", ["node", "-e", script])
    assert out.stdout.strip() == "1", out.stdout + out.stderr


@pytest.mark.parametrize("db", ["sqlite", "postgres"])
def test_n8n_owner_reset_logs_in_with_new_password(app_client, test_namespace, db):
    release = f"n8n-{db[:2]}"
    pod = _deploy_n8n(test_namespace, release, db)
    old_password = _password()
    # 沒有 owner（setup 模式）時：拒絕且什麼都不改。
    resp = app_client.post(f"/api/releases/{test_namespace}/{release}/n8n/owner-password",
                           json={"new_password": _password()})
    assert resp.json()["error_code"] == "admin_missing", resp.text
    assert resp.json()["error_kind"] == "failed"

    # owner setup 的路由偶爾比 /rest/settings 晚註冊（postgres 實測先回 404）——重試到不是 404。
    deadline = time.monotonic() + 120
    while (status := _n8n_http(test_namespace, pod, "POST", "/rest/owner/setup", {
            "email": OWNER_EMAIL, "firstName": "Int", "lastName": "Test", "password": old_password})) == 404:
        assert time.monotonic() < deadline, "n8n owner setup route never appeared"
        time.sleep(3)
    assert status == 200, status
    assert _n8n_login(test_namespace, pod, old_password) == 200
    _enable_fake_n8n_mfa(test_namespace, pod, db)

    new_password = _password()
    resp = app_client.post(f"/api/releases/{test_namespace}/{release}/n8n/owner-password",
                           json={"new_password": new_password})

    body = resp.json()
    assert resp.status_code == 200 and body["success"] is True, resp.text
    assert body["login"] == OWNER_EMAIL
    assert body["second_factor_cleared"] is True
    assert new_password not in resp.text
    # 不重啟：同一顆 pod 立刻用新密碼登得進去（MFA 已關，所以不會被要求驗證碼）。
    assert _pod_name(test_namespace, f"app.kubernetes.io/instance={release}") == pod
    assert _n8n_login(test_namespace, pod, new_password) == 200
    assert _n8n_login(test_namespace, pod, old_password) == 401


# --- Odoo --------------------------------------------------------------------

_ODOO_CONF = """[options]
db_host = localhost
db_port = 5432
db_user = odoo
db_password = odoo-int-1370
db_name = tenant
dbfilter = ^tenant$
list_db = False
data_dir = /var/lib/odoo
http_port = 8069
workers = 0
max_cron_threads = 0
"""

_ODOO_ORM = """
import json, sys, odoo
odoo.tools.config.parse_config(["-c", "/etc/odoo/odoo.conf", "--no-http"])
with odoo.registry("tenant").cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    admin = env.ref("base.user_admin").with_context(active_test=False)
    action = sys.argv[1]
    if action == "prepare":
        admin.write({"login": "boss-1370@example.com", "password": sys.stdin.read()})
    elif action == "totp_on":
        admin.write({"totp_secret": "JBSWY3DPEHPK3PXP"})
    elif action == "archive":
        admin.write({"active": False})
    cr.execute("SELECT password FROM res_users WHERE id = %s", [admin.id])
    print(json.dumps({"hash": cr.fetchone()[0], "totp": bool(admin.totp_secret), "active": admin.active}))
"""

_ODOO_AUTH = """
import sys, xmlrpc.client
uid = xmlrpc.client.ServerProxy("http://localhost:8069/xmlrpc/2/common").authenticate(
    "tenant", "boss-1370@example.com", sys.stdin.read(), {})
print("OK" if uid else "DENIED")
"""


def _deploy_odoo(namespace, release):
    _apply(f"""
apiVersion: v1
kind: ConfigMap
metadata: {{name: {release}-conf, namespace: {namespace}}}
data:
  odoo.conf: |
{''.join('    ' + line + chr(10) for line in _ODOO_CONF.splitlines())}
---
apiVersion: v1
kind: Pod
metadata:
  name: {release}
  namespace: {namespace}
  labels:
    app.kubernetes.io/instance: {release}
    app.kubernetes.io/component: server
    app.kubernetes.io/name: odoo
spec:
{_node_selector_yaml(2)}  containers:
  - name: postgresql
    image: {POSTGRES_IMAGE}
    env:
    - {{name: POSTGRES_USER, value: odoo}}
    - {{name: POSTGRES_PASSWORD, value: odoo-int-1370}}
    - {{name: POSTGRES_DB, value: postgres}}
  - name: odoo
    image: {ODOO_IMAGE}
    command: [/bin/sh, -c]
    args:
    - until python3 -c "import socket; socket.create_connection(('localhost', 5432), 2)"; do sleep 2; done;
      odoo -c /etc/odoo/odoo.conf -d tenant -i base,auth_totp --without-demo=all --stop-after-init &&
      exec odoo -c /etc/odoo/odoo.conf
    readinessProbe:
      tcpSocket: {{port: 8069}}
      periodSeconds: 5
      failureThreshold: 120
    volumeMounts:
    - {{name: conf, mountPath: /etc/odoo}}
    - {{name: data, mountPath: /var/lib/odoo}}
  volumes:
  - {{name: conf, configMap: {{name: {release}-conf}}}}
  - {{name: data, emptyDir: {{}}}}
""")
    _wait_ready(namespace, f"app.kubernetes.io/instance={release}", timeout=900)
    return _pod_name(namespace, f"app.kubernetes.io/instance={release}")


def _odoo_orm(namespace, pod, action, stdin=""):
    out = _exec(namespace, pod, "odoo", ["python3", "-c", _ODOO_ORM, action], stdin=stdin, timeout=180)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _odoo_auth(namespace, pod, password):
    return _exec(namespace, pod, "odoo", ["python3", "-c", _ODOO_AUTH], stdin=password).stdout.strip()


def test_odoo_admin_reset_logs_in_with_new_password_and_clears_totp(app_client, test_namespace):
    release = "odoo-1370"
    pod = _deploy_odoo(test_namespace, release)
    old_password = _password()
    _odoo_orm(test_namespace, pod, "prepare", stdin=old_password)
    assert _odoo_auth(test_namespace, pod, old_password) == "OK"
    # 開 2FA 之後，XML-RPC 不再接受密碼登入（Odoo 要 API key）——reset 後又能用密碼
    # 登入，就證明 TOTP 真的被清掉了，而不只是欄位讀起來是空的。
    assert _odoo_orm(test_namespace, pod, "totp_on")["totp"] is True
    assert _odoo_auth(test_namespace, pod, old_password) == "DENIED"

    started = time.monotonic()
    new_password = _password()
    resp = app_client.post(f"/api/releases/{test_namespace}/{release}/odoo/admin-password",
                           json={"new_password": new_password})
    elapsed = time.monotonic() - started

    body = resp.json()
    assert resp.status_code == 200 and body["success"] is True, resp.text
    assert body["login"] == "boss-1370@example.com"
    assert body["second_factor_cleared"] is True
    assert new_password not in resp.text
    after = _odoo_orm(test_namespace, pod, "read")
    assert after["totp"] is False
    # 不重啟：同一顆 pod 立刻用新密碼登得進去、舊的不行。
    assert _odoo_auth(test_namespace, pod, new_password) == "OK"
    assert _odoo_auth(test_namespace, pod, old_password) == "DENIED"
    # 預算：exec 逾時是 45s，實測要留明顯餘裕（冷啟動 registry 的時間）。
    assert elapsed < 40, elapsed

    # 封存的管理員：fail-loud、密碼雜湊一個 bit 都不動。
    archived = _odoo_orm(test_namespace, pod, "archive")
    resp = app_client.post(f"/api/releases/{test_namespace}/{release}/odoo/admin-password",
                           json={"new_password": _password()})
    body = resp.json()
    assert (body["success"], body["error_kind"], body["error_code"]) == (False, "failed", "admin_inactive"), resp.text
    assert _odoo_orm(test_namespace, pod, "read")["hash"] == archived["hash"]


def test_reset_without_pod_is_not_executed(app_client, test_namespace):
    resp = app_client.post(f"/api/releases/{test_namespace}/nothing-here/odoo/admin-password",
                           json={"new_password": _password()})
    body = resp.json()
    assert (body["success"], body["error_kind"], body["error_step"]) == (False, "not_executed", "find_pod"), resp.text

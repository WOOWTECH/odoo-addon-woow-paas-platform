"""#1370 integration：Odoo／n8n／Omnigent 管理員密碼重設打真的 app 容器（真 kubectl、真 DB）。

unit 測試只驗得到「operator 有沒有把密碼經 stdin 交出去」與失敗分類；它驗不到
真正會壞的地方——全部都綁 app 的內部契約，只在真容器裡現形：

- n8n：image 內 bcryptjs／sqlite3／pg 的位置、``user`` 表與 ``roleSlug`` 欄位、
  MFA 欄位的序列化（simple-array 空陣列＝''）、改完不重啟就能登入。
  sqlite（預設）與 postgres（開通表單可選）兩種 DB 各跑一次。
- Odoo：``base.user_admin`` xmlid、``auth_totp`` 的 ``totp_secret``、
  ``/etc/odoo/odoo.conf`` 的 db_name、改完不重啟就能經 XML-RPC 登入；
  封存的管理員要 fail-loud 而且密碼不得被動到。
- Omnigent：image 內 omnigent 的 ``SqlAlchemyAccountStore``／``hash_password``／
  ``target_account_scope`` 等內部 API（上游改版就會壞）、改完不重啟就能經
  ``/auth/login`` 登入、既有 session 不被登出；sqlite（chart 預設）與 postgres
  （charts#139 existingSecret 的 PG 後端）各跑一次。

本地跑（無 CI runner；與其他 integration 同一套 recipe）::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- \\
    OPERATOR_TEST_NODE_NAME=<避開正式站的 node> \\
    pytest -m integration tests/integration/test_app_admin_password_reset.py -v

image 可用 ``OPERATOR_TEST_N8N_IMAGE``／``OPERATOR_TEST_ODOO_IMAGE``／
``OPERATOR_TEST_OMNIGENT_IMAGE``／``OPERATOR_TEST_POSTGRES_IMAGE`` 覆寫（預設即平台 seed
釘的版本）。n8n／omnigent 升版時必跑。JCR 的 image 要節點拉得到（節點有 pull 憑證，或
``OPERATOR_TEST_NODE_NAME`` 指到已快取該 digest 的節點）。
"""
import json
import os
import secrets
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

# 平台 seed 釘的是 JCR 鏡像 n8n:2.35.3@sha256:…（src/data/cloud_app_templates.xml），與 Docker Hub
# 同一個 digest；這裡用 Docker Hub 那顆，節點不需要 JCR pull 憑證（同 test_n8n_init_owner_changed_1534）。
N8N_IMAGE = os.environ.get("OPERATOR_TEST_N8N_IMAGE", "docker.io/n8nio/n8n:2.35.3")
ODOO_IMAGE = os.environ.get(
    "OPERATOR_TEST_ODOO_IMAGE",
    "jcr-prod.woowtech.io/woow-paas-docker-local/cloud-odoo@sha256:"
    "8f984434b810c0ebeeea54c460167b8f487ece5ff87286f334c4fd47f43e5cd3",
)
OMNIGENT_IMAGE = os.environ.get(
    "OPERATOR_TEST_OMNIGENT_IMAGE",
    "jcr-prod.woowtech.io/woow-paas-docker-local/omnigent-server-kubernetes@sha256:"
    "ba384bba57cb1f80b28e3cbfab7c58d6ad94f5cd99c3773a86a1987ffe3bf902",
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
"Content-Length":Buffer.byteLength(d)}},res=>{let b="";res.on("data",c=>b+=c).on("end",()=>{let j=false;
try{const v=JSON.parse(b);j=v!==null&&typeof v==="object"&&!Array.isArray(v)}catch(e){}
console.log(JSON.stringify({status:res.statusCode,json:j}))})});
r.on("error",e=>console.log(JSON.stringify({status:0,json:false})));
r.write(d);r.end();});
"""


def _n8n_request(namespace, pod, method, path, body=None):
    """``{"status": <HTTP 狀態>, "json": <body 是不是 JSON 物件>}``（body 本身不帶出來）。"""
    out = _exec(namespace, pod, "n8n", ["node", "-e", _N8N_HTTP],
                stdin=json.dumps({"method": method, "path": path, "body": body}))
    return json.loads(out.stdout.strip().splitlines()[-1])


def _n8n_http(namespace, pod, method, path, body=None):
    return _n8n_request(namespace, pod, method, path, body)["status"]


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
    # #1616：pod Ready（/healthz）與 /rest/settings 回 200 都不代表 DB 已就緒。n8n 2.x 一啟動先開
    # 暫時的 server：migration 跑完之前，除了 /healthz/readiness 之外任何路徑都回 200＋純文字
    # 「n8n is starting up. Please wait」（2.35.3 實測 postgres 約 1–6 秒，那時 user 表還沒有
    # roleSlug 欄位，reset 就回 db_unsupported），接著路由註冊完之前回 404。比照 init/n8n
    # （n8n_init._wait_for_ready）：/rest/settings 回 200 **而且是 JSON** 才算 ready。
    deadline = time.monotonic() + 300
    while (resp := _n8n_request(namespace, pod, "GET", "/rest/settings")) != {"status": 200, "json": True}:
        assert time.monotonic() < deadline, f"n8n REST API never became ready (last {resp})"
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


# --- Omnigent ----------------------------------------------------------------

# 在 server 容器內打 omnigent 自己的 HTTP API（不經 Service／Ingress）。argv[1]=動作、
# argv[2]=cookie 檔；stdin＝密碼。cookie 檔讓「reset 前登入的 session 還活著」可驗。
_OMNI_HTTP = """
import http.cookiejar, json, sys, urllib.error, urllib.request
action, jar_path = sys.argv[1], sys.argv[2]
jar = http.cookiejar.MozillaCookieJar(jar_path)
try:
    jar.load(ignore_discard=True, ignore_expires=True)
except OSError:
    pass
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
if action == "login":
    req = urllib.request.Request(
        "http://localhost:8000/auth/login", method="POST",
        data=json.dumps({"username": "admin", "password": sys.stdin.read()}).encode(),
        headers={"Content-Type": "application/json"})
else:
    req = urllib.request.Request("http://localhost:8000/auth/me")
try:
    status = opener.open(req, timeout=30).status
except urllib.error.HTTPError as exc:
    status = exc.code
jar.save(ignore_discard=True, ignore_expires=True)
print(status)
"""

# 直接讀寫 omnigent 的 users 表：取 hash（驗「拒絕時一個 bit 都不動」）、拿掉管理員角色。
_OMNI_STORE = """
import json, os, sys
from omnigent.server.accounts_store import SqlAlchemyAccountStore
store = SqlAlchemyAccountStore(os.environ["DATABASE_URL"])
if sys.argv[1] == "demote":
    store.set_admin("admin", False)
user = store.get_user("admin")
print(json.dumps({"hash": store.get_password_hash("admin"), "is_admin": bool(user and user.is_admin)}))
"""


def _deploy_omnigent(namespace, release, db, admin_password):
    """chart（woow-paas-charts omnigent）server 容器的同一組 env／label／容器名。"""
    if db == "postgres":
        database_url = "postgresql://omnigent:omni-int-1370@localhost:5432/omnigent"
        pg = f"""  - name: postgresql
    image: {POSTGRES_IMAGE}
    env:
    - {{name: POSTGRES_USER, value: omnigent}}
    - {{name: POSTGRES_PASSWORD, value: omni-int-1370}}
    - {{name: POSTGRES_DB, value: omnigent}}
"""
    else:
        database_url, pg = "sqlite:////data/omnigent.db", ""
    # 測試用一次性密碼直接放 env（chart 用 secretKeyRef，容器內看到的是同一組變數）；
    # 不覆寫 command——跑 image 自己的 entrypoint。PG 還沒起來時 server 會 crash 重啟，
    # readinessProbe 等到它連上為止。
    _apply(f"""
apiVersion: v1
kind: Pod
metadata:
  name: {release}
  namespace: {namespace}
  labels:
    app.kubernetes.io/instance: {release}
    app.kubernetes.io/name: omnigent
    app.kubernetes.io/component: server
spec:
{_node_selector_yaml(2)}  containers:
{pg}  - name: server
    image: {OMNIGENT_IMAGE}
    env:
    - {{name: PORT, value: "8000"}}
    - {{name: HOST, value: "0.0.0.0"}}
    - {{name: HOME, value: /data/home}}
    - {{name: DATABASE_URL, value: {json.dumps(database_url)}}}
    - {{name: ARTIFACT_DIR, value: /data/artifacts}}
    - {{name: OMNIGENT_AUTH_PROVIDER, value: accounts}}
    - {{name: OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME, value: admin}}
    - {{name: OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD, value: {json.dumps(admin_password)}}}
    - {{name: OMNIGENT_ACCOUNTS_COOKIE_SECRET, value: {json.dumps(secrets.token_hex(32))}}}
    readinessProbe:
      httpGet: {{path: /health, port: 8000}}
      periodSeconds: 5
      failureThreshold: 120
    volumeMounts:
    - {{name: data, mountPath: /data}}
  volumes:
  - {{name: data, emptyDir: {{}}}}
""")
    _wait_ready(namespace, f"app.kubernetes.io/instance={release}", timeout=600)
    return _pod_name(namespace, f"app.kubernetes.io/instance={release}")


def _omni_http(namespace, pod, action, jar, password=""):
    out = _exec(namespace, pod, "server", ["python", "-c", _OMNI_HTTP, action, jar], stdin=password)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip().splitlines()[-1])


def _omni_store(namespace, pod, action="read"):
    out = _exec(namespace, pod, "server", ["python", "-c", _OMNI_STORE, action])
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("db", ["sqlite", "postgres"])
def test_omnigent_admin_reset_logs_in_and_keeps_sessions(app_client, test_namespace, db):
    release = f"omni-{db[:2]}"
    old_password = _password()
    pod = _deploy_omnigent(test_namespace, release, db, old_password)
    # reset 前登入一次、留下 session cookie（與上游 Members → Reset 一樣，不該被登出）。
    assert _omni_http(test_namespace, pod, "login", "/tmp/before.jar", old_password) == 200
    assert _omni_http(test_namespace, pod, "me", "/tmp/before.jar") == 200

    started = time.monotonic()
    new_password = _password()
    resp = app_client.post(f"/api/releases/{test_namespace}/{release}/omnigent/admin-password",
                           json={"new_password": new_password})
    elapsed = time.monotonic() - started

    body = resp.json()
    assert resp.status_code == 200 and body["success"] is True, resp.text
    assert body["login"] == "admin"
    assert body["second_factor_cleared"] is False
    assert new_password not in resp.text
    # 不重啟：同一顆 pod 立刻用新密碼登得進去、舊的不行；reset 前的 session 仍有效。
    assert _pod_name(test_namespace, f"app.kubernetes.io/instance={release}") == pod
    assert _omni_http(test_namespace, pod, "login", "/tmp/after.jar", new_password) == 200
    assert _omni_http(test_namespace, pod, "login", "/tmp/stale.jar", old_password) == 401
    assert _omni_http(test_namespace, pod, "me", "/tmp/before.jar") == 200
    # 預算：exec 逾時 30s（OMNIGENT_RESET_EXEC_TIMEOUT_SECONDS），實測要留明顯餘裕。
    assert elapsed < 25, elapsed

    # 被拿掉管理員角色：fail-loud（admin_inactive）、密碼雜湊一個 bit 都不動。
    demoted = _omni_store(test_namespace, pod, "demote")
    assert demoted["is_admin"] is False
    resp = app_client.post(f"/api/releases/{test_namespace}/{release}/omnigent/admin-password",
                           json={"new_password": _password()})
    body = resp.json()
    assert (body["success"], body["error_kind"], body["error_code"]) == (False, "failed", "admin_inactive"), resp.text
    assert _omni_store(test_namespace, pod)["hash"] == demoted["hash"]


def test_reset_without_pod_is_not_executed(app_client, test_namespace):
    resp = app_client.post(f"/api/releases/{test_namespace}/nothing-here/odoo/admin-password",
                           json={"new_password": _password()})
    body = resp.json()
    assert (body["success"], body["error_kind"], body["error_step"]) == (False, "not_executed", "find_pod"), resp.text

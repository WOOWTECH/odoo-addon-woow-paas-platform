"""#1534 integration：租戶改了 owner 密碼／開了 MFA／刪了 init 的 key 之後，``/init/n8n`` 仍成功。

真 n8n（預設 2.35.3＝平台 seed 釘的版本）、真 kubectl、真 FastAPI。unit 測試驗不到的是
``n8n_scripts/ensure_api_key.js`` 綁的 n8n 內部契約：``user``／``user_api_keys``／
``deployment_key`` 表、簽章密鑰來源、JWT 形狀、公開 API 的驗證——只在真容器裡現形。

情境（同一顆 n8n 依序）：
1. 首次 init（owner 尚未建立）：建 owner、登入、建 key——舊路徑不變。
2. 租戶在 n8n 改密碼 ⇒ 平台存的那組登入 401；init（已確認）仍成功，沿用同一把 key。
3. 租戶在 n8n 刪掉 init 的 key ⇒ init 簽一把新的，公開 API 認得它。
4. 開 MFA（密碼正確也登不進去，998）⇒ init 仍成功。
5. 平台沒確認過 owner：Email 不符 ⇒ ``owner_identity``／failed、不發 key；大小寫不同視為同一人。
sqlite（預設）與 postgres 各跑一次。

本地跑（無 CI runner；與其他 integration 同一套 recipe）::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- \\
    OPERATOR_TEST_NODE_NAME=<避開正式站的 node> \\
    pytest -m integration tests/integration/test_n8n_init_owner_changed_1534.py -v

image 可用 ``OPERATOR_TEST_N8N_1534_IMAGE`` 覆寫；n8n 升版時必跑。
"""
import base64
import json
import os
import subprocess
import time

import pytest

from tests.integration import test_app_admin_password_reset as reset_it

pytestmark = pytest.mark.integration

N8N_IMAGE = os.environ.get("OPERATOR_TEST_N8N_1534_IMAGE", "docker.io/n8nio/n8n:2.35.3")
OWNER_EMAIL = reset_it.OWNER_EMAIL
KEY_LABEL = "mcp-sidecar"

# 在 n8n 容器內以租戶身分操作：登入拿 cookie 後打第二個請求。參數走 stdin，輸出只有狀態碼。
_TENANT = """
const http=require("http");let s="";process.stdin.on("data",c=>s+=c).on("end",async()=>{
const a=JSON.parse(s);
const req=(method,path,body,cookie,extra)=>new Promise(r=>{const d=body?JSON.stringify(body):"";
const h=Object.assign({"Content-Type":"application/json","Content-Length":Buffer.byteLength(d),"browser-id":"it-1534"},extra||{});
if(cookie)h.Cookie=cookie;const q=http.request({hostname:"localhost",port:5678,path,method,headers:h},res=>{let b="";
res.on("data",c=>b+=c).on("end",()=>{let ck=null;for(const c of res.headers["set-cookie"]||[])if(c.startsWith("n8n-auth="))ck=c.split(";")[0];
r({status:res.statusCode,body:b,cookie:ck})})});q.on("error",()=>r({status:0,body:""}));q.write(d);q.end();});
const out={};
if(a.action==="apikey"){out.status=(await req("GET","/api/v1/workflows?limit=1",null,null,{"X-N8N-API-KEY":a.key})).status;}
else{const l=await req("POST","/rest/login",{emailOrLdapLoginId:a.email,password:a.password});out.login=l.status;
if(l.cookie&&a.action==="change-password"){out.status=(await req("PATCH","/rest/me/password",{currentPassword:a.password,newPassword:a.new_password},l.cookie)).status;}
if(l.cookie&&a.action==="delete-key"){const k=JSON.parse((await req("GET","/rest/api-keys",null,l.cookie)).body);
const items=(k.data&&k.data.items)||k.data||[];out.deleted=0;for(const i of items)if(i.label===a.label){
if((await req("DELETE","/rest/api-keys/"+i.id,null,l.cookie)).status===200)out.deleted++;}}}
console.log(JSON.stringify(out));});
"""


def _tenant(namespace, pod, **payload):
    out = reset_it._exec(namespace, pod, "n8n", ["node", "-e", _TENANT], stdin=json.dumps(payload))
    return json.loads(out.stdout.strip().splitlines()[-1])


def _secret_key(namespace, release):
    raw = subprocess.check_output(
        ["kubectl", "get", "secret", f"{release}-n8n-app-secret", "-n", namespace,
         "-o", "jsonpath={.data.N8N_API_KEY}"], text=True)
    return base64.b64decode(raw).decode()


def _make_app_secret(namespace, release):
    """8gears chart 建的 ``<release>-n8n-app-secret``（init 找它、把 key 寫進去）。"""
    reset_it._apply(f"""
apiVersion: v1
kind: Secret
metadata:
  name: {release}-n8n-app-secret
  namespace: {namespace}
  labels: {{app.kubernetes.io/instance: {release}}}
stringData: {{N8N_API_KEY: set-by-platform-init}}
""")


def _wait_owner_setup_route(namespace, pod, timeout=180):
    """``/rest/settings`` 回 200 時 owner setup 的路由不一定已註冊（postgres 實測先回 404 或
    非 JSON，同 test_app_admin_password_reset 的註解）。用空 body 打它：路由在了會回 400。
    首次 init 的 owner setup 不是本單範圍，這裡只讓前置條件穩定。"""
    deadline = time.monotonic() + timeout
    while (status := reset_it._n8n_http(namespace, pod, "POST", "/rest/owner/setup", {})) != 400:
        assert time.monotonic() < deadline, f"n8n owner setup route never became ready (last {status})"
        time.sleep(3)


def _init(app_client, namespace, release, password, **extra):
    resp = app_client.post(f"/api/releases/{namespace}/{release}/init/n8n", json={
        "owner_email": extra.pop("email", OWNER_EMAIL), "owner_password": password, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("db", ["sqlite", "postgres"])
def test_init_survives_tenant_owner_changes(app_client, test_namespace, db, monkeypatch):
    monkeypatch.setattr(reset_it, "N8N_IMAGE", N8N_IMAGE)
    release = f"n8n-{db[:2]}"
    _make_app_secret(test_namespace, release)
    pod = reset_it._deploy_n8n(test_namespace, release, db)
    _wait_owner_setup_route(test_namespace, pod)
    platform_password = reset_it._password()

    # 1. 首次 init：owner 尚未建立 ⇒ 建 owner、登入、建 key（不變的舊路徑）。
    body = _init(app_client, test_namespace, release, platform_password)
    assert body["success"] is True, body
    first_key = _secret_key(test_namespace, release)
    assert first_key == body["api_key"] and first_key != "set-by-platform-init"
    assert _tenant(test_namespace, pod, action="apikey", key=first_key)["status"] == 200

    # 2. 租戶改密碼 ⇒ 平台那組登入 401；init（已確認）不登入、沿用同一把 key。
    tenant_password = reset_it._password()
    changed = _tenant(test_namespace, pod, action="change-password", email=OWNER_EMAIL,
                      password=platform_password, new_password=tenant_password)
    assert changed == {"login": 200, "status": 200}, changed
    assert reset_it._n8n_login(test_namespace, pod, platform_password) == 401
    body = _init(app_client, test_namespace, release, platform_password, owner_confirmed=True)
    assert body["success"] is True, body
    assert body["api_key"] == first_key, "key 還能用就沿用，不必每次 Redeploy 都換"
    assert platform_password not in json.dumps(body)

    # 3. 租戶刪掉 init 的 key ⇒ init 簽一把新的，n8n 自己的公開 API 認得它。
    deleted = _tenant(test_namespace, pod, action="delete-key", email=OWNER_EMAIL,
                      password=tenant_password, label=KEY_LABEL)
    assert deleted == {"login": 200, "deleted": 1}, deleted
    assert _tenant(test_namespace, pod, action="apikey", key=first_key)["status"] == 401
    body = _init(app_client, test_namespace, release, platform_password, owner_confirmed=True)
    assert body["success"] is True, body
    second_key = body["api_key"]
    assert second_key != first_key and second_key == _secret_key(test_namespace, release)
    assert _tenant(test_namespace, pod, action="apikey", key=second_key)["status"] == 200

    # 4. 開 MFA ⇒ 連正確密碼都登不進去（998）；init 仍成功、沿用 key。
    reset_it._enable_fake_n8n_mfa(test_namespace, pod, db)
    assert reset_it._n8n_login(test_namespace, pod, tenant_password) == 401
    body = _init(app_client, test_namespace, release, platform_password, owner_confirmed=True)
    assert body["success"] is True, body
    assert body["api_key"] == second_key

    # 5. 平台沒確認過 owner：Email 必須是開通時那個（大小寫不計），否則不發 key。
    body = _init(app_client, test_namespace, release, platform_password, email="intruder@example.com")
    assert (body["success"], body["error_step"], body["error_kind"]) == (False, "owner_identity", "failed"), body
    assert body["api_key"] is None
    assert OWNER_EMAIL not in json.dumps(body), "失敗訊息不回傳實際的 owner Email"
    body = _init(app_client, test_namespace, release, platform_password, email=OWNER_EMAIL.upper())
    assert body["success"] is True and body["api_key"] == second_key, body

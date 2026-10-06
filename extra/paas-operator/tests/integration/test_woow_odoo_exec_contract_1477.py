"""#1477／#1186：真 woow-odoo chart 上的 exec 端點（module-states、odoo/admin-password）。

#1370 的 ``test_app_admin_password_reset.py`` 用手刻 Pod（label 照抄、PG 同 Pod），
驗得到 app 內部契約，但驗不到「operator 對真 chart 的 Pod 定位」——#1477 就是在這裡
漏網（``odoo_module.py`` 還在找 Bitnami 的容器名與 conf）。這支用**真 chart** 部署：

- ``module-states``：``component=server`` 的 Pod、容器 ``odoo``、``/etc/odoo/odoo.conf``
  （chart 的 conf Secret 含 ``db_*``）；勾選的 ``mcp_server`` 應為 ``uninstalled``。
- ``odoo/admin-password``：改 chart bootstrap 建的 ``base.user_admin``，新密碼立即可登入、
  舊密碼被拒、TOTP 清掉；封存後 fail-loud 且雜湊不動。另記 exec 實測耗時（逾時階梯）。

需要的環境變數（沒設 ``WOOW_ODOO_CHART`` 就 skip——**skip 不是通過**）：

- ``WOOW_ODOO_CHART``：woow-odoo chart 目錄（例：woow-paas-charts 上
  ``git archive <0.2.7 commit> charts/woow-odoo | tar x``）。
- ``OPERATOR_TEST_PULL_SECRET_SOURCE``（選填）：``<namespace>/<secret>``，唯讀複製進測試
  namespace 並掛成 chart 的 ``imagePullSecrets``（cloud-odoo 在私有 registry）。
  沒設時只能靠 node 上的 image cache。
- ``OPERATOR_TEST_ODOO_IMAGE_TAG``／``OPERATOR_TEST_ODOO_IMAGE_DIGEST``：預設即平台
  seed（``src/data/cloud_app_templates.xml``）釘的 cloud-odoo。

本地跑::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-<topic>- \\
    WOOW_ODOO_CHART=<chart 目錄> HELM_BINARY=<helm 3.13.3> \\
    pytest -m integration tests/integration/test_woow_odoo_exec_contract_1477.py -v -s
"""
import json
import os
import secrets
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

_RELEASE = "svc-1477"
_DB = "odoo"  # chart 預設 postgres.database
_ADMIN_LOGIN = "admin-1186@example.com"
ODOO_IMAGE_TAG = os.environ.get("OPERATOR_TEST_ODOO_IMAGE_TAG", "20260929.0134")
ODOO_IMAGE_DIGEST = os.environ.get(
    "OPERATOR_TEST_ODOO_IMAGE_DIGEST",
    "sha256:8f984434b810c0ebeeea54c460167b8f487ece5ff87286f334c4fd47f43e5cd3",
)
_SERVER = f"app.kubernetes.io/instance={_RELEASE},app.kubernetes.io/component=server"


def _kubectl(*args, stdin=None, timeout=60):
    return subprocess.run(
        ["kubectl", *args], check=True, capture_output=True, text=True,
        input=stdin, timeout=timeout,
    ).stdout


def _password():
    return secrets.token_urlsafe(18)


def _copy_pull_secret(namespace):
    """``OPERATOR_TEST_PULL_SECRET_SOURCE`` 唯讀複製進測試 ns；secret 內容只走 stdin。"""
    source = os.environ.get("OPERATOR_TEST_PULL_SECRET_SOURCE")
    if not source:
        return None
    src_ns, name = source.split("/", 1)
    secret = json.loads(_kubectl("get", "secret", name, "-n", src_ns, "-o", "json"))
    copied = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": secret["type"],
        "metadata": {"name": name, "namespace": namespace},
        "data": secret["data"],
    }
    _kubectl("apply", "-n", namespace, "-f", "-", stdin=json.dumps(copied))
    return name


def _install_chart(namespace, admin_password):
    values = {
        "image": {"tag": ODOO_IMAGE_TAG, "digest": ODOO_IMAGE_DIGEST},
        "conf": {"adminEmail": _ADMIN_LOGIN, "adminPassword": admin_password, "workers": 0},
        "addons": {"selected": ["mcp_server"]},
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
        [os.environ["HELM_BINARY"], "install", _RELEASE, os.environ["WOOW_ODOO_CHART"],
         "--namespace", namespace, "-f", "-"],
        check=True, capture_output=True, text=True, input=json.dumps(values), timeout=120,
    )


def _wait_server_ready(namespace, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = json.loads(_kubectl("get", "pods", "-n", namespace, "-l", _SERVER, "-o", "json"))["items"]
        for pod in items:
            conditions = pod.get("status", {}).get("conditions", [])
            if any(c["type"] == "Ready" and c["status"] == "True" for c in conditions):
                return pod["metadata"]["name"]
        time.sleep(5)
    raise AssertionError(f"{timeout}s 內 woow-odoo server 沒有 Ready")


def _exec_odoo(namespace, pod, command, stdin="", timeout=300):
    return _kubectl("exec", pod, "-n", namespace, "-c", "odoo", "-i", "--", *command,
                    stdin=stdin, timeout=timeout)


_ODOO_ORM = """
import json, sys, odoo
odoo.tools.config.parse_config(["-c", "/etc/odoo/odoo.conf", "--no-http"])
with odoo.registry(odoo.tools.config["db_name"]).cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    admin = env.ref("base.user_admin").with_context(active_test=False)
    action = sys.argv[1]
    if action == "totp_on":
        admin.write({"totp_secret": "JBSWY3DPEHPK3PXP"})
    elif action == "archive":
        admin.write({"active": False})
    cr.execute("SELECT password FROM res_users WHERE id = %s", [admin.id])
    print(json.dumps({"hash": cr.fetchone()[0], "totp": bool(admin.totp_secret),
                      "active": admin.active, "login": admin.login}))
"""

_ODOO_AUTH = """
import sys, xmlrpc.client
uid = xmlrpc.client.ServerProxy("http://localhost:8069/xmlrpc/2/common").authenticate(
    sys.argv[1], sys.argv[2], sys.stdin.read(), {})
print("OK" if uid else "DENIED")
"""


def _odoo_orm(namespace, pod, action):
    out = _exec_odoo(namespace, pod, ["python3", "-c", _ODOO_ORM, action])
    return json.loads(out.strip().splitlines()[-1])


def _odoo_auth(namespace, pod, password):
    out = _exec_odoo(namespace, pod, ["python3", "-c", _ODOO_AUTH, _DB, _ADMIN_LOGIN], stdin=password)
    return out.strip().splitlines()[-1]


def test_exec_endpoints_on_real_woow_odoo_chart(app_client, test_namespace):
    if not os.environ.get("WOOW_ODOO_CHART"):
        pytest.skip("WOOW_ODOO_CHART 未設定：需要真 woow-odoo chart 目錄")

    initial_password = _password()
    _install_chart(test_namespace, initial_password)
    pod = _wait_server_ready(test_namespace)

    # --- #1477 module-states -------------------------------------------------
    resp = app_client.post(f"/api/odoo/{test_namespace}/{_RELEASE}/module-states",
                           json={"module_names": ["mcp_server", "base"]})
    body = resp.json()
    assert resp.status_code == 200 and body["success"] is True, resp.text
    assert body["modules"]["base"]["state"] == "installed"
    assert body["modules"]["mcp_server"]["state"] == "uninstalled", body

    # --- #1186 odoo/admin-password ------------------------------------------
    assert _odoo_auth(test_namespace, pod, initial_password) == "OK"
    # chart bootstrap 只 -i base；auth_totp 另外裝（與租戶在 Odoo 內開 2FA 的前提相同）。
    _exec_odoo(test_namespace, pod, [
        "odoo", "-c", "/etc/odoo/odoo.conf", "-d", _DB, "-i", "auth_totp",
        "--stop-after-init", "--no-http",
    ], timeout=600)
    assert _odoo_orm(test_namespace, pod, "totp_on")["totp"] is True
    # 開 2FA 後 XML-RPC 不再接受密碼登入；reset 後又能登入＝TOTP 真的清掉。
    assert _odoo_auth(test_namespace, pod, initial_password) == "DENIED"

    new_password = _password()
    started = time.monotonic()
    resp = app_client.post(f"/api/releases/{test_namespace}/{_RELEASE}/odoo/admin-password",
                           json={"new_password": new_password})
    elapsed = time.monotonic() - started
    print(f"\n[#1186] odoo/admin-password on woow-odoo took {elapsed:.1f}s")

    body = resp.json()
    assert resp.status_code == 200 and body["success"] is True, resp.text
    assert body["login"] == _ADMIN_LOGIN
    assert body["second_factor_cleared"] is True
    assert new_password not in resp.text
    assert _odoo_orm(test_namespace, pod, "read")["totp"] is False
    assert _odoo_auth(test_namespace, pod, new_password) == "OK"
    assert _odoo_auth(test_namespace, pod, initial_password) == "DENIED"
    # exec 逾時 45s（app_admin_password.ODOO_RESET_EXEC_TIMEOUT_SECONDS）要留餘裕。
    assert elapsed < 40, elapsed

    archived = _odoo_orm(test_namespace, pod, "archive")
    resp = app_client.post(f"/api/releases/{test_namespace}/{_RELEASE}/odoo/admin-password",
                           json={"new_password": _password()})
    body = resp.json()
    assert (body["success"], body["error_kind"], body["error_code"]) == (False, "failed", "admin_inactive"), resp.text
    assert _odoo_orm(test_namespace, pod, "read")["hash"] == archived["hash"]

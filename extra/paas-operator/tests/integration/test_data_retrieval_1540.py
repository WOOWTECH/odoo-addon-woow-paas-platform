"""woow_paas_platform #1540 integration：取回入口對真叢集。

單元測試把 kubectl mock 掉，看不到的那幾格：

* 產生的 Pod 在真的 API server／kubelet 上跑得起來（restricted securityContext、唯讀根目錄、
  nginx 設定語法、initContainer 的 symlink、uid 33 讀得到 0770 的資料目錄）；
* 唯讀是真的：GET 拿得到檔案、PUT／DELETE／POST 被拒、容器裡寫 ``/data`` 是 Read-only file system；
* basic auth 真的擋：沒帶／帶錯密碼 401；
* ``activeDeadlineSeconds`` 真的會讓 kubelet 停掉 pod；
* 關閉後 Pod／Service／Secret／ConfigMap／NetworkPolicy 都消失。

Cloudflare 一律假在 httpx 的 GET／PUT（integration 不受單元的 outbound 守線保護，所以這裡**一定**
要自己 patch；任何真的 CF 呼叫都會打到不該打的 tunnel）。

會建 PVC（Longhorn）與兩個小 pod；``DATA_RETRIEVAL_TEST_EXCLUDED_NODES``（預設
``ubuntuserver-59mqj``）讓所有 pod 避開正式站主機。

Run::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-r1540- KUBECONFIG=<kubeconfig> \
        pytest -m integration tests/integration/test_data_retrieval_1540.py -v
"""
import base64
import copy
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api import retrieval
from src.config import settings
from src.services import data_retrieval as dr
from tests.integration.conftest import _apply_manifest
from tests.namespace_scope import integration_storage_class

pytestmark = pytest.mark.integration

RELEASE = "svc-int1540"
PVC = f"{RELEASE}-nextcloud-data"
IMAGE = settings.data_retrieval_image
EXCLUDED = [n for n in os.environ.get(
    "DATA_RETRIEVAL_TEST_EXCLUDED_NODES", "ubuntuserver-59mqj").split(",") if n]

FILES = {
    "alice/files/hello.txt": "hello from alice\n",
    "alice/files/中文 資料夾/報告.txt": "中文內容\n",
    "bob/files/sub/b.txt": "bob\n",
    "appdata_oc123/files/internal.txt": "must not be exported\n",
    "alice/files_trashbin/files/deleted.txt": "not exported\n",
}


def _kubectl(*args, check=True, input_data=None, timeout=60):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=check,
                          input=input_data, timeout=timeout)


def _affinity_yaml(indent):
    if not EXCLUDED:
        return ""
    pad = " " * indent
    values = ", ".join(EXCLUDED)
    return (f"{pad}affinity:\n{pad}  nodeAffinity:\n"
            f"{pad}    requiredDuringSchedulingIgnoredDuringExecution:\n"
            f"{pad}      nodeSelectorTerms:\n{pad}      - matchExpressions:\n"
            f"{pad}        - {{key: kubernetes.io/hostname, operator: NotIn, values: [{values}]}}\n")


def _seed_pvc(namespace, labels):
    """建一顆像 Nextcloud 資料 PVC 的 volume，放檔案、chown 33、目錄 0770（同 chart 的 fix-data-perms）。"""
    pvc = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": PVC, "namespace": namespace, "labels": {
            "app.kubernetes.io/instance": RELEASE, "app.kubernetes.io/component": "app",
            **labels}},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": "1Gi"}}, **integration_storage_class()},
    }
    _kubectl("apply", "-f", "-", input_data=json.dumps(pvc))
    script = ["set -e", "cd /data"]
    for path, content in FILES.items():
        b64 = base64.b64encode(content.encode()).decode()
        script.append(f'mkdir -p "$(dirname "{path}")" && echo {b64} | base64 -d > "{path}"')
    script += ["chown -R 33:33 /data", "chmod 0770 /data", "echo seeded"]
    _apply_manifest(f"""
apiVersion: v1
kind: Pod
metadata:
  name: seed
  namespace: {namespace}
spec:
  restartPolicy: Never
{_affinity_yaml(2)}  containers:
  - name: seed
    image: {IMAGE}
    command: ["/bin/sh", "-c", {json.dumps(chr(10).join(script))}]
    securityContext:
      runAsUser: 0
    resources:
      requests: {{cpu: 10m, memory: 16Mi}}
      limits: {{cpu: 200m, memory: 64Mi}}
    volumeMounts:
    - {{name: data, mountPath: /data}}
  volumes:
  - name: data
    persistentVolumeClaim: {{claimName: {PVC}}}
""")
    deadline = time.time() + 300
    phase = ""
    while time.time() < deadline:
        phase = _kubectl("get", "pod", "seed", "-n", namespace, "-o",
                         "jsonpath={.status.phase}", check=False).stdout
        if phase in ("Succeeded", "Failed"):
            break
        time.sleep(3)
    assert phase == "Succeeded", _kubectl("logs", "seed", "-n", namespace, check=False).stdout
    _kubectl("delete", "pod", "seed", "-n", namespace, "--wait=true", timeout=120)


class _FakeTunnel:
    def __init__(self):
        self.ingress = [{"hostname": "other.woowtech.io",
                         "service": "http://o.paas-ws-other.svc.cluster.local:80"},
                        {"service": "http_status:404"}]
        self.puts = []

    @staticmethod
    def _resp(body):
        resp = MagicMock(status_code=200, text="ok")
        resp.json.return_value = body
        return resp

    async def get(self, url, headers=None, timeout=None, params=None):
        assert url.endswith("/configurations"), url
        return self._resp({"success": True,
                           "result": {"config": {"ingress": copy.deepcopy(self.ingress)}}})

    async def put(self, url, headers=None, json=None, timeout=None):
        assert url.endswith("/configurations"), url
        self.puts.append(copy.deepcopy(json))
        self.ingress = copy.deepcopy(json["config"]["ingress"])
        return self._resp({"success": True})


@pytest.fixture
def tunnel():
    fake = _FakeTunnel()
    svc = retrieval.cloudflare_service
    with patch.object(svc, "enabled", True), \
            patch.object(svc, "dns_enabled", False), \
            patch.object(svc, "domain", "woowtech.io"), \
            patch.object(svc, "account_id", "acc-int"), \
            patch.object(svc, "tunnel_id", "tun-int"), \
            patch.object(svc, "api_token", "fake-token"), \
            patch.object(settings, "data_retrieval_excluded_nodes", ",".join(EXCLUDED)), \
            patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake.get)), \
            patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake.put)), \
            patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=AssertionError("no CF POST"))), \
            patch("httpx.AsyncClient.delete", new=AsyncMock(side_effect=AssertionError("no CF DELETE"))):
        yield fake


def _wait_ready(app_client, namespace, timeout=300):
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = app_client.get(f"/api/releases/{namespace}/{RELEASE}/retrieval").json()
        if body.get("status") == "active":
            return body
        time.sleep(3)
    pods = _kubectl("describe", "pod", "-n", namespace, "-l", dr.label_selector(RELEASE),
                    check=False).stdout
    raise AssertionError(f"retrieval pod never became ready: {body}\n{pods[-3000:]}")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _PortForward:
    def __init__(self, namespace):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            ["kubectl", "port-forward", "-n", namespace, f"pod/{dr.resource_name(RELEASE)}",
             f"{self.port}:8080"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.5)
        raise AssertionError("port-forward did not come up")

    def request(self, path, method="GET", auth=None, data=None):
        url = f"http://127.0.0.1:{self.port}{urllib.request.quote(path)}"
        req = urllib.request.Request(url, method=method, data=data)
        if auth:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)


def _leftovers(namespace):
    return _kubectl("get", "pods,services,secrets,configmaps,networkpolicies", "-n", namespace,
                    "-l", dr.label_selector(RELEASE), "-o", "name", check=False).stdout.split()


def _wait_gone(namespace, timeout=90):
    deadline = time.time() + timeout
    left = _leftovers(namespace)
    while left and time.time() < deadline:
        time.sleep(3)
        left = _leftovers(namespace)
    return left


def test_deleted_service_round_trip(app_client, test_namespace, tunnel):
    """刪除後（孤兒 PVC 保留中）：開 → basic auth 列目錄、下載 → 唯讀 → 關 → 全部消失。"""
    orphaned_at = int(time.time()) - 3600
    _seed_pvc(test_namespace, {dr.ORPHANED_AT_LABEL: str(orphaned_at),
                               dr.GRACE_PERIOD_DAYS_LABEL: "7",
                               "paas.woowtech.io/release-name": RELEASE})

    opened = app_client.post(f"/api/releases/{test_namespace}/{RELEASE}/retrieval",
                             json={"profile": "nextcloud", "ttl_hours": 2})
    assert opened.status_code == 201, opened.text
    body = opened.json()
    password = body["password"]
    assert body["route"] == "created" and body["pvc"] == PVC
    svc_url = f"http://{dr.resource_name(RELEASE)}.{test_namespace}.svc.cluster.local:8080"
    assert [r["service"] for r in tunnel.ingress if r.get("hostname") == body["hostname"]] == [
        svc_url]

    # 第二次開 ⇒ 409（同一台只能有一個入口）。
    again = app_client.post(f"/api/releases/{test_namespace}/{RELEASE}/retrieval",
                            json={"profile": "nextcloud"})
    assert again.status_code == 409
    assert again.json()["detail"]["error_code"] == "retrieval_already_open"

    _wait_ready(app_client, test_namespace)
    pod = json.loads(_kubectl("get", "pod", dr.resource_name(RELEASE), "-n", test_namespace,
                              "-o", "json").stdout)
    assert pod["spec"]["activeDeadlineSeconds"] <= 2 * 3600
    assert pod["spec"]["nodeName"] not in EXCLUDED

    pf = _PortForward(test_namespace)
    try:
        auth = ("download", password)
        assert pf.request("/")[0] == 401
        assert pf.request("/", auth=("download", "wrong-password"))[0] == 401
        status, listing, headers = pf.request("/", auth=auth)
        assert status == 200
        assert "alice/" in listing and "bob/" in listing
        assert "appdata" not in listing and "files_trashbin" not in listing
        assert headers.get("Cache-Control") == "no-store"
        assert headers.get("Content-Security-Policy") == "sandbox"
        assert pf.request("/alice/hello.txt", auth=auth)[:2] == (200, "hello from alice\n")
        assert pf.request("/alice/中文 資料夾/報告.txt", auth=auth)[:2] == (200, "中文內容\n")
        assert pf.request("/bob/sub/b.txt", auth=auth)[:2] == (200, "bob\n")
        assert pf.request("/appdata_oc123/internal.txt", auth=auth)[0] == 404
        # 唯讀：寫入類 method 一律被拒。
        for method in ("PUT", "DELETE", "POST", "MKCOL"):
            code = pf.request("/alice/new.txt", method=method, auth=auth, data=b"x")[0]
            assert code in (403, 405), (method, code)
        assert pf.request("/alice/new.txt", auth=auth)[0] == 404
    finally:
        pf.close()

    # 容器裡直接寫也不行（volume 唯讀＋根目錄唯讀）。
    touch = _kubectl("exec", "-n", test_namespace, dr.resource_name(RELEASE), "-c", "nginx", "--",
                     "sh", "-c", "touch /data/alice/files/x", check=False)
    assert touch.returncode != 0 and "Read-only file system" in touch.stderr, touch.stderr
    touch_root = _kubectl("exec", "-n", test_namespace, dr.resource_name(RELEASE), "-c", "nginx",
                          "--", "sh", "-c", "touch /etc/x", check=False)
    assert touch_root.returncode != 0

    closed = app_client.delete(f"/api/releases/{test_namespace}/{RELEASE}/retrieval")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed" and closed.json()["route"] == "deleted"
    assert not [r for r in tunnel.ingress if r.get("hostname") == body["hostname"]]
    assert _wait_gone(test_namespace) == []
    status = app_client.get(f"/api/releases/{test_namespace}/{RELEASE}/retrieval").json()
    assert status["status"] == "not_found"


def test_suspended_service_and_running_guard(app_client, test_namespace, tunnel):
    """停權中（release 還在、workload 0）可開；workload 還在跑、或有 pod 掛著 PVC 則拒絕。"""
    _seed_pvc(test_namespace, {})
    # 假的 helm release 狀態 Secret＋縮到 0 的 Deployment（與 pvc-cleanup／operator 同一判準）。
    _kubectl("create", "secret", "generic", f"sh.helm.release.v1.{RELEASE}.v1", "-n",
             test_namespace)
    _kubectl("label", "secret", f"sh.helm.release.v1.{RELEASE}.v1", "-n", test_namespace,
             "owner=helm", f"name={RELEASE}")
    _apply_manifest(f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {RELEASE}-nextcloud
  namespace: {test_namespace}
  labels: {{app.kubernetes.io/instance: {RELEASE}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: holder}}}}
  template:
    metadata: {{labels: {{app: holder}}}}
    spec:
{_affinity_yaml(6)}      containers:
      - name: holder
        image: {IMAGE}
        command: ["sleep", "3600"]
        resources: {{requests: {{cpu: 10m, memory: 16Mi}}, limits: {{cpu: 100m, memory: 32Mi}}}}
        volumeMounts: [{{name: data, mountPath: /data}}]
      volumes: [{{name: data, persistentVolumeClaim: {{claimName: {PVC}}}}}]
""")
    url = f"/api/releases/{test_namespace}/{RELEASE}/retrieval"
    running = app_client.post(url, json={"profile": "nextcloud"})
    assert running.status_code == 409
    assert running.json()["detail"]["error_code"] == "retrieval_release_running"

    # 縮到 0 但 pod 還沒走完的那幾秒：仍然拒絕（RWO）。
    _kubectl("scale", "deployment", f"{RELEASE}-nextcloud", "-n", test_namespace, "--replicas=0")
    deadline = time.time() + 120
    while time.time() < deadline:
        pods = _kubectl("get", "pods", "-n", test_namespace, "-l", "app=holder", "-o", "name",
                        check=False).stdout.split()
        if not pods:
            break
        early = app_client.post(url, json={"profile": "nextcloud"})
        if early.status_code == 409:
            assert early.json()["detail"]["error_code"] == "retrieval_pvc_in_use"
        else:
            # pod 剛好在這兩次呼叫之間走完：已經開了，關掉再走正常流程。
            assert early.status_code == 201, early.text
            app_client.delete(url)
            _wait_gone(test_namespace)
        time.sleep(2)

    opened = app_client.post(url, json={"profile": "nextcloud"})
    assert opened.status_code == 201, opened.text
    assert opened.json()["data_expires_at"] is None
    _wait_ready(app_client, test_namespace)
    # 停權中的服務本身沒有被喚醒。
    replicas = _kubectl("get", "deployment", f"{RELEASE}-nextcloud", "-n", test_namespace, "-o",
                        "jsonpath={.spec.replicas}").stdout
    assert replicas == "0"

    # 到期：sweep 依 k8s 上的到期標註收掉（假裝時間過了 73 小時）。
    future = time.time() + 73 * 3600
    with patch("src.api.retrieval.time.time", return_value=future):
        swept = app_client.post("/api/retrievals/sweep")
    assert swept.status_code == 200, swept.text
    assert f"{test_namespace}/{RELEASE}" in swept.json()["closed"]
    assert _wait_gone(test_namespace) == []
    assert not [r for r in tunnel.ingress if r.get("hostname") == opened.json()["hostname"]]


def test_active_deadline_is_enforced_by_kubelet(app_client, test_namespace):
    """``activeDeadlineSeconds``：就算平台 cron 與 sweep 都沒跑，kubelet 也會準時停掉入口。"""
    _seed_pvc(test_namespace, {})
    now = int(time.time())
    manifests = dr.build_manifests(
        namespace=test_namespace, release=RELEASE, profile=dr.PROFILES["nextcloud"],
        pvc_name=PVC, htpasswd=dr.ssha_htpasswd_line("download", "x"), subdomain=None,
        opened_at=now, expires_at=now + 45, image=IMAGE, excluded_nodes=EXCLUDED,
    )
    retrieval.k8s_service.create_manifests(manifests)
    deadline = time.time() + 240
    status = {}
    while time.time() < deadline:
        status = json.loads(_kubectl("get", "pod", dr.resource_name(RELEASE), "-n",
                                     test_namespace, "-o", "json").stdout).get("status", {})
        if status.get("phase") == "Failed":
            break
        time.sleep(5)
    assert status.get("phase") == "Failed" and status.get("reason") == "DeadlineExceeded", status
    retrieval.k8s_service.delete_by_selector(
        retrieval.RETRIEVAL_KINDS, test_namespace, dr.label_selector(RELEASE))

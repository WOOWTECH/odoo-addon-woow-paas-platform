"""#1527 integration：租戶 ns 的 egress baseline 與 Pod Security 在真叢集上的效果。

unit 測試（``tests/test_ws_lan_baseline_1441.py``）只驗得到「組出來的 manifest 長什麼樣、
kubectl argv 對不對」。驗不到的是叢集真正的行為，而 #1527 要的正是行為：

- NetworkPolicy 由 CNI 執行（woow-k3s 是 kube-router，**在 kube-proxy DNAT 之後**比對）：
  連 Service ClusterIP 實際比對的是後端 pod／node IP。manifest 對、行為錯的情況只有真叢集看得到
  （例如只放 Service CIDR 卻漏了 DNAT 後的 node IP，#404 踩過）。
- PSA 由 API server 的 PodSecurity admission 執行：標籤字串差一個字就等於沒設，而且不會報錯。

每個測試都有**對照組**：同一個目標從「沒有任何 NetworkPolicy 的 ns」連得到，才把 baseline
ns 裡的「連不到」算成 baseline 擋的；公網對照組也連不到時（叢集沒有對外網路），公網那條斷言
改成 skip 並寫明原因，不會假綠也不會假紅。

連線只做 TCP connect（busybox ``nc -z``），不送任何資料。

Run（共用叢集用具名 dev-sandbox 前綴；kube-router 規則生效要幾秒，測試會等）::

    OPERATOR_TEST_NAMESPACE_PREFIX=dev-sandbox-egpsa-int- \\
    KUBECONFIG=... pytest -m integration \\
        tests/integration/test_tenant_baseline_egress_psa_1527.py -v
"""
import json
import subprocess
import time
import uuid

import pytest

from src.config import settings
from src.services.helm import KubernetesService
from src.services.tenant_baseline import (
    BASELINE_EGRESS_NP_NAME,
    BASELINE_EGRESS_WEB_NP_NAME,
    EGRESS_PROFILE_LABEL,
    EGRESS_PROFILE_WEB,
    PSA_EXEMPT_ANNOTATION,
    build_baseline_egress_netpol,
    build_baseline_egress_web_netpol,
    build_pod_security_labels,
)
from tests.namespace_scope import integration_namespace_prefix

pytestmark = pytest.mark.integration

_IMAGE = "nginx:alpine"  # 只用它的 busybox（nc／nslookup）；其他 integration 測試已在用
_PORT = 8080
_WORKSPACE_ID = 915270  # 不會撞到真工作區的 id；只出現在本測試 ns 的 reach-ws 選擇器
# 叢集真的會執行 NetworkPolicy 的上限等待：#1497 量到新 pod 頭 2–3 秒不受限，
# 規則變更本身也要 kube-router 同步一輪。
_NP_SETTLE_TIMEOUT = 90

# 測試 pod 一律符合 restricted（ns 開了 warn=restricted，不要讓測試自己製造警告），
# 並避開 ubuntuserver-59mqj（共用叢集上的正式站節點；docker-desktop／kind 上這條無作用）。
_POD_TEMPLATE = {
    "apiVersion": "v1",
    "kind": "Pod",
    "spec": {
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 1,
        "securityContext": {
            "runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{
                "key": "kubernetes.io/hostname", "operator": "NotIn",
                "values": ["ubuntuserver-59mqj"],
            }]}],
        }}},
        "containers": [{
            "name": "c",
            "image": _IMAGE,
            "securityContext": {
                "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
            },
            "resources": {
                "requests": {"cpu": "10m", "memory": "16Mi", "ephemeral-storage": "8Mi"},
                "limits": {"cpu": "100m", "memory": "64Mi", "ephemeral-storage": "32Mi"},
            },
        }],
    },
}


def _kubectl(*args, input_data=None, check=True):
    proc = subprocess.run(["kubectl", *args], input=input_data, capture_output=True,
                          text=True, timeout=120)
    if check and proc.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)} failed: {proc.stderr}")
    return proc


def _apply(obj: dict):
    return _kubectl("apply", "-f", "-", input_data=json.dumps(obj))


def _pod(namespace: str, name: str, command: list, labels: dict) -> dict:
    pod = json.loads(json.dumps(_POD_TEMPLATE))
    pod["metadata"] = {"name": name, "namespace": namespace, "labels": labels}
    pod["spec"]["containers"][0]["command"] = command
    return pod


def _server(namespace: str) -> None:
    """一個 TCP server pod＋Service（port 8080，回一行 ok）。"""
    _apply(_pod(namespace, "srv",
                ["sh", "-c", f"while true; do nc -lk -p {_PORT} -e echo ok; done"],
                {"app": "egpsa-srv"}))
    _apply({"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "srv", "namespace": namespace},
            "spec": {"selector": {"app": "egpsa-srv"},
                     "ports": [{"port": _PORT, "targetPort": _PORT}]}})


def _client(namespace: str) -> None:
    _apply(_pod(namespace, "cli", ["sleep", "3600"], {"app": "egpsa-cli"}))


def _wait_ready(namespace: str, *pods: str) -> None:
    for pod in pods:
        _kubectl("wait", "--for=condition=Ready", f"pod/{pod}", "-n", namespace,
                 "--timeout=180s")


def _pod_ip(namespace: str, pod: str) -> str:
    return _kubectl("get", "pod", pod, "-n", namespace,
                    "-o", "jsonpath={.status.podIP}").stdout.strip()


def _connect(namespace: str, host: str, port: int, pod: str = "cli") -> bool:
    """在 cli pod 裡 TCP connect（3 秒逾時）。True＝連上。"""
    proc = _kubectl("exec", pod, "-n", namespace, "--", "nc", "-z", "-w", "3",
                    host, str(port), check=False)
    return proc.returncode == 0


def _resolves(namespace: str, name: str) -> bool:
    proc = _kubectl("exec", "cli", "-n", namespace, "--", "nslookup", name, check=False)
    return proc.returncode == 0


def _apiserver_targets():
    """kube-apiserver 的 Service ClusterIP:443 與 DNAT 後的真實 endpoint（node IP:6443）。"""
    svc_ip = _kubectl("get", "svc", "kubernetes", "-n", "default",
                      "-o", "jsonpath={.spec.clusterIP}").stdout.strip()
    ep = json.loads(_kubectl("get", "endpoints", "kubernetes", "-n", "default",
                             "-o", "json").stdout)
    subset = (ep.get("subsets") or [{}])[0]
    ep_ip = (subset.get("addresses") or [{}])[0].get("ip")
    ep_port = (subset.get("ports") or [{}])[0].get("port")
    targets = [(svc_ip, 443)]
    if ep_ip and ep_port:
        targets.append((ep_ip, ep_port))
    return targets


def _wait_until(predicate, timeout=_NP_SETTLE_TIMEOUT, interval=2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def baseline_on(monkeypatch):
    """兩個開關打開；namespace 前綴換成本輪的測試前綴（validate_namespace 才會放行）。"""
    monkeypatch.setattr(settings, "namespace_prefix", integration_namespace_prefix())
    monkeypatch.setattr(settings, "tenant_baseline_egress_enabled", True)
    monkeypatch.setattr(settings, "tenant_pod_security_enabled", True)
    monkeypatch.setattr(settings, "psa_exempt_namespaces", "")
    return KubernetesService()


@pytest.fixture
def control_namespace():
    """沒有任何 NetworkPolicy／PSA 標籤的 ns：對照組（＝正式站那些沒設 NP 的非 PaaS ns）。"""
    name = f"{integration_namespace_prefix()}{uuid.uuid4().hex[:8]}"
    _kubectl("create", "namespace", name)
    yield name
    _kubectl("delete", "namespace", name, "--wait=false", check=False)


def test_baseline_egress_behaviour_on_real_cni(test_namespace, control_namespace, baseline_on):
    """套上 operator 的 baseline 後：同 ns（pod 與 Service）與 kube-dns 通；別的 ns、
    kube-apiserver（ClusterIP 與 DNAT 後的 node IP）不通；公網通。每一項都和對照組比。"""
    svc = baseline_on
    svc.apply_pod_security(test_namespace)
    svc._apply_baseline_netpol(test_namespace, _WORKSPACE_ID)

    np = json.loads(_kubectl("get", "networkpolicy", BASELINE_EGRESS_NP_NAME,
                             "-n", test_namespace, "-o", "json").stdout)
    assert np["spec"] == build_baseline_egress_netpol(test_namespace)["spec"]

    for ns in (test_namespace, control_namespace):
        _server(ns)
        _client(ns)
    for ns in (test_namespace, control_namespace):
        _wait_ready(ns, "srv", "cli")

    other_svc = f"srv.{control_namespace}.svc.cluster.local"
    other_pod = _pod_ip(control_namespace, "srv")
    apiserver = _apiserver_targets()

    # 對照組先確認目標本身是活的、而且沒有 NP 的 ns 連得到——否則「連不到」不能算 baseline 擋的。
    assert _connect(control_namespace, other_svc, _PORT)
    assert _connect(control_namespace, other_pod, _PORT)
    reachable_api = [t for t in apiserver if _connect(control_namespace, *t)]
    assert reachable_api, f"control ns cannot reach the apiserver at all: {apiserver}"

    # 等 kube-router 把規則套到新 pod（#1497 啟動空窗），然後才斷言穩定狀態。
    assert _wait_until(lambda: not _connect(test_namespace, other_svc, _PORT)), (
        "egress baseline never took effect: baseline ns still reaches another ns")

    # 同工作區＝區網：pod IP 與 Service 都通。
    assert _connect(test_namespace, f"srv.{test_namespace}.svc.cluster.local", _PORT)
    assert _connect(test_namespace, _pod_ip(test_namespace, "srv"), _PORT)
    # DNS 只放 kube-dns：名稱解析要正常（否則所有對外連線都會壞）。
    assert _resolves(test_namespace, "kubernetes.default.svc.cluster.local")
    # 別的 ns（Service 與 pod IP）、kube-apiserver（ClusterIP 與 node IP:6443）都不通。
    assert not _connect(test_namespace, other_svc, _PORT)
    assert not _connect(test_namespace, other_pod, _PORT)
    for host, port in reachable_api:
        assert not _connect(test_namespace, host, port), f"apiserver {host}:{port} reachable"

    # 公網：對照組連得到才斷言（離線的 docker-desktop／kind 叢集 skip，不假紅）。
    public = ("1.1.1.1", 443)
    if not _connect(control_namespace, *public):
        pytest.skip("cluster has no internet egress (control ns cannot reach 1.1.1.1:443); "
                    "public-internet assertion not applicable")
    assert _connect(test_namespace, *public)


def test_baseline_egress_port_controls_on_real_cni(test_namespace, control_namespace,
                                                   baseline_on, monkeypatch):
    """商用驗收 egress-all-ports-open：封埠（endPort 範圍）與 web profile 在 kube-router 上真的生效。

    一般 pod：公網 443、853 通，22 不通（封 22,25）。帶 egress-profile=web 的 pod：公網只剩 443，
    853、22 都不通；同 ns 與 DNS 照通。每個公網目標先在對照組確認連得到才斷言。
    """
    monkeypatch.setattr(settings, "tenant_baseline_egress_blocked_ports", "22,25")
    monkeypatch.setattr(settings, "tenant_baseline_egress_web_profile", True)
    svc = baseline_on
    svc._apply_baseline_netpol(test_namespace, _WORKSPACE_ID)

    live = {n: json.loads(_kubectl("get", "networkpolicy", n, "-n", test_namespace,
                                   "-o", "json").stdout)["spec"]
            for n in (BASELINE_EGRESS_NP_NAME, BASELINE_EGRESS_WEB_NP_NAME)}
    # API server 接受 endPort 且原樣存回（沒有被靜默丟掉）。
    assert live[BASELINE_EGRESS_NP_NAME] == build_baseline_egress_netpol(
        test_namespace, blocked_ports=[22, 25], web_profile=True)["spec"]
    assert live[BASELINE_EGRESS_WEB_NP_NAME] == build_baseline_egress_web_netpol(
        test_namespace)["spec"]

    _server(test_namespace)
    _client(test_namespace)
    _client(control_namespace)
    _apply(_pod(test_namespace, "cli-web", ["sleep", "3600"],
                {"app": "egpsa-cli-web", EGRESS_PROFILE_LABEL: EGRESS_PROFILE_WEB}))
    _wait_ready(test_namespace, "srv", "cli", "cli-web")
    _wait_ready(control_namespace, "cli")

    web_ok, dot, ssh = ("1.1.1.1", 443), ("1.1.1.1", 853), ("github.com", 22)
    for target in (web_ok, dot, ssh):
        if not _connect(control_namespace, *target):
            pytest.skip(f"control ns cannot reach {target}; port assertions not applicable")

    # 等 kube-router 套上規則（#1497 空窗）：一般 pod 的 22 斷掉才算生效。
    assert _wait_until(lambda: not _connect(test_namespace, *ssh)), (
        "blocked port 22 never took effect for a plain pod")
    assert _wait_until(lambda: not _connect(test_namespace, *dot, pod="cli-web")), (
        "web profile never took effect: web pod still reaches 1.1.1.1:853")

    # 一般 pod：封掉的埠不通，其他埠照通（endPort 範圍被正確執行，不是只放 port 起點）。
    assert _connect(test_namespace, *web_ok)
    assert _connect(test_namespace, *dot)
    assert not _connect(test_namespace, *ssh)
    # web pod：公網只剩 80/443；同 ns 與 DNS 照通。
    assert _connect(test_namespace, *web_ok, pod="cli-web")
    assert not _connect(test_namespace, *dot, pod="cli-web")
    assert not _connect(test_namespace, *ssh, pod="cli-web")
    assert _connect(test_namespace, f"srv.{test_namespace}.svc.cluster.local", _PORT,
                    pod="cli-web")
    proc = _kubectl("exec", "cli-web", "-n", test_namespace, "--", "nslookup",
                    "github.com", check=False)
    assert proc.returncode == 0, proc.stderr


def test_pod_security_enforced_by_admission(test_namespace, baseline_on):
    """PSA 標籤真的讓 API server 拒收能碰主機的 pod，一般 pod 照收。"""
    labels = baseline_on.apply_pod_security(test_namespace)
    assert labels == build_pod_security_labels(
        enforce=settings.tenant_psa_enforce_level, warn=settings.tenant_psa_warn_level,
        audit=settings.tenant_psa_audit_level, version=settings.tenant_psa_version)
    live = json.loads(_kubectl("get", "ns", test_namespace, "-o", "json").stdout)
    for key, value in labels.items():
        assert live["metadata"]["labels"].get(key) == value

    def dry_run(mutate) -> subprocess.CompletedProcess:
        pod = _pod(test_namespace, "psa-probe", ["sleep", "1"], {"app": "psa-probe"})
        mutate(pod["spec"])
        return _kubectl("create", "--dry-run=server", "-f", "-",
                        input_data=json.dumps(pod), check=False)

    plain = dry_run(lambda spec: None)
    assert plain.returncode == 0, plain.stderr

    def privileged(spec):
        spec["containers"][0]["securityContext"] = {"privileged": True}

    def host_path(spec):
        spec["volumes"] = [{"name": "h", "hostPath": {"path": "/"}}]
        spec["containers"][0]["volumeMounts"] = [{"name": "h", "mountPath": "/h"}]

    def host_network(spec):
        spec["hostNetwork"] = True

    def net_admin(spec):
        spec["containers"][0]["securityContext"]["capabilities"] = {"add": ["NET_ADMIN"]}

    for name, mutate in (("privileged", privileged), ("hostPath", host_path),
                         ("hostNetwork", host_network), ("NET_ADMIN", net_admin)):
        proc = dry_run(mutate)
        assert proc.returncode != 0, f"{name} pod was admitted"
        assert "violates PodSecurity" in proc.stderr, f"{name}: {proc.stderr}"


def test_psa_exempt_namespace_keeps_no_enforce(test_namespace, baseline_on, monkeypatch):
    """例外清單內的 ns：不設 enforce、留下理由註記；特權 pod 照收（行為與今天相同）。"""
    monkeypatch.setattr(settings, "psa_exempt_namespaces", test_namespace)
    _kubectl("label", "ns", test_namespace, "pod-security.kubernetes.io/enforce=baseline",
             "--overwrite")  # 殘留的標籤要被拿掉
    assert baseline_on.apply_pod_security(test_namespace) == {}
    live = json.loads(_kubectl("get", "ns", test_namespace, "-o", "json").stdout)
    assert not any(k.startswith("pod-security.kubernetes.io/")
                   for k in live["metadata"].get("labels", {}))
    assert PSA_EXEMPT_ANNOTATION in live["metadata"].get("annotations", {})


def test_switches_off_leave_namespace_untouched(test_namespace, monkeypatch):
    """開關關著（預設）：不建 paas-baseline-egress、不加 PSA 標籤——合併／發版不改行為。"""
    monkeypatch.setattr(settings, "namespace_prefix", integration_namespace_prefix())
    monkeypatch.setattr(settings, "tenant_baseline_egress_enabled", False)
    monkeypatch.setattr(settings, "tenant_pod_security_enabled", False)
    svc = KubernetesService()
    assert svc.apply_pod_security(test_namespace) == {}
    svc._apply_baseline_netpol(test_namespace, _WORKSPACE_ID)
    names = _kubectl("get", "networkpolicy", "-n", test_namespace,
                     "-o", "jsonpath={.items[*].metadata.name}").stdout.split()
    assert BASELINE_EGRESS_NP_NAME not in names
    assert "paas-baseline-default-deny-ingress" in names  # 既有 ingress baseline 照舊
    live = json.loads(_kubectl("get", "ns", test_namespace, "-o", "json").stdout)
    assert not any(k.startswith("pod-security.kubernetes.io/")
                   for k in live["metadata"].get("labels", {}))


def test_backfill_endpoint_applies_both(app_client, test_namespace, monkeypatch):
    """既有 ns 的回填路徑（POST /api/namespaces/{ns}/baseline-netpol）在開關打開時兩項都套。"""
    monkeypatch.setattr(settings, "tenant_baseline_egress_enabled", True)
    monkeypatch.setattr(settings, "tenant_pod_security_enabled", True)
    monkeypatch.setattr(settings, "psa_exempt_namespaces", "")
    resp = app_client.post(f"/api/namespaces/{test_namespace}/baseline-netpol",
                           json={"workspace_id": _WORKSPACE_ID})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pod_security_labels"]["pod-security.kubernetes.io/enforce"] == "baseline"
    names = _kubectl("get", "networkpolicy", "-n", test_namespace,
                     "-o", "jsonpath={.items[*].metadata.name}").stdout.split()
    assert BASELINE_EGRESS_NP_NAME in names

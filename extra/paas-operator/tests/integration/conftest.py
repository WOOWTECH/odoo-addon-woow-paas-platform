"""Integration test fixtures: real K8s, real kubectl, real HTTP through FastAPI.

These tests are the safety net the unit-test suite cannot provide. The unit
suite mocks ``k8s_service`` so it cannot catch:

- BUG-6: a deployed image that lags the source code (pure deploy-flow issue)
- BUG-7: workload-type bugs (Deployment-only handling missing StatefulSet)
- BUG-8: client/server HTTP contract drift (body vs query string)
- BUG-9: ServiceAccount RBAC subresource permissions (deployments/scale)

To run these against an existing cluster::

    KUBECONFIG=~/.kube/config pytest -m integration tests/integration/

The tests will skip themselves if ``kubectl cluster-info`` cannot reach a
cluster; in CI we provision a kind cluster and set ``KUBECONFIG`` first.

Each test gets its own isolated namespace prefixed ``paas-ws-test-op-`` so a
failed cleanup never bleeds into another test.
"""
import os
import subprocess
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.namespace_scope import integration_namespace_prefix


# All tests in this directory are integration tests.
pytestmark = pytest.mark.integration


def _kubectl_available() -> bool:
    try:
        return subprocess.run(
            ["kubectl", "version", "--client"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _has_cluster() -> bool:
    try:
        return subprocess.run(
            ["kubectl", "cluster-info"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _apply_manifest(manifest: str) -> None:
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifest.encode(),
        check=True,
        capture_output=True,
    )


def _wait_for_replicas(kind: str, name: str, namespace: str, target: int,
                       timeout: int = 30) -> int:
    """Poll ``kubectl get`` until ``.spec.replicas`` matches; return final value."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                ["kubectl", "get", kind, name, "-n", namespace,
                 "-o", "jsonpath={.spec.replicas}"],
                stderr=subprocess.DEVNULL,
            )
            last = int(out.decode() or "0")
            if last == target:
                return last
        except (subprocess.CalledProcessError, ValueError):
            pass
        time.sleep(0.5)
    return last if last is not None else -1


@pytest.fixture(scope="session", autouse=True)
def _check_environment():
    """Skip the whole integration session when no cluster is reachable."""
    if not _kubectl_available():
        pytest.skip("kubectl not on PATH; integration tests require K8s access",
                    allow_module_level=True)
    if not _has_cluster():
        pytest.skip("No reachable K8s cluster (set KUBECONFIG or run kind)",
                    allow_module_level=True)


@pytest.fixture(scope="session")
def app_client():
    """FastAPI TestClient bound to the real operator app.

    No mocking — ``k8s_service`` will shell out to ``kubectl`` against the
    cluster pointed to by ``KUBECONFIG``.

    F-PC-4: the operator now fails closed (503) on an unset API key, so the
    suite authenticates with a real key + matching header instead of the old
    empty-key pass-through. The key is forced onto the already-instantiated
    settings singleton (it may have been read at import time, before this
    fixture set the env).
    """
    api_key = os.environ.get("API_KEY") or "integration-test-key"
    os.environ["API_KEY"] = api_key
    test_prefix = integration_namespace_prefix()
    os.environ["NAMESPACE_PREFIX"] = test_prefix
    # #279 F6 env guard: an unset PAAS_ENV aborts boot on the shared prod/stg
    # cluster (cross-env label collision). Integration tests run against a
    # single-env dev/local cluster (docker-desktop), so opt into the documented
    # dev/local bypass rather than pinning a fake PAAS_ENV. The test prefix
    # (paas-ws-test-op-) names no real env, so the bypass is accepted. Forced onto
    # the settings singleton too (it may have been read at import, like api_key).
    os.environ.setdefault("ALLOW_UNSET_PAAS_ENV", "true")

    from src.config import settings
    from src.main import app
    settings.api_key = api_key
    settings.namespace_prefix = test_prefix
    settings.allow_unset_paas_env = True
    with TestClient(app, headers={"X-API-Key": api_key}) as client:
        yield client


def _reclaim_retained_pvs(owns) -> int:
    """把 claim 屬於測試 namespace、reclaimPolicy=Retain 的 PV 改成 Delete；回傳改了幾顆。

    #1431：共用叢集的預設 StorageClass（Longhorn）是 Retain。測試 namespace 一刪，PVC 跟著
    消失，PV 卻停在 ``Released`` 永遠不回收——10-04 兩輪 integration 就留下 14 顆，要人手清。
    翻成 Delete 之後，PV（與底層 volume）會隨 PVC 一起刪掉（已 Released 的也會被回收）。

    ``owns(claim_namespace)`` 決定範圍：逐測試時是「恰好這個 namespace」，收尾掃描時是
    「本輪測試前綴」（``integration_namespace_prefix`` 把關，只會是 ``paas-ws-test-op-`` 或
    具名的 ``dev-sandbox-*-``）。best-effort，失敗不影響測試結果。
    """
    try:
        out = subprocess.run(
            ["kubectl", "get", "pv", "-o",
             'jsonpath={range .items[*]}{.metadata.name}{"\\t"}'
             '{.spec.claimRef.namespace}{"\\t"}{.spec.persistentVolumeReclaimPolicy}{"\\n"}{end}'],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    patched = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1] and owns(parts[1]) and parts[2] == "Retain":
            subprocess.run(
                ["kubectl", "patch", "pv", parts[0], "--type=merge",
                 "-p", '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'],
                capture_output=True, timeout=30,
            )
            patched += 1
    return patched


@pytest.fixture(scope="session", autouse=True)
def _sweep_retained_test_pvs(_check_environment):
    """收尾：Longhorn 是 Immediate 綁定、非同步建 PV——PVC 剛建好就結束的測試，PV 可能在
    逐測試的翻轉**之後**才出現，namespace 刪掉後照樣變 Released（實測 1 顆）。所以整輪
    結束時再對本輪前綴掃幾次，直到連續一段時間沒有新的 Retain PV 為止（最多約 2 分鐘）。
    """
    yield
    prefix = integration_namespace_prefix()
    deadline = time.time() + 120
    quiet_since = time.time()
    while time.time() < deadline:
        if _reclaim_retained_pvs(lambda ns: ns.startswith(prefix)):
            quiet_since = time.time()
        elif time.time() - quiet_since >= 30:
            break
        time.sleep(5)


@pytest.fixture
def test_namespace():
    """Isolated namespace per test, deleted (async) on teardown."""
    name = f"{integration_namespace_prefix()}{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["kubectl", "create", "namespace", name],
        check=True, capture_output=True,
    )
    yield name
    _reclaim_retained_pvs(lambda ns: ns == name)
    # --wait=false: kubelet finalizers can take seconds; let CI keep moving.
    subprocess.run(
        ["kubectl", "delete", "namespace", name, "--wait=false"],
        capture_output=True,
    )


def _make_deployment(namespace: str, name: str, release: str) -> str:
    return f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/instance: {release}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
      - name: nginx
        image: nginx:alpine
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
"""


def _make_statefulset(namespace: str, name: str, release: str) -> str:
    return f"""
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/instance: {release}
spec:
  serviceName: {name}
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
"""


@pytest.fixture
def deployment_release(test_namespace):
    name, release = "test-deploy", "test-deploy-rel"
    _apply_manifest(_make_deployment(test_namespace, name, release))
    return {"namespace": test_namespace, "release": release, "name": name}


@pytest.fixture
def statefulset_release(test_namespace):
    name, release = "test-sts", "test-sts-rel"
    _apply_manifest(_make_statefulset(test_namespace, name, release))
    return {"namespace": test_namespace, "release": release, "name": name}


@pytest.fixture
def chart_fullname_release(test_namespace):
    """#357 C04: a release whose Deployment is named like the mcp-server chart.

    The chart's fullname renders ``{{ .Release.Name }}-{{ .Chart.Name }}``, so
    the Deployment is ``{release}-mcp-server`` — NOT the release name. Only the
    ``app.kubernetes.io/instance`` label ties it back to the release. The unit
    suite mocks this boundary away, which is exactly why the reset handler could
    restart ``deployment/{release}`` for months with 9/9 tests green.
    """
    release = "svc-c04rel"
    name = f"{release}-mcp-server"
    _apply_manifest(_make_deployment(test_namespace, name, release))
    return {"namespace": test_namespace, "release": release, "name": name}


@pytest.fixture
def ambiguous_instance_label(test_namespace):
    """Two Deployments sharing one instance label — the >1 discovery branch."""
    release = "svc-c04dup"
    names = [f"{release}-mcp-server", f"{release}-sidecar"]
    for name in names:
        _apply_manifest(_make_deployment(test_namespace, name, release))
    return {"namespace": test_namespace, "release": release, "names": names}


@pytest.fixture
def mixed_release(test_namespace):
    """Same release label drives one Deployment AND one StatefulSet.

    Mirrors a real release where e.g. an app server (Deployment) sits in
    front of a database (StatefulSet) — both must scale together.
    """
    release = "test-mixed-rel"
    _apply_manifest(_make_deployment(test_namespace, "test-app", release))
    _apply_manifest(_make_statefulset(test_namespace, "test-db", release))
    return {
        "namespace": test_namespace,
        "release": release,
        "deployment_name": "test-app",
        "statefulset_name": "test-db",
    }

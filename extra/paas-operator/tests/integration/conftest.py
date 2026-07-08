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
    os.environ.setdefault("NAMESPACE_PREFIX", "paas-ws-test-op-")
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
    settings.allow_unset_paas_env = True
    with TestClient(app, headers={"X-API-Key": api_key}) as client:
        yield client


@pytest.fixture
def test_namespace():
    """Isolated namespace per test, deleted (async) on teardown."""
    name = f"paas-ws-test-op-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["kubectl", "create", "namespace", name],
        check=True, capture_output=True,
    )
    yield name
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

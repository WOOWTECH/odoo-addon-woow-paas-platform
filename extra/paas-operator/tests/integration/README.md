# Operator Integration Tests

Real K8s + real `kubectl` + real HTTP through FastAPI. Companion to the
mocked unit tests in `tests/test_*.py`.

## What these catch that unit tests do not

- **BUG-7 class**: workload-type bugs (`scale_release` only handling
  Deployment, missing StatefulSet)
- **BUG-8 class**: client/server HTTP contract drift (body vs. query string)
- **BUG-9 class**: ServiceAccount RBAC subresource permissions
  (`deployments/scale`, `statefulsets/scale`) — exercised when run with the
  real operator ServiceAccount in CI

Pure unit tests with `mock_k8s` cannot expose any of the above because the
mock layer hides exactly the boundary that drifts.

## Running locally

```bash
# Against any reachable cluster (helm binary path varies by OS)
HELM_BINARY=$(which helm) KUBECONFIG=~/.kube/config \
    pytest -m integration tests/integration/

# Against a kind cluster you've already created
kind create cluster --name op-int
HELM_BINARY=$(which helm) pytest -m integration tests/integration/
```

If `kubectl cluster-info` fails the suite skips itself (no setup penalty
for a default `pytest` run, which also defaults to `-m "not integration"`
via `pytest.ini`).

`HELM_BINARY` is required because the operator's FastAPI lifespan refuses
to start when helm is missing. Linux CI installs helm at `/usr/local/bin/`
so it picks up the default; on macOS Homebrew puts it at `/opt/homebrew/`.

## Running in CI

`.gitea/workflows/operator-integration-test.yml` provisions a kind cluster
on each PR that touches `extra/paas-operator/**`, then runs:

```bash
pytest -m integration tests/integration/ -v
```

## Test isolation

Each test gets its own namespace (`paas-ws-int-{uuid8}`) created and torn
down by the `test_namespace` fixture. Failed cleanup never bleeds across
tests because namespaces are always uniquely named.

## Adding a test

1. Pick or write a fixture in `conftest.py` that lays down the K8s state
   you want to exercise (a new workload kind, a different label scheme, …).
2. Use the `app_client` fixture to send the real HTTP request.
3. Assert the response body **and** the resulting K8s state via
   `kubectl get -o jsonpath`.

Avoid mocks. The whole point is to catch the things the mocks hide.

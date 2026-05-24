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

### macOS quickstart

```bash
cd extra/paas-operator

# Pick a cluster context (once per shell)
kubectl config use-context docker-desktop   # or your woow-k3s context

# First time only: install Python deps
pip install -r requirements.txt

# Run
HELM_BINARY=$(which helm) pytest -m integration tests/integration/ -v
```

7 tests, ~10 s on a clean cluster. The `app_client` fixture sets a real
`API_KEY` (default `integration-test-key`) + `NAMESPACE_PREFIX="paas-ws-test-op-"`
automatically and sends the matching `X-API-Key` header — no need to export.
(Since F-PC-4 the operator fails closed with 503 on an unset key, so the suite
authenticates rather than relying on the old empty-key pass-through.)

Cluster choice:

| Cluster         | Notes                                                          |
| --------------- | -------------------------------------------------------------- |
| docker-desktop  | Recommended for dev; fully isolated from shared infra.         |
| woow-k3s        | Cross-env validation; tests create/delete unique namespaces.   |
| kind (ephemeral)| `kind create cluster --name op-int` then run; delete after.    |

### Generic invocation

```bash
# Any reachable cluster
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
to start when helm is missing. macOS Homebrew puts helm at
`/opt/homebrew/bin/`, so the default `/usr/local/bin/helm` is wrong there.

## Test isolation

Each test gets its own namespace (`paas-ws-test-op-{uuid8}`) created and torn
down by the `test_namespace` fixture. Failed cleanup never bleeds across
tests because namespaces are always uniquely named.

## Adding a test

1. Pick or write a fixture in `conftest.py` that lays down the K8s state
   you want to exercise (a new workload kind, a different label scheme, …).
2. Use the `app_client` fixture to send the real HTTP request.
3. Assert the response body **and** the resulting K8s state via
   `kubectl get -o jsonpath`.

Avoid mocks. The whole point is to catch the things the mocks hide.

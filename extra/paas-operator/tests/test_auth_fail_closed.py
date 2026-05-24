"""TDD red phase — Phase D F-PC-4 (D-1): operator auth global fail-closed.

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md WS1 F-PC-4 (caveat C-2).
Bug  : ``src/main.py:89-91`` — when ``settings.api_key`` is unset (""), the
       auth middleware logs a warning and ``call_next``s the request through,
       i.e. EVERY protected route is served with no authentication (fail-OPEN).
       A misconfigured operator (empty key) therefore exposes spawn / reap /
       delete / scale to anyone who can reach it.

Fix direction (D-1, decided): an unset key is a misconfiguration, not a
"disable auth" switch → the middleware must fail CLOSED with 503 for every
protected route (health/docs stay exempt). The same PR updates the ~6 existing
fixtures that rely on ``api_key=""`` pass-through (test_api ``client`` fixture,
test_tunnels / test_scale / test_agent_runtimes / test_odoo_module, and
integration ``conftest.py`` ``app_client``) to set a real key or assert 503.

Red expectation today (fix NOT started):

* test_unset_api_key_fails_closed_*  → RED: with api_key="" a protected route
  is currently served (passes the middleware), so the status is NOT 503
  (404/200/500 depending on the route) → the ``== 503`` assertion fails.
* test_configured_key_missing_header_still_401 → GREEN now & after: the
  existing "key set, header missing → 401" path is unchanged (this is the
  regression lock proving the fix only tightens the UNSET case, not the
  normal auth flow).
* test_unset_api_key_health_still_ok → GREEN now & after: fail-closed must NOT
  break the unauthenticated health probe (k8s liveness).

Boundary (intentionally NOT here):
* Per-route authorization / RBAC — out of scope (this is the global gate).
* Real Helm/k8s side effects — the middleware runs BEFORE routing, so these
  probes never need a live backend.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app


# Representative protected routes across the routers (releases / agent-runtimes
# / tunnels / namespaces). The middleware runs BEFORE routing, so the exact
# route need not exist for the post-fix 503 — but each is a real prefix so the
# pre-fix "served / not-503" red is unambiguous.
_PROTECTED_PROBES = [
    ("GET", "/api/releases/paas-ws-test/test"),
    ("GET", "/api/agent-runtimes/pa0000000000abcd/status"),
    ("POST", "/api/agent-runtimes/pa0000000000abcd/reap"),
    ("GET", "/api/tunnels"),
    ("GET", "/api/namespaces"),
]


def test_unset_api_key_fails_closed_on_protected_routes():
    """F-PC-4: api_key="" must yield 503 on EVERY protected route (global)."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = ""  # unset / misconfigured
        mock_settings.namespace_prefix = "paas-ws-"
        client = TestClient(app)
        for method, path in _PROTECTED_PROBES:
            resp = client.request(method, path)
            assert resp.status_code == 503, (
                "F-PC-4: with api_key unset the operator must fail CLOSED "
                "(503 Service Unavailable) on protected route %s %s, but it "
                "was served (status=%s) — fail-OPEN pass-through at "
                "main.py:89-91 is the live security hole."
                % (method, path, resp.status_code)
            )


def test_configured_key_missing_header_still_401():
    """Regression: a CONFIGURED key + missing header stays 401 (not 503).

    Locks that the fail-closed change only fires on an UNSET key; the normal
    "bad/absent credential" path is unchanged.
    """
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "secret-key"
        mock_settings.namespace_prefix = "paas-ws-"
        client = TestClient(app)
        resp = client.get("/api/releases/paas-ws-test/test")
        assert resp.status_code == 401, (
            "Regression: with a configured key, a request without the "
            "X-API-Key header must be 401 (unchanged), got %s."
            % resp.status_code
        )


def test_unset_api_key_health_still_ok():
    """Regression: fail-closed must not break the exempt /health probe."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = ""
        mock_settings.namespace_prefix = "paas-ws-"
        with patch("src.main.HelmService") as mock_helm:
            mock_helm.return_value.get_version.return_value = "v3.13.3"
            client = TestClient(app)
            resp = client.get("/health")
            assert resp.status_code == 200, (
                "Regression: /health must stay reachable without auth even "
                "when the key is unset (k8s liveness), got %s."
                % resp.status_code
            )

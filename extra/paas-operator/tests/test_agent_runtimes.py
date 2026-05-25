"""TDD red phase — Phase C 塊①: paas-operator agent-runtime endpoints.

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase C Tasks 1-6
Design:
  - §2.11   New paas-operator endpoints (per-project pivot)
  - §2.11.1 Token lifecycle (spawn-or-attach / resume 重簽 SKILL_BUNDLE_TOKEN
            + patch pod env BEFORE scale-1)
  - §2.11.2 既有 router 不動 (releases/namespaces/routes/tunnels/init/odoo_modules)
  - §5.3    Odoo ↔ Pod auth — HS256 bearer token + shared secret
  - §5.5    webhook_dispatcher — HMAC-SHA256 sign + retry 3x exp backoff
  - §6.6    Token replay defense — dual-secret current/previous + jti cache

Acceptance criteria covered (依賴序 ①):

J.1  ``jwt_signer.sign_ws_token`` produces an HS256 JWT carrying
     iss / aud / project_hash / channel_id / invoker_user_id /
     invoker_role / exp / iat / jti.
J.2  ``jwt_signer.sign_skill_bundle_token`` produces an HS256 JWT carrying
     iss / project_hash / project_id / workspace_ids / exp / iat / jti
     (project_id + workspace_ids are the *authoritative* claims that the
     Odoo skill-bundle endpoint decodes — block ②, HC-2 ruling).
J.3  The two token scopes are distinguishable — a ws_token does not carry
     the bundle-access claims (project_id) and a skill_bundle_token does
     not carry the per-invocation channel_id.
J.4  ``verify_token`` round-trips a freshly signed token (current secret).
J.5  Dual-secret fallback — a token signed with the *previous* secret
     still verifies (rotation window, §6.6).
J.6  ``verify_token`` rejects a token signed with an unknown secret
     (signature invalid against both current + previous).
J.7  ``verify_token`` rejects an expired token.
J.8  jti replay — verifying the same jti a second time is rejected.

S.1  ``POST /api/agent-runtimes/spawn-or-attach`` cold start → helm install
     + signs BOTH tokens; injects SKILL_BUNDLE_TOKEN into pod env; returns
     {pod_ws_url, ws_token/auth_token, pod_state, spawn_age_ms}.
S.2  spawn-or-attach attach (release already scaled-1) → no-op install.
S.3  spawn-or-attach resume (release replicas=0) → RE-SIGN skill_bundle_token
     + patch pod env with the new token BEFORE scaling to 1 (§2.11.1).
S.4  ``POST /{hash}/reap`` → scale to 0 + reaped-since annotation;
     idempotent (already_reaped / not_found).
S.5  ``POST /{hash}/resume`` → re-sign skill_bundle_token + patch env
     before scale-1 (§2.11.1 token lifecycle).
S.6  ``POST /{hash}/invalidate-mcp`` → delegates to mcp_invalidator.
S.7  ``POST /{hash}/sync-workspaces`` → helm upgrade re-renders NetworkPolicy
     egress allowlist from project_workspace_ids.
S.8  ``DELETE /{hash}`` → helm uninstall.
S.9  ``GET /{hash}/status`` → returns {state, last_active, pod_ws_url,
     spawn_age_ms}.

W.1  ``webhook_dispatcher`` signs the body with HMAC-SHA256 under the
     webhook secret and sends header ``X-PaaS-Signature``.
W.2  ``webhook_dispatcher`` retries 3x with exponential backoff on 5xx.

M.1  ``mcp_invalidator`` patches the ConfigMap ``agent-pa-{hash}-mcp``.

R.1  REGRESSION — existing routers untouched: ``/api/releases`` &
     ``/api/namespaces`` validation still behaves as before (§2.11.2).

Red expectation today (Phase C implementation has NOT started):

* J.* / W.* / M.* — in-function ``import`` of the not-yet-created
  ``src.services.jwt_signer`` / ``webhook_dispatcher`` / ``mcp_invalidator``
  raises ModuleNotFoundError *inside the test call* → clean FAIL pointing
  at missing production module (not a collection-time error — every other
  test in this file still collects & runs).
* S.* — entering ``with patch("src.api.agent_runtimes.<svc>")`` raises
  ModuleNotFoundError (router module absent) → clean per-test FAIL.
* R.1 — PASSES today and must STAY GREEN post-impl (regression lock that
  the new router did not disturb the existing ones).

Boundary (intentionally NOT tested here):

* Real K8s pod spawn / attach — paas-operator integration suite
  (``-m integration``); these unit tests mock helm + k8s entirely.
* Pod-side token verification (dual-secret + jti) lives in the
  paas-agent-runtime image (runtime/transport/auth.py), NOT this repo —
  here we only test the *signer* + the operator-side verify helper.
* Odoo-side SKILL_BUNDLE_TOKEN verification → block ② Odoo test.
"""
import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #
def _decode_jwt_payload(token):
    """Decode a JWT payload segment WITHOUT verifying the signature.

    Pure stdlib (no PyJWT dependency) so claim-shape assertions stay valid
    even before the signer wires its crypto deps in.
    """
    payload_b64 = token.split(".")[1]
    padding = "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64 + padding))


@pytest.fixture
def client():
    """FastAPI test client (F-PC-4: real key + matching header, mirrors test_api.py)."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


_SECRET_CURRENT = "current-secret-aaaaaaaaaaaaaaaaaaaa"
_SECRET_PREVIOUS = "previous-secret-bbbbbbbbbbbbbbbbbbbb"


# --------------------------------------------------------------------------- #
# J.* — jwt_signer (HS256 dual-secret + two token scopes + jti replay)          #
# --------------------------------------------------------------------------- #
class TestJwtSigner:
    """§5.3 + §6.6 — sign / verify roundtrip, dual-secret, jti replay."""

    def _signer(self):
        """Import the signer module inside the test (red → ModuleNotFound FAIL)."""
        from src.services import jwt_signer
        return jwt_signer

    def test_sign_ws_token_claims(self):
        """J.1: ws_token carries per-invocation scope claims, HS256."""
        signer = self._signer()
        token = signer.sign_ws_token(
            project_hash="pa1234567890abcd",
            channel_id=42,
            invoker_user_id=7,
            invoker_role="user",
            secret=_SECRET_CURRENT,
        )
        # Header alg must be HS256
        header_b64 = token.split(".")[0]
        header = json.loads(
            base64.urlsafe_b64decode(header_b64 + "=" * (-len(header_b64) % 4))
        )
        assert header.get("alg") == "HS256", "ws_token must be signed with HS256"

        claims = _decode_jwt_payload(token)
        assert claims["project_hash"] == "pa1234567890abcd"
        assert claims["channel_id"] == 42
        assert claims["invoker_user_id"] == 7
        assert claims["invoker_role"] == "user"
        assert claims["iss"] == "paas-operator"
        assert "exp" in claims and "iat" in claims and "jti" in claims
        assert claims["exp"] > claims["iat"], "exp must be after iat"

    def test_sign_skill_bundle_token_claims(self):
        """J.2: skill_bundle_token carries project_id + workspace_ids (authoritative)."""
        signer = self._signer()
        token = signer.sign_skill_bundle_token(
            project_hash="pa1234567890abcd",
            project_id=99,
            workspace_ids=[3, 5, 8],
            secret=_SECRET_CURRENT,
        )
        claims = _decode_jwt_payload(token)
        assert claims["project_hash"] == "pa1234567890abcd"
        assert claims["project_id"] == 99, (
            "project_id is the authoritative claim the Odoo skill-bundle "
            "endpoint decodes (HC-2 ruling — do not trust query param)"
        )
        assert sorted(claims["workspace_ids"]) == [3, 5, 8]
        assert "exp" in claims and "iat" in claims and "jti" in claims

    def test_token_scopes_are_distinguishable(self):
        """J.3: ws_token ≠ skill_bundle_token — disjoint scope claims."""
        signer = self._signer()
        ws = _decode_jwt_payload(signer.sign_ws_token(
            project_hash="pa1234567890abcd", channel_id=1,
            invoker_user_id=2, invoker_role="admin", secret=_SECRET_CURRENT,
        ))
        bundle = _decode_jwt_payload(signer.sign_skill_bundle_token(
            project_hash="pa1234567890abcd", project_id=99,
            workspace_ids=[1], secret=_SECRET_CURRENT,
        ))
        # ws_token must NOT grant bundle access (no authoritative project_id)
        assert "project_id" not in ws, (
            "ws_token (per-invocation) must not carry bundle-access project_id"
        )
        # skill_bundle_token must NOT carry per-invocation channel scope
        assert "channel_id" not in bundle, (
            "skill_bundle_token (per-pod-lifetime) must not carry channel_id"
        )

    # --- C-1 hardening: explicit token_use claim (defense in depth) -------- #
    def test_C1_ws_token_carries_token_use_ws(self):
        """C-1: sign_ws_token stamps token_use='ws' so verifiers can reject a
        ws token presented to a skill_bundle context (and vice versa) without
        relying on the implicit claim-shape difference. Red today: signer does
        not emit a token_use claim yet."""
        signer = self._signer()
        claims = _decode_jwt_payload(signer.sign_ws_token(
            project_hash="pa1234567890abcd", channel_id=1,
            invoker_user_id=2, invoker_role="user", secret=_SECRET_CURRENT,
        ))
        assert claims.get("token_use") == "ws", (
            "C-1: ws_token must carry explicit token_use='ws' "
            "(got %r)" % claims.get("token_use")
        )

    def test_C1_skill_bundle_token_carries_token_use_skill_bundle(self):
        """C-1: sign_skill_bundle_token stamps token_use='skill_bundle'."""
        signer = self._signer()
        claims = _decode_jwt_payload(signer.sign_skill_bundle_token(
            project_hash="pa1234567890abcd", project_id=99,
            workspace_ids=[1], secret=_SECRET_CURRENT,
        ))
        assert claims.get("token_use") == "skill_bundle", (
            "C-1: skill_bundle_token must carry explicit "
            "token_use='skill_bundle' (got %r)" % claims.get("token_use")
        )

    def test_verify_roundtrip_current_secret(self):
        """J.4: verify_token round-trips a token signed with the current secret."""
        signer = self._signer()
        token = signer.sign_ws_token(
            project_hash="pa1234567890abcd", channel_id=1,
            invoker_user_id=2, invoker_role="user", secret=_SECRET_CURRENT,
        )
        claims = signer.verify_token(
            token, current=_SECRET_CURRENT, previous=_SECRET_PREVIOUS,
        )
        assert claims["project_hash"] == "pa1234567890abcd"

    def test_verify_dual_secret_previous_fallback(self):
        """J.5: token signed with previous secret still verifies (§6.6 rotation)."""
        signer = self._signer()
        token = signer.sign_ws_token(
            project_hash="pa1234567890abcd", channel_id=1,
            invoker_user_id=2, invoker_role="user", secret=_SECRET_PREVIOUS,
        )
        claims = signer.verify_token(
            token, current=_SECRET_CURRENT, previous=_SECRET_PREVIOUS,
        )
        assert claims["project_hash"] == "pa1234567890abcd"

    def test_verify_rejects_unknown_secret(self):
        """J.6: token signed with neither current nor previous → rejected."""
        signer = self._signer()
        token = signer.sign_ws_token(
            project_hash="pa1234567890abcd", channel_id=1,
            invoker_user_id=2, invoker_role="user", secret="totally-other-secret",
        )
        with pytest.raises(Exception):
            signer.verify_token(
                token, current=_SECRET_CURRENT, previous=_SECRET_PREVIOUS,
            )

    def test_verify_rejects_expired(self):
        """J.7: expired token rejected (exp in the past)."""
        signer = self._signer()
        token = signer.sign_ws_token(
            project_hash="pa1234567890abcd", channel_id=1,
            invoker_user_id=2, invoker_role="user", secret=_SECRET_CURRENT,
            exp_seconds=-10,
        )
        with pytest.raises(Exception):
            signer.verify_token(
                token, current=_SECRET_CURRENT, previous=_SECRET_PREVIOUS,
            )

    def test_verify_jti_replay_rejected(self):
        """J.8: same jti verified twice → second rejected (replay defense)."""
        signer = self._signer()
        token = signer.sign_ws_token(
            project_hash="pa1234567890abcd", channel_id=1,
            invoker_user_id=2, invoker_role="user", secret=_SECRET_CURRENT,
        )
        first = signer.verify_token(
            token, current=_SECRET_CURRENT, previous=_SECRET_PREVIOUS,
        )
        assert first["project_hash"] == "pa1234567890abcd"
        with pytest.raises(Exception):
            signer.verify_token(
                token, current=_SECRET_CURRENT, previous=_SECRET_PREVIOUS,
            )


# --------------------------------------------------------------------------- #
# S.* — spawn-or-attach / reap / resume / invalidate / sync / delete / status   #
# --------------------------------------------------------------------------- #
_HASH = "pa1234567890abcd"
_NS = "paas-ws-pa1234567890abcd"
_RELEASE = "agent-pa-pa1234567890abcd"

_SPAWN_BODY = {
    "project_hash": _HASH,
    "project_id": 99,
    "channel_id": 42,
    "invoker_user_id": 7,
    "invoker_role": "user",
    "project_workspace_ids": [3, 5],
    "agent_template_snapshot": {"id": 1, "system_prompt": "hi"},
    "mcp_snapshot": {"servers": {}},
    "skill_snapshot": [{"name": "deploy", "version": "1.0.0", "sha256": "ab"}],
}


def test_pod_ws_url_matches_design_and_chart_service_contract():
    """_pod_ws_url must match design §B + agent-runtime chart Service:
    ``ws://<release>-svc.<ns>.svc.cluster.local:8000/v1/chat``.

    Regression guard for the operator/design/chart transport drift
    (was :8080 + /ws + bare release name without the -svc suffix).
    """
    from src.api.agent_runtimes import _pod_ws_url

    assert _pod_ws_url("paas-ws-pa-abc", "agent-pa-abc") == (
        "ws://agent-pa-abc-svc.paas-ws-pa-abc.svc.cluster.local:8000/v1/chat"
    )


# --------------------------------------------------------------------------- #
# Namespace identity — the agent-runtime ns must be DERIVED from the operator's #
# allowed prefix (settings.namespace_prefix) so it always nests inside the      #
# validate_namespace guard. Regression: spawn 500'd on stg where the operator   #
# is env-scoped to 'paas-ws-stg-' but _names hardcoded 'paas-ws-pa-' (outside   #
# the guard). Mocked unit tests of spawn never exercised validate_namespace —   #
# exactly the BUG-7/8/9 mock-hidden contract drift class.                       #
# --------------------------------------------------------------------------- #
class TestNamespacePrefixDerivation:
    def test_names_derives_namespace_from_configured_prefix(self):
        from src.api import agent_runtimes

        # env-scoped (stg) prefix → the ns must nest under it
        with patch("src.api.agent_runtimes.settings.namespace_prefix", "paas-ws-stg-"):
            ns, release = agent_runtimes._names("abc")
            assert ns == "paas-ws-stg-pa-abc"
            assert release == "agent-pa-abc"  # release name is prefix-independent
        # default prefix reproduces the original value (backward-compat)
        with patch("src.api.agent_runtimes.settings.namespace_prefix", "paas-ws-"):
            ns, _ = agent_runtimes._names("abc")
            assert ns == "paas-ws-pa-abc"

    def test_names_namespace_passes_validate_namespace_under_stg_prefix(self):
        """The exact spawn-500 regression: _names output must satisfy the
        operator's validate_namespace guard under a tightened env-scoped prefix.
        """
        from src.api import agent_runtimes
        from src.services.helm import validate_namespace

        with patch("src.api.agent_runtimes.settings.namespace_prefix", "paas-ws-stg-"):
            ns, _ = agent_runtimes._names("abc")
            validate_namespace(ns)  # must NOT raise (was ValueError before fix)

    def test_mcp_invalidator_namespace_tracks_names(self):
        """mcp_invalidator targets the SAME project ns as _names; both must
        derive from settings.namespace_prefix or the ConfigMap patch lands on
        the wrong namespace under a non-default prefix.
        """
        from src.api import agent_runtimes
        from src.services import mcp_invalidator

        with patch("src.api.agent_runtimes.settings.namespace_prefix", "paas-ws-stg-"):
            ns, _ = agent_runtimes._names("abc")
            assert mcp_invalidator._namespace("abc") == ns == "paas-ws-stg-pa-abc"


class TestSpawnOrAttach:
    """S.1-S.3 — spawn-or-attach cold start / attach / resume."""

    def test_spawn_or_attach_cold_start_signs_both_tokens(self, client):
        """S.1: cold start installs release, signs both tokens, returns ws url."""
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes.jwt_signer") as mock_signer:
            # release absent → cold start
            from src.services.helm import HelmException
            mock_helm.get.side_effect = HelmException(
                message="not found", command="helm get", stderr="release: not found",
            )
            mock_helm.install.return_value = MagicMock()
            mock_k8s.get_pods.return_value = [
                MagicMock(phase="Running", ready="1/1")
            ]
            mock_signer.sign_ws_token.return_value = "ws.jwt.token"
            mock_signer.sign_skill_bundle_token.return_value = "skill.jwt.token"

            resp = client.post("/api/agent-runtimes/spawn-or-attach", json=_SPAWN_BODY)

            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["pod_ws_url"]
            assert data.get("ws_token") or data.get("auth_token")
            assert "pod_state" in data
            assert "spawn_age_ms" in data
            # BOTH token scopes signed
            mock_signer.sign_ws_token.assert_called_once()
            mock_signer.sign_skill_bundle_token.assert_called_once()
            # skill_bundle_token must be injected into pod env at install time
            install_kwargs = json.dumps(
                mock_helm.install.call_args.kwargs, default=str,
            )
            install_args = json.dumps(
                mock_helm.install.call_args.args, default=str,
            )
            assert "SKILL_BUNDLE_TOKEN" in (install_kwargs + install_args), (
                "SKILL_BUNDLE_TOKEN must be passed to helm install as pod env"
            )

    def test_spawn_cold_start_labels_namespace_for_odoo_netpol(self, client):
        """Cold start must pre-create the agent-runtime namespace carrying the
        ``paas-managed=true`` LABEL: the Odoo NetworkPolicy authorises pod<->Odoo
        traffic by that label (name-agnostic), and ``helm --create-namespace``
        makes a BARE (unlabelled) ns. So the operator applies the labelled ns and
        installs into it (create_namespace=False).
        """
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes.jwt_signer") as mock_signer:
            from src.services.helm import HelmException
            mock_helm.get.side_effect = HelmException(
                message="not found", command="helm get", stderr="release: not found",
            )
            mock_helm.install.return_value = MagicMock()
            mock_k8s.get_pods.return_value = [MagicMock(phase="Running", ready="1/1")]
            mock_signer.sign_ws_token.return_value = "ws.jwt"
            mock_signer.sign_skill_bundle_token.return_value = "skill.jwt"

            resp = client.post("/api/agent-runtimes/spawn-or-attach", json=_SPAWN_BODY)
            assert resp.status_code == 200, resp.text

            ns_manifests = [
                c.args[0] for c in mock_k8s.apply_manifest.call_args_list
                if c.args and isinstance(c.args[0], dict)
                and c.args[0].get("kind") == "Namespace"
            ]
            assert ns_manifests, (
                "cold start must pre-create the agent-runtime Namespace "
                "(helm --create-namespace leaves it unlabelled)"
            )
            labels = ns_manifests[0].get("metadata", {}).get("labels", {})
            assert labels.get("paas-managed") == "true", (
                "agent-runtime ns must carry paas-managed=true for the Odoo NP"
            )
            # helm must NOT also race to create the (already labelled) namespace
            assert mock_helm.install.call_args.kwargs.get("create_namespace") is False

    def test_build_install_values_carries_all_chart_required_keys(self):
        """Contract test (operator <-> agent-runtime chart values). The chart's
        _helpers.tpl ``agent-runtime.validate`` fails the install at RENDER time
        unless every required value is set: projectHash + projectId +
        auth.currentKey (when manageSecret=true). _build_install_values MUST
        carry all of them or spawn 500s at chart render — the gate-3 class
        (projectHash shipped, projectId was missed-forward despite being a
        required field on the request). Locks the full required contract so a
        future missed-forward fails here, not in a live spawn.
        """
        import os
        from src.api.agent_runtimes import _build_install_values
        from src.models.schemas import SpawnOrAttachRequest

        req = SpawnOrAttachRequest(**_SPAWN_BODY)
        with patch.dict(os.environ,
                        {"AGENT_RUNTIME_AUTH_SECRET_CURRENT": "hs256-current-key"}):
            values = _build_install_values(req, "skill.jwt.token")

        assert values.get("projectHash") == _SPAWN_BODY["project_hash"]
        assert values.get("projectId") == _SPAWN_BODY["project_id"]
        assert values.get("auth", {}).get("currentKey") == "hs256-current-key"

    def test_spawn_or_attach_cold_start_passes_repo_and_auth_to_install(self, client):
        """gap C: cold-start install fetches the chart from the PRIVATE classic
        Helm repo — repo URL / chart version / read credentials are plumbed from
        settings into helm install (--repo/--version/--username/--password)."""
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes.jwt_signer") as mock_signer, \
             patch("src.api.agent_runtimes.settings.agent_runtime_repo_url",
                   "http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm"), \
             patch("src.api.agent_runtimes.settings.agent_runtime_chart_version", "0.1.0"), \
             patch("src.api.agent_runtimes.settings.agent_runtime_repo_username", "ci-bot"), \
             patch("src.api.agent_runtimes.settings.agent_runtime_repo_password", "gitea-token"):
            from src.services.helm import HelmException
            mock_helm.get.side_effect = HelmException(
                message="not found", command="helm get", stderr="release: not found",
            )
            mock_helm.install.return_value = MagicMock()
            mock_k8s.get_pods.return_value = [MagicMock(phase="Running", ready="1/1")]
            mock_signer.sign_ws_token.return_value = "ws.jwt.token"
            mock_signer.sign_skill_bundle_token.return_value = "skill.jwt.token"

            resp = client.post("/api/agent-runtimes/spawn-or-attach", json=_SPAWN_BODY)

            assert resp.status_code == 200, resp.text
            kwargs = mock_helm.install.call_args.kwargs
            assert kwargs.get("repo") == (
                "http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm"
            )
            assert kwargs.get("version") == "0.1.0"
            assert kwargs.get("username") == "ci-bot"
            assert kwargs.get("password") == "gitea-token"

    def test_spawn_cold_start_passes_auth_key_from_signing_secret(self, client):
        """(4) D-CAVEAT-1: the pod verifies ws_token with the SAME HS256 key the
        operator signs with. _build_install_values carries auth.currentKey from
        jwt_signer's own signing-secret source → pod key == sign key (zero drift)."""
        import os
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes.jwt_signer.sign_ws_token", return_value="ws.jwt"), \
             patch("src.api.agent_runtimes.jwt_signer.sign_skill_bundle_token", return_value="skill.jwt"), \
             patch.dict(os.environ, {"AGENT_RUNTIME_AUTH_SECRET_CURRENT": "hs256-current-key"}):
            from src.services.helm import HelmException
            mock_helm.get.side_effect = HelmException(
                message="not found", command="helm get", stderr="release: not found",
            )
            mock_helm.install.return_value = MagicMock()
            mock_k8s.get_pods.return_value = [MagicMock(phase="Running", ready="1/1")]

            resp = client.post("/api/agent-runtimes/spawn-or-attach", json=_SPAWN_BODY)

            assert resp.status_code == 200, resp.text
            values = mock_helm.install.call_args.kwargs["values"]
            assert values.get("auth", {}).get("currentKey") == "hs256-current-key", (
                "pod auth.currentKey must equal the operator's signing secret"
            )

    def test_spawn_cold_start_omits_auth_when_signing_secret_unset(self, client):
        """Backward-compat: no AGENT_RUNTIME_AUTH_SECRET_CURRENT → no auth block
        in install values (unconfigured path unchanged)."""
        import os
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes.jwt_signer.sign_ws_token", return_value="ws.jwt"), \
             patch("src.api.agent_runtimes.jwt_signer.sign_skill_bundle_token", return_value="skill.jwt"), \
             patch.dict(os.environ, {"AGENT_RUNTIME_AUTH_SECRET_CURRENT": ""}):
            from src.services.helm import HelmException
            mock_helm.get.side_effect = HelmException(
                message="not found", command="helm get", stderr="release: not found",
            )
            mock_helm.install.return_value = MagicMock()
            mock_k8s.get_pods.return_value = [MagicMock(phase="Running", ready="1/1")]

            resp = client.post("/api/agent-runtimes/spawn-or-attach", json=_SPAWN_BODY)

            assert resp.status_code == 200, resp.text
            values = mock_helm.install.call_args.kwargs["values"]
            assert "auth" not in values

    def test_spawn_or_attach_attach_existing_is_noop(self, client):
        """S.2: release already running (replicas=1) → no re-install."""
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes.jwt_signer") as mock_signer:
            mock_helm.get.return_value = MagicMock(status="deployed")
            mock_k8s.get_deployment_replicas.return_value = 1
            mock_k8s.get_pods.return_value = [MagicMock(phase="Running", ready="1/1")]
            mock_signer.sign_ws_token.return_value = "ws.jwt.token"
            mock_signer.sign_skill_bundle_token.return_value = "skill.jwt.token"

            resp = client.post("/api/agent-runtimes/spawn-or-attach", json=_SPAWN_BODY)

            assert resp.status_code == 200, resp.text
            mock_helm.install.assert_not_called()

    def test_spawn_or_attach_resume_resigns_token_before_scale(self, client):
        """S.3 / §2.11.1: replicas=0 → re-sign skill_bundle_token + patch env
        BEFORE scaling to 1 (token lifecycle — avoid stale token race)."""
        call_order = []
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes.jwt_signer") as mock_signer:
            mock_helm.get.return_value = MagicMock(status="deployed")
            mock_k8s.get_deployment_replicas.return_value = 0  # reaped
            mock_k8s.get_pods.return_value = [MagicMock(phase="Running", ready="1/1")]
            mock_signer.sign_ws_token.return_value = "ws.jwt.token"
            mock_signer.sign_skill_bundle_token.side_effect = (
                lambda *a, **k: call_order.append("resign") or "fresh.skill.token"
            )

            def _record_env_patch(*a, **k):
                blob = json.dumps([a, k], default=str)
                if "SKILL_BUNDLE_TOKEN" in blob:
                    call_order.append("patch_env")
            mock_k8s.patch_deployment.side_effect = _record_env_patch
            mock_k8s.set_deployment_env.side_effect = _record_env_patch

            def _record_scale(*a, **k):
                call_order.append("scale")
            mock_helm.scale.side_effect = _record_scale
            mock_k8s.scale_deployment.side_effect = _record_scale

            resp = client.post("/api/agent-runtimes/spawn-or-attach", json=_SPAWN_BODY)

            assert resp.status_code == 200, resp.text
            assert "resign" in call_order, (
                "resume must RE-SIGN skill_bundle_token (§2.11.1)"
            )
            assert "patch_env" in call_order, (
                "resume must patch pod env with the fresh SKILL_BUNDLE_TOKEN"
            )
            assert "scale" in call_order, "resume must scale the deployment to 1"
            assert call_order.index("patch_env") < call_order.index("scale"), (
                "env patch must happen BEFORE scale-1 (§2.11.1 race avoidance)"
            )


class TestReap:
    """S.4 — reap idempotency."""

    def test_reap_scales_to_zero(self, client):
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s:
            mock_helm.get.return_value = MagicMock(status="deployed")
            mock_k8s.get_deployment_replicas.return_value = 1
            resp = client.post(f"/api/agent-runtimes/{_HASH}/reap")
            assert resp.status_code == 200, resp.text
            assert resp.json()["action"] == "reaped"

    def test_reap_already_reaped(self, client):
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s:
            mock_helm.get.return_value = MagicMock(status="deployed")
            mock_k8s.get_deployment_replicas.return_value = 0
            resp = client.post(f"/api/agent-runtimes/{_HASH}/reap")
            assert resp.status_code == 200, resp.text
            assert resp.json()["action"] == "already_reaped"

    def test_reap_not_found(self, client):
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service"):
            from src.services.helm import HelmException
            mock_helm.get.side_effect = HelmException(
                message="not found", command="helm get", stderr="release: not found",
            )
            resp = client.post(f"/api/agent-runtimes/{_HASH}/reap")
            assert resp.status_code == 200, resp.text
            assert resp.json()["action"] == "not_found"


class TestResume:
    """S.5 / §2.11.1 — dedicated resume re-signs token + patches env before scale."""

    def test_resume_resigns_and_patches_env_before_scale(self, client):
        call_order = []
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s, \
             patch("src.api.agent_runtimes.jwt_signer") as mock_signer:
            mock_helm.get.return_value = MagicMock(status="deployed")
            mock_k8s.get_deployment_replicas.return_value = 0
            mock_k8s.get_pods.return_value = [MagicMock(phase="Running", ready="1/1")]
            mock_signer.sign_skill_bundle_token.side_effect = (
                lambda *a, **k: call_order.append("resign") or "fresh.token"
            )
            mock_signer.sign_ws_token.return_value = "ws.token"

            def _env_patch(*a, **k):
                if "SKILL_BUNDLE_TOKEN" in json.dumps([a, k], default=str):
                    call_order.append("patch_env")
            mock_k8s.patch_deployment.side_effect = _env_patch
            mock_k8s.set_deployment_env.side_effect = _env_patch
            mock_helm.scale.side_effect = lambda *a, **k: call_order.append("scale")
            mock_k8s.scale_deployment.side_effect = lambda *a, **k: call_order.append("scale")

            resp = client.post(f"/api/agent-runtimes/{_HASH}/resume")

            assert resp.status_code == 200, resp.text
            assert "resign" in call_order and "patch_env" in call_order
            assert call_order.index("patch_env") < call_order.index("scale")


class TestInvalidateMcp:
    """S.6 — delegates to mcp_invalidator."""

    def test_invalidate_mcp_delegates(self, client):
        with patch("src.api.agent_runtimes.mcp_invalidator") as mock_inv:
            mock_inv.invalidate.return_value = {"status": "ok"}
            resp = client.post(
                f"/api/agent-runtimes/{_HASH}/invalidate-mcp",
                json={"mcp_snapshot": {"servers": {}}},
            )
            assert resp.status_code == 200, resp.text
            mock_inv.invalidate.assert_called_once()


class TestSyncWorkspaces:
    """S.7 — helm upgrade re-renders NetworkPolicy egress allowlist."""

    def test_sync_workspaces_helm_upgrade(self, client):
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service"):
            mock_helm.get.return_value = MagicMock(status="deployed")
            mock_helm.upgrade.return_value = MagicMock()
            resp = client.post(
                f"/api/agent-runtimes/{_HASH}/sync-workspaces",
                json={"project_workspace_ids": [3, 5, 8]},
            )
            assert resp.status_code == 200, resp.text
            mock_helm.upgrade.assert_called_once()

    def test_sync_workspaces_passes_repo_and_auth_to_upgrade(self, client):
        """gap C: sync-workspaces helm upgrade must resolve the same private-repo
        chart — repo URL / version / credentials plumbed from settings."""
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service"), \
             patch("src.api.agent_runtimes.settings.agent_runtime_repo_url",
                   "http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm"), \
             patch("src.api.agent_runtimes.settings.agent_runtime_chart_version", "0.1.0"), \
             patch("src.api.agent_runtimes.settings.agent_runtime_repo_username", "ci-bot"), \
             patch("src.api.agent_runtimes.settings.agent_runtime_repo_password", "gitea-token"):
            mock_helm.get.return_value = MagicMock(status="deployed")
            mock_helm.upgrade.return_value = MagicMock()
            resp = client.post(
                f"/api/agent-runtimes/{_HASH}/sync-workspaces",
                json={"project_workspace_ids": [3, 5, 8]},
            )
            assert resp.status_code == 200, resp.text
            kwargs = mock_helm.upgrade.call_args.kwargs
            assert kwargs.get("repo") == (
                "http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm"
            )
            assert kwargs.get("version") == "0.1.0"
            assert kwargs.get("username") == "ci-bot"
            assert kwargs.get("password") == "gitea-token"


class TestDeleteRuntime:
    """S.8 — helm uninstall."""

    def test_delete_uninstalls(self, client):
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service"):
            mock_helm.uninstall.return_value = {"message": "uninstalled"}
            resp = client.delete(f"/api/agent-runtimes/{_HASH}")
            assert resp.status_code == 200, resp.text
            mock_helm.uninstall.assert_called_once()


class TestRuntimeStatus:
    """S.9 — status returns lifecycle snapshot."""

    def test_status_returns_state(self, client):
        with patch("src.api.agent_runtimes.helm_service") as mock_helm, \
             patch("src.api.agent_runtimes.k8s_service") as mock_k8s:
            mock_helm.get.return_value = MagicMock(status="deployed")
            mock_k8s.get_deployment_replicas.return_value = 1
            mock_k8s.get_pods.return_value = [MagicMock(phase="Running", ready="1/1")]
            resp = client.get(f"/api/agent-runtimes/{_HASH}/status")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert "state" in data
            assert "pod_ws_url" in data


# --------------------------------------------------------------------------- #
# W.* — webhook_dispatcher (HMAC sign + retry)                                  #
# --------------------------------------------------------------------------- #
class TestWebhookDispatcher:
    """§5.5 — outbound pod→Odoo state-update with HMAC-SHA256 + retry."""

    def test_dispatcher_signs_hmac_header(self):
        """W.1: body signed HMAC-SHA256 under secret → X-PaaS-Signature header."""
        import hashlib
        import hmac as _hmac
        from src.services import webhook_dispatcher

        secret = "webhook-secret-xyz"
        captured = {}

        def _fake_post(url, data=None, headers=None, timeout=None, **kw):
            captured["url"] = url
            captured["body"] = data
            captured["headers"] = headers or {}
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch.object(webhook_dispatcher, "_http_post", _fake_post, create=True):
            webhook_dispatcher.dispatch_state_update(
                odoo_base_url="http://odoo.local",
                payload={"project_hash": _HASH, "new_state": "active"},
                secret=secret,
            )

        sig_header = None
        for k, v in captured["headers"].items():
            if k.lower() == "x-paas-signature":
                sig_header = v
        assert sig_header, "X-PaaS-Signature header missing"
        body_bytes = (
            captured["body"].encode() if isinstance(captured["body"], str)
            else captured["body"]
        )
        expected = _hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        assert expected in sig_header, "signature must be HMAC-SHA256(body, secret)"

    def test_dispatcher_retries_on_5xx(self):
        """W.2: 5xx → retry up to 3 attempts (exp backoff)."""
        from src.services import webhook_dispatcher

        attempts = {"n": 0}

        def _flaky_post(url, data=None, headers=None, timeout=None, **kw):
            attempts["n"] += 1
            resp = MagicMock()
            resp.status_code = 200 if attempts["n"] >= 3 else 503
            return resp

        with patch.object(webhook_dispatcher, "_http_post", _flaky_post, create=True), \
             patch.object(webhook_dispatcher, "time") as mock_time:
            mock_time.sleep = MagicMock()
            webhook_dispatcher.dispatch_state_update(
                odoo_base_url="http://odoo.local",
                payload={"project_hash": _HASH, "new_state": "reaped"},
                secret="s",
            )
        assert attempts["n"] >= 3, "dispatcher must retry the 5xx responses"


# --------------------------------------------------------------------------- #
# M.* — mcp_invalidator (ConfigMap patch)                                       #
# --------------------------------------------------------------------------- #
class TestMcpInvalidator:
    """§5.4 — patch the mcp.json ConfigMap for a project namespace."""

    def test_invalidator_patches_configmap(self):
        from src.services import mcp_invalidator

        with patch.object(mcp_invalidator, "k8s_service", create=True) as mock_k8s:
            mcp_invalidator.invalidate(
                project_hash=_HASH,
                mcp_snapshot={"servers": {"a": {}}},
            )
            patched = (
                mock_k8s.patch_configmap.called
                or mock_k8s.apply_manifest.called
                or mock_k8s.replace_configmap.called
            )
            assert patched, "invalidator must patch/replace the mcp.json ConfigMap"


# --------------------------------------------------------------------------- #
# R.1 — REGRESSION: existing routers untouched (§2.11.2)                         #
# --------------------------------------------------------------------------- #
class TestExistingRoutersUntouched:
    """R.1: the new router must not disturb releases/namespaces (regression lock)."""

    def test_releases_invalid_namespace_still_400(self, client):
        with patch("src.api.releases.helm_service") as mock_helm:
            mock_helm.install.side_effect = ValueError("Invalid namespace")
            resp = client.post(
                "/api/releases",
                json={"namespace": "invalid", "name": "t", "chart": "nginx"},
            )
            assert resp.status_code == 400

    def test_namespaces_invalid_name_still_400(self, client):
        with patch("src.api.namespaces.k8s_service") as mock_k8s:
            mock_k8s.create_namespace.side_effect = ValueError("Invalid namespace")
            resp = client.post(
                "/api/namespaces",
                json={
                    "name": "invalid", "cpu_limit": "2",
                    "memory_limit": "4Gi", "storage_limit": "20Gi",
                },
            )
            assert resp.status_code == 400

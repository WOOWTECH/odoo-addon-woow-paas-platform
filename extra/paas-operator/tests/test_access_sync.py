"""Tests for Cloudflare Access (Zero Trust) sync on Security Access."""
from unittest.mock import AsyncMock, patch

import pytest

from src.services.cloudflare import CloudflareService, CloudflareException
from src.models.schemas import (
    AccessSyncRequest,
    AccessPolicySpec,
)


def test_access_schemas_importable():
    req = AccessSyncRequest(
        enabled=True, canonical_name="paas-sa-x-y", app_name="x",
        primary_hostname="paas-sa-x-y.woowtech.io",
        protected_hostnames=["paas-sa-x-y.woowtech.io"],
        session_duration="8h", login_idp_types=["google", "onetimepin"],
        policies=[AccessPolicySpec(name="p1", decision="allow",
                                   rule_type="email_domain",
                                   rule_value="acme.com", precedence=1)],
    )
    assert req.enabled is True
    assert req.policies[0].decision == "allow"


@pytest.fixture
def cf_service():
    svc = CloudflareService()
    svc.api_token = "test-token"
    svc.account_id = "test-account"
    svc.zone_id = "test-zone"
    svc.domain = "woowtech.io"
    svc.enabled = True
    return svc


class TestListIdentityProviders:
    @pytest.mark.asyncio
    async def test_lists_and_filters_by_type(self, cf_service):
        payload = {"success": True, "result": [
            {"id": "idp-g", "name": "Google", "type": "google"},
            {"id": "idp-otp", "name": "One-time PIN", "type": "onetimepin"},
            {"id": "idp-gh", "name": "GH", "type": "github"},
        ]}

        class FakeResponse:
            status_code = 200
            @staticmethod
            def json():
                return payload

        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(return_value=FakeResponse())):
            idps = await cf_service.list_identity_providers()
        by_type = {i["type"]: i["id"] for i in idps}
        assert by_type["google"] == "idp-g"
        assert by_type["onetimepin"] == "idp-otp"

    @pytest.mark.asyncio
    async def test_raises_on_403(self, cf_service):
        class FakeResponse:
            status_code = 403
            text = "forbidden"
            @staticmethod
            def json():
                return {"success": False}

        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(return_value=FakeResponse())):
            with pytest.raises(CloudflareException):
                await cf_service.list_identity_providers()


class TestReusablePolicyCrud:
    @pytest.mark.asyncio
    async def test_create_reusable_policy_email_domain(self, cf_service):
        captured = {}

        class FakeResponse:
            status_code = 201
            @staticmethod
            def json():
                return {"success": True, "result": {"id": "pol-1"}}

        async def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

        with patch("httpx.AsyncClient.post",
                   new=AsyncMock(side_effect=fake_post)):
            pid = await cf_service.create_reusable_policy(
                name="paas-sa-x-y: corp", decision="allow",
                include=[{"email_domain": {"domain": "acme.com"}}])
        assert pid == "pol-1"
        assert captured["json"]["decision"] == "allow"
        assert captured["json"]["include"] == [
            {"email_domain": {"domain": "acme.com"}}]
        assert captured["json"]["reusable"] is True
        assert "/access/policies" in captured["url"]

    @pytest.mark.asyncio
    async def test_delete_reusable_policy(self, cf_service):
        class FakeResponse:
            status_code = 200
            @staticmethod
            def json():
                return {"success": True, "result": {"id": "pol-1"}}

        with patch("httpx.AsyncClient.delete",
                   new=AsyncMock(return_value=FakeResponse())):
            ok = await cf_service.delete_reusable_policy("pol-1")
        assert ok is True


class TestSyncAccessApp:
    @pytest.mark.asyncio
    async def test_enabled_creates_app_and_policies(self, cf_service):
        calls = {"app_put": None, "app_post": None, "policy_posts": [],
                 "list_apps": True}

        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    # identity_providers list (type→id) and app list (none yet)
                    if "identity_providers" in url:
                        return {"success": True, "result": [
                            {"id": "idp-g", "name": "G", "type": "google"},
                            {"id": "idp-otp", "name": "O", "type": "onetimepin"},
                        ]}
                    return {"success": True, "result": []}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            class R:
                status_code = 201
                @staticmethod
                def json():
                    if "/policies" in url:
                        return {"success": True, "result": {"id": "pol-new"}}
                    return {"success": True, "result": {"id": "app-new"}}
            if "/policies" in url:
                calls["policy_posts"].append(json)
            else:
                # The app create-POST body — this is what actually decides
                # protection, captured here so the test can assert its shape.
                calls["app_post"] = json
            return R()

        async def fake_put(url, headers=None, json=None, timeout=None):
            calls["app_put"] = json
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "app-existing"}}
            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)), \
             patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake_put)), \
             patch("httpx.AsyncClient.delete", new=AsyncMock(return_value=type(
                 "R", (), {"status_code": 200,
                           "json": staticmethod(lambda: {"success": True,
                                                         "result": {"id": "x"}})})())):
            result = await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y",
                app_name="my-gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=["paas-sa-x-y.woowtech.io",
                                     "waltek-ha.woowtech.io"],
                session_duration="8h",
                login_idp_types=["google", "onetimepin"],
                policies=[{"name": "corp", "decision": "allow",
                           "rule_type": "email_domain", "rule_value": "acme.com",
                           "precedence": 1},
                          {"name": "block-bad", "decision": "deny",
                           "rule_type": "email_domain",
                           "rule_value": "bad.com", "precedence": 5}],
                existing_app_id=None,
                existing_policy_ids=[],
            )
        # allowed_idps resolved from types
        assert set(result["allowed_idps"]) == {"idp-g", "idp-otp"}
        # two reusable policies created (allow precedence 1, deny precedence 5)
        assert len(calls["policy_posts"]) == 2
        assert calls["policy_posts"][0]["include"] == [
            {"email_domain": {"domain": "acme.com"}}]
        assert result["app_id"]
        # The app create-POST body is the security-load-bearing shape: it
        # decides protection. A regression that dropped a protected_hostname
        # from destinations (app protecting nothing), reordered precedence
        # (block-before-allow inversion), flipped auto_redirect_to_identity to
        # True, or changed type would pass every other assertion green — so
        # pin the body explicitly here.
        app = calls["app_post"]
        assert app is not None, "app create-POST body was not captured"
        assert app["type"] == "self_hosted"
        assert app["session_duration"] == "8h"
        assert app["auto_redirect_to_identity"] is False
        # destinations must map EVERY protected hostname (none dropped).
        dest_uris = [d["uri"] for d in app["destinations"]]
        assert dest_uris == ["paas-sa-x-y.woowtech.io", "waltek-ha.woowtech.io"]
        assert all(d["type"] == "public" for d in app["destinations"])
        # allowed_idps on the app match the resolved set.
        assert set(app["allowed_idps"]) == {"idp-g", "idp-otp"}
        # policies carry the caller's precedence in order (allow 1, deny 5):
        # a block-before-allow inversion or reorder would flip these values.
        precedences = [p["precedence"] for p in app["policies"]]
        assert precedences == [1, 5]

    @pytest.mark.asyncio
    async def test_disabled_tears_down(self, cf_service):
        deleted = {"app": [], "policies": []}

        async def fake_delete(url, headers=None, timeout=None):
            if "/apps/" in url:
                deleted["app"].append(url)
            elif "/policies/" in url:
                deleted["policies"].append(url)
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "x"}}
            return R()

        with patch("httpx.AsyncClient.delete",
                   new=AsyncMock(side_effect=fake_delete)):
            result = await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y", app_name="my-gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=[], session_duration="8h",
                login_idp_types=[], policies=[], enabled=False,
                existing_app_id="app-old", existing_policy_ids=["pol-a", "pol-b"])
        assert len(deleted["app"]) == 1
        assert len(deleted["policies"]) == 2
        assert result["app_id"] is None
        assert result["policy_ids"] == []

    @pytest.mark.asyncio
    async def test_enabled_but_no_protected_hostnames_tears_down(self, cf_service):
        """enabled=True but zero protected hostnames is a reachable state (Access
        toggled on, no is_protected route). It must hit the SAME teardown branch
        as enabled=False — never fall through to app creation with an empty
        destinations list (CF would reject, or worse build an app protecting
        nothing). Locks the second teardown trigger (`not protected_hostnames`)."""
        deleted = {"app": [], "policies": []}
        created = {"app": False, "policies": []}

        async def fake_delete(url, headers=None, timeout=None):
            if "/apps/" in url:
                deleted["app"].append(url)
            elif "/policies/" in url:
                deleted["policies"].append(url)
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "x"}}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            if "/policies" in url:
                created["policies"].append(json)
            else:
                created["app"] = True
            class R:
                status_code = 201
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "should-not"}}
            return R()

        with patch("httpx.AsyncClient.delete",
                   new=AsyncMock(side_effect=fake_delete)), \
             patch("httpx.AsyncClient.post",
                   new=AsyncMock(side_effect=fake_post)):
            result = await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y", app_name="my-gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=[], session_duration="8h",
                login_idp_types=["google"],
                policies=[{"name": "corp", "decision": "allow",
                           "rule_type": "email_domain", "rule_value": "acme.com",
                           "precedence": 1}],
                enabled=True,
                existing_app_id="app-old", existing_policy_ids=["pol-a"])
        # Teardown branch taken, NOT app creation.
        assert result["app_id"] is None
        assert result["policy_ids"] == []
        assert created["app"] is False
        assert created["policies"] == []
        assert len(deleted["app"]) == 1
        assert len(deleted["policies"]) == 1

    @pytest.mark.asyncio
    async def test_app_update_failure_reclaims_new_policies(self, cf_service):
        """If the app PUT/POST fails AFTER reusable policies were created, those
        just-created policies must be best-effort deleted before re-raising —
        otherwise the caller's DB rollback loses all reference to them and they
        become permanent Cloudflare orphans (no future re-sync ever reclaims
        them). Locks the two-stage compensating-cleanup guard."""
        created_pids = []
        deleted_pids = []

        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    if "identity_providers" in url:
                        return {"success": True, "result": [
                            {"id": "idp-g", "name": "G", "type": "google"}]}
                    return {"success": True, "result": []}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            if "/policies" in url:
                pid = f"pol-new-{len(created_pids)}"
                created_pids.append(pid)
                class RP:
                    status_code = 201
                    @staticmethod
                    def json():
                        return {"success": True, "result": {"id": pid}}
                return RP()
            # App POST (create-new path) fails with 500.
            class RA:
                status_code = 500
                text = "cf boom"
                @staticmethod
                def json():
                    return {"success": False}
            return RA()

        async def fake_delete(url, headers=None, timeout=None):
            if "/policies/" in url:
                deleted_pids.append(url.rsplit("/", 1)[-1])
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "x"}}
            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)), \
             patch("httpx.AsyncClient.delete",
                   new=AsyncMock(side_effect=fake_delete)):
            with pytest.raises(CloudflareException):
                await cf_service.sync_access_app(
                    canonical_name="paas-sa-x-y", app_name="my-gw",
                    primary_hostname="paas-sa-x-y.woowtech.io",
                    protected_hostnames=["paas-sa-x-y.woowtech.io"],
                    session_duration="8h", login_idp_types=["google"],
                    policies=[{"name": "corp", "decision": "allow",
                               "rule_type": "email_domain",
                               "rule_value": "acme.com", "precedence": 1}],
                    enabled=True,
                    existing_app_id=None, existing_policy_ids=[])
        # The policy created in this batch was reclaimed (no orphan).
        assert created_pids, "expected a reusable policy to have been created"
        assert set(deleted_pids) == set(created_pids)


    @pytest.mark.asyncio
    async def test_resync_stale_policy_delete_failure_is_tolerated(self, cf_service):
        """Resync path (enabled=True, protected hostnames present): before
        recreating, it deletes each existing_policy_id. A delete that raises
        CloudflareException is swallowed (warn + continue) so the recreate is NOT
        aborted — that stale CF policy becomes an orphan but the app converges to
        the desired set. This locks the swallow as DELIBERATE tolerance (not a
        fail-fast), and proves the returned policy_ids are the freshly-created
        set, NOT the un-deletable stale id. If someone changed this to fail-fast
        or wrongly returned the stale id, this test goes red."""
        deleted_attempts = []

        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    if "identity_providers" in url:
                        return {"success": True, "result": [
                            {"id": "idp-g", "name": "G", "type": "google"}]}
                    return {"success": True, "result": []}
            return R()

        async def fake_delete(url, headers=None, timeout=None):
            pid = url.rsplit("/", 1)[-1]
            deleted_attempts.append(pid)
            if pid == "pol-stuck":
                # This stale reusable policy cannot be deleted (e.g. CF 5xx).
                raise CloudflareException("cannot delete stale policy",
                                          status_code=500)
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": pid}}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            class R:
                status_code = 201
                @staticmethod
                def json():
                    if "/policies" in url:
                        return {"success": True, "result": {"id": "pol-fresh"}}
                    return {"success": True, "result": {"id": "app-new"}}
            return R()

        async def fake_put(url, headers=None, json=None, timeout=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "app-existing"}}
            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)), \
             patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake_put)), \
             patch("httpx.AsyncClient.delete",
                   new=AsyncMock(side_effect=fake_delete)):
            result = await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y", app_name="gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=["paas-sa-x-y.woowtech.io"],
                session_duration="8h", login_idp_types=["google"],
                policies=[{"name": "corp", "decision": "allow",
                           "rule_type": "email_domain", "rule_value": "acme.com",
                           "precedence": 1}],
                enabled=True, existing_app_id="app-existing",
                existing_policy_ids=["pol-ok", "pol-stuck"])
        # Both stale ids were ATTEMPTED for deletion (the failing one did not
        # short-circuit the loop).
        assert set(deleted_attempts) >= {"pol-ok", "pol-stuck"}
        # Recreate completed despite the swallowed failure: app + fresh policy.
        assert result["app_id"] == "app-existing"
        # Returned ids are the FRESH set, never the un-deletable stale id.
        assert result["policy_ids"] == ["pol-fresh"]
        assert "pol-stuck" not in result["policy_ids"]

    @pytest.mark.asyncio
    async def test_app_put_update_failure_reclaims_new_policies(self, cf_service):
        """PUT-update variant of the compensating-cleanup guard. The existing
        reclaim test only drives existing_app_id=None (POST-create). For an
        already-provisioned SA the common operational path is PUT-update
        (existing_app_id set); that branch shares the try/except but takes a
        different request call. A future early-return / status tweak in the PUT
        branch would silently break the reclaim. Drive a PUT that 500s after
        policies were created and assert the new batch is reclaimed."""
        created_pids = []
        deleted_pids = []

        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    if "identity_providers" in url:
                        return {"success": True, "result": [
                            {"id": "idp-g", "name": "G", "type": "google"}]}
                    return {"success": True, "result": []}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            # Only policy POSTs happen on the PUT-update path (app uses PUT).
            pid = f"pol-new-{len(created_pids)}"
            created_pids.append(pid)
            class RP:
                status_code = 201
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": pid}}
            return RP()

        async def fake_put(url, headers=None, json=None, timeout=None):
            # App PUT-update fails with 500 after policies were created.
            class RA:
                status_code = 500
                text = "cf boom on put"
                @staticmethod
                def json():
                    return {"success": False}
            return RA()

        async def fake_delete(url, headers=None, timeout=None):
            if "/policies/" in url:
                deleted_pids.append(url.rsplit("/", 1)[-1])
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "x"}}
            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)), \
             patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake_put)), \
             patch("httpx.AsyncClient.delete",
                   new=AsyncMock(side_effect=fake_delete)):
            with pytest.raises(CloudflareException):
                await cf_service.sync_access_app(
                    canonical_name="paas-sa-x-y", app_name="gw",
                    primary_hostname="paas-sa-x-y.woowtech.io",
                    protected_hostnames=["paas-sa-x-y.woowtech.io"],
                    session_duration="8h", login_idp_types=["google"],
                    policies=[{"name": "corp", "decision": "allow",
                               "rule_type": "email_domain",
                               "rule_value": "acme.com", "precedence": 1}],
                    enabled=True,
                    existing_app_id="app-existing", existing_policy_ids=[])
        # The policy created on the PUT-update path was reclaimed (no orphan).
        assert created_pids, "expected a reusable policy to have been created"
        assert set(deleted_pids) == set(created_pids)


class TestBuildAccessRule:
    """_build_access_rule maps (rule_type, value) → the CF include shape that
    actually decides who passes Access auth. A wrong key would silently
    allow/deny the wrong principals, so every branch is pinned here."""

    def test_email_domain(self):
        assert CloudflareService._build_access_rule(
            "email_domain", "@acme.com ") == [
            {"email_domain": {"domain": "acme.com"}}]

    def test_email_list_splits_and_strips(self):
        assert CloudflareService._build_access_rule(
            "email_list", " a@x.com , b@y.com ,") == [
            {"email": {"email": "a@x.com"}},
            {"email": {"email": "b@y.com"}}]

    def test_ip_range(self):
        assert CloudflareService._build_access_rule(
            "ip_range", " 203.0.113.0/24 ") == [
            {"ip": {"ip": "203.0.113.0/24"}}]

    def test_unknown_rule_type_raises(self):
        with pytest.raises(CloudflareException):
            CloudflareService._build_access_rule("nope", "whatever")


class TestResolveIdpIds:
    """_resolve_idp_ids security-relevant branch: a requested login method with
    no matching account IdP is silently dropped → if that empties the set the
    app falls back to ALL IdPs (auth surface widened). The caller must learn of
    it, so the unresolved types are returned, not just logged."""

    @pytest.fixture
    def cf_service(self):
        svc = CloudflareService()
        svc.api_token = "test-token"
        svc.account_id = "test-account"
        svc.zone_id = "test-zone"
        svc.domain = "woowtech.io"
        svc.enabled = True
        return svc

    @pytest.mark.asyncio
    async def test_missing_type_reported_not_silently_dropped(self, cf_service):
        # Account only has Google; user requested google + onetimepin.
        async def fake_list():
            return [{"id": "idp-g", "name": "Google", "type": "google"}]

        with patch.object(cf_service, "list_identity_providers",
                          new=AsyncMock(side_effect=fake_list)):
            resolved, unresolved = await cf_service._resolve_idp_ids(
                ["google", "onetimepin"])
        assert resolved == ["idp-g"]
        assert unresolved == ["onetimepin"]

    @pytest.mark.asyncio
    async def test_all_requested_missing_widens_to_all_and_reports(
            self, cf_service):
        # User requested ONLY Google but account has no Google IdP →
        # resolved=[] (= app shows all IdPs). This is the fail-open widening
        # the reviewer flagged: it must surface in unresolved.
        async def fake_list():
            return [{"id": "idp-otp", "name": "OTP", "type": "onetimepin"}]

        with patch.object(cf_service, "list_identity_providers",
                          new=AsyncMock(side_effect=fake_list)):
            resolved, unresolved = await cf_service._resolve_idp_ids(["google"])
        assert resolved == []
        assert unresolved == ["google"]

    @pytest.mark.asyncio
    async def test_empty_request_is_not_unresolved(self, cf_service):
        resolved, unresolved = await cf_service._resolve_idp_ids([])
        assert resolved == []
        assert unresolved == []

    @pytest.mark.asyncio
    async def test_sync_propagates_unresolved_to_result(self, cf_service):
        """End-to-end: sync_access_app surfaces unresolved_idp_types in its
        return so Odoo can persist/warn on the silent login widening."""
        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    if "identity_providers" in url:
                        return {"success": True, "result": [
                            {"id": "idp-g", "name": "G", "type": "google"}]}
                    return {"success": True, "result": []}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            class R:
                status_code = 201
                @staticmethod
                def json():
                    if "/policies" in url:
                        return {"success": True, "result": {"id": "pol-x"}}
                    return {"success": True, "result": {"id": "app-x"}}
            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
            result = await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y", app_name="gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=["paas-sa-x-y.woowtech.io"],
                session_duration="8h",
                login_idp_types=["google", "onetimepin"],
                policies=[{"name": "corp", "decision": "allow",
                           "rule_type": "email_domain", "rule_value": "acme.com",
                           "precedence": 1}],
                enabled=True, existing_app_id=None, existing_policy_ids=[])
        assert result["allowed_idps"] == ["idp-g"]
        assert result["unresolved_idp_types"] == ["onetimepin"]


from unittest.mock import patch as sync_patch
from fastapi.testclient import TestClient
from src.main import app


@pytest.fixture
def client():
    with sync_patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestAccessEndpoints:
    @sync_patch("src.api.access.cloudflare_service")
    def test_sync_access_happy_path(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.sync_access_app = AsyncMock(return_value={
            "app_id": "app-1", "policy_ids": ["pol-1"],
            "allowed_idps": ["idp-g", "idp-otp"]})
        resp = client.put("/api/access/sync", json={
            "enabled": True, "canonical_name": "paas-sa-x-y", "app_name": "gw",
            "primary_hostname": "paas-sa-x-y.woowtech.io",
            "protected_hostnames": ["paas-sa-x-y.woowtech.io"],
            "session_duration": "8h", "login_idp_types": ["google", "onetimepin"],
            "policies": [{"name": "corp", "decision": "allow",
                          "rule_type": "email_domain", "rule_value": "acme.com",
                          "precedence": 1}],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["app_id"] == "app-1"
        assert data["policy_ids"] == ["pol-1"]
        call = mock_cf.sync_access_app.call_args
        assert call.kwargs["login_idp_types"] == ["google", "onetimepin"]

    @sync_patch("src.api.access.cloudflare_service")
    def test_sync_forwards_existing_ids(self, mock_cf, client):
        """The endpoint reads existing_app_id / existing_policy_ids off the
        request and forwards them to the service. They decide PUT-update vs
        POST-create and which stale policies get deleted — a getattr typo that
        dropped them to None would force every sync into create-new + orphan the
        old policies. Lock the route-level passthrough end-to-end."""
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.sync_access_app = AsyncMock(return_value={
            "app_id": "app-existing", "policy_ids": ["pol-z"],
            "allowed_idps": [], "unresolved_idp_types": []})
        resp = client.put("/api/access/sync", json={
            "enabled": True, "canonical_name": "paas-sa-x-y", "app_name": "gw",
            "primary_hostname": "paas-sa-x-y.woowtech.io",
            "protected_hostnames": ["paas-sa-x-y.woowtech.io"],
            "session_duration": "8h", "login_idp_types": [],
            "policies": [],
            "existing_app_id": "app-existing",
            "existing_policy_ids": ["pol-old-1", "pol-old-2"]})
        assert resp.status_code == 200, resp.text
        call = mock_cf.sync_access_app.call_args
        assert call.kwargs["existing_app_id"] == "app-existing"
        assert call.kwargs["existing_policy_ids"] == ["pol-old-1", "pol-old-2"]

    @sync_patch("src.api.access.cloudflare_service")
    def test_sync_requires_credentials(self, mock_cf, client):
        mock_cf.api_token = None
        mock_cf.account_id = None
        resp = client.put("/api/access/sync", json={
            "enabled": True, "canonical_name": "x", "app_name": "x",
            "primary_hostname": "x.woowtech.io", "protected_hostnames": [],
            "session_duration": "8h", "login_idp_types": [], "policies": []})
        assert resp.status_code == 400

    @sync_patch("src.api.access.cloudflare_service")
    def test_list_idps(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.list_identity_providers = AsyncMock(return_value=[
            {"id": "idp-g", "name": "Google", "type": "google"},
            {"id": "idp-otp", "name": "OTP", "type": "onetimepin"}])
        resp = client.get("/api/access/identity-providers")
        assert resp.status_code == 200
        types = [i["type"] for i in resp.json()["identity_providers"]]
        assert "google" in types and "onetimepin" in types

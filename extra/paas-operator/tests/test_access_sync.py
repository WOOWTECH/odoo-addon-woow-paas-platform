"""Tests for Cloudflare Access (Zero Trust) sync on Security Access."""
from unittest.mock import AsyncMock, patch

import pytest

from src.services.cloudflare import CloudflareService, CloudflareException
from src.models.schemas import (
    AccessSyncRequest,
    AccessPolicySpec,
    AccessRuleSpec,
)


def test_access_schemas_importable():
    req = AccessSyncRequest(
        enabled=True, canonical_name="paas-sa-x-y", app_name="x",
        primary_hostname="paas-sa-x-y.woowtech.io",
        protected_hostnames=["paas-sa-x-y.woowtech.io"],
        session_duration="8h", login_idp_types=["google", "onetimepin"],
        policies=[AccessPolicySpec(
            name="p1", decision="allow",
            include=[AccessRuleSpec(rule_type="email_domain",
                                    rule_value="acme.com")],
            precedence=1)],
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
    @pytest.mark.parametrize("status", [200, 202, 404])
    async def test_delete_reusable_policy_accepts_2xx_and_404(self, cf_service,
                                                              status):
        """Cloudflare's DELETE on Access policies replies **202 Accepted**
        (not 200), and 404 when already gone. All must count as success — a
        live CF teardown returns 202, and reading that as failure orphaned the
        policy in stg (mocked tests previously hard-coded 200, masking it)."""
        class FakeResponse:
            status_code = status
            text = ""  # raise branch formats response.text; keep present so a
                       # reverted fix reds with CloudflareException, not AttributeError
            @staticmethod
            def json():
                return {"success": True, "result": {"id": "pol-1"}}

        with patch("httpx.AsyncClient.delete",
                   new=AsyncMock(return_value=FakeResponse())):
            ok = await cf_service.delete_reusable_policy("pol-1")
        assert ok is True

    @pytest.mark.asyncio
    async def test_delete_reusable_policy_raises_on_real_error(self, cf_service):
        class FakeResponse:
            status_code = 500
            text = "boom"
            @staticmethod
            def json():
                return {"success": False}

        with patch("httpx.AsyncClient.delete",
                   new=AsyncMock(return_value=FakeResponse())):
            with pytest.raises(CloudflareException):
                await cf_service.delete_reusable_policy("pol-1")


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
                           "include": [{"rule_type": "email_domain",
                                        "rule_value": "acme.com"}],
                           "precedence": 1},
                          {"name": "block-bad", "decision": "deny",
                           "include": [{"rule_type": "email_domain",
                                        "rule_value": "bad.com"}],
                           "precedence": 5}],
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
                # Real Cloudflare returns 202 Accepted on Access delete. With the
                # old (200, 404)-only check, the app delete raised here and the
                # policy-cleanup loop below never ran → orphan policies. This 202
                # locks the teardown fix: app delete must NOT abort policy cleanup.
                status_code = 202
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
        assert len(deleted["policies"]) == 2  # 202 on app delete did NOT abort
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
                           "include": [{"rule_type": "email_domain",
                                        "rule_value": "acme.com"}],
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
                               "include": [{"rule_type": "email_domain",
                                            "rule_value": "acme.com"}],
                               "precedence": 1}],
                    enabled=True,
                    existing_app_id=None, existing_policy_ids=[])
        # The policy created in this batch was reclaimed (no orphan).
        assert created_pids, "expected a reusable policy to have been created"
        assert set(deleted_pids) == set(created_pids)


    @pytest.mark.asyncio
    async def test_resync_deletes_old_policies_after_app_update(self, cf_service):
        """Resync path (enabled=True, protected hostnames present) — detach
        before delete: create the NEW reusable policies, PUT the app to point at
        them (detaching the OLD ones), and only THEN delete the old policies.
        Deleting old policies first — while the app still references them — makes
        Cloudflare reject the delete as "in use", silently orphaning them (the
        exact failure caught in stg). This locks the ORDER (old-policy deletes
        come after the app PUT) AND the swallow tolerance: a stuck old-policy
        delete must warn+continue, not abort the converged sync, and the returned
        policy_ids must be the FRESH set, never the un-deletable stale id."""
        events = []

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
            events.append(f"delete:{pid}")
            if pid == "pol-stuck":
                # This stale reusable policy cannot be deleted (e.g. CF 5xx).
                raise CloudflareException("cannot delete stale policy",
                                          status_code=500)
            class R:
                status_code = 202
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": pid}}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            events.append("create_policy" if "/policies" in url else "post_app")
            class R:
                status_code = 201
                @staticmethod
                def json():
                    if "/policies" in url:
                        return {"success": True, "result": {"id": "pol-fresh"}}
                    return {"success": True, "result": {"id": "app-new"}}
            return R()

        async def fake_put(url, headers=None, json=None, timeout=None):
            events.append("put_app")
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
                           "include": [{"rule_type": "email_domain",
                                        "rule_value": "acme.com"}],
                           "precedence": 1}],
                enabled=True, existing_app_id="app-existing",
                existing_policy_ids=["pol-ok", "pol-stuck"])
        # ORDER: every old-policy delete happens AFTER the app PUT (detach first).
        put_idx = events.index("put_app")
        old_deletes = [i for i, e in enumerate(events)
                       if e in ("delete:pol-ok", "delete:pol-stuck")]
        assert old_deletes, "old policies must be deleted"
        assert min(old_deletes) > put_idx, \
            f"old-policy delete must follow app PUT; events={events}"
        # Both stale ids were ATTEMPTED (the failing one did not short-circuit).
        assert {"delete:pol-ok", "delete:pol-stuck"} <= set(events)
        # Recreate completed despite the swallowed failure: app + fresh policy.
        assert result["app_id"] == "app-existing"
        # Returned ids are the FRESH set, never the un-deletable stale id.
        assert result["policy_ids"] == ["pol-fresh"]
        assert "pol-stuck" not in result["policy_ids"]

    @pytest.mark.asyncio
    async def test_resync_old_policy_transient_error_does_not_fail_sync(self, cf_service):
        """Old-policy cleanup runs on the SUCCESS path (after the app PUT already
        committed CF's desired state). A NON-CloudflareException there — e.g. a
        transient httpx transport error, which is NOT a CloudflareException —
        must NOT propagate and fail the converged sync: that would roll back the
        caller's DB write-back of the new app/policy ids into CF↔DB divergence
        plus compounding orphans (the exact class this fix eliminates). The sync
        must still return the new app_id and surface the un-deleted id via
        orphaned_policy_ids. Catching only CloudflareException would red this."""
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
            # Old reusable policy delete blows up with a transport-style error
            # (NOT a CloudflareException).
            if url.rsplit("/", 1)[-1] == "pol-old":
                raise RuntimeError("connection reset")  # stand-in for httpx error
            class R:
                status_code = 202
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "x"}}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            class R:
                status_code = 201
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "pol-fresh"}}
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
                           "include": [{"rule_type": "email_domain",
                                        "rule_value": "acme.com"}],
                           "precedence": 1}],
                enabled=True, existing_app_id="app-existing",
                existing_policy_ids=["pol-old"])
        # Sync converged despite the transient delete error (did NOT raise).
        assert result["app_id"] == "app-existing"
        assert result["policy_ids"] == ["pol-fresh"]
        # The un-deletable old policy is surfaced, not silently dropped.
        assert result["orphaned_policy_ids"] == ["pol-old"]

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
                               "include": [{"rule_type": "email_domain",
                                            "rule_value": "acme.com"}],
                               "precedence": 1}],
                    enabled=True,
                    existing_app_id="app-existing", existing_policy_ids=[])
        # The policy created on the PUT-update path was reclaimed (no orphan).
        assert created_pids, "expected a reusable policy to have been created"
        assert set(deleted_pids) == set(created_pids)

    @pytest.mark.asyncio
    async def test_three_groups_and_new_selectors(self, cf_service):
        """A policy with include (email_domain) + require (country) + exclude
        (email_list) → create_reusable_policy receives all THREE Cloudflare rule
        groups, with the new country/geo selector mapped. Locks the P-rule
        contract: the operator must build include/require/exclude, not just
        include (the pre-P-rule behaviour silently dropped require/exclude)."""
        posts = []

        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": []}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            if "/policies" in url:
                posts.append(json)
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
            await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y", app_name="gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=["paas-sa-x-y.woowtech.io"],
                session_duration="8h", login_idp_types=[],
                policies=[{"name": "mix", "decision": "allow", "precedence": 1,
                           "include": [{"rule_type": "email_domain",
                                        "rule_value": "acme.com"}],
                           "require": [{"rule_type": "country",
                                        "rule_value": " tw, us "}],
                           "exclude": [{"rule_type": "email_list",
                                        "rule_value": "x@a.com, y@a.com"}]}],
                enabled=True, existing_app_id=None, existing_policy_ids=[])
        assert len(posts) == 1
        body = posts[0]
        assert body["include"] == [{"email_domain": {"domain": "acme.com"}}]
        assert body["require"] == [{"geo": {"country_code": "TW"}},
                                   {"geo": {"country_code": "US"}}]
        assert body["exclude"] == [{"email": {"email": "x@a.com"}},
                                   {"email": {"email": "y@a.com"}}]

    @pytest.mark.asyncio
    async def test_bypass_decision_and_everyone_passthrough(self, cf_service):
        """Bypass policy with an everyone include → decision passes straight to
        CF and everyone maps to {"everyone": {}}."""
        posts = []

        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": []}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            if "/policies" in url:
                posts.append(json)
            class R:
                status_code = 201
                @staticmethod
                def json():
                    if "/policies" in url:
                        return {"success": True, "result": {"id": "pol-b"}}
                    return {"success": True, "result": {"id": "app-b"}}
            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
            await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y", app_name="gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=["paas-sa-x-y.woowtech.io"],
                session_duration="8h", login_idp_types=[],
                policies=[{"name": "bp", "decision": "bypass", "precedence": 1,
                           "include": [{"rule_type": "everyone"}]}],
                enabled=True, existing_app_id=None, existing_policy_ids=[])
        assert posts[0]["decision"] == "bypass"
        assert posts[0]["include"] == [{"everyone": {}}]

    @pytest.mark.asyncio
    async def test_app_policies_sorted_by_precedence(self, cf_service):
        """Policies declared out of precedence order are sorted ascending (lower
        first) in the app payload, so CF receives a deterministic evaluation
        order. (CF still tiers Bypass/Service-Auth ahead — not re-tiered here.)"""
        app_body = {}
        pid_seq = iter(["pol-a", "pol-b"])

        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": []}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            is_policy = "/policies" in url
            class R:
                status_code = 201
                @staticmethod
                def json():
                    if is_policy:
                        return {"success": True, "result": {"id": next(pid_seq)}}
                    return {"success": True, "result": {"id": "app-x"}}
            if not is_policy:
                app_body.update(json)
            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
            await cf_service.sync_access_app(
                canonical_name="x", app_name="gw",
                primary_hostname="x.woowtech.io",
                protected_hostnames=["x.woowtech.io"],
                session_duration="8h", login_idp_types=[],
                policies=[{"name": "later", "decision": "allow",
                           "precedence": 30,
                           "include": [{"rule_type": "everyone"}]},
                          {"name": "earlier", "decision": "allow",
                           "precedence": 10,
                           "include": [{"rule_type": "everyone"}]}],
                enabled=True, existing_app_id=None, existing_policy_ids=[])
        precs = [p["precedence"] for p in app_body["policies"]]
        assert precs == [10, 30], \
            f"app policies must be precedence-sorted ascending: {precs}"

    @pytest.mark.asyncio
    async def test_existing_app_revoke_returns_acceptance_not_denial_proof(
            self, cf_service):
        cf_service._resolve_idp_ids = AsyncMock(return_value=([], []))
        cf_service.create_reusable_policy = AsyncMock(return_value="pol-new")
        cf_service.delete_reusable_policy = AsyncMock()
        cf_service.revoke_access_app_tokens = AsyncMock(return_value="accepted")

        class PutResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"success": True, "result": {"id": "app-owned"}}

        with patch("httpx.AsyncClient.put",
                   new=AsyncMock(return_value=PutResponse())):
            result = await cf_service.sync_access_app(
                canonical_name="owned", app_name="owned",
                primary_hostname="owned.example.test",
                protected_hostnames=["owned.example.test"],
                session_duration="8h", login_idp_types=[],
                policies=[{"name": "allow", "decision": "allow",
                           "precedence": 1,
                           "include": [{"rule_type": "everyone"}]}],
                enabled=True, existing_app_id="app-owned",
                existing_policy_ids=["pol-old"], revoke_tokens=True)

        assert result["token_revocation"] == "accepted"
        assert "denial" not in result
        cf_service.revoke_access_app_tokens.assert_awaited_once_with("app-owned")


class TestAccessPolicySpecValidation:
    """AccessPolicySpec model validators — defense in depth. Odoo enforces the
    same invariants, but a direct operator API call must also be rejected."""

    def test_requires_at_least_one_include(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AccessPolicySpec(
                name="p", decision="allow",
                require=[AccessRuleSpec(rule_type="country", rule_value="TW")])

    def test_bypass_rejects_email_selector(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AccessPolicySpec(
                name="p", decision="bypass",
                include=[AccessRuleSpec(rule_type="email_domain",
                                        rule_value="acme.com")])

    def test_bypass_with_ip_ok(self):
        spec = AccessPolicySpec(
            name="p", decision="bypass",
            include=[AccessRuleSpec(rule_type="ip_range",
                                    rule_value="10.0.0.0/8")])
        assert spec.decision == "bypass"

    def test_invalid_decision_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AccessPolicySpec(name="p", decision="nope",
                             include=[AccessRuleSpec(rule_type="everyone")])

    def test_everyone_rule_value_false_coerced_to_none(self):
        """Odoo reads an unset Char as Python False, so a valueless 'everyone'
        selector arrives as JSON false. AccessRuleSpec must coerce falsy → None
        and accept it, NOT 422 as a non-string. Reproduces the Phase 2
        everyone/bypass sync bug the mock tests hid (they omitted rule_value)."""
        spec = AccessPolicySpec(
            name="p", decision="bypass",
            include=[AccessRuleSpec(rule_type="everyone", rule_value=False)])
        assert spec.include[0].rule_value is None


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

    def test_everyone(self):
        # everyone carries no value → CF empty-dict selector.
        assert CloudflareService._build_access_rule("everyone", "") == [
            {"everyone": {}}]

    def test_country_splits_strips_and_uppercases(self):
        assert CloudflareService._build_access_rule(
            "country", " tw , us ,") == [
            {"geo": {"country_code": "TW"}},
            {"geo": {"country_code": "US"}}]

    def test_unknown_rule_type_raises(self):
        with pytest.raises(CloudflareException):
            CloudflareService._build_access_rule("nope", "whatever")


class TestResolveIdpIds:
    """Explicit login restrictions must resolve before any remote mutation."""

    @staticmethod
    def sync_kwargs(login_idp_types):
        return {
            "canonical_name": "paas-sa-x-y",
            "app_name": "gw",
            "primary_hostname": "paas-sa-x-y.woowtech.io",
            "protected_hostnames": ["paas-sa-x-y.woowtech.io"],
            "session_duration": "8h",
            "enabled": True,
            "login_idp_types": login_idp_types,
            "policies": [{
                "name": "corp", "decision": "allow", "precedence": 1,
                "include": [{"rule_type": "email_domain",
                             "rule_value": "acme.com"}],
            }],
            "existing_app_id": "app-old",
            "existing_policy_ids": ["policy-old"],
        }

    @pytest.mark.parametrize("idps, requested, missing", [
        ([{"id": "idp-g", "type": "google"}],
         ["google", "onetimepin"], "onetimepin"),
        ([{"id": "idp-otp", "type": "onetimepin"}],
         ["google"], "google"),
        ([], ["google", "onetimepin"], "google, onetimepin"),
        ([{"id": "", "type": "google"}], ["google"], "google"),
    ], ids=["partial-missing", "all-missing", "no-idps", "missing-id"])
    @pytest.mark.asyncio
    async def test_missing_idp_rejects_before_remote_writes(
            self, cf_service, idps, requested, missing):
        import httpx

        response = httpx.Response(200, json={"success": True, "result": idps})
        mutation_response = httpx.Response(
            201, json={"success": True, "result": {"id": "new-resource"}})
        delete_response = httpx.Response(202, json={"success": True})
        with patch("httpx.AsyncClient.get", new=AsyncMock(
                return_value=response)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(
                 return_value=mutation_response)) as post, \
             patch("httpx.AsyncClient.put", new=AsyncMock(
                 return_value=mutation_response)) as put, \
             patch("httpx.AsyncClient.delete", new=AsyncMock(
                 return_value=delete_response)) as delete:
            with pytest.raises(CloudflareException, match=missing) as caught:
                await cf_service.sync_access_app(**self.sync_kwargs(requested))
            assert caught.value.status_code == 400
            post.assert_not_awaited()
            put.assert_not_awaited()
            delete.assert_not_awaited()

    @pytest.mark.parametrize("failure", ["api-503", "connect-error"])
    @pytest.mark.asyncio
    async def test_idp_lookup_failure_leaves_old_app_and_policies_untouched(
            self, cf_service, failure):
        import httpx

        if failure == "api-503":
            get = AsyncMock(return_value=httpx.Response(503, text="Unavailable"))
            exception = CloudflareException
        else:
            get = AsyncMock(side_effect=httpx.ConnectError("Unavailable"))
            exception = httpx.ConnectError
        response = httpx.Response(
            201, json={"success": True, "result": {"id": "new-resource"}})
        delete_response = httpx.Response(202, json={"success": True})
        with patch("httpx.AsyncClient.get", new=get), \
             patch("httpx.AsyncClient.post", new=AsyncMock(
                 return_value=response)) as post, \
             patch("httpx.AsyncClient.put", new=AsyncMock(
                 return_value=response)) as put, \
             patch("httpx.AsyncClient.delete", new=AsyncMock(
                 return_value=delete_response)) as delete:
            with pytest.raises(exception):
                await cf_service.sync_access_app(
                    **self.sync_kwargs(["google", "onetimepin"]))
            post.assert_not_awaited()
            put.assert_not_awaited()
            delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_google_and_otp_resolve_without_downgrade(self, cf_service):
        with patch.object(cf_service, "list_identity_providers", new=AsyncMock(
                return_value=[
                    {"id": "idp-g", "type": "google"},
                    {"id": "idp-otp", "type": "onetimepin"},
                ])):
            resolved, unresolved = await cf_service._resolve_idp_ids(
                ["google", "onetimepin"])
        assert resolved == ["idp-g", "idp-otp"]
        assert unresolved == []

    @pytest.mark.asyncio
    async def test_empty_request_intentionally_allows_all_idps(self, cf_service):
        import httpx

        created = httpx.Response(
            201, json={"success": True, "result": {"id": "policy-new"}})
        updated = httpx.Response(
            200, json={"success": True, "result": {"id": "app-old"}})
        deleted = httpx.Response(202, json={"success": True})
        with patch.object(cf_service, "list_identity_providers",
                          new=AsyncMock()) as lookup, \
             patch("httpx.AsyncClient.post", new=AsyncMock(
                 return_value=created)), \
             patch("httpx.AsyncClient.put", new=AsyncMock(
                 return_value=updated)) as put, \
             patch("httpx.AsyncClient.delete", new=AsyncMock(
                 return_value=deleted)):
            result = await cf_service.sync_access_app(**self.sync_kwargs([]))
        lookup.assert_not_awaited()
        assert result["allowed_idps"] == []
        assert result["unresolved_idp_types"] == []
        assert put.call_args.kwargs["json"]["allowed_idps"] == []

    @pytest.mark.parametrize("enabled, protected_hostnames", [
        (False, ["paas-sa-x-y.woowtech.io"]),
        (True, []),
    ], ids=["disabled", "no-protected-hostnames"])
    @pytest.mark.asyncio
    async def test_teardown_does_not_require_available_idps(
            self, cf_service, enabled, protected_hostnames):
        import httpx

        args = self.sync_kwargs(["google"])
        args.update(enabled=enabled, protected_hostnames=protected_hostnames)
        with patch.object(cf_service, "list_identity_providers",
                          new=AsyncMock()) as lookup, \
             patch("httpx.AsyncClient.delete", new=AsyncMock(
                 return_value=httpx.Response(
                     202, json={"success": True}))) as delete:
            result = await cf_service.sync_access_app(**args)
        lookup.assert_not_awaited()
        assert delete.await_count == 2
        assert result["app_id"] is None
        assert result["policy_ids"] == []

    def test_missing_idp_api_returns_400_without_remote_writes(
            self, cf_service, client):
        import httpx

        idps = [{"id": "idp-otp", "type": "onetimepin",
                 "config": {"client_secret": "provider-test-secret"}}]
        response = httpx.Response(200, json={"success": True, "result": idps})
        changed = httpx.Response(
            201, json={"success": True, "result": {"id": "new-resource"}})
        deleted = httpx.Response(202, json={"success": True})
        with patch("src.api.access.cloudflare_service", cf_service), \
             patch("httpx.AsyncClient.get", new=AsyncMock(
                 return_value=response)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(
                 return_value=changed)) as post, \
             patch("httpx.AsyncClient.put", new=AsyncMock(
                 return_value=changed)) as put, \
             patch("httpx.AsyncClient.delete", new=AsyncMock(
                 return_value=deleted)) as delete:
            response = client.put("/api/access/sync",
                                  json=self.sync_kwargs(["google"]))
        assert response.status_code == 400, response.text
        assert "google" in response.json()["detail"]
        assert "provider-test-secret" not in response.text
        assert cf_service.api_token not in response.text
        post.assert_not_awaited()
        put.assert_not_awaited()
        delete.assert_not_awaited()


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
                          "include": [{"rule_type": "email_domain",
                                       "rule_value": "acme.com"}],
                          "precedence": 1}],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["app_id"] == "app-1"
        assert data["policy_ids"] == ["pol-1"]
        call = mock_cf.sync_access_app.call_args
        assert call.kwargs["login_idp_types"] == ["google", "onetimepin"]

    @sync_patch("src.api.access.cloudflare_service")
    def test_sync_forwards_normalized_policy_shape(self, mock_cf, client):
        """End-to-end serialization contract through model_dump(mode="json"):
        an everyone-include bypass policy whose rule_value arrives as JSON false
        (the exact shape Odoo emits for a valueless selector) must reach the
        service as decision="bypass" (plain str, not an AccessDecision enum) and
        rule_value=None — NOT 422. Reproduces and locks the Phase 2 cross-contract
        bug: dropping mode="json" (enum object) or the falsy→None coercion would
        red this."""
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.sync_access_app = AsyncMock(return_value={
            "app_id": "app-1", "policy_ids": ["pol-1"],
            "allowed_idps": [], "unresolved_idp_types": []})
        resp = client.put("/api/access/sync", json={
            "enabled": True, "canonical_name": "paas-sa-x-y", "app_name": "gw",
            "primary_hostname": "paas-sa-x-y.woowtech.io",
            "protected_hostnames": ["paas-sa-x-y.woowtech.io"],
            "session_duration": "8h", "login_idp_types": [],
            "policies": [{"name": "bp", "decision": "bypass", "precedence": 1,
                          "include": [{"rule_type": "everyone",
                                       "rule_value": False}]}]})
        assert resp.status_code == 200, resp.text
        policies = mock_cf.sync_access_app.call_args.kwargs["policies"]
        assert policies[0]["decision"] == "bypass"
        assert type(policies[0]["decision"]) is str
        assert policies[0]["include"] == [
            {"rule_type": "everyone", "rule_value": None}]

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
    def test_sync_access_cloudflare_exception_passes_status(self, mock_cf,
                                                            client):
        """CloudflareException→HTTPException translation: the endpoint maps
        e.status_code through (HTTPException(status_code=e.status_code or 500)).
        This is the operator's error contract the Odoo client relies on to
        translate operator failures into PaaSOperatorError; the service-level
        reclaim paths are tested, but this route-layer exception→HTTP boundary
        was uncovered. A regression that swallowed the status or 500'd
        everything would break the client's status-aware handling. Assert a
        502 from the service surfaces as a 502 at the route."""
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.sync_access_app = AsyncMock(
            side_effect=CloudflareException("cf bad gateway", status_code=502))
        resp = client.put("/api/access/sync", json={
            "enabled": True, "canonical_name": "paas-sa-x-y", "app_name": "gw",
            "primary_hostname": "paas-sa-x-y.woowtech.io",
            "protected_hostnames": ["paas-sa-x-y.woowtech.io"],
            "session_duration": "8h", "login_idp_types": [], "policies": []})
        assert resp.status_code == 502, resp.text

    @sync_patch("src.api.access.cloudflare_service")
    def test_sync_access_cloudflare_exception_defaults_to_500(self, mock_cf,
                                                              client):
        """status_code=None fallback branch: a CloudflareException with no
        status (e.status_code is None) must fall back to 500, not crash. Locks
        the `or status.HTTP_500_INTERNAL_SERVER_ERROR` half of the mapping."""
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.sync_access_app = AsyncMock(
            side_effect=CloudflareException("cf unknown error"))
        resp = client.put("/api/access/sync", json={
            "enabled": True, "canonical_name": "paas-sa-x-y", "app_name": "gw",
            "primary_hostname": "paas-sa-x-y.woowtech.io",
            "protected_hostnames": ["paas-sa-x-y.woowtech.io"],
            "session_duration": "8h", "login_idp_types": [], "policies": []})
        assert resp.status_code == 500, resp.text

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

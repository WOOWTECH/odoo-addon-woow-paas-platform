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
        calls = {"app_put": None, "policy_posts": [], "list_apps": True}

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
                           "precedence": 1}],
                existing_app_id=None,
                existing_policy_ids=[],
            )
        # allowed_idps resolved from types
        assert set(result["allowed_idps"]) == {"idp-g", "idp-otp"}
        # one reusable policy created
        assert len(calls["policy_posts"]) == 1
        assert calls["policy_posts"][0]["include"] == [
            {"email_domain": {"domain": "acme.com"}}]
        assert result["app_id"]
        assert result["policy_ids"] == ["pol-new"]

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

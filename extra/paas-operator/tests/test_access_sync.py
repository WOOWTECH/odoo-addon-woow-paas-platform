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

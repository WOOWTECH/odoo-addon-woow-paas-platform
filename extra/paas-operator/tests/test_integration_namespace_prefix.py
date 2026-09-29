"""integration namespace override 的離線守線；不連 K8s。"""

from unittest.mock import MagicMock

import pytest

from src.config import settings
from tests.integration import conftest as integration_fixtures
from tests.integration import test_skill_bundle_token_secret as skill_fixtures


@pytest.mark.parametrize("override, expected", [
    (None, "paas-ws-test-op-"),
    ("dev-sandbox-1129-", "dev-sandbox-1129-"),
])
def test_both_namespace_fixtures_share_validated_prefix(monkeypatch, tmp_path, override, expected):
    monkeypatch.delenv("OPERATOR_TEST_NAMESPACE_PREFIX", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_REAL_CHART", raising=False)
    if override is not None:
        monkeypatch.setenv("OPERATOR_TEST_NAMESPACE_PREFIX", override)
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    namespace_run = integration_fixtures.test_namespace.__wrapped__()
    agent_run = skill_fixtures.agent_runtime_env.__wrapped__(MagicMock(), tmp_path)
    try:
        namespace = next(namespace_run)
        agent = next(agent_run)
        assert namespace.startswith(expected)
        assert agent["namespace"].startswith(expected + "pa-")
        assert settings.namespace_prefix == expected
        assert len(namespace) <= 63
        assert len(agent["namespace"]) <= 63
    finally:
        list(agent_run)
        list(namespace_run)


@pytest.mark.parametrize("prefix", [
    "", "paas-ws-", "default-", "dev-sandbox-", "dev-sandbox-1129",
    "dev-sandbox-UPPER-", "dev-sandbox-../../-", "dev-sandbox-1129-\n",
    "dev-sandbox-" + "x" * 34 + "-",
])
def test_invalid_override_fails_before_either_fixture_can_write(monkeypatch, tmp_path, prefix):
    monkeypatch.setenv("OPERATOR_TEST_NAMESPACE_PREFIX", prefix)
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    for generator in (
        integration_fixtures.test_namespace.__wrapped__(),
        skill_fixtures.agent_runtime_env.__wrapped__(MagicMock(), tmp_path),
    ):
        with pytest.raises(ValueError, match="namespace prefix"):
            next(generator)
    run.assert_not_called()

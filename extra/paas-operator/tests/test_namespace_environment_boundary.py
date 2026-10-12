"""Production's broad prefix must not include the reserved staging prefix."""
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import pytest

from src.config import settings
from src.main import app
from src.services.helm import HelmService, KubernetesService, validate_namespace


@pytest.mark.parametrize('prefix,namespace,allowed', [
    ('paas-ws-', 'paas-ws-production', True),
    ('paas-ws-', 'paas-ws-stg-example', False),
    ('paas-ws-', 'paas-ws-staging-example', True),
    ('paas-ws-stg-', 'paas-ws-stg-example', True),
    ('paas-ws-stg-', 'paas-ws-production', False),
    ('dev-sandbox-own-', 'dev-sandbox-own-example', True),
    ('dev-sandbox-own-', 'paas-ws-production', False),
    ('', 'paas-ws-production', False),
])
def test_namespace_environment_boundary(monkeypatch, prefix, namespace, allowed):
    monkeypatch.setattr(settings, 'namespace_prefix', prefix)
    if allowed:
        validate_namespace(namespace)
    else:
        with pytest.raises(ValueError):
            validate_namespace(namespace)


def test_production_projection_rejects_staging_before_helm(monkeypatch):
    monkeypatch.setattr(settings, 'paas_env', 'prod')
    monkeypatch.setattr(settings, 'namespace_prefix', 'paas-ws-')
    monkeypatch.setattr(settings, 'api_key', 'synthetic-boundary-key')
    with patch.object(HelmService, 'get_version', return_value='v3.13.3'), \
            patch('src.api.releases.helm_service.get_config_projection',
                  return_value={'values': {'config.enabled': False}, 'missing': []}) as project:
        with TestClient(app, headers={'X-API-Key': 'synthetic-boundary-key'}) as client:
            response = client.post('/api/releases/paas-ws-stg-example/fixture/config-projection',
                                   json={'revision': 1, 'paths': ['config.enabled']})
        assert response.status_code == 403
        project.assert_not_called()


@pytest.mark.parametrize('environment,prefix,namespace', [
    ('prod', 'paas-ws-', 'paas-ws-production'),
    ('stg', 'paas-ws-stg-', 'paas-ws-stg-example'),
])
def test_each_environment_keeps_its_projection(monkeypatch, environment, prefix, namespace):
    monkeypatch.setattr(settings, 'paas_env', environment)
    monkeypatch.setattr(settings, 'namespace_prefix', prefix)
    monkeypatch.setattr(settings, 'api_key', 'synthetic-boundary-key')
    with patch.object(HelmService, 'get_version', return_value='v3.13.3'), \
            patch('src.api.releases.helm_service.get_config_projection',
                  return_value={'values': {'config.enabled': False}, 'missing': []}) as project:
        with TestClient(app, headers={'X-API-Key': 'synthetic-boundary-key'}) as client:
            response = client.post('/api/releases/' + namespace + '/fixture/config-projection',
                                   json={'revision': 1, 'paths': ['config.enabled']})
        assert response.status_code == 200
        assert response.json()['values'] == {'config.enabled': False}
        project.assert_called_once_with(namespace, 'fixture', 1, ['config.enabled'], [])


def test_production_namespace_delete_rejects_staging_before_kubectl(monkeypatch):
    monkeypatch.setattr(settings, 'namespace_prefix', 'paas-ws-')
    service = KubernetesService()
    with patch.object(service, '_run_command', return_value=Mock(stdout='{}')) as run:
        with pytest.raises(ValueError):
            service.delete_namespace('paas-ws-stg-example')
        run.assert_not_called()

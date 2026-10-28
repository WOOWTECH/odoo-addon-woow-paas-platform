"""固定 revision 的非憑證 leaf 投影，不回傳整份 Helm values。"""
import json
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import ReleaseConfigProjectionRequest
from src.services.config_projection import project_values
from src.services.helm import HelmService


def test_scalar_projection_preserves_false_zero_and_absence():
    values = {'config': {'timezone': 'Asia/Taipei', 'ws': False, 'days': 0,
                         'sensitive': {'admin_password': 'synthetic-secret'}}}
    result = project_values(values, ['config.timezone', 'config.ws', 'config.days', 'config.absent'])
    assert result == {'values': {'config.timezone': 'Asia/Taipei', 'config.ws': False,
                                 'config.days': 0}, 'missing': ['config.absent']}
    assert 'synthetic-secret' not in json.dumps(result)


@pytest.mark.parametrize('key', ['config.sensitive', 'auth.apiServerKey', 'smtp.password',
                                 'a.*', 'a..b', '../x', '_init.email'])
def test_secret_or_malformed_path_rejected(key):
    with pytest.raises(ValueError):
        project_values({}, [key])


def test_objects_are_refused_instead_of_serialized():
    with pytest.raises(ValueError):
        project_values({'public': {'hidden': 'secret'}}, ['public'])


def test_helm_computed_values_are_pinned_to_exact_revision():
    helm = HelmService()
    with patch.object(helm, '_run_command', return_value=Mock(stdout='{"setting":false}')) as run:
        assert helm.get_config_projection('paas-ws-qa', 'svc-qa', 3, ['setting'])['values'] == {'setting': False}
    args = run.call_args.args[0]
    assert '--all' in args and args[args.index('--revision') + 1] == '3'


def test_preserved_path_is_excluded_even_when_name_looks_public():
    with pytest.raises(ValueError):
        project_values({'auth': {'username': 'current'}}, ['auth.username'], ['auth'])


def test_authenticated_endpoint_never_returns_other_values():
    with patch('src.main.settings') as settings, patch('src.api.releases.helm_service') as helm:
        settings.api_key = 'test-api-key'
        helm.get_config_projection.return_value = {'values': {'setting': False}, 'missing': []}
        with patch('src.main.HelmService.get_version', return_value='v3.13.3'), \
                TestClient(app, headers={'X-API-Key': 'test-api-key'}) as client:
            response = client.post('/api/releases/paas-ws-qa/svc-qa/config-projection',
                                   json={'revision': 3, 'paths': ['setting'], 'excluded_paths': []})
            assert response.status_code == 200, response.text
            assert response.json() == {'revision': 3, 'values': {'setting': False}, 'missing': []}
            assert client.post('/api/releases/paas-ws-qa/svc-qa/config-projection',
                               json={'revision': 0, 'paths': ['setting']}).status_code == 422
            assert client.post('/api/releases/paas-ws-qa/svc-qa/config-projection',
                               json={'revision': 3, 'paths': ['auth.apiServerKey']}).status_code == 422


def test_addon_name_list_is_projected_only_for_the_allowlisted_path():
    values = {'addons': {'selected': ['ai_base_gt', 'mail_fetchmail_durable', 'odoo_ai_assistant_chatgpt_connector']},
              'other': {'names': ['not_allowed']}}
    result = project_values(values, ['addons.selected'])
    assert result == {'values': {'addons.selected': ['ai_base_gt', 'mail_fetchmail_durable',
                                                     'odoo_ai_assistant_chatgpt_connector']}, 'missing': []}
    # 其他路徑的清單照舊被拒（只有 LIST_PATHS 開例外）
    with pytest.raises(ValueError):
        project_values(values, ['other.names'])


def test_empty_addon_list_is_a_valid_projection():
    assert project_values({'addons': {'selected': []}}, ['addons.selected']) == {
        'values': {'addons.selected': []}, 'missing': []}


@pytest.mark.parametrize('selected', [
    ['ok', 3],                       # 不是字串
    ['has space'],                   # 字元集不合
    ['../etc/passwd'],               # 路徑穿越形狀
    ['x' * 129],                     # 單筆過長
    [''],                            # 空字串
    [{'name': 'nested'}],            # 巢狀物件
    ['n' + str(i) for i in range(513)],  # 筆數過多
])
def test_addon_list_with_unbounded_items_is_refused(selected):
    with pytest.raises(ValueError):
        project_values({'addons': {'selected': selected}}, ['addons.selected'])


def test_addon_list_projection_is_a_copy_not_a_reference():
    values = {'addons': {'selected': ['a_b']}}
    result = project_values(values, ['addons.selected'])
    result['values']['addons.selected'].append('mutated')
    assert values['addons']['selected'] == ['a_b']


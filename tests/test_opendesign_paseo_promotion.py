"""Offline source-to-values contracts; synthetic digests never become seed data.

The Odoo-free harness executes the real leaf functions from cloud_service.py.
Native ORM/operator coverage lives in src/tests/test_service_deployment_pin.py.
"""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROMOTION = load('scripts/release/prepare_opendesign_paseo.py', 'promotion')
TARGET = load('src/services/deployment_target.py', 'deployment_target')
TAG = '20261026.1234@sha256:' + 'a' * 64
CURRENT_SOURCE = (ROOT / PROMOTION.SEED).read_text()


def legacy_seed(source):
    """Keep an explicit pre-promotion fixture after the real seed is promoted."""
    pattern = re.compile(r'(<record id="cloud_app_opendesign"[^>]*>)(.*?)(</record>)', re.S)
    record = pattern.search(source).group(0)
    fields = {f.get('name'): f.text for f in ET.fromstring(record).findall('field')}
    values = json.loads(fields['helm_default_values'])
    values['image']['tag'] = '20261025.0000@sha256:' + 'c' * 64
    values.pop('paseo', None)
    switches = json.loads(fields['rollback_feature_switches'])
    switches.pop('paseo.enabled', None)
    for name, value in {'helm_chart_version': '0.5.7',
                        'helm_default_values': json.dumps(values),
                        'rollback_feature_switches': json.dumps(switches)}.items():
        record = re.sub(r'(<field name="' + name + r'">).*?(</field>)',
                        lambda m: m[1] + value + m[2], record, flags=re.S)
    return pattern.sub(lambda _: record, source)


SOURCE = legacy_seed(CURRENT_SOURCE)
MODEL = ast.parse((ROOT / 'src/models/cloud_service.py').read_text())
FUNCTIONS = {'_iter_image_refs', '_refresh_image_refs'}
nodes = [n for n in MODEL.body if isinstance(n, ast.FunctionDef) and n.name in FUNCTIONS]
service = next(n for n in MODEL.body if isinstance(n, ast.ClassDef) and n.name == 'CloudService')
origin = copy.deepcopy(next(n for n in service.body if isinstance(n, ast.FunctionDef)
                           and n.name == '_inject_allowed_origins'))
origin.decorator_list = []
nodes.append(origin)
NS = {'copy': copy, '_IMAGE_REF_KEYS': ('registry', 'repository', 'tag', 'digest')}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'cloud_service.py', 'exec'), NS)
inject = NS['_inject_allowed_origins']
controller = next(n for n in ast.parse((ROOT / 'src/controllers/paas.py').read_text()).body
                  if isinstance(n, ast.ClassDef) and n.name == 'PaasController')
merge = copy.deepcopy(next(n for n in controller.body if isinstance(n, ast.FunctionDef)
                          and n.name == '_deep_merge'))
merge.decorator_list = []
exec(compile(ast.Module(body=[merge], type_ignores=[]), 'paas.py', 'exec'), NS)
NS['PaasController'] = SimpleNamespace(_deep_merge=NS['_deep_merge'])


def fields(source):
    record = ET.fromstring(source).find(".//record[@id='cloud_app_opendesign']")
    return {f.get('name'): f.text for f in record.findall('field')}


def promoted():
    data = fields(PROMOTION.promoted_seed(SOURCE, TAG))
    return data, json.loads(data['helm_default_values'])


def resolve(data, values, **kw):
    return TARGET.resolve_deployment_target(
        policy=kw.pop('policy', 'template'), chart=data['helm_chart_name'],
        repo_url=data['helm_repo_url'], template_version=data['helm_chart_version'],
        template_values=values,
        image_paths=[p for p, _ in NS['_iter_image_refs'](values)],
        tenant_keys={'auth.anthropicApiKey', 'auth.openrouterApiKey'}, **kw)


def test_missing_digest_emits_no_patch_and_does_not_write_source():
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/release/prepare_opendesign_paseo.py')],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == ''
    assert 'AWAITING_COORDINATOR_DIGEST' in result.stderr
    assert (ROOT / PROMOTION.SEED).read_text() == CURRENT_SOURCE


def test_real_seed_promotes_native_paseo_with_an_immutable_image_and_empty_origin():
    data = fields(CURRENT_SOURCE)
    values = json.loads(data['helm_default_values'])
    assert data['helm_chart_version'] == '0.5.9'
    assert values['image']['repository'] == PROMOTION.IMAGE_REPOSITORY
    assert PROMOTION.IMAGE_TAG.fullmatch(values['image']['tag'])
    assert values['paseo'] == {'enabled': True, 'origin': ''}
    assert json.loads(data['rollback_feature_switches'])['paseo.enabled'] == ['image']


@pytest.mark.parametrize('tag', [None, '', 'latest', TAG + '\n', '20261026.1234',
    '20261026.1234@sha256:abc', '20261026.1234@sha256:' + 'A' * 64,
    'paseo-candidate@sha256:' + 'a' * 64, '<UNREVIEWED>'])
def test_invalid_or_feature_image_cannot_promote(tag):
    with pytest.raises(ValueError, match='AWAITING_COORDINATOR_DIGEST'):
        PROMOTION.promoted_seed(SOURCE, tag)


def test_old_image_cannot_enable_feature():
    old_tag = json.loads(fields(SOURCE)['helm_default_values'])['image']['tag']
    with pytest.raises(ValueError, match='old product image'):
        PROMOTION.promoted_seed(SOURCE, old_tag)


def test_promotion_changes_only_target_product_and_three_coupled_fields():
    after = PROMOTION.promoted_seed(SOURCE, TAG)
    before_tree, after_tree = ET.fromstring(SOURCE), ET.fromstring(after)
    before_record = before_tree.find(".//record[@id='cloud_app_opendesign']")
    after_record = after_tree.find(".//record[@id='cloud_app_opendesign']")
    before, data = fields(SOURCE), fields(after)
    assert {k for k in before if before[k] != data[k]} == {
        'helm_chart_version', 'helm_default_values', 'rollback_feature_switches'}
    for f in after_record.findall('field'):
        f.text = before[f.get('name')]
    assert ET.tostring(before_tree) == ET.tostring(after_tree)
    assert before_record.get('model') == after_record.get('model')
    values = json.loads(data['helm_default_values'])
    expected = json.loads(before['helm_default_values'])
    expected['image']['tag'] = TAG
    expected['paseo'] = {'enabled': True, 'origin': ''}
    assert values == expected
    assert json.loads(data['rollback_feature_switches']) == {
        **json.loads(before['rollback_feature_switches']), 'paseo.enabled': ['image']}
    assert PROMOTION.promoted_seed(after, TAG) == after
    # Existing native module-update path reloads this record (no new migration).
    parent = next(d for d in before_tree.findall('data') if before_record in list(d))
    assert parent.get('noupdate') == '0'


def test_new_install_resolves_promoted_default_and_exact_public_origin():
    data, defaults = promoted()
    target = resolve(data, defaults)
    merged = NS['_deep_merge'](defaults, {'auth': {'anthropicApiKey': 'synthetic-key'}})
    # The controller persists JSON; assembly reads a fresh service snapshot.
    values = json.loads(json.dumps(merged))
    url = 'https://svc-new-install.tenant.example'
    assert inject(values, 'open-design', url) is values
    assert target['version'] == '0.5.9'
    assert target['image_values']['image']['tag'] == TAG
    assert values['paseo'] == {'enabled': True, 'origin': url}
    assert values['config']['allowedOrigins'] == url
    assert values['auth']['anthropicApiKey'] == 'synthetic-key'
    assert defaults['paseo']['origin'] == ''


@pytest.mark.parametrize('service_id', [10, 244])
def test_pinned_instances_keep_chart_image_features_and_data(service_id):
    data, defaults = promoted()
    pin = {'repository': PROMOTION.IMAGE_REPOSITORY,
           'tag': f'paseo-service-{service_id}@sha256:' + 'b' * 64}
    target = resolve(data, defaults, policy='pinned', pinned_version='0.5.8',
                     image_overrides={'image': pin})
    stored = json.loads(fields(SOURCE)['helm_default_values'])
    stored.update({'image': pin, 'paseo': {'enabled': True,
        'origin': f'https://svc-{service_id}.tenant.example'},
        'omnigentHost': {'enabled': True}, 'mcp': {'enabled': False}})
    before = copy.deepcopy(stored)
    refreshed, changes = NS['_refresh_image_refs'](stored, target['image_values'])
    assert inject(refreshed, 'open-design', stored['paseo']['origin'])['paseo'] == before['paseo']
    refreshed.pop('config')
    assert target['version'] == '0.5.8'
    assert changes == []
    assert refreshed == stored == before
    assert data['helm_chart_version'] == '0.5.9'


@pytest.mark.parametrize('paseo', [None, {}, {'enabled': False}, {'enabled': 'true'}])
def test_existing_default_following_service_does_not_auto_enable(paseo):
    data, defaults = promoted()
    stored = json.loads(fields(SOURCE)['helm_default_values'])
    if paseo is not None:
        stored['paseo'] = copy.deepcopy(paseo)
    target = resolve(data, defaults)
    refreshed, _ = NS['_refresh_image_refs'](stored, target['image_values'])
    inject(refreshed, 'open-design', 'https://legacy.tenant.example')
    assert target['version'] == '0.5.9'
    assert refreshed['image']['tag'] == TAG
    assert refreshed.get('paseo') == stored.get('paseo')
    assert refreshed['persistence'] == stored['persistence']


def test_enabled_origin_is_authoritative_and_other_templates_are_untouched():
    values = {'paseo': {'enabled': True, 'origin': 'https://stale.example'}}
    expected = copy.deepcopy(values)
    assert inject(values, 'hermes-agent', 'https://own.example') == expected
    inject(values, 'open-design', 'https://own.example:8443')
    assert values['paseo']['origin'] == 'https://own.example:8443'
    assert values['config']['allowedOrigins'] == values['paseo']['origin']


def test_patch_updates_native_seed_contract_and_is_applyable(tmp_path):
    # Exercise the whole historical transition even after the real source moved.
    seed = tmp_path / PROMOTION.SEED
    seed.parent.mkdir(parents=True)
    seed.write_text(SOURCE)
    native_test = (ROOT / PROMOTION.TEST).read_text()
    promoted_assertion = ("self.assertEqual(tpl.helm_chart_version, '0.5.9')\n"
                          "        self.assertEqual(self._values()['paseo'], {'enabled': True, 'origin': ''})\n"
                          "        self.assertEqual(json.loads(tpl.rollback_feature_switches)['paseo.enabled'], ['image'])")
    native_test = native_test.replace(promoted_assertion, "self.assertEqual(tpl.helm_chart_version, '0.5.7')")
    test_file = tmp_path / PROMOTION.TEST
    test_file.parent.mkdir(parents=True)
    test_file.write_text(native_test)
    patch = PROMOTION.promotion_patch(tmp_path, TAG)
    assert patch.count('--- a/') == 2
    assert "+        self.assertEqual(tpl.helm_chart_version, '0.5.9')" in patch
    assert "['paseo.enabled']" in patch
    result = subprocess.run(['git', '-C', str(tmp_path), 'apply', '--check', '-'],
                            input=patch, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    subprocess.run(['git', 'apply', '-'], cwd=tmp_path, input=patch, text=True, check=True)
    assert PROMOTION.promotion_patch(tmp_path, TAG) == ''
    assert (ROOT / PROMOTION.SEED).read_text() == CURRENT_SOURCE

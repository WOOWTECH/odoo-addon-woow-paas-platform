"""Focused resolver tests: no Odoo, database or live operator required."""
import copy
import importlib.util
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / 'src/services/deployment_target.py'
SPEC = importlib.util.spec_from_file_location('deployment_target', SOURCE)
TARGET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TARGET)
TAG = 'candidate@sha256:' + 'a' * 64
REF = {'repository': 'registry.example/open-design', 'tag': TAG}
DEFAULTS = {
    'image': {'repository': 'registry.example/open-design', 'tag': 'template',
              'digest': 'sha256:old', 'pullPolicy': 'IfNotPresent'},
    'console': {'image': {'repository': 'registry.example/console', 'tag': 'stable'}},
    'paseo': {'enabled': False}, 'omnigentHost': {'enabled': False},
}


def resolve(**overrides):
    args = dict(
        policy='pinned', chart='oci://registry.example/open-design', repo_url='',
        template_version='0.5.7', template_values=DEFAULTS,
        image_paths=[('image',), ('console', 'image')], tenant_keys=set(),
        pinned_version='0.5.8-paseo.2', image_overrides={'image': REF},
    )
    args.update(overrides)
    return TARGET.resolve_deployment_target(**args)


@pytest.mark.parametrize('version', ['1.2.3', '0.5.8-paseo.2', '0.5.8+build.7',
                                    '0.5.8-paseo.2+build.7', '1.2.3-0'])
def test_exact_versions_preserved(version):
    target = resolve(pinned_version=version)
    assert target['version'] == version
    assert target['chart'] == 'oci://registry.example/open-design'


@pytest.mark.parametrize('version', [None, '', False, 1, 'latest', '^0.5.8',
                                    '>=0.5.8', '0.5', ' 0.5.8', '0.5.8\n',
                                    '01.2.3', '0.5.8-01', '--version=1.2.3'])
def test_ranges_and_malformed_chart_versions_rejected(version):
    with pytest.raises(ValueError):
        resolve(pinned_version=version)


def test_pin_changes_only_selected_image_and_never_mutates_input():
    before = copy.deepcopy(DEFAULTS)
    target = resolve()
    image = target['image_values']['image']
    assert image == {**REF, 'pullPolicy': 'IfNotPresent'}
    assert target['image_values']['console'] == before['console']
    assert target['image_values']['paseo'] == before['paseo']
    assert DEFAULTS == before


def test_peer_following_template_has_no_pin_effect():
    pinned = resolve()
    peer = resolve(policy='template', pinned_version=None, image_overrides={})
    assert pinned['version'] == '0.5.8-paseo.2'
    assert peer['version'] == '0.5.7'
    assert peer['image_values'] == DEFAULTS


@pytest.mark.parametrize('overrides', [[], '', 42, {'unknown.image': REF},
    {'paseo': {'enabled': True}}, {'image': {**REF, 'extraEnv': []}},
    {'image': {'repository': REF['repository'], 'tag': 'latest'}},
    {'image': {'repository': REF['repository'], 'tag': 'candidate@sha256:abc'}},
    {'image': {**REF, 'repository': 'https://registry.example/app'}},
    {'image': {**REF, 'repository': 'user:credential@registry.example/app'}},
    {'image': {**REF, 'registry': 'registry.example/path'}},
    {'image': {**REF, 'tag': TAG + '\n'}},
    {'image': {**REF, 'digest': 'sha256:' + 'b' * 64}},
])
def test_strict_image_overrides(overrides):
    with pytest.raises(ValueError):
        resolve(image_overrides=overrides)


@pytest.mark.parametrize('tenant_key', ['image', 'image.tag', 'image.repository'])
def test_tenant_owned_image_cannot_be_pinned(tenant_key):
    with pytest.raises(ValueError):
        resolve(tenant_keys={tenant_key})


def test_nested_image_override():
    target = resolve(image_overrides={'console.image': REF})
    assert target['image_values']['console']['image'] == REF
    assert target['image_values']['image'] == DEFAULTS['image']


def test_no_silent_inactive_pin_and_unknown_policy():
    for args in ({'policy': 'template'}, {'policy': 'unknown'},
                 {'policy': 'template', 'pinned_version': None, 'image_overrides': []}):
        with pytest.raises(ValueError):
            resolve(**args)


def test_empty_overrides_pin_only_chart_and_unpinned_legacy_template():
    assert resolve(image_overrides={})['image_values'] == DEFAULTS
    target = resolve(policy='template', pinned_version=None, image_overrides={},
                     template_version=False)
    assert target['version'] is False  # Existing lifecycle guard owns this error.

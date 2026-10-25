"""Resolve an administrator's deployment pin without reading tenant values.

Image overrides map existing dotted image paths to complete immutable references,
e.g. {"image": {"repository": "registry.example/app", "tag": "v1@sha256:..."}}.
Unspecified images still follow the product; non-image configuration never enters
this resolver. The observed Helm version remains a separate deployment ledger.
"""
import copy
import re


_NUMBER = r'(?:0|[1-9][0-9]*)'
_PRE = rf'(?:{_NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)'
_VERSION = re.compile(
    rf'{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:-{_PRE}(?:\.{_PRE})*)?'
    r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z')
_REPOSITORY = re.compile(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?'
                         r'(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z')
_TAG = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}@sha256:[0-9a-f]{64}\Z')
_REF_KEYS = frozenset({'repository', 'tag', 'registry'})


def resolve_deployment_target(*, policy, chart, repo_url, template_version,
                              template_values, image_paths, tenant_keys,
                              pinned_version=None, image_overrides=None):
    """Return a fresh target dict; invalid pins raise ValueError before any I/O."""
    if policy not in ('template', 'pinned'):
        raise ValueError('Unknown deployment policy.')
    if image_overrides is None or image_overrides is False:
        image_overrides = {}
    if not isinstance(image_overrides, dict) or len(image_overrides) > 32:
        raise ValueError('Deployment image overrides must be an object with at most 32 image paths.')
    target = {
        'policy': policy, 'chart': chart, 'repo_url': repo_url,
        'version': template_version, 'image_values': copy.deepcopy(template_values),
    }
    if policy == 'template':
        if pinned_version or image_overrides:
            raise ValueError('Clear the deployment version and image overrides when following the template.')
        return target
    if (not isinstance(pinned_version, str) or len(pinned_version) > 256
            or not _VERSION.fullmatch(pinned_version)):
        raise ValueError('A pinned deployment requires an exact SemVer chart version (no ranges or latest).')
    if not isinstance(template_values, dict):
        raise ValueError('The product must have valid object-shaped Helm defaults.')
    allowed_paths = {'.'.join(path) for path in image_paths if path}
    for path, ref in image_overrides.items():
        if path not in allowed_paths:
            raise ValueError('Deployment image overrides must name an existing product image path.')
        if any(key == path or key.startswith(path + '.') or path.startswith(key + '.')
               for key in tenant_keys):
            raise ValueError('A deployment pin cannot override a tenant-configurable image.')
        if not isinstance(ref, dict) or set(ref) - _REF_KEYS:
            raise ValueError('Image overrides accept only repository, tag and optional registry.')
        repository, tag = ref.get('repository'), ref.get('tag')
        if (not isinstance(repository, str) or len(repository) > 255
                or not _REPOSITORY.fullmatch(repository)):
            raise ValueError('An image repository must be a container repository name, without a scheme, tag or credentials.')
        if not isinstance(tag, str) or not _TAG.fullmatch(tag):
            raise ValueError('A pinned image tag must include an immutable @sha256 digest (64 lowercase hex characters).')
        if 'registry' in ref and (not isinstance(ref['registry'], str)
                                 or '/' in ref['registry']
                                 or not _REPOSITORY.fullmatch(ref['registry'])):
            raise ValueError('Invalid image registry.')
        node = target['image_values']
        parts = path.split('.')
        for part in parts[:-1]:
            node = node[part]
        image = node[parts[-1]]
        # Keep unrelated image options, but never retain a stale digest/registry
        # that would shadow this complete repository + tag@digest reference.
        for key in ('registry', 'repository', 'tag', 'digest'):
            image.pop(key, None)
        image.update(copy.deepcopy(ref))
    target['version'] = pinned_version
    return target

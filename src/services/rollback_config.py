"""回滾公開設定合約：spec 驅動、固定 revision、缺席明確刪除。"""
import copy
import hashlib
import json
import math
import re


def ledger_digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def public_paths(specs, preserved=()):
    if not isinstance(specs, dict):
        raise ValueError('unreadable configuration specs')
    paths = {}
    for section in ('required', 'optional'):
        for spec in specs.get(section) or []:
            if not isinstance(spec, dict):
                continue
            key = spec.get('key')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*', key):
                continue
            if (spec.get('type') not in ('text', 'select', 'number', 'boolean')
                    or spec.get('create_only') is True
                    or 'sensitive' in key.lower().split('.')
                    or any(word in key.lower() for word in ('password', 'secret', 'token', 'apikey', 'apiserverkey', 'authkey'))
                    or any(key == path or key.startswith(path + '.') or path.startswith(key + '.')
                           for path in preserved)):
                continue
            paths[key] = spec.get('type')
    return paths


def apply_projection(current, response, revision, paths):
    if not isinstance(response, dict):
        raise ValueError('invalid configuration response')
    if type(response.get('revision')) is not int or response['revision'] != revision:
        raise ValueError('configuration revision mismatch')
    values, missing = response.get('values'), response.get('missing')
    if not isinstance(values, dict) or not isinstance(missing, list):
        raise ValueError('invalid configuration projection')
    if (any(not isinstance(k, str) for k in missing)
            or len(missing) != len(set(missing))
            or set(values) & set(missing)
            or set(values) | set(missing) != set(paths)):
        raise ValueError('configuration path mismatch')
    result = copy.deepcopy(current)
    for key in paths:
        if key in values:
            value, kind = values[key], paths[key]
            if (kind in ('text', 'select') and (not isinstance(value, str) or len(value) > 16384)
                    or kind == 'boolean' and type(value) is not bool
                    or kind == 'number' and (type(value) not in (int, float) or not math.isfinite(value))):
                raise ValueError('configuration type mismatch')
        node = result
        parts = key.split('.')
        for part in parts[:-1]:
            if key in missing and not isinstance(node.get(part), dict):
                break
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        else:
            if key in values:
                node[parts[-1]] = copy.deepcopy(values[key])
            else:
                node.pop(parts[-1], None)
    return result

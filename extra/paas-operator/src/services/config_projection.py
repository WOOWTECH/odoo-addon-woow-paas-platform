"""內部設定同步只傳回明確要求的非憑證標量。"""
import copy
import math
import re


def validate_paths(paths, excluded_paths=()):
    if len(paths) > 128:
        raise ValueError('too many configuration paths')
    for path in paths:
        if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*', path):
            raise ValueError('invalid configuration path')
        if ('sensitive' in path.lower().split('.') or
                any(word in path.lower() for word in ('password', 'secret', 'token', 'apikey', 'apiserverkey', 'authkey'))):
            raise ValueError('credential path cannot be projected')
        if any(path == key or path.startswith(key + '.') or key.startswith(path + '.')
               for key in excluded_paths):
            raise ValueError('preserved path cannot be projected')


def project_values(values, paths, excluded_paths=()):
    validate_paths(paths, excluded_paths)
    selected, missing = {}, []
    for path in dict.fromkeys(paths):
        node = values
        for part in path.split('.'):
            if not isinstance(node, dict) or part not in node:
                missing.append(path)
                break
            node = node[part]
        else:
            if (node is not None and type(node) not in (str, bool, int, float)
                    or isinstance(node, str) and len(node) > 16384
                    or isinstance(node, float) and not math.isfinite(node)):
                raise ValueError('configuration value must be a bounded scalar')
            selected[path] = copy.deepcopy(node)
    return {'values': selected, 'missing': missing}

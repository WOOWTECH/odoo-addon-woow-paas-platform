"""內部設定同步只傳回明確要求的非憑證標量。

例外：``LIST_PATHS`` 裡的路徑可以是「短技術名稱清單」（Odoo 租戶勾選的模組
``addons.selected``）。平台在回復版本後要把自己記的勾選對齊到目標 revision 實際套用的
清單；清單是模組技術名，不是憑證，但仍然有界（筆數、單筆長度與字元集）。
"""
import copy
import math
import re

#: 允許投影成「短字串清單」的路徑（其餘路徑一律只收有界標量）。新增路徑前先確認它的內容
#: 不是憑證、不是自由文字。
LIST_PATHS = frozenset({'addons.selected'})
MAX_LIST_ITEMS = 512
_LIST_ITEM_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}')


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
            if path in LIST_PATHS and isinstance(node, list):
                if (len(node) > MAX_LIST_ITEMS
                        or any(not isinstance(item, str) or not _LIST_ITEM_RE.fullmatch(item)
                               for item in node)):
                    raise ValueError('configuration list must contain bounded technical names')
            elif (node is not None and type(node) not in (str, bool, int, float)
                    or isinstance(node, str) and len(node) > 16384
                    or isinstance(node, float) and not math.isfinite(node)):
                raise ValueError('configuration value must be a bounded scalar')
            selected[path] = copy.deepcopy(node)
    return {'values': selected, 'missing': missing}

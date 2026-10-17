"""回滾公開設定合約：spec 驅動、固定 revision、缺席明確刪除。"""
import copy
import hashlib
import json
import math
import re


def ledger_digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


# AI-OMN-005-2：範本管理的 image 參照（#1408 的 ``_IMAGE_REF_KEYS``）也要隨回復回到
# 目標 revision；否則帳本仍是回復前的 image，下一次租戶儲存設定（沿用帳本 image）就把
# 服務悄悄前滾。operator 單次 projection 上限 128 條（``config_projection.validate_paths``）。
IMAGE_KIND = 'image'
MAX_PATHS = 128


def projectable(key, preserved=()):
    """``key`` 能不能交給 operator 投影：路徑格式合法、不是憑證、不與保留路徑重疊。

    與 operator 端 ``validate_paths`` 同一組判準——這裡先濾掉，免得單一條不合格的路徑
    讓整次同步失敗、服務一直卡在「等待回復設定同步」。
    """
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*', key):
        return False
    return not ('sensitive' in key.lower().split('.')
                or any(word in key.lower() for word in ('password', 'secret', 'token', 'apikey', 'apiserverkey', 'authkey'))
                or any(key == path or key.startswith(path + '.') or path.startswith(key + '.')
                       for path in preserved))


def public_paths(specs, preserved=()):
    if not isinstance(specs, dict):
        raise ValueError('unreadable configuration specs')
    paths = {}
    for section in ('required', 'optional'):
        for spec in specs.get(section) or []:
            if not isinstance(spec, dict):
                continue
            key = spec.get('key')
            if not projectable(key, preserved):
                continue
            if (spec.get('type') not in ('text', 'select', 'number', 'boolean')
                    or spec.get('create_only') is True):
                continue
            paths[key] = spec.get('type')
    return paths


def image_paths(candidates, tenant_keys=(), preserved=(), taken=()):
    """範本管理 image 鍵 → ``{dotted path: IMAGE_KIND}``（AI-OMN-005-2）。

    ``candidates`` 是帳本裡 image 參照的葉路徑（呼叫端用 #1408 的 ``_iter_image_refs``
    產生，外加 Omnigent 的字串 ``runner.image``）。租戶可設定的鍵（spec key 本身或其
    祖先）屬於租戶、走 ``public_paths``，這裡不收；``taken`` 是已在 ``public_paths``
    的鍵；不可投影的路徑略過。總數不超過 ``MAX_PATHS``（含 ``taken``）。
    """
    tenant = set(tenant_keys or ())
    taken = set(taken or ())
    paths = {}
    for dotted in candidates:
        parts = dotted.split('.') if isinstance(dotted, str) else []
        if (not parts or dotted in taken or dotted in paths
                or any('.'.join(parts[:i]) in tenant for i in range(1, len(parts) + 1))
                or not projectable(dotted, preserved)):
            continue
        if len(taken) + len(paths) >= MAX_PATHS:
            break
        paths[dotted] = IMAGE_KIND
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
                    or kind == 'number' and (type(value) not in (int, float) or not math.isfinite(value))
                    # image tag 在 YAML 裡可能被解析成數字（``tag: 1.25``）；照實寫回。
                    or kind == IMAGE_KIND and value is not None and not (
                        isinstance(value, str) and len(value) <= 16384
                        or type(value) in (int, float) and math.isfinite(value))):
                raise ValueError('configuration type mismatch')
        # image 鍵在目標 revision 是 null（chart 預設 ``digest: null`` 之類）＝沒有這個鍵。
        absent = key not in values or (paths[key] == IMAGE_KIND and values[key] is None)
        node = result
        parts = key.split('.')
        for part in parts[:-1]:
            if absent and not isinstance(node.get(part), dict):
                break
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        else:
            if absent:
                node.pop(parts[-1], None)
            else:
                node[parts[-1]] = copy.deepcopy(values[key])
    return result

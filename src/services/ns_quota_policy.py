"""#1679 namespace quota 的「不降額」推送政策（純計算，不呼叫 operator、不碰 ORM）。

operator 的 PUT／POST 是整組 ``kubectl apply``（``extra/paas-operator/src/services/helm.py``
``_build_quota_manifest``）：requests／limits 同值、``persistentvolumeclaims`` 固定 10、
ephemeral 兩鍵有帶才寫。它沒有任何「不可降低」保護，所以平台在送出前必須：

1. 每個 payload 欄位取 max(目標, 叢集現況)，以**原字串**挑較大者——避免把 ``2500m`` 之類
   的叢集值重新格式化成別的字串；
2. 依 operator 同一條規則展開成完整 hard（8 個 key），逐項與叢集 spec.hard／status.hard
   比對，任何一項會下降（例如 pvc 現況大於 10）就整筆不送；
3. 目標低於叢集的項目回報為「待處理降額」，本票不推下降（降額由後續票交付）。

看不懂的叢集值一律視為「無法證明不降額」，不送。
"""
from decimal import Decimal, InvalidOperation
import re

PVC_HARD = '10'

# payload 欄位 → operator 會寫入的 ResourceQuota hard key
PAYLOAD_KEYS = {
    'cpu_limit': ('requests.cpu', 'limits.cpu'),
    'memory_limit': ('requests.memory', 'limits.memory'),
    'storage_limit': ('requests.storage',),
    'ephemeral_storage_request': ('requests.ephemeral-storage',),
    'ephemeral_storage_limit': ('limits.ephemeral-storage',),
}

_BINARY = {'Ki': 2 ** 10, 'Mi': 2 ** 20, 'Gi': 2 ** 30, 'Ti': 2 ** 40, 'Pi': 2 ** 50, 'Ei': 2 ** 60}
_DECIMAL = {'n': Decimal('1e-9'), 'u': Decimal('1e-6'), 'm': Decimal('1e-3'), 'k': Decimal(10 ** 3),
            'M': Decimal(10 ** 6), 'G': Decimal(10 ** 9), 'T': Decimal(10 ** 12),
            'P': Decimal(10 ** 15), 'E': Decimal(10 ** 18)}
_QUANTITY_RE = re.compile(r'^([+]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)([A-Za-z]{0,2})$')


class QuotaNotProvable(Exception):
    """叢集現況看不懂或不完整：無法證明這次推送不會降額。"""


def parse_quantity(value):
    """k8s quantity 字串 → 有限、非負的 Decimal；看不懂回 ``None``。"""
    if not isinstance(value, str):
        return None
    match = _QUANTITY_RE.match(value.strip())
    if not match:
        return None
    number, suffix = match.groups()
    try:
        base = Decimal(number)
    except InvalidOperation:
        return None
    if not base.is_finite():
        return None
    if not suffix:
        return base
    if suffix in _BINARY:
        return base * _BINARY[suffix]
    if suffix in _DECIMAL:
        return base * _DECIMAL[suffix]
    return None


def build_hard(payload):
    """與 operator ``_build_quota_manifest`` 的 spec.hard 同形。"""
    hard = {
        'requests.cpu': payload['cpu_limit'],
        'limits.cpu': payload['cpu_limit'],
        'requests.memory': payload['memory_limit'],
        'limits.memory': payload['memory_limit'],
        'persistentvolumeclaims': PVC_HARD,
        'requests.storage': payload['storage_limit'],
    }
    if payload.get('ephemeral_storage_request'):
        hard['requests.ephemeral-storage'] = payload['ephemeral_storage_request']
    if payload.get('ephemeral_storage_limit'):
        hard['limits.ephemeral-storage'] = payload['ephemeral_storage_limit']
    return hard


def _live_values(key, *maps):
    """某個 hard key 在叢集 spec／status 裡的值（有出現才算）；看不懂就 raise。"""
    values = []
    for live in maps:
        if key not in live:
            continue
        number = parse_quantity(live[key])
        if number is None:
            raise QuotaNotProvable('%s=%r' % (key, live[key]))
        values.append((number, live[key]))
    return values


def merge_never_lower(target, spec_hard, status_hard):
    """把目標 payload 合併成「不降低叢集任何一項」的 payload。

    回傳 ``(payload, lowering, pending_decrease)``：
    - ``payload``：每個欄位取 max(目標, 叢集現況) 的原字串；
    - ``lowering``：即使合併後仍會下降的 hard key（例如 pvc），非空就**不得送出**；
    - ``pending_decrease``：目標低於叢集、因不降額而保留叢集值的項目
      ``[(key, 叢集值, 目標值)]``。
    看不懂的叢集值 raise ``QuotaNotProvable``。
    """
    spec_hard = spec_hard or {}
    status_hard = status_hard or {}
    payload = {}
    pending = []
    for field, keys in PAYLOAD_KEYS.items():
        wanted = target.get(field)
        if not wanted:
            continue
        wanted_number = parse_quantity(wanted)
        if wanted_number is None:
            raise QuotaNotProvable('target %s=%r' % (field, wanted))
        best_number, best_text = wanted_number, wanted
        for key in keys:
            live = _live_values(key, spec_hard, status_hard)
            if not live:
                continue
            live_max = max(live, key=lambda item: item[0])
            if live_max[0] > wanted_number:
                pending.append((key, live_max[1], wanted))
            if live_max[0] > best_number:
                best_number, best_text = live_max
        payload[field] = best_text
    lowering = []
    for key, value in build_hard(payload).items():
        number = parse_quantity(value)
        for live_number, _text in _live_values(key, spec_hard, status_hard):
            if number is None or live_number > number:
                lowering.append(key)
                break
    return payload, lowering, pending


# ------------------------------------------------------------------------- #
#  #1681／#1682 安全降額判斷（管理員確認流程與生命週期自動降額共用這一份）     #
# ------------------------------------------------------------------------- #
#: 可以降額的 payload 欄位 → 對應的 hard key。ephemeral-storage 與 pvc 不在範圍內，維持不降額。
LOWERABLE_FIELDS = {
    'cpu_limit': ('requests.cpu', 'limits.cpu'),
    'memory_limit': ('requests.memory', 'limits.memory'),
    'storage_limit': ('requests.storage',),
}
#: 只要有任何降額，這 5 項 used 都必須存在且看得懂（「完整用量」，缺一項就 fail-closed）。
SAFE_LOWER_KEYS = ('requests.cpu', 'limits.cpu', 'requests.memory', 'limits.memory',
                   'requests.storage')


def plan_safe_lower(target, managed):
    """依受管 quota 的 hard／used 判斷這次能不能把叢集降到目標（純函式）。

    ``target``：``_ns_quota_payload`` 形狀；``managed``：operator GET
    ``include_managed=true`` 回的 ``managed_quota``。

    回傳 dict：

    - ``payload``：要送的 payload。全部降額都安全時，降額欄位取目標值；否則（全有或全無）
      等於 ``merge_never_lower`` 的結果，只送增額。``managed`` 不可用或叢集值看不懂時是
      ``None``（沒有可信的叢集現值，不能組任何 payload）。
    - ``lowered``：``[(hard_key, 叢集值, 目標值, used)]``，這次真的會降的項（全部已通過檢查）。
    - ``blocked``：``[(hard_key, reason)]``，想降但不安全。reason 為 ``used_exceeds: …``、
      ``used_missing``、``used_invalid``；整體性的原因 key 為 ``'*'``：
      ``operator_unsupported``（舊 operator：沒有受管 quota 報表或沒有 ``sync_token`` 鍵）、
      ``managed_unavailable: <reason>``、``quota_not_provable: …``。
    - ``pending_decrease``：``[(key, 叢集值, 目標值)]``，目標低於叢集但這次不降的項
      （ephemeral-storage，以及被擋下的降額）。
    - ``lowering_unsafe``：合併後仍會降、但不在可降範圍的 hard key（例如 pvc 高於 10）——
      呼叫端要整筆不送（同 ``would_lower``）。

    叢集現值取 spec.hard 與 status.hard 的較大者。``used`` 等於目標算安全（admission 是 ≤）。
    當下用量通過檢查不代表未來 rollout 有空間，也不是與 K8s admission 原子化的容量保留。
    """
    def _result(payload, blocked, pending=(), lowering=(), lowered=()):
        return {'payload': payload, 'lowered': list(lowered), 'blocked': list(blocked),
                'pending_decrease': list(pending), 'lowering_unsafe': list(lowering)}

    if not isinstance(managed, dict):
        return _result(None, [('*', 'operator_unsupported')])
    if managed.get('available') is not True:
        return _result(None, [('*', 'managed_unavailable: %s' % (
            managed.get('reason') or 'managed_quota_unavailable'))])
    spec_hard = managed.get('spec_hard') or {}
    status_hard = managed.get('hard') or {}
    try:
        base, lowering, pending = merge_never_lower(target, spec_hard, status_hard)
    except QuotaNotProvable as exc:
        return _result(None, [('*', 'quota_not_provable: %s' % exc)])

    decreases = {key: (live, wanted) for key, live, wanted in pending if key in SAFE_LOWER_KEYS}
    if not decreases:
        return _result(base, [], pending, lowering)
    if 'sync_token' not in managed:
        # 舊 operator 沒有版本防護：降額一旦被晚到的舊請求蓋掉就看不出來，不降。
        return _result(base, [('*', 'operator_unsupported')], pending, lowering)

    used = managed.get('used') if isinstance(managed.get('used'), dict) else {}
    used_numbers = {}
    blocked = []
    for key in SAFE_LOWER_KEYS:
        if key not in used:
            blocked.append((key, 'used_missing'))
            continue
        number = parse_quantity(used[key])
        if number is None:
            blocked.append((key, 'used_invalid'))
            continue
        used_numbers[key] = number
    lower_fields = [field for field, keys in LOWERABLE_FIELDS.items()
                    if any(key in decreases for key in keys)]
    if not blocked:
        for field in lower_fields:
            wanted_number = parse_quantity(target[field])
            for key in LOWERABLE_FIELDS[field]:
                if used_numbers[key] > wanted_number:
                    blocked.append((key, 'used_exceeds: used %s > target %s' % (
                        used[key], target[field])))
    if blocked:
        return _result(base, blocked, pending, lowering)

    payload = dict(base)
    lowered = []
    lowered_keys = set()
    for field in lower_fields:
        payload[field] = target[field]
        for key in LOWERABLE_FIELDS[field]:
            if key in decreases:
                live, wanted = decreases[key]
                lowered.append((key, live, wanted, used[key]))
                lowered_keys.add(key)
    remaining = [item for item in pending if item[0] not in lowered_keys]
    unsafe = []
    for key, value in build_hard(payload).items():
        if key in lowered_keys:
            continue
        number = parse_quantity(value)
        for live_number, _text in _live_values(key, spec_hard, status_hard):
            if number is None or live_number > number:
                unsafe.append(key)
                break
    return _result(payload, [], remaining, unsafe, lowered)

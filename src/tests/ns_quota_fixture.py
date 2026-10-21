"""#1679 測試共用：有狀態的假 operator（只模擬 namespace quota 的讀寫邊界）。

形狀對齊真 operator：
- GET ``/api/namespaces/{ns}/quota?include_managed=true`` 的 ``managed_quota``
  （``extra/paas-operator/src/services/namespace_quota.py`` ``managed_quota_report``）；
- PUT／POST 的 hard map 由 ``_build_quota_manifest`` 同一條規則組出
  （``extra/paas-operator/src/services/helm.py``：requests／limits 同值、pvc 固定 10、
  ephemeral 兩鍵有帶才寫）。

故障只在這個邊界注入（``get_error``／``put_error``／``create_error``），不 mock 平台內部。

#1680：預設模擬新 operator 的版本防護寫入（``extra/paas-operator/src/services/helm.py``
``_write_quota_manifest``）——GET 回 ``sync_token``，帶 ``quota_guard`` 的寫入以
resourceVersion 為前提，前提不成立回 409 ``quota_conflict``（建立路徑回 ``quota: conflict``）。
``guarded=False`` 模擬舊 operator：GET 沒有 ``sync_token`` 鍵，寫入一律無條件覆寫。

#1681／#1682：``used``／``drop_used`` 設定每個 namespace 的 status.used（安全降額判斷讀它）。

#1781：``pvc_capable=True`` 模擬支援 ``pvc_limit`` 的 operator——GET 帶 ``supports_pvc_limit``、
寫入照送來的值寫 pvc 並在回應回報 ``pvc_limit``。預設 ``False``＝#1781 之前的 operator（現行
prod）：GET 沒有能力鍵，寫入靜默忽略 ``pvc_limit``、一律寫 10。``pvc_write_capable`` 可單獨關掉
寫入端，模擬滾動更新時 GET 落在新 pod、PUT 落在舊 pod。``pvc_sent`` 記每筆寫入收到的值。

遠端延遲也只在這個邊界注入：

- ``lose_response``：寫入照套用，但回應遺失（raise 逾時）；
- ``hold_writes``：請求「卡在 operator 裡」——不套用、回應逾時；之後 ``land_held()`` 才以
  當初的前提真正寫入（模擬 kubectl 晚到），前提已不成立就落不下。
"""
import threading
from unittest.mock import DEFAULT, MagicMock

from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorTimeoutError

PVC_HARD = '10'


def build_hard(cpu_limit, memory_limit, storage_limit,
               ephemeral_storage_request=None, ephemeral_storage_limit=None, pvc_limit=None):
    """與 operator ``_build_quota_manifest`` 的 spec.hard 同形。"""
    hard = {
        'requests.cpu': cpu_limit,
        'limits.cpu': cpu_limit,
        'requests.memory': memory_limit,
        'limits.memory': memory_limit,
        'persistentvolumeclaims': pvc_limit or PVC_HARD,
        'requests.storage': storage_limit,
    }
    if ephemeral_storage_request:
        hard['requests.ephemeral-storage'] = ephemeral_storage_request
    if ephemeral_storage_limit:
        hard['limits.ephemeral-storage'] = ephemeral_storage_limit
    return hard


def zero_managed_quota(namespace):
    """各資源皆為 0 的受管 quota（pvc 為固定值）：低於任何目標，不降額同步照送目標值。"""
    hard = build_hard('0', '0', '0')
    return {
        'namespace': namespace, 'name': namespace + '-quota', 'uid': 'uid-' + namespace,
        'resource_version': '1', 'observed_at': '2026-10-15T00:00:00Z',
        'namespace_exists': True, 'available': True, 'reason': None,
        'hard': hard, 'used': {key: '0' for key in hard}, 'spec_hard': dict(hard),
    }


def answer_quota_reads(client):
    """既有測試的 MagicMock client 補上 #1679「推送前先讀叢集」的讀取。

    ``include_managed=True`` 的讀取回 :func:`zero_managed_quota`，開通與重算推送都照送目標值，
    既有對 ``create_namespace``／``update_namespace_quota`` 引數的斷言不變；沒帶
    ``include_managed`` 的既有讀取（#1647 預檢）仍回測試設定的 ``return_value``。
    """
    def read(namespace, *args, include_managed=False, **kwargs):
        if not include_managed:
            return DEFAULT
        current = client.get_namespace_quota.return_value
        report = dict(current) if isinstance(current, dict) else {'namespace': namespace}
        report['managed_quota'] = zero_managed_quota(namespace)
        return report

    client.get_namespace_quota.side_effect = read
    return client


class FakeQuotaOperator:
    """``namespaces``：{ns: hard dict}＝已存在且有受管 quota 的 namespace。

    ``missing_quota``：namespace 存在但沒有受管 quota；``namespace_exists``
    控制「找不到受管 quota」時回報的 namespace 存在性（True／False／None）。
    ``tokens``：{ns: 目前 annotation 上的 sync token}；``guards``：每筆寫入帶的 quota_guard。
    多執行緒測試會同時呼叫，所有狀態都在 ``lock`` 內讀寫。
    """

    def __init__(self, namespaces=None, *, guarded=True, pvc_capable=False):
        self.guarded = guarded
        self.pvc_capable = pvc_capable
        self.pvc_write_capable = None  # None＝跟 pvc_capable 相同
        self.pvc_sent = []
        self.lock = threading.RLock()
        self.live = {ns: dict(hard) for ns, hard in (namespaces or {}).items()}
        self.tokens = {}
        self.rv = {ns: 100 for ns in self.live}
        self.missing_quota = set()
        self.namespace_exists = {}
        # #1681／#1682：叢集用量（status.used）。未設的 key 回 '0'；drop_used 讓 GET 不回那些 key。
        self.used = {}
        self.drop_used = {}
        self.get_error = None
        self.put_error = None
        self.create_error = None
        self.on_put = None
        self.lose_response = False
        self.hold_writes = False
        self.held = []
        self.puts = []
        self.creates = []
        self.guards = []
        self.reads = []
        self._rv = 100

    # --- operator client 介面 ---
    def get_namespace_quota(self, namespace, *, include_managed=False):
        with self.lock:
            self.reads.append(namespace)
            if self.get_error:
                raise self.get_error
            report = {'namespace': namespace, 'available': namespace in self.live}
            if not include_managed:
                return report
            if namespace in self.live:
                hard = dict(self.live[namespace])
                managed = {
                    'namespace': namespace, 'name': namespace + '-quota',
                    'uid': 'uid-' + namespace, 'resource_version': str(self.rv.get(namespace, 1)),
                    'observed_at': '2026-10-15T00:00:00Z', 'namespace_exists': True,
                    'available': True, 'reason': None,
                    'hard': hard, 'used': self._used(namespace, hard), 'spec_hard': dict(hard),
                }
            else:
                managed = {
                    'namespace': namespace, 'name': namespace + '-quota', 'uid': None,
                    'resource_version': None, 'observed_at': '2026-10-15T00:00:00Z',
                    'namespace_exists': self.namespace_exists.get(
                        namespace, True if namespace in self.missing_quota else None),
                    'available': False, 'reason': 'managed_quota_not_found',
                    'hard': {}, 'used': {},
                }
            if self.guarded:
                managed['sync_token'] = self.tokens.get(namespace)
            if self.pvc_capable:
                managed['supports_pvc_limit'] = True
            report['managed_quota'] = managed
            return report

    def _used(self, namespace, hard):
        used = {key: self.used.get(namespace, {}).get(key, '0') for key in hard}
        for key in self.drop_used.get(namespace, ()):
            used.pop(key, None)
        return used

    def update_namespace_quota(self, namespace, cpu_limit, memory_limit, storage_limit,
                               ephemeral_storage_request=None, ephemeral_storage_limit=None,
                               quota_guard=None, pvc_limit=None):
        call = (namespace, cpu_limit, memory_limit, storage_limit,
                ephemeral_storage_request, ephemeral_storage_limit)
        with self.lock:
            self.puts.append(call)
            self.guards.append(quota_guard)
            self.pvc_sent.append(pvc_limit)
            pvc = self._pvc_written(pvc_limit)
        if self.on_put:
            self.on_put(*call)
        with self.lock:
            if self.put_error:
                raise self.put_error
            outcome = self._write(namespace, call[1:] + (pvc,), quota_guard)
        if outcome == 'conflict':
            raise PaaSOperatorAPIError('API error: quota_conflict', status_code=409,
                                       detail='quota_conflict')
        result = {'message': 'Namespace %s quota updated' % namespace}
        if self.guarded and quota_guard:
            result['quota'] = 'applied'
        if pvc:
            result['pvc_limit'] = pvc
        return result

    def create_namespace(self, namespace, cpu_limit='2', memory_limit='4Gi',
                         storage_limit='20Gi', workspace_id=None,
                         ephemeral_storage_request=None, ephemeral_storage_limit=None,
                         quota_guard=None, pvc_limit=None):
        call = (namespace, cpu_limit, memory_limit, storage_limit,
                ephemeral_storage_request, ephemeral_storage_limit)
        with self.lock:
            self.creates.append(call)
            self.guards.append(quota_guard)
            self.pvc_sent.append(pvc_limit)
            pvc = self._pvc_written(pvc_limit)
            if self.create_error:
                raise self.create_error
            outcome = self._write(namespace, call[1:] + (pvc,), quota_guard)
        result = {'message': 'Namespace %s created with quota' % namespace}
        if self.guarded and quota_guard:
            result['quota'] = outcome
        if pvc and outcome == 'applied':
            result['pvc_limit'] = pvc
        return result

    def _pvc_written(self, pvc_limit):
        """這筆寫入實際會寫的 pvc（``None``＝operator 寫固定 10）。"""
        capable = self.pvc_capable if self.pvc_write_capable is None else self.pvc_write_capable
        return pvc_limit if capable else None

    def _write(self, namespace, quota_args, quota_guard):
        """套用一筆寫入；``hold_writes``／``lose_response`` 在這裡注入遠端延遲。"""
        guard = quota_guard if self.guarded else None
        if self.hold_writes:
            self.held.append((namespace, quota_args, guard))
            raise PaaSOperatorTimeoutError('Request timeout')
        outcome = self._apply_guarded(namespace, quota_args, guard)
        if self.lose_response and outcome == 'applied':
            raise PaaSOperatorTimeoutError('Request timeout')
        return outcome

    def _apply_guarded(self, namespace, quota_args, guard):
        if guard:
            if guard.get('expect_absent'):
                if namespace in self.live:
                    return 'conflict'
            elif (namespace not in self.live
                  or str(self.rv.get(namespace)) != guard.get('expected_resource_version')):
                return 'conflict'
        self._apply(namespace, *quota_args)
        if guard:
            self.tokens[namespace] = guard['sync_token']
        else:
            self.tokens.pop(namespace, None)
        return 'applied'

    def land_held(self):
        """讓卡住的請求真的寫入（kubectl 晚到）；回傳各筆結果 'applied'／'conflict'。"""
        with self.lock:
            held, self.held = self.held, []
            return [self._apply_guarded(ns, args, guard) for ns, args, guard in held]

    def touch(self, namespace):
        """其他寫入者（例如 quota controller 更新 status）讓 resourceVersion 前進。"""
        with self.lock:
            self._rv += 1
            self.rv[namespace] = self._rv

    def _apply(self, namespace, *quota_args):
        self.live[namespace] = build_hard(*quota_args)
        self.missing_quota.discard(namespace)
        self._rv += 1
        self.rv[namespace] = self._rv

    def bind(self, client=None):
        """把 quota 三個方法接到 ``client``（MagicMock），其餘方法維持 MagicMock。"""
        client = client or MagicMock()
        client.get_namespace_quota.side_effect = self.get_namespace_quota
        client.update_namespace_quota.side_effect = self.update_namespace_quota
        client.create_namespace.side_effect = self.create_namespace
        return client

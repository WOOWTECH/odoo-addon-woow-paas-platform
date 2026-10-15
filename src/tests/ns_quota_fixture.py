"""#1679 測試共用：有狀態的假 operator（只模擬 namespace quota 的讀寫邊界）。

形狀對齊真 operator：
- GET ``/api/namespaces/{ns}/quota?include_managed=true`` 的 ``managed_quota``
  （``extra/paas-operator/src/services/namespace_quota.py`` ``managed_quota_report``）；
- PUT／POST 的 hard map 由 ``_build_quota_manifest`` 同一條規則組出
  （``extra/paas-operator/src/services/helm.py``：requests／limits 同值、pvc 固定 10、
  ephemeral 兩鍵有帶才寫）。

故障只在這個邊界注入（``get_error``／``put_error``／``create_error``），不 mock 平台內部。
"""
from unittest.mock import MagicMock

PVC_HARD = '10'


def build_hard(cpu_limit, memory_limit, storage_limit,
               ephemeral_storage_request=None, ephemeral_storage_limit=None):
    """與 operator ``_build_quota_manifest`` 的 spec.hard 同形。"""
    hard = {
        'requests.cpu': cpu_limit,
        'limits.cpu': cpu_limit,
        'requests.memory': memory_limit,
        'limits.memory': memory_limit,
        'persistentvolumeclaims': PVC_HARD,
        'requests.storage': storage_limit,
    }
    if ephemeral_storage_request:
        hard['requests.ephemeral-storage'] = ephemeral_storage_request
    if ephemeral_storage_limit:
        hard['limits.ephemeral-storage'] = ephemeral_storage_limit
    return hard


class FakeQuotaOperator:
    """``namespaces``：{ns: hard dict}＝已存在且有受管 quota 的 namespace。

    ``missing_quota``：namespace 存在但沒有受管 quota；``namespace_exists``
    控制「找不到受管 quota」時回報的 namespace 存在性（True／False／None）。
    """

    def __init__(self, namespaces=None):
        self.live = {ns: dict(hard) for ns, hard in (namespaces or {}).items()}
        self.missing_quota = set()
        self.namespace_exists = {}
        self.get_error = None
        self.put_error = None
        self.create_error = None
        self.on_put = None
        self.puts = []
        self.creates = []
        self.reads = []
        self._rv = 100

    # --- operator client 介面 ---
    def get_namespace_quota(self, namespace, *, include_managed=False):
        self.reads.append(namespace)
        if self.get_error:
            raise self.get_error
        report = {'namespace': namespace, 'available': namespace in self.live}
        if not include_managed:
            return report
        if namespace in self.live:
            hard = dict(self.live[namespace])
            report['managed_quota'] = {
                'namespace': namespace, 'name': namespace + '-quota',
                'uid': 'uid-' + namespace, 'resource_version': str(self._rv),
                'observed_at': '2026-10-15T00:00:00Z', 'namespace_exists': True,
                'available': True, 'reason': None,
                'hard': hard, 'used': {k: '0' for k in hard}, 'spec_hard': dict(hard),
            }
        else:
            report['managed_quota'] = {
                'namespace': namespace, 'name': namespace + '-quota', 'uid': None,
                'resource_version': None, 'observed_at': '2026-10-15T00:00:00Z',
                'namespace_exists': self.namespace_exists.get(
                    namespace, True if namespace in self.missing_quota else None),
                'available': False, 'reason': 'managed_quota_not_found',
                'hard': {}, 'used': {},
            }
        return report

    def update_namespace_quota(self, namespace, cpu_limit, memory_limit, storage_limit,
                               ephemeral_storage_request=None, ephemeral_storage_limit=None):
        call = (namespace, cpu_limit, memory_limit, storage_limit,
                ephemeral_storage_request, ephemeral_storage_limit)
        self.puts.append(call)
        if self.on_put:
            self.on_put(*call)
        if self.put_error:
            raise self.put_error
        self._apply(namespace, *call[1:])
        return {'message': 'Namespace %s quota updated' % namespace}

    def create_namespace(self, namespace, cpu_limit='2', memory_limit='4Gi',
                         storage_limit='20Gi', workspace_id=None,
                         ephemeral_storage_request=None, ephemeral_storage_limit=None):
        call = (namespace, cpu_limit, memory_limit, storage_limit,
                ephemeral_storage_request, ephemeral_storage_limit)
        self.creates.append(call)
        if self.create_error:
            raise self.create_error
        self._apply(namespace, *call[1:])
        return {'message': 'Namespace %s created with quota' % namespace}

    def _apply(self, namespace, *quota_args):
        self.live[namespace] = build_hard(*quota_args)
        self.missing_quota.discard(namespace)
        self._rv += 1

    def bind(self, client=None):
        """把 quota 三個方法接到 ``client``（MagicMock），其餘方法維持 MagicMock。"""
        client = client or MagicMock()
        client.get_namespace_quota.side_effect = self.get_namespace_quota
        client.update_namespace_quota.side_effect = self.update_namespace_quota
        client.create_namespace.side_effect = self.create_namespace
        return client

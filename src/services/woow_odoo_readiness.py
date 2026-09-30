# -*- coding: utf-8 -*-
"""#1251：woow-odoo chart 的「契約管理」判定，與依 Pod 身分的就緒判定。

設計依據：``docs/decisions/patterns/woow-odoo-readiness-identity.md``。

純函式、不 import odoo：輸入是 operator ``get_status`` 的回應 dict，輸出是
``(ready, detail)``——與 ``CloudService._is_container_ready`` 同形，由
``CloudService._is_release_ready``（model poll 與 controller 共用的單一入口）分流呼叫。
"""

#: woow-odoo chart 在 ``helm list`` 的名字（operator 以 ``rsplit("-", 1)`` 拆出，
#: 見 ``extra/paas-operator/src/services/helm.py``；chart 版號因此禁止 pre-release 後綴）。
WOOW_ODOO_CHART = 'woow-odoo'

#: :func:`woow_odoo_contract` 的回傳值。``None`` ＝ 不受 woow-odoo 契約管理。
CONTRACT_MANAGED = 'managed'
CONTRACT_MISMATCH = 'mismatch'

#: 必要組：component → Pod 的直接 controller owner kind（Q19，沿用 #1129
#: ``validate_fallback_pod`` 的語意：Job-owned 的 Pod 不能冒充 server）。
_REQUIRED_COMPONENTS = (
    ('server', 'ReplicaSet'),
    ('postgresql', 'StatefulSet'),
)

#: 例行 Job 豁免白名單（Q6）：chart 的兩支 CronJob 共用 component=db-backup、以
#: ``woow.paas/task`` 區分。**不論 phase** 都不參與判定——它們綁 server 的 podAffinity，
#: server 有問題時必要組會獨立抓到；Job 自己 Pending 是維護問題，不是服務沒起來。
#: ``db-restore`` 刻意不在內（只該出現在 `restoring`，#1213）。
_EXEMPT_COMPONENT = 'db-backup'
_EXEMPT_TASKS = frozenset({'backup', 'housekeeping'})


def chart_name_of(chart_ref):
    """template 的 ``helm_chart_name`` → chart 名。

    三種寫法都取最後一段：``oci://<registry>/<path>/woow-odoo``、``<repo>/woow-odoo``、
    ``woow-odoo``。
    """
    return (chart_ref or '').rstrip('/').rsplit('/', 1)[-1] or None


def woow_odoo_contract(status, expected_chart_ref=None):
    """這個 release 受不受 woow-odoo 契約管理（Q17 共用判定）。

    依**同一輪** ``get_status`` 回應的 ``release.chart``（實際部署值）判定，不看
    template slug：cutover 期間 slug ``odoo`` 同時指著 Bitnami 實例。

    Args:
        status: operator ``get_status`` 的回應 dict。
        expected_chart_ref: template 的 ``helm_chart_name``（期望值）。

    Returns:
        :data:`CONTRACT_MANAGED` ＝ 實際跑的是 woow-odoo；
        :data:`CONTRACT_MISMATCH` ＝ template 期望 woow-odoo、實際不是（#1128：in-place
        upgrade 換不了 chart）；``None`` ＝ 不受管（呼叫端走通用判定）。
    """
    actual = (status.get('release') or {}).get('chart')
    if actual == WOOW_ODOO_CHART:
        return CONTRACT_MANAGED
    if chart_name_of(expected_chart_ref) == WOOW_ODOO_CHART:
        return CONTRACT_MISMATCH
    return None


def _is_active(pod):
    """與 #1156 同：收尾中的 Pod（Succeeded／Failed／帶 deletion_timestamp）不參與。"""
    return (
        pod.get('phase') not in ('Succeeded', 'Failed')
        and not pod.get('deletion_timestamp')
    )


def _is_pod_ready(pod):
    """Running 且 ready n/n（n>0）。"""
    ready, _, total = (pod.get('ready') or '').partition('/')
    return (
        pod.get('phase') == 'Running'
        and ready.isdigit() and total.isdigit()
        and int(total) > 0 and ready == total
    )


def _required_component(pod):
    """Pod 屬於哪個必要 component；label 與 owner kind 都對得上才算。"""
    for component, owner_kind in _REQUIRED_COMPONENTS:
        if pod.get('component') == component and pod.get('owner_kind') == owner_kind:
            return component
    return None


def _is_exempt(pod):
    return (
        pod.get('component') == _EXEMPT_COMPONENT
        and pod.get('task') in _EXEMPT_TASKS
    )


def _describe(pod):
    """detail 用的 Pod 摘要：名字＋身分證據＋狀態，讓維運不進叢集也看得出是誰。"""
    return '%s[component=%s task=%s owner=%s %s %s]' % (
        pod.get('name'), pod.get('component'), pod.get('task'),
        pod.get('owner_kind'), pod.get('phase'), pod.get('ready'),
    )


def woow_odoo_ready(status):
    """woow-odoo release 的就緒判定。

    Returns:
        ``(ready: bool, detail: str)``。
    """
    if status.get('pod_retrieval_error'):
        # 與 `_is_container_ready` 同字串：沿用 `_TIMEOUT_HINTS` 的 RBAC 方向。
        return False, f"HARD: pod lookup failed: {status['pod_retrieval_error']}"
    # Q18：舊 operator 不回 component／owner_kind。**以 echo 旗標為準、`is True` 比對**——
    # 缺 key（#1263 前的真實形狀）與 False 都擋；不從 Pod 欄位是否為 None 推斷。
    # 不退回通用判定：那會讓負例 1（只剩 Job 卻判綠）回來。
    if status.get('pod_identity_included') is not True:
        return False, (
            'HARD: operator lacks pod identity (pod_identity_included missing or '
            'false) — refusing to judge woow-odoo readiness without '
            'component/owner evidence'
        )

    required = {component: [] for component, _ in _REQUIRED_COMPONENTS}
    exempt, unexpected = [], []
    for pod in filter(_is_active, status.get('pods') or []):
        component = _required_component(pod)
        if component:
            required[component].append(pod)
        elif _is_exempt(pod):
            exempt.append(pod)
        else:
            unexpected.append(pod)

    missing = [component for component, pods in required.items() if not pods]
    hard = []
    if missing:
        hard.append('required component missing: ' + ', '.join(missing))
    if unexpected:
        hard.append('unexpected pod outside the woow-odoo contract: '
                    + ', '.join(map(_describe, unexpected)))
    if hard:
        ready, detail = False, 'HARD: ' + '; '.join(hard)
    elif not all(_is_pod_ready(pod) for pods in required.values() for pod in pods):
        ready, detail = False, 'required pods not ready'
    else:
        ready, detail = True, 'woow-odoo required components ready'
    if exempt:
        detail += ' (exempt routine jobs: %s)' % ', '.join(map(_describe, exempt))
    return ready, detail

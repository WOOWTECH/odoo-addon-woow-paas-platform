# -*- coding: utf-8 -*-
"""#1647：running 服務的 workload「期望有 Pod、實際一顆都沒有」的判定。

2026-10-10 事故：服務顯示 ``running``，Recreate Deployment 的新 Pod 被 namespace quota
拒絕（ReplicaSet ``FailedCreate: exceeded quota``），剩 0 顆 Pod 近 6 小時，平台沒有任何訊號。

純函式、不 import odoo（同 ``woow_odoo_readiness.py``）：輸入是 operator
``get_status(include_workloads=True)`` 的回應 dict 與「現在」（UTC naive），輸出是
``(verdict, code, detail)``，由 ``CloudService._apply_workload_observation`` 落欄位。

## 範圍（刻意窄）

只認「期望 ≥1、實際 0」。下面這些**不算**，各有理由：

- ``desired_replicas == 0``：停權／刻意縮到 0，不是故障；
- ``replicas >= 1``（即使帶 ``replica_failure``）：RollingUpdate 的新 Pod 被擋、舊 Pod 仍在
  服務——服務沒有中斷，不在本票範圍；
- Pod 存在但不 ready／HTTP 不通：那是 readiness 的事，不是「建不出來」。

## 缺席不是訊號

舊 operator 會忽略 ``include_workloads``、回應沒有 ``workloads_included`` ⇒ 判
``unsupported``，呼叫端一格都不寫。判準是 ``is True``：缺 key、``False``、字串 ``'true'``、
測試裡沒設定回傳值的 MagicMock 都不是 True（pitfall
``operator-route-none-conflates-disabled-and-failed``：用缺席當判準的守線會在「還沒有這個
欄位的舊版本」誤擊）。
"""
from datetime import datetime

#: ``workload_fault_code`` 的三個值。
FAULT_QUOTA = 'quota_exceeded'
FAULT_CREATE_FAILED = 'create_failed'
FAULT_NO_PODS = 'no_pods'
#: 有 ``FailedCreate`` 證據的代碼：第一次觀測就算確認。``no_pods`` 沒有證據（可能只是
#: 換 Pod 的瞬間），要持續一段時間才算（見 ``CloudService._workload_fault_confirmed``）。
EVIDENCE_CODES = frozenset({FAULT_QUOTA, FAULT_CREATE_FAILED})
#: controller 事件多新才算證據。事件會留在叢集裡約一小時；太舊的 ``FailedCreate`` 可能是
#: 上一段已經恢復的異常留下的。``replica_failure`` 是 condition（現況），不受這個限制。
WARNING_FRESH_SECONDS = 1800
#: ``workload_fault_detail`` 的長度上限（只給管理員看）。
DETAIL_LIMIT = 1000

#: :func:`workload_verdict` 的四種結論。
VERDICT_UNSUPPORTED = 'unsupported'  # operator 沒有回報 workloads（舊版）——一格都不寫
VERDICT_UNKNOWN = 'unknown'          # 這一輪查詢失敗——一格都不寫，下一輪再看
VERDICT_HEALTHY = 'healthy'
VERDICT_FAULT = 'fault'

_FAILED_CREATE = 'FailedCreate'
_QUOTA_NEEDLE = 'exceeded quota'


def _is_fresh(last_seen, now):
    """事件的 ``last_seen``（RFC 3339）距 ``now`` 不超過 ``WARNING_FRESH_SECONDS``。

    只取前 19 個字元（``YYYY-MM-DDTHH:MM:SS``）：小數秒與結尾的 ``Z`` 形狀不一。解析不了
    一律視為不新鮮——寧可少一筆證據（退回要持續 5 分鐘的 ``no_pods``），也不拿一個讀不懂
    的時間當證據。
    """
    if not isinstance(last_seen, str):
        return False
    try:
        seen = datetime.strptime(last_seen[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return False
    return (now - seen).total_seconds() <= WARNING_FRESH_SECONDS


def _failed_create_evidence(workload, now):
    """被擋的 workload 的 ``FailedCreate`` 證據 → ``(有沒有, 訊息)``。

    先看 ``replica_failure``（Deployment 的 ``ReplicaFailure`` condition），再看
    ``last_warning``（controller 事件；StatefulSet 沒有 condition，只有事件）。
    """
    failure = workload.get('replica_failure')
    if isinstance(failure, dict) and failure.get('reason') == _FAILED_CREATE:
        return True, failure.get('message') or ''
    warning = workload.get('last_warning')
    if (isinstance(warning, dict) and warning.get('reason') == _FAILED_CREATE
            and _is_fresh(warning.get('last_seen'), now)):
        return True, warning.get('message') or ''
    return False, ''


def workload_verdict(status, now):
    """operator status 回應 → ``(verdict, code, detail)``。

    Args:
        status: operator ``get_status(include_workloads=True)`` 的回應 dict。
        now: 現在時間（UTC naive；呼叫端傳 ``fields.Datetime.now()``）。

    Returns:
        - ``(VERDICT_UNSUPPORTED, None, '')``：回應不是 dict，或 ``workloads_included``
          不是 ``True``（舊 operator）；
        - ``(VERDICT_UNKNOWN, None, '')``：``workload_retrieval_error`` 有值，或
          ``workloads`` 不是 list；
        - ``(VERDICT_HEALTHY, None, '')``：沒有任何 workload「期望 ≥1、實際 0」；
        - ``(VERDICT_FAULT, code, detail)``：``code`` 是 ``FAULT_*`` 之一，``detail`` 是給
          管理員看的英文說明（每個被擋的 workload 一段，含叢集原文，不得給租戶）。
    """
    if not isinstance(status, dict) or status.get('workloads_included') is not True:
        return VERDICT_UNSUPPORTED, None, ''
    workloads = status.get('workloads')
    if status.get('workload_retrieval_error') or not isinstance(workloads, list):
        return VERDICT_UNKNOWN, None, ''

    blocked = []
    for workload in workloads:
        if not isinstance(workload, dict):
            continue
        try:
            desired = int(workload.get('desired_replicas'))
            replicas = int(workload.get('replicas'))
        except (TypeError, ValueError):
            continue
        if desired >= 1 and replicas == 0:
            blocked.append((workload, desired))
    if not blocked:
        return VERDICT_HEALTHY, None, ''

    has_evidence = False
    quota = False
    parts = []
    for workload, desired in blocked:
        found, message = _failed_create_evidence(workload, now)
        part = '%s %s: 0 of %s pods exist' % (
            workload.get('kind') or 'Workload', workload.get('name') or '?', desired)
        if workload.get('strategy'):
            part += ' (%s)' % workload['strategy']
        if found:
            has_evidence = True
            quota = quota or _QUOTA_NEEDLE in message
            part += ' — %s: %s' % (_FAILED_CREATE, message or '(no message)')
        else:
            part += ' — no controller warning found'
        parts.append(part)

    if quota:
        code = FAULT_QUOTA
    elif has_evidence:
        code = FAULT_CREATE_FAILED
    else:
        code = FAULT_NO_PODS
    detail = '; '.join(parts)
    if len(detail) > DETAIL_LIMIT:
        detail = detail[:DETAIL_LIMIT - 1] + '…'
    return VERDICT_FAULT, code, detail

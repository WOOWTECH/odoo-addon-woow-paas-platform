# -*- coding: utf-8 -*-
"""#1647／#1456：running 服務的 workload「期望有 Pod、實際一顆都沒有」與「Pod 都在、卻沒有
一顆就緒」的判定。

2026-10-10 事故：服務顯示 ``running``，Recreate Deployment 的新 Pod 被 namespace quota
拒絕（ReplicaSet ``FailedCreate: exceeded quota``），剩 0 顆 Pod 近 6 小時，平台沒有任何訊號。

純函式、不 import odoo（同 ``woow_odoo_readiness.py``）：輸入是 operator
``get_status(include_workloads=True)`` 的回應 dict 與「現在」（UTC naive），輸出是
``(verdict, code, detail)``，由 ``CloudService._apply_workload_observation`` 落欄位。

## 範圍（刻意窄）

兩類，第一類優先（中斷最徹底）：

1. #1647「期望 ≥1、實際 0」——Pod 根本建不出來（``quota_exceeded``／``create_failed``／
   ``no_pods``）；
2. #1456「期望 ≥1、Pod 在、就緒 0」——Pod 都在、卻沒有一顆就緒（``oom_killed``／
   ``crash_loop``／``not_ready``）。2026-10-16 商用驗收：n8n 250 進 CrashLoopBackOff
   80–110 秒、被 OOMKilled 約 35 秒，平台 ``state`` 全程 running、/woow 一直「線上」
   （DATA-N8N-009-1／010-1）。代碼依證據挑：沒就緒的 workload Pod（ReplicaSet／StatefulSet
   建的、沒在收尾）上一次是 ``OOMKilled`` → ``oom_killed``；容器卡在
   ``CrashLoopBackOff`` → ``crash_loop``；都沒有 → ``not_ready``。

下面這些**不算**，各有理由：

- ``desired_replicas == 0``：停權／刻意縮到 0，不是故障；
- 至少一顆 Pod 就緒（即使帶 ``replica_failure``）：RollingUpdate 的新 Pod 被擋、舊 Pod 仍在
  服務——服務沒有中斷；
- Pod 就緒但 HTTP 不通：平台看不到（要靠 chart 的 readinessProbe 把它反映成不就緒）。

第二類三個代碼都**沒有**放進 ``EVIDENCE_CODES``：running 服務每 10 分鐘才看一次，第一次
就確認的話，一次剛好撞上的重啟（單次 OOM、CrashLoopBackOff 的一次 back-off）會對租戶顯示
「沒有在執行」直到下一趟。要「首次看到滿 5 分鐘後又看到一次」才確認——這是 #1456 留言
22714 待產品拍板的「告警門檻」，保守取法；要改成第一次就確認，把代碼加進
``EVIDENCE_CODES`` 即可。

## 缺席不是訊號

舊 operator 會忽略 ``include_workloads``、回應沒有 ``workloads_included`` ⇒ 判
``unsupported``，呼叫端一格都不寫。判準是 ``is True``：缺 key、``False``、字串 ``'true'``、
測試裡沒設定回傳值的 MagicMock 都不是 True（pitfall
``operator-route-none-conflates-disabled-and-failed``：用缺席當判準的守線會在「還沒有這個
欄位的舊版本」誤擊）。
"""
from datetime import datetime

#: ``workload_fault_code`` 的值：#1647「一顆 Pod 都沒有」三個……
FAULT_QUOTA = 'quota_exceeded'
FAULT_CREATE_FAILED = 'create_failed'
FAULT_NO_PODS = 'no_pods'
#: ……與 #1456「Pod 在、沒有一顆就緒」三個。
FAULT_OOM_KILLED = 'oom_killed'
FAULT_CRASH_LOOP = 'crash_loop'
FAULT_NOT_READY = 'not_ready'
#: 全部代碼（``cloud_service.workload_fault_code`` 的 selection 必須正好是這些）。
FAULT_CODES = (FAULT_QUOTA, FAULT_CREATE_FAILED, FAULT_NO_PODS,
               FAULT_OOM_KILLED, FAULT_CRASH_LOOP, FAULT_NOT_READY)
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
_CRASH_LOOP_BACK_OFF = 'CrashLoopBackOff'
_OOM_KILLED = 'OOMKilled'
#: 由 workload 建的 Pod 的直接 owner。Job（備份、init）與認不出 owner 的 Pod 不算證據。
_WORKLOAD_POD_OWNERS = frozenset({'ReplicaSet', 'StatefulSet'})


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


def _int(value):
    """轉 int；轉不了回 None（形狀不對的元素由呼叫端略過）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pod_fully_ready(ready):
    """``PodInfo.ready``（``"就緒數/容器數"``）→ 是否全部就緒；讀不懂回 None。"""
    if not isinstance(ready, str) or '/' not in ready:
        return None
    done, _sep, total = ready.partition('/')
    done, total = _int(done), _int(total)
    if done is None or total is None:
        return None
    return total > 0 and done >= total


def _unready_pod_evidence(pods):
    """沒就緒的 workload Pod 的證據 → ``(有 OOMKilled, 有 CrashLoopBackOff, 說明片段)``。

    只看「由 ReplicaSet／StatefulSet 建的、沒在收尾、確定沒有全部就緒」的 Pod：就緒的 Pod
    身上的舊 ``OOMKilled`` 是已經恢復的事；Job 的 Pod 是備份或 init，不是服務本身；
    ``owner_kind`` 是 None（舊 operator 沒有身分欄位）就不猜。``last_terminated_reason`` 是
    #1456 起的 operator 才送的 key，缺席當沒有線索。
    """
    oom = crash = False
    parts = []
    for pod in pods if isinstance(pods, list) else ():
        if not isinstance(pod, dict) or pod.get('deletion_timestamp'):
            continue
        if pod.get('owner_kind') not in _WORKLOAD_POD_OWNERS:
            continue
        if _pod_fully_ready(pod.get('ready')) is not False:
            continue
        waiting = pod.get('waiting')
        waiting_reason = waiting.get('reason') if isinstance(waiting, dict) else None
        last_reason = pod.get('last_terminated_reason')
        clues = []
        if waiting_reason == _CRASH_LOOP_BACK_OFF:
            crash = True
            clues.append('%s (container %s)' % (
                _CRASH_LOOP_BACK_OFF, waiting.get('container') or '?'))
        elif isinstance(waiting_reason, str) and waiting_reason:
            clues.append('waiting: %s' % waiting_reason)
        if last_reason == _OOM_KILLED:
            oom = True
        if isinstance(last_reason, str) and last_reason:
            clues.append('last terminated: %s' % last_reason)
        restarts = _int(pod.get('restarts'))
        if restarts:
            clues.append('restarts %s' % restarts)
        parts.append('pod %s: %s' % (pod.get('name') or '?',
                                     ', '.join(clues) or 'not ready'))
    return oom, crash, parts


def _not_ready_verdict(status, workloads):
    """#1456：期望 ≥1、Pod 在、就緒 0 的 workload → ``(verdict, code, detail)``。"""
    unready = []
    for workload in workloads:
        if not isinstance(workload, dict):
            continue
        desired = _int(workload.get('desired_replicas'))
        replicas = _int(workload.get('replicas'))
        ready = _int(workload.get('ready_replicas'))
        if desired is None or replicas is None or ready is None:
            continue
        if desired >= 1 and replicas >= 1 and ready == 0:
            unready.append((workload, desired, replicas))
    if not unready:
        return VERDICT_HEALTHY, None, ''

    parts = []
    for workload, desired, replicas in unready:
        part = '%s %s: 0 of %s pods ready, %s exist' % (
            workload.get('kind') or 'Workload', workload.get('name') or '?', desired, replicas)
        if workload.get('strategy'):
            part += ' (%s)' % workload['strategy']
        parts.append(part)
    oom, crash, pod_parts = _unready_pod_evidence(status.get('pods'))
    if oom:
        code = FAULT_OOM_KILLED
    elif crash:
        code = FAULT_CRASH_LOOP
    else:
        code = FAULT_NOT_READY
    return VERDICT_FAULT, code, _join_detail(parts + pod_parts)


def _join_detail(parts):
    detail = '; '.join(parts)
    if len(detail) > DETAIL_LIMIT:
        detail = detail[:DETAIL_LIMIT - 1] + '…'
    return detail


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
        - ``(VERDICT_HEALTHY, None, '')``：沒有任何 workload「期望 ≥1、實際 0」，也沒有
          「期望 ≥1、Pod 在、就緒 0」（#1456）；
        - ``(VERDICT_FAULT, code, detail)``：``code`` 是 ``FAULT_*`` 之一，``detail`` 是給
          管理員看的英文說明（每個有問題的 workload／Pod 一段，含叢集原文，不得給租戶）。
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
        return _not_ready_verdict(status, workloads)

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
    return VERDICT_FAULT, code, _join_detail(parts)

# -*- coding: utf-8 -*-
"""#1526：「回復版本」的 chart 版本下限（純函式，不碰 ORM／operator）。

chart 升版帶著平台的安全修正（pi-agent 0.1.5 收斂 egress、加 ephemeral-storage 上限；
hermes-agent 0.3.0 修 console RBAC）。回復原本照目標 revision 的 chart 版本重新渲染或
重放 manifest ⇒ 工作區 admin 按兩下就能撤掉這些修正。規則：目標 revision 的 chart 版本
不得低於**目前部署中**那一版的 chart 版本；同版、較新的照常可回復。

operator（``extra/paas-operator/src/api/releases.py`` 的 ``_refuse_chart_downgrade``）是
權威的那一道，讀的是 helm release 本身的 ``Chart.Metadata.Version``；平台這份用在
(1) 對話框把不能選的 revision 標出來、(2) 送出前先擋並給租戶看得懂的訊息、(3) operator
還沒升到有下限的版本時仍然擋得住。兩邊是不同的部署單位，版本比較規則各寫一份、同規則。
"""
import re

#: operator 擋下時 409 ``detail.error_code`` 的值（與 operator 的同名常數一致）。
ROLLBACK_CHART_DOWNGRADE_ERROR_CODE = 'rollback_chart_downgrade'

_SEMVER_RE = re.compile(
    r'^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$')
# helm history 的 chart 欄是 ``<name>-<version>``；名稱本身可能含 ``-``（``pi-agent``）、
# 版本也可能含 ``-``（prerelease），所以從「第一個接著 SemVer 三段數字的 ``-``」切。
_CHART_FIELD_RE = re.compile(
    r'-(v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$')


def chart_version_key(version):
    """SemVer 2 的比較鍵；讀不懂回 ``None``。prerelease 低於同號正式版、逐段比較。"""
    if not isinstance(version, str):
        return None
    match = _SEMVER_RE.match(version.strip())
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    if pre is None:
        return (int(major), int(minor), int(patch), 1, ())
    pre_key = tuple(
        (0, int(part), '') if part.isdigit() else (1, 0, part)
        for part in pre.split('.'))
    return (int(major), int(minor), int(patch), 0, pre_key)


def chart_version_not_lower(target, current):
    """``target`` 是否**不低於** ``current``。任一方未知或讀不懂 ⇒ False（fail-closed）。"""
    if not target or not current:
        return False
    if target == current:
        return True
    target_key, current_key = chart_version_key(target), chart_version_key(current)
    if target_key is None or current_key is None:
        return False
    return target_key >= current_key


def chart_version_from_history(chart_field):
    """從 helm history 的 ``<name>-<version>`` 取出版本；取不到回 ``None``。"""
    if not isinstance(chart_field, str):
        return None
    match = _CHART_FIELD_RE.search(chart_field)
    return match.group(1) if match else None


def _revision_number(revision):
    try:
        return int(revision.get('revision'))
    except (TypeError, ValueError, AttributeError):
        return None


def rollback_baseline(revisions):
    """下限的基準：目前**部署中**的 revision（沒有就取最新的）。回傳 (revision, chart 版本)。

    最新的 revision 不一定是部署中的——升級失敗的 revision 狀態是 failed，前一版仍是
    deployed；拿失敗的新版當下限，會連「退回原本那一版」都擋掉（與 operator 同判準）。
    """
    numbered = [(n, r) for r in revisions or [] if (n := _revision_number(r)) is not None]
    if not numbered:
        return None, None
    deployed = [(n, r) for n, r in numbered if str(r.get('status', '')).lower() == 'deployed']
    number, revision = max(deployed or numbered, key=lambda item: item[0])
    return number, chart_version_from_history(revision.get('chart'))


def rollback_target_number(revisions, requested):
    """回復的目標 revision 號（``None``／0＝上一版，與 operator／helm 同語意）。"""
    numbers = [n for r in revisions or [] if (n := _revision_number(r)) is not None]
    if requested:
        return int(requested)
    return (max(numbers) - 1) if numbers else None


def annotate_rollback_floor(revisions):
    """替每個 revision 加上 ``chart_version`` 與 ``rollback_allowed``（對話框用）。

    回傳新的 list（不改呼叫端的物件）。部署中那一版本身標成可回復（它就是現況；前端
    另外以「目前版本」擋住）。
    """
    _number, current_version = rollback_baseline(revisions)
    annotated = []
    for revision in revisions or []:
        item = dict(revision) if isinstance(revision, dict) else {'revision': revision}
        version = chart_version_from_history(item.get('chart'))
        item['chart_version'] = version
        item['rollback_allowed'] = chart_version_not_lower(version, current_version)
        annotated.append(item)
    return annotated


def rollback_floor_violation(revisions, requested):
    """回復到 ``requested`` 會讓 chart 降版 ⇒ 回 dict（目標／目前版本）；可以回復回 ``None``。

    目標 revision 不在歷史裡 ⇒ ``None``（交給 operator 回 400，不在這裡重複那個判斷）。
    """
    target = rollback_target_number(revisions, requested)
    if target is None:
        return None
    by_number = {_revision_number(r): r for r in revisions or []}
    if target not in by_number:
        return None
    _baseline, current_version = rollback_baseline(revisions)
    target_version = chart_version_from_history(by_number[target].get('chart'))
    if chart_version_not_lower(target_version, current_version):
        return None
    return {
        'target_revision': target,
        'target_chart_version': target_version,
        'current_chart_version': current_version,
    }


def drop_failed_revisions(revisions):
    """回復版本清單不列 ``failed`` 的 revision（INFRA-FIX-018／INFRA-HS-002 觀察）。

    helm 的失敗升級（含 ``--atomic`` 自動回滾那一筆）會留下狀態 ``failed`` 的 revision：它從來
    沒有成功部署過，「回復」到它沒有意義，列在清單裡只會讓租戶選到一個不該選的版本。商用驗收
    實測：Headscale 238 三次失敗的改密碼各留 2 筆，清單被 6 筆 failed 塞滿（rev 8～13），
    helm 預設只留最近 10 筆，真正可回復的 rev 7 因此被擠出歷史。

    只用在「給人選」的清單；判斷基準（``rollback_baseline``）與請求檢查
    （``rollback_request_problem``）仍讀完整歷史，所以放在 ``annotate_rollback_floor`` **之後**
    才過濾。回傳新的 list（不改呼叫端的物件）。
    """
    return [
        revision for revision in revisions or []
        if not (isinstance(revision, dict)
                and str(revision.get('status', '')).lower() == 'failed')
    ]

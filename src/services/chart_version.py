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


def reported_helm_revision(value):
    """#1414：operator 回報的 helm revision ⇒ 可寫進帳本的正整數；不能用回 ``None``。

    operator 舊版讀錯欄位（``info.revision``；helm 的版次在頂層 ``version``），install／
    upgrade 一律回報 0。0、缺值、bool、非整數都代表「不知道」——呼叫端不得拿它覆蓋帳本
    ``helm_revision``，改保留原值或另讀一次 release。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


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


def possibly_live_revisions(revisions):
    """#2069：叢集上**可能**正在跑的 revision 號（由小到大、不重複）。

    - 歷史裡有 ``deployed``（不分大小寫）⇒ 起點＝最大的 deployed revision，加上每一個比它新的
      revision（不論狀態）。平台的一般升級不帶 ``--atomic``／``--wait``（只有 reset 那條帶
      ``--atomic``）：升級做到一半失敗（``failed``）或還在進行（``pending-upgrade``／
      ``pending-rollback``…），helm 可能已經把前面的資源（Deployment 排在 StatefulSet／CronJob／
      Ingress 之前）換成那個 revision 的版本。比起點舊的不算（它們已被起點取代）。
    - 歷史裡**沒有** deployed ⇒ 叢集上跑的是哪一版釘不住，**每一個**有可用 revision 號的都算。
      純 ``helm rollback``（不帶 ``--wait``，woow-odoo 租戶走這條）在套用時失敗，helm 會把當時
      最新的那一列（``Releases.Last``，不論狀態）改標 ``superseded``、新的那一列標 ``failed``，叢集
      卻仍跑原本那一版。沙盒實測（helm 3.19.5）：``[1 superseded, 2 deployed]`` 失敗一次變成
      ``[1 s, 2 s, 3 failed]``，再失敗一次變成 ``[1 s, 2 s, 3 s, 4 failed]``，兩次之後叢集上都仍是
      rev2——「最新的 superseded」第二次就指到 rev3，所以不能用它當起點。

    沒有可用 revision 號的歷史列略過（與 ``rollback_baseline`` 同判準）；完全沒有可用 revision 號
    ⇒ ``[]``（呼叫端 fail-closed）。``rollback_baseline``（chart 版本下限與對話框「目前」）刻意不跟著
    改：它要的是單一一版。
    """
    rows = [(n, r) for r in revisions or [] if (n := _revision_number(r)) is not None]
    numbers = sorted({n for n, _r in rows})
    deployed = [n for n, r in rows if str(r.get('status', '')).lower() == 'deployed']
    if not deployed:
        return numbers
    start = max(deployed)
    return [n for n in numbers if n >= start]


def rollback_target_number(revisions, requested):
    """回復的目標 revision 號（``None``／0＝上一版，與 operator／helm 同語意）。"""
    numbers = [n for r in revisions or [] if (n := _revision_number(r)) is not None]
    if requested:
        return int(requested)
    return (max(numbers) - 1) if numbers else None


def is_valid_rollback_revision(requested):
    """回復目標必須是 ≥ 1 的整數（``bool`` 不算）。

    AUDITFIX rollback-api-input-validation：``None``／0 **不再**是「上一版」。helm 把 0
    解讀成上一版，誤送 0 會真的回復並重啟（INFRA-EMQX-013-1、AI-PI-004-4 把 pi 映像退回
    舊版）；對話框一律送明確的 revision 號，API 也照同一個契約。
    """
    return isinstance(requested, int) and not isinstance(requested, bool) and requested >= 1


def rollback_request_problem(revisions, requested):
    """回復請求本身不成立時回問題代碼；可以送出回 ``None``（DATA-ODOO-010-1／AUDITFIX）。

    - ``'invalid_revision'``：``requested`` 不是 ≥ 1 的整數（含 ``None``／0）。
    - ``'not_found'``：明確的 revision 不在歷史裡（AI-LLM-007-2：原本交給 operator，租戶看到
      helm 的英文原文「Revision 9999 not found in release history」）。
    - ``'already_current'``：目標就是**目前已部署的最新** revision。helm 照樣會「回復」到自己
      並產生新 revision、換掉 Pod（實測三個請求把一個沒變動的服務連續 helm 回復三次、Pod 重建
      三次），沒有任何效果卻有中斷。最新 revision 不是 deployed（升級失敗）時不算——回復到前一個
      deployed 版本是合法的修復動作。

    歷史讀不到（空）時只驗型別（其餘回 ``None``），交給 operator 回 400。
    """
    if not is_valid_rollback_revision(requested):
        return 'invalid_revision'
    numbers = [n for r in revisions or [] if (n := _revision_number(r)) is not None]
    if not numbers:
        return None
    if requested not in numbers:
        return 'not_found'
    latest = max(numbers)
    latest_row = next(r for r in revisions if _revision_number(r) == latest)
    if requested == latest and str(latest_row.get('status', '')).lower() == 'deployed':
        return 'already_current'
    return None


def annotate_rollback_floor(revisions):
    """替每個 revision 加上 ``chart_version``、``rollback_allowed`` 與 ``is_current``（對話框用）。

    回傳新的 list（不改呼叫端的物件）。部署中那一版本身標成可回復（它就是現況；前端
    另外以「目前版本」擋住）。
    """
    _number, current_version = rollback_baseline(revisions)
    # #1414／#1456：「目前」＝部署中的那一版（最新的 deployed），由 history 決定。帳本
    # `helm_revision` 在更新／回復後會落後，對話框原本拿它標「目前」⇒ 舊版被標成目前。
    # 沒有任何 deployed 時不標（前端退回帳本值）。
    deployed = [n for r in revisions or [] if isinstance(r, dict)
                and str(r.get('status', '')).lower() == 'deployed'
                and (n := _revision_number(r)) is not None]
    current_number = max(deployed) if deployed else None
    annotated = []
    for revision in revisions or []:
        item = dict(revision) if isinstance(revision, dict) else {'revision': revision}
        version = chart_version_from_history(item.get('chart'))
        item['chart_version'] = version
        item['rollback_allowed'] = chart_version_not_lower(version, current_version)
        item['is_current'] = (current_number is not None
                              and _revision_number(item) == current_number)
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

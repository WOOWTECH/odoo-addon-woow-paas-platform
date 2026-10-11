# -*- coding: utf-8 -*-
"""#1647：工作區頁的配額顯示——把 operator 的 quota 報表轉成前台用的形狀。

2026-10-10 正式站事故：一台服務的 CPU limit 被調到超過工作區 namespace 的 ``limits.cpu``
上限，新 pod 被 ResourceQuota 拒絕、停了近六小時；操作的人事前不知道上限是多少，前台
也沒有任何地方顯示。這裡提供工作區頁面板要的三格（CPU／記憶體／磁碟）：上限、已佔用、
剩餘、比例，以及「快用完／已用完」的等級。

本模組**不呼叫 operator、不寫 DB**：取報表在 controller（``PaasController.
_workspace_quota_view``，經 ``PaaSOperatorClient.get_namespace_quota``），這裡只有

* :func:`workspace_namespace`：工作區的 K8s namespace 名（與開通時同一條推導）；
* :func:`build_view`：報表 → 前台形狀，等級與百分比**只在這裡算**（前端只讀欄位，不自己
  用門檻再算一次；見 docs/decisions/patterns/capability-gate-single-source-field.md）；
* :func:`unavailable`：拿不到時的固定形狀。「沒有」「operator 太舊」「失敗」各有自己的
  ``reason``，不共用一個空值。
"""
import math

from .naming import make_namespace

#: 已佔用達上限的這個百分比＝``warning``（快用完）；達 100＝``full``（已用完）。
#: 模組常數，刻意不做成 config parameter：這是呈現用的提示門檻，不是政策。
QUOTA_WARN_PERCENT = 90

#: (前台 key, operator 報表的 k8s 資源鍵, 顯示單位, 換算除數)。順序即前台顯示順序。
#:
#: CPU／記憶體取 ``limits.*`` 而不是 ``requests.*``：
#:   * operator 寫 quota 時 requests 與 limits 的 hard 是同一個值（``_build_quota_manifest``），
#:     上限看哪一組都一樣；
#:   * #1639 起容器的 requests 小於 limits ⇒ 先撞到上限的一定是 limits 那一組，看
#:     requests 會顯示「還很空」而實際已經建不出 pod；
#:   * 2026-10-10 事故被擋的就是 ``limits.cpu``。
#: 磁碟只有 ``requests.storage``（PVC 沒有 limits）。bytes → GB 以 1024³ 換算，與方案規格
#: （``allocated_ram_gb``／``allocated_storage_gb`` 對 ``Gi``）同一把尺。
_VIEW_RESOURCES = (
    ('cpu', 'limits.cpu', 'vcpu', 1),
    ('memory', 'limits.memory', 'gb', 1024 ** 3),
    ('storage', 'requests.storage', 'gb', 1024 ** 3),
)

#: 等級由輕到重；整體等級取各格最嚴重者。
_LEVELS = ('ok', 'warning', 'full')

#: operator ``namespace_quota`` 的 reason → 這裡的 reason（extra/paas-operator
#: ``QUOTA_NOT_FOUND``／``QUOTA_UNREADABLE``；改名要兩邊一起改）。
_OPERATOR_REASONS = {
    'quota_not_found': 'quota_not_found',
    'quota_unreadable': 'unreadable',
}


def unavailable(reason):
    """拿不到配額資料時回給前台的固定形狀。

    ``reason`` 值域（前台據此決定整塊不畫，或顯示「暫時無法取得」）：

    * ``no_namespace``：工作區還沒有部署在它自己 namespace 裡的服務；
    * ``disabled``：平台沒有設定 operator；
    * ``operator_outdated``：operator 比這個端點舊（404／405）；
    * ``quota_not_found``：namespace 內沒有 quota，或報表裡沒有任何一格可顯示；
    * ``unreadable``：operator 讀不到叢集（kubectl 失敗／逾時）；
    * ``error``：連不上 operator、逾時、其他錯誤，或回應形狀不對。
    """
    return {'available': False, 'reason': reason, 'level': 'ok', 'resources': []}


def workspace_namespace(env, workspace):
    """工作區的 K8s namespace 名；推導不出來（``ws_hash`` 不合法）回 ``None``。

    與開通時同一條推導（``PaasController._create_service``）：``make_namespace(ws_hash,
    prefix)``，prefix 來自 ``woow_paas_platform.namespace_prefix``。
    """
    # sudo 只用來讀一個平台級設定值（namespace 前綴）：租戶沒有 ir.config_parameter 的
    # 讀取權，而這個值不隨租戶變、也不回傳給呼叫端以外的任何人。
    prefix = env['ir.config_parameter'].with_context({}).sudo().get_param(
        'woow_paas_platform.namespace_prefix', 'paas-ws-')
    try:
        return make_namespace(workspace.ws_hash, prefix=prefix or 'paas-ws-')
    except ValueError:
        return None


def _number(value):
    """int／float（排除 bool、NaN、無限大）才算數字；其餘回 ``None``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:  # 大到轉不成 float 的整數
        return None
    return value if finite else None


def _view_cell(key, unit, divisor, cell):
    """operator 報表的一格 → 前台的一格；資料不完整或不合理回 ``None``（略過該格）。"""
    if not isinstance(cell, dict):
        return None
    hard = _number(cell.get('hard_value'))
    used = _number(cell.get('used_value'))
    if hard is None or used is None or hard <= 0 or used < 0:
        return None
    # 等級與百分比一律由 hard／used 算，**不使用** operator 給的 ``ratio``：那是四捨五入過
    # 的顯示值（0.89996 會變 0.9），拿它比門檻會提早跳級。
    if used >= hard:
        level = 'full'
    elif used * 100 >= hard * QUOTA_WARN_PERCENT:
        level = 'warning'
    else:
        level = 'ok'
    try:
        return {
            'key': key,
            'unit': unit,
            'used': round(used / divisor, 2),
            'hard': round(hard / divisor, 2),
            'remaining': round(max(hard - used, 0) / divisor, 2),
            # 無條件捨去（89.99% 顯示 89，與 ok 等級一致）；超額時照實大於 100。
            # 1e-9 抵銷浮點誤差：0.29 * 100 / 1 是 28.999999999999996，不加會顯示成 28。
            'percent': int(used * 100 / hard + 1e-9),
            'level': level,
        }
    except ArithmeticError:
        # 相除溢位（used 與 hard 差了三百多個數量級）：不是合理的配額數字，略過該格。
        return None


def build_view(report):
    """operator 的 quota 報表 → 工作區頁面板的資料。永不 raise。

    Args:
        report: ``PaaSOperatorClient.get_namespace_quota`` 的回應（形狀見該方法）。

    Returns:
        拿得到::

            {'available': True, 'reason': '', 'level': 'ok'|'warning'|'full',
             'resources': [{'key': 'cpu'|'memory'|'storage', 'unit': 'vcpu'|'gb',
                            'used': float, 'hard': float, 'remaining': float,
                            'percent': int, 'level': 'ok'|'warning'|'full'}, ...]}

        ``resources`` 順序固定 cpu、memory、storage；報表裡缺鍵或數值不合理的那一格略過。
        拿不到（含三格都沒有）回 :func:`unavailable`。
    """
    if not isinstance(report, dict):
        return unavailable('error')
    if report.get('available') is not True:
        reason = report.get('reason')
        return unavailable(_OPERATOR_REASONS.get(reason, 'error')
                           if isinstance(reason, str) else 'error')
    resources = report.get('resources')
    if not isinstance(resources, dict):
        return unavailable('error')

    cells = []
    for key, resource, unit, divisor in _VIEW_RESOURCES:
        cell = _view_cell(key, unit, divisor, resources.get(resource))
        if cell is not None:
            cells.append(cell)
    if not cells:
        return unavailable('quota_not_found')

    worst = max(cells, key=lambda cell: _LEVELS.index(cell['level']))['level']
    return {'available': True, 'reason': '', 'level': worst, 'resources': cells}

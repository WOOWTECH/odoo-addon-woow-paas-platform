"""L4 reverse-orphan reconciler — Odoo 端左集蒐集 + 差集引擎（方向 A）。

operator 端（``extra/paas-operator/src/api/reconcile.py``）唯讀回 CF 帳號級 inventory（右集
ground truth）；本模組蒐集 Odoo 持有的 stored-id 左集（白名單），TS-3c 再對右集取差集
（CF 有、左集沒有 = 孤兒候選）——這是 record-driven 的 ``subscription_lifecycle_cron`` 永遠
做不到的反向。

**首版零刪除**：本檔與 cron / 報表同屬 reconcile 路徑，受 delete-gate（grep）守，禁任何
``delete_*`` / ``uninstall`` / ``unlink`` / ``scale_*`` / ``subprocess`` 等破壞動詞（見
``src/tests/test_reconcile_delete_gate.py``）。

**.sudo() 是 reconciler 的正當權限，非 ACL 反模式**：L4 是平台級反向對帳，必須看見**所有
租戶**的 stored-id 才能判定某 CF 資源是否仍被某租戶持有（漏看 = 把別租戶的活躍資源誤判孤兒）。
這與「controller 端 tenant 請求不可 .sudo() 繞 ir.rule」是不同情境（後者是反模式）；此處同
``subscription_lifecycle_cron`` 的 system-level 掃描。
"""
import logging
from dataclasses import dataclass, field

from .naming import reconstruct_mcp_subdomain
from .paas_operator import get_paas_operator_client

_logger = logging.getLogger(__name__)


@dataclass
class LeftSet:
    """Odoo 持有的 stored-id / subdomain 左集（白名單），依 kind 分組。

    id 類（與 operator inventory 的 ``CfResource.id`` 對齊，可直接差集）：
      ``tunnel_ids`` / ``access_app_ids`` / ``access_policy_ids``。
    DNS 類（待 operator inventory 補 DNS 後啟用差集；TS-3b 先蒐齊）：
      ``dns_record_ids``（id 對齊）+ ``subdomains``（bare label 第二鍵：subdomain_prefix /
      CS subdomain / 重建的 MCP subdomain / SM subdomain；覆蓋 in-flight 未回填 ``dns_record_id``
      的活躍 route。FQDN ↔ label 的對映在 DNS 差集落地時解，TS-3b 蒐 bare label 較 immutable）。
    """

    tunnel_ids: set = field(default_factory=set)
    access_app_ids: set = field(default_factory=set)
    access_policy_ids: set = field(default_factory=set)
    dns_record_ids: set = field(default_factory=set)
    subdomains: set = field(default_factory=set)


_EMPTY = (False, None, "")


def _clean(values):
    """丟掉 False / None / 空字串（空 stored-id 不入左集，design B.3：in-flight 半完成不誤當持有）。"""
    return {v for v in values if v not in _EMPTY}


def collect_left_set(env) -> LeftSet:
    """蒐集 SA / CS / SM 三表的 stored-id 左集（白名單）。

    - **SA（2 層巢狀 walk）**：``tunnel_id`` + ``route_ids``（``dns_record_id`` /
      ``access_app_id`` / ``subdomain_prefix``）+ **巢狀** ``route_ids.access_policy_ids.cf_policy_id``。
      走 ``route_id`` binding 鏈（dotted ``mapped`` 自動展開兩層）；**不**走 policy 的 store=True
      related ``security_access_id``（design B.3 警示：那只供 ir.rule/quota，非真 binding）。
    - **CS**：``subdomain`` + 重建的 MCP sidecar subdomain（``reconstruct_mcp_subdomain``，
      與 operator deploy/teardown 同源；缺 helm 欄位的未部署 CS 不亂湊）。
    - **SM**：``tunnel_id``（dedicated 反查鍵）+ ``subdomain``。
    """
    ls = LeftSet()

    # ── SA：2 層巢狀（最高衝擊正確性點）──
    sa = env['woow_paas_platform.security_access'].sudo().search([])
    ls.tunnel_ids |= _clean(sa.mapped('tunnel_id'))
    routes = sa.mapped('route_ids')
    ls.dns_record_ids |= _clean(routes.mapped('dns_record_id'))
    ls.access_app_ids |= _clean(routes.mapped('access_app_id'))
    ls.subdomains |= _clean(routes.mapped('subdomain_prefix'))
    # 第 2 層：route_ids → access_policy_ids → cf_policy_id（dotted mapped 走完整 binding 鏈）
    ls.access_policy_ids |= _clean(sa.mapped('route_ids.access_policy_ids.cf_policy_id'))

    # ── CS ──
    cs = env['woow_paas_platform.cloud_service'].sudo().search([])
    ls.subdomains |= _clean(cs.mapped('subdomain'))
    for service in cs:
        if service.helm_release_name and service.helm_namespace:
            ls.subdomains.add(
                reconstruct_mcp_subdomain(service.helm_namespace, service.helm_release_name)
            )

    # ── SM ──
    sm = env['woow_paas_platform.smart_home'].sudo().search([])
    ls.tunnel_ids |= _clean(sm.mapped('tunnel_id'))
    ls.subdomains |= _clean(sm.mapped('subdomain'))

    return ls


# ── 差集引擎（右集 − 左集 = 孤兒候選）─────────────────────────────────────


@dataclass
class OrphanCandidate:
    """單筆疑似孤兒（對齊 operator ReconcileResponse 的 OrphanCandidate shape）。

    首版（TS-3）只做**結構**差集：``reconcilable`` 恆 False、``env_hint`` 恆 'unknown'
    （env 歸屬 / R1-R5 分類是 TS-4）。
    """

    id: str
    name: str
    kind: str
    env_hint: str = 'unknown'
    reconcilable: bool = False
    reason: str = ''


@dataclass
class ReconcileResult:
    """Odoo reconciler 產出（對齊 ReconcileResponse golden：dry_run / partial / orphan_candidates）。"""

    dry_run: bool = True
    partial: bool = False
    orphan_candidates: list = field(default_factory=list)


# CF 資源 kind → LeftSet 上對應的 id 集合屬性（只列「右集 inventory 目前會回」的 kind）。
_KIND_TO_LEFTSET_ATTR = {
    'tunnel': 'tunnel_ids',
    'access_app': 'access_app_ids',
    'access_policy': 'access_policy_ids',
    # 'dns_record': 'dns_record_ids',  # 待 operator inventory 補 DNS 後啟用
}


def compute_orphans(resources, left_set, *, partial=False) -> ReconcileResult:
    """純差集：右集（resources）− 左集（left_set 對應 id 集）= 孤兒候選。

    方向恆為 **右 − 左**（CF 有、Odoo 沒有），故右集退化 / 空 → 少報（safe miss），
    **絕不**把左集全判孤兒（= 災難）。畸形（缺 id）或尚無左集對映的 kind（如未來 dns_record
    在對映補上前）→ 跳過 + 標 partial（不亂判、不靜默漏 = no-silent-cap）。
    首版只做**結構**差集：所有候選 reconcilable=False / env_hint='unknown'（分類是 TS-4）。
    """
    candidates = []
    degraded = bool(partial)
    for r in resources:
        rid = r.get('id')
        kind = r.get('kind')
        attr = _KIND_TO_LEFTSET_ATTR.get(kind)
        if not rid or attr is None:
            degraded = True
            _logger.warning(
                "reconcile diff: 跳過無法比對的 inventory 項（id=%r kind=%r）→ 標 partial",
                rid, kind,
            )
            continue
        if rid not in getattr(left_set, attr):
            candidates.append(OrphanCandidate(
                id=rid,
                name=r.get('name', '') or '',
                kind=kind,
                env_hint='unknown',
                reconcilable=False,
                reason='CF %s 不在 Odoo stored-id 左集；結構孤兒候選（未分類，人工審）' % kind,
            ))
    return ReconcileResult(dry_run=True, partial=degraded, orphan_candidates=candidates)


def _parse_inventory(inventory):
    """驗 operator inventory body shape，malformed → fail-loud raise；回 ``(resources, partial)``。

    HTTP status fail-loud（``_request``）只守『門』；body shape 是沒守的『窗』——operator 回
    200 但 body 是**缺鍵的 dict**（gateway/auth-proxy 的 ``{"detail": ...}`` envelope、欄位改名、
    半序列化）會被 ``.get(key, default)`` 靜默變成 ``partial=False`` + 空 ``resources`` = 假性
    『掃完、無孤兒』（正是本工具要防的「退化掃描被當完整」）。故缺鍵 / 型別不符一律 raise，
    不 default 成『安全/完整』值。另：``failed_kinds`` 任一非空 → 視為 partial（即使 operator
    的 ``partial`` 旗標防禦性地為 False，也不假設其內部一致）。
    """
    if not isinstance(inventory, dict):
        raise RuntimeError("operator inventory 非 dict（malformed body）→ fail-loud，不當『無孤兒』。")
    if 'resources' not in inventory or 'partial' not in inventory:
        raise RuntimeError(
            "operator inventory 缺 resources / partial 鍵（malformed body）→ fail-loud；"
            "缺鍵與『掃完無孤兒』無法區分，不 default 成空 / 完整。"
        )
    resources = inventory['resources']
    if not isinstance(resources, list):
        raise RuntimeError("operator inventory.resources 非 list（malformed body）→ fail-loud。")
    partial = bool(inventory['partial']) or bool(inventory.get('failed_kinds'))
    return resources, partial


def reconcile_orphans(env, client=None) -> ReconcileResult:
    """端到端：取右集 inventory（operator client）+ 蒐左集 + 差集 → dry-run 孤兒候選。

    - client 未設定（operator_url / api_key 缺）→ raise，**不**降級回空候選（空候選會被當
      『無孤兒』，與『未設定 / 沒掃到』無法區分，B.0 fail-loud）。
    - ``client.get_reconcile_inventory()`` 對 404 / 503 / 連線 fail-loud raise；body malformed
      由 ``_parse_inventory`` fail-loud raise（不靜默變空 / 完整）。
    - **先取右集（inventory）再蒐左集**：provision 是「先建 CF → 再寫 Odoo stored-id」，左集讀得
      越晚、Odoo 寫回越有時間落地，越能縮短「CF 已建、id 未回填」的 in-flight 資源被誤判窗。
    """
    if client is None:
        client = get_paas_operator_client(env)
    if client is None:
        raise RuntimeError(
            "PaaS Operator 未設定（operator_url / operator_api_key）→ 無法反向對帳；"
            "fail-loud，不降級回空候選（空候選會被當『無孤兒』）。"
        )
    inventory = client.get_reconcile_inventory()        # 右集 T1（先讀）
    resources, partial = _parse_inventory(inventory)    # body shape fail-loud
    left_set = collect_left_set(env)                    # 左集 T2（後讀，縮 in-flight 誤判窗）
    return compute_orphans(resources, left_set, partial=partial)

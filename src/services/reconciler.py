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
from dataclasses import dataclass, field

from .naming import reconstruct_mcp_subdomain


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

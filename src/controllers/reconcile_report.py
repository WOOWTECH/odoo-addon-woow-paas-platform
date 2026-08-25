"""TS-5 Phase D Slice C：L4 反向孤兒 dry-run 報表 endpoints（admin-only）。

``/woow`` 後台報表頁（Slice D）的後端：admin 讀分類後 snapshot + 單筆 TOCTOU 重查。

**admin 雙層把關**：前端 nav 隱藏只是 cosmetic、本層 ``has_group('base.group_system')``
才是真把關（非 admin 直接打 endpoint 也擋）。

**唯讀、零刪除**：本檔在 delete-gate 監看清單內（``RECONCILE_PATH_FILES``），禁任何
``delete_*`` / ``uninstall`` / ``unlink`` / ``subprocess`` 等破壞動詞——刪除權永遠留人手
（未來若加「刪除此孤兒」endpoint 會被 gate 擋下，逼維持發現↔刪除分離）。
"""
import logging

from odoo.http import Controller, request, route

from ..services.reconciler import reconcile_orphans
from odoo import _

_logger = logging.getLogger(__name__)


class ReconcileReportController(Controller):

    def _is_admin(self):
        """admin-only 真把關（``base.group_system``）。前端隱藏 nav 只是 cosmetic、此處才是邊界。"""
        return request.env.user.has_group('base.group_system')

    @route("/api/reconcile-report/list", auth="user", methods=["POST"], type="json")
    def api_reconcile_report_list(self, **kw):
        """回最新 snapshot 的**全部**候選（含 reconcilable=False）+ scanned_at + partial。

        **no-silent-drop（§C.4 #7）**：不得以 reconcilable 過濾——cross-env / protected /
        unknown（全 reconcilable=False）須全列呈現附 reason + env_hint，靠 reconcilable=False
        達成「skip 刪除」而非「從清單消失」（上游保留、下游濾掉＝假性 no-silent-drop）。
        """
        if not self._is_admin():
            return {'success': False, 'error': _('Admin access required')}
        rows = request.env['woow_paas_platform.reconcile_report'].search([])
        candidates = [{
            'id': r.id,
            'resource_uid': r.resource_uid or '',
            'name': r.name or '',
            'kind': r.kind or '',
            'env_hint': r.env_hint or '',
            'reconcilable': r.reconcilable,
            'reason': r.reason or '',
            'scanned_at': r.scanned_at.isoformat() if r.scanned_at else None,
        } for r in rows]
        scanned_at = next((r.scanned_at.isoformat() for r in rows if r.scanned_at), None)
        partial = any(r.partial for r in rows)
        return {'success': True, 'data': {
            'scanned_at': scanned_at,
            'partial': partial,
            'candidates': candidates,
        }}

    @route("/api/reconcile-report/reverify", auth="user", methods=["POST"], type="json")
    def api_reconcile_report_reverify(self, kind=None, resource_uid=None, **kw):
        """單筆唯讀 TOCTOU 重查：重跑 ``reconcile_orphans`` 看 (kind, resource_uid) 是否仍為孤兒。

        **唯讀**：不刪、不改 stored row。人工刪除前的即時重查——報表當時是孤兒、現在可能已被
        新 provision 重用同資源（hash 碰撞 / retry 重建）→ 刪了會誤殺活躍資源。全量重算（單資源
        無法不重抓 inventory + 左集即判其孤兒狀態），是手動低頻按鈕、可接受。
        """
        if not self._is_admin():
            return {'success': False, 'error': _('Admin access required')}
        if not kind or not resource_uid:
            return {'success': False, 'error': _('kind 與 resource_uid 必填')}
        try:
            result = reconcile_orphans(request.env)
        except Exception as exc:
            return {'success': False, 'error': _('重新對帳失敗：%s') % exc}
        match = next((c for c in result.orphan_candidates
                      if c.kind == kind and c.id == resource_uid), None)
        return {'success': True, 'data': {
            'still_orphan': match is not None,
            'reconcilable': match.reconcilable if match else False,
            'env_hint': match.env_hint if match else '',
            'reason': (match.reason if match
                       else '已不在最新對帳的孤兒清單（可能已被新 provision 重用同資源、'
                            '或已被人工清除）——刪除前請再確認'),
        }}

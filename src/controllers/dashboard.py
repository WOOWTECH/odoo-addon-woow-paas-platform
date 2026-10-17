"""G4-P2: Dashboard summary endpoint.

Aggregates user / billing / workspaces / members / recent_activity into
a single JSON-RPC response so the OWL dashboard page can hydrate from
ONE round-trip instead of fanning out to four endpoints.

Single source of truth for monetary values: every credit number on the
dashboard derives from `partner.wallet_balance` — the same field the
header CREDITS reads through `/api/billing/wallet`. This eliminates the
class of bug behind Issue #9 (header showed 576.15 while card showed
1255.00 because they pulled from independent mock-vs-real sources).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from odoo.http import request, route, Controller

from ..models.subscription_lifecycle_mixin import _subscription_total_with_tax
from .ha_paas import HiddenChainMixin
from odoo import _

_logger = logging.getLogger(__name__)

_ONE_DAY = timedelta(days=1)


# Number of recent wallet.transaction rows surfaced in the activity feed.
# Picked at the middle of the plan's "5-10" range; small enough that the
# card doesn't overflow on 1280px viewports, large enough that a busy day's
# top-up + several deductions all fit.
_RECENT_ACTIVITY_LIMIT = 8

# Map upstream wallet.transaction.transaction_type values to the dashboard's
# action vocabulary. Unknown types pass through as the literal type string
# so a future addition (e.g. 'rebate') doesn't break the contract.
_ACTION_MAP = {
    'topup': 'topup',
    'deduct': 'subscription_charge',
    'refund': 'refund',
    'adjustment': 'adjustment',
    'card_charge': 'card_charge',
    'card_bind': 'card_bind',
    'card_unbind': 'card_unbind',
}


class DashboardController(HiddenChainMixin, Controller):
    _name = 'woow_paas_platform.dashboard'

    @route("/api/dashboard/summary", auth="user", methods=["POST"], type="json")
    def api_dashboard_summary(self, **kw: Any) -> dict[str, Any]:
        """Return the full dashboard payload for the current user.

        See `docs/plans/2026-04-28-sprint3-g4p2-dashboard-summary.md` for the
        documented response shape and per-field source mapping.
        """
        try:
            user = request.env.user
            partner = user.partner_id

            data = {
                'user': self._user_block(user, partner),
                'billing': self._billing_block(partner),
                'workspaces': self._workspaces_block(user),
                'members': self._members_block(user),
                'recent_activity': self._recent_activity(partner),
            }
            return {'success': True, 'data': data}
        except Exception:  # pragma: no cover — defensive top-level guard
            # Log full traceback server-side; return a stable user-facing string
            # so internal field/table names don't leak to the client.
            _logger.exception("Dashboard summary failed for user=%s", request.env.user.id)
            return {
                'success': False,
                'error': _("Failed to load dashboard. Please try again."),
            }

    # ------------------------------------------------------------------
    # Block builders
    # ------------------------------------------------------------------

    def _user_block(self, user, partner) -> dict[str, Any]:
        """User identity block. avatar_url uses the same path /api/me does
        so the header avatar and dashboard greeting can't drift apart."""
        return {
            'id': user.id,
            'name': partner.name or '',
            'avatar_url': f'/web/image/res.partner/{partner.id}/image_128',
        }

    def _billing_block(self, partner) -> dict[str, Any]:
        """Billing block.

        `credits_left` is the load-bearing field: it MUST share its source
        with the header CREDITS indicator. Both surface `partner.wallet_balance`
        directly so the original Issue #9 root cause (independent sources)
        cannot recur.

        AUDITFIX dashboard-next-bill-wrong（FU2-U-plat-ux-3／-5）：原本拿「最近開始」的那一筆
        訂閱當 primary——``next_bill_date`` 與帳期來自它，``next_bill_estimate`` 卻是全部訂閱合計
        （實測：日期 10-21 對上 40,400；實際 10-18 就先扣 30,650）；試用中的訂閱起日（試用結束
        次日）還會晚於迄日（next_payment − 1）。現在：

        * ``next_bill_date``＝所有有效訂閱裡**最早**的 ``sh_date_of_next_payment``；
        * ``next_bill_estimate``＝**那一天到期**的訂閱含稅合計（就是那天會扣的金額）；
        * ``recurring_total_estimate``＝全部有效訂閱每期含稅合計（原本 ``next_bill_estimate``
          的意思，另給一個名字）；
        * ``current_cycle_*``＝最早到期那一筆的**目前這一期**：試用中＝試用期（迄日＝試用最後
          一天），付費中＝[next_payment − 一期, next_payment − 1]；保證起日不晚於迄日。

        沒有有效訂閱時：帳期退回本月，``next_bill_*`` 為 ``null``（前端顯示「—」）。
        """
        # #336：先濾掉 hidden（0 元 HA PaaS）sub 再取 primary，避免 0 元 sub
        # 搶 primary 綁架 cycle/next_bill 估算。
        active_subs = request.env['sh.subscription.subscription'].sudo().search(
            [('sh_partner_id', '=', partner.id), ('state', '=', 'in_progress'),
             ('id', 'not in', self._hidden_subscription_ids(request.env))],
            order='sh_start_date desc, id desc',
        )

        today = date.today()
        scheduled = active_subs.filtered('sh_date_of_next_payment')
        next_bill_date = (
            min(scheduled.mapped('sh_date_of_next_payment')) if scheduled else None)
        due_first = scheduled.filtered(
            lambda sub: sub.sh_date_of_next_payment == next_bill_date)
        primary = due_first[:1] or active_subs[:1]
        cycle_start, cycle_end = _current_cycle(primary, today)

        # Cycle usage = sum of ABSOLUTE deduct amounts since cycle_start.
        # `wallet.transaction.amount` is negative for deducts; we negate the
        # sum so the dashboard shows usage as a positive number.
        Transaction = request.env['wallet.transaction'].sudo()
        deducts = Transaction.search_read(
            [
                ('partner_id', '=', partner.id),
                ('transaction_type', '=', 'deduct'),
                ('create_date', '>=', cycle_start),
            ],
            ['amount'],
        )
        cycle_usage = round(-sum(d['amount'] for d in deducts), 2)
        # Defensive: `amount` may already be positive on a malformed row;
        # negative usage is meaningless on a dashboard, so floor at 0.
        if cycle_usage < 0:
            cycle_usage = 0.0

        def _total(subs):
            return round(sum(_subscription_total_with_tax(sub)[3] for sub in subs), 2)

        return {
            'credits_left': round(partner.wallet_balance or 0.0, 2),
            'current_cycle_start': cycle_start.isoformat() if cycle_start else None,
            'current_cycle_end': cycle_end.isoformat() if cycle_end else None,
            'current_cycle_usage': cycle_usage,
            'next_bill_date': next_bill_date.isoformat() if next_bill_date else None,
            'next_bill_estimate': _total(due_first) if next_bill_date else None,
            'recurring_total_estimate': _total(active_subs) if active_subs else None,
            'currency': 'pts',
        }

    def _dashboard_workspace_ids(self, user) -> list[int]:
        """儀表板要算的工作區：使用者有權限、沒有封存、不是 HA PaaS 專屬隱藏工作區。

        AUDITFIX（FU2-U-plat-ux-4）：原本直接拿 ``workspace_access`` 的列，已封存的工作區也
        算進去（uid 205：列表 16 個、儀表板 107）。工作區與成員兩個區塊共用這一份。
        """
        workspace_ids = self._accessible_workspace_ids(user)
        # #336：剔除 HA PaaS 專屬 hidden workspace（D2 不變式：hidden service 住
        # 自建專屬 workspace），讓 workspace 計數不含隱藏 workspace。
        hidden_ws = set(self._hidden_workspace_ids(request.env))
        return [wid for wid in workspace_ids if wid not in hidden_ws]

    def _accessible_workspace_ids(self, user) -> list[int]:
        """Return distinct workspace IDs the given user can access.

        Source: `workspace_access.user_id`. We filter by user (not partner)
        because workspace_access links to res.users.
        """
        # AUDITFIX（FU2-U-plat-ux-4）：已封存的工作區不算（/woow 的工作區列表也不列）。
        access_rows = request.env['woow_paas_platform.workspace_access'].sudo().search_read(
            [('user_id', '=', user.id), ('workspace_id.state', '!=', 'archived')],
            ['workspace_id'],
        )
        # workspace_id comes back as [id, "Display Name"] tuples.
        return list({row['workspace_id'][0] for row in access_rows if row['workspace_id']})

    def _workspaces_block(self, user) -> dict[str, Any]:
        """Workspaces + nested services counts.

        `limit: null` is hardcoded: there's no plan-tier model yet (per plan's
        "out of scope"). The frontend renders raw counts ("1 active") instead
        of ratios ("1/3").

        `services.running` filters by `cloud_service.state == 'running'` —
        verified against the model file, where the `state` field has 'running'
        as one of the selection values.
        """
        workspace_ids = self._dashboard_workspace_ids(user)
        if not workspace_ids:
            return {
                'total': 0,
                'limit': None,
                'services': {'running': 0, 'total': 0},
            }

        CloudService = request.env['woow_paas_platform.cloud_service'].sudo()
        # 服務計數再加 template 過濾（defense-in-depth：即使某 hidden service 意外
        # 不住 hidden workspace 也不計入）。
        hidden_tpl = self._hidden_template_ids(request.env)
        services_total = CloudService.search_count(
            [('workspace_id', 'in', workspace_ids),
             ('template_id', 'not in', hidden_tpl)],
        )
        services_running = CloudService.search_count(
            [('workspace_id', 'in', workspace_ids), ('state', '=', 'running'),
             ('template_id', 'not in', hidden_tpl)],
        )
        return {
            'total': len(workspace_ids),
            'limit': None,
            'services': {'running': services_running, 'total': services_total},
        }

    def _members_block(self, user) -> dict[str, Any]:
        """Members across the user's accessible workspaces.

        `pending` is hardcoded 0 because workspace_access has no pending
        state — users either have access or don't (plan documents this as
        a placeholder for future invite flow).
        """
        workspace_ids = self._dashboard_workspace_ids(user)
        if not workspace_ids:
            return {'total': 0, 'active': 0, 'pending': 0}
        # AUDITFIX（FU2-U-plat-ux-4）：不重複的人數——同一個人在 N 個工作區原本算 N 次。
        rows = request.env['woow_paas_platform.workspace_access'].sudo().search_read(
            [('workspace_id', 'in', workspace_ids)], ['user_id'],
        )
        total = len({row['user_id'][0] for row in rows if row['user_id']})
        return {'total': total, 'active': total, 'pending': 0}

    def _recent_activity(self, partner) -> list[dict[str, Any]]:
        """Last N wallet transactions for the partner, DESC by create_date.

        wallet.transaction's upstream _order is already 'create_date desc, id
        desc' so the search returns rows in the right order without an
        explicit `order=` clause; we pass it anyway for documentation and as
        a guard against future _order changes.
        """
        Transaction = request.env['wallet.transaction'].sudo()
        rows = Transaction.search(
            [('partner_id', '=', partner.id)],
            order='create_date desc, id desc',
            limit=_RECENT_ACTIVITY_LIMIT,
        )

        result = []
        actor_name = partner.name or ''
        for row in rows:
            target = self._derive_target(row)
            result.append({
                'ts': row.create_date.isoformat() if row.create_date else None,
                'actor': actor_name,
                'action': _ACTION_MAP.get(row.transaction_type, row.transaction_type or ''),
                'target': target,
                'description': row.description or row.name or '',
            })
        return result

    def _derive_target(self, txn) -> str:
        """Best-effort 'target' string for a wallet.transaction row.

        - top-up: linked topup record's plan name → falls back to "Wallet"
        - deduct: linked subscription's plan name → falls back to "Subscription"
        - everything else: the type's human label
        """
        if txn.transaction_type == 'topup':
            topup = request.env['woow.wallet.topup'].sudo().search(
                [('wallet_transaction_id', '=', txn.id)], limit=1,
            )
            if topup and topup.plan_id:
                return topup.plan_id.name or 'Wallet'
            return 'Wallet'
        if txn.transaction_type == 'deduct':
            sub = txn.subscription_id
            if sub and sub.sh_subscription_plan_id:
                return sub.sh_subscription_plan_id.name or 'Subscription'
            return 'Subscription'
        return (txn.transaction_type or '').replace('_', ' ').title() or 'Wallet'


# ----------------------------------------------------------------------
# Date helpers (module-private)
# ----------------------------------------------------------------------


def _current_cycle(sub, today: date) -> tuple[date, date]:
    """``sub`` 目前這一期的 (起, 迄)；沒有訂閱時是本月。保證起日不晚於迄日。

    * 試用中：迄＝試用最後一天（``sh_trial_end_date``，缺值時 next_payment − 1），起＝開通日。
    * 有下次扣款日：迄＝next_payment − 1，起＝next_payment − 一期（``_cycle_delta``）；
      不早於 ``sh_start_date``。
    * 其他：起＝``sh_start_date``，迄＝當月最後一天（舊行為）。
    """
    month_start = today.replace(day=1)
    if not sub:
        return month_start, _last_of_month(month_start)
    next_payment = sub.sh_date_of_next_payment
    if sub._woow_in_trial():
        end = sub.sh_trial_end_date or (next_payment - _ONE_DAY if next_payment else today)
        opened = sub.create_date.date() if sub.create_date else today
        start = min(opened, end)
    elif next_payment:
        end = next_payment - _ONE_DAY
        delta = sub._cycle_delta()
        start = next_payment - delta if delta else (sub.sh_start_date or month_start)
        if sub.sh_start_date and sub.sh_start_date <= end:
            start = max(start, sub.sh_start_date)
    else:
        start = sub.sh_start_date or month_start
        end = _last_of_month(start)
    if start > end:
        start = end
    return start, end


def _last_of_month(d: date) -> date:
    """Return the last day of the calendar month containing `d`."""
    if d.month == 12:
        next_month_first = date(d.year + 1, 1, 1)
    else:
        next_month_first = date(d.year, d.month + 1, 1)
    return next_month_first - _ONE_DAY

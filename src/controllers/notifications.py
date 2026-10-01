"""#1310：/woow 通知中心端點。

Header 鈴鐺過去沒有任何資料來源（按鈕無 handler、紅點寫死）。本端點把
「租戶需要知道／需要處理」的既有狀態彙整成一份通知清單，**不新增 model**：

- ``trial_ending``：試用將在 3 天內結束、即將開始扣點的訂閱
- ``renewal_low_balance``：3 天內續約、但錢包點數不足（含稅）的訂閱
- ``service_error``：使用者可存取工作區內處於 error 的雲端服務／智慧家庭／遠端安全連結
- ``invoice_unpaid``：已過帳、尚未付清的發票
- ``wallet_activity``：近 7 天錢包異動（僅資訊，不算待處理）

每筆帶穩定 ``id``（同一事件重抓時不變），前端據此記錄「已讀」；
``severity`` 為 error／warning 的才算待處理、會亮紅點。

授權：一律以 ``request.env.user`` 的 partner／workspace_access 過濾後再 sudo 讀，
與 ``dashboard.py`` 同一套判準；hidden（HA PaaS）鏈沿用 ``HiddenChainMixin`` 排除。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from odoo import _, fields
from odoo.http import request, route, Controller
from odoo.tools.misc import format_date

from ..models.subscription_lifecycle_mixin import _subscription_total_with_tax
from .ha_paas import HiddenChainMixin

_logger = logging.getLogger(__name__)

# 試用結束／續約提醒的前置天數；與 /api/billing/upcoming-renewals 的 3 天一致。
_LOOKAHEAD_DAYS = 3
# 錢包異動只回近 N 天、最多 M 筆，避免面板被歷史資料淹沒。
_ACTIVITY_DAYS = 7
_ACTIVITY_LIMIT = 5
# 服務錯誤／未付發票各自的上限（防禦性；正常租戶遠低於此）。
_ITEM_LIMIT = 20

_SEVERITY_ORDER = {'error': 0, 'warning': 1, 'info': 2}


class NotificationsController(HiddenChainMixin, Controller):
    _name = 'woow_paas_platform.notifications'

    @route("/api/notifications", auth="user", methods=["POST"], type="json")
    def api_notifications(self, **kw: Any) -> dict[str, Any]:
        """Return the notification list for the current user.

        Response ``data``:
            - items (list): id, type, severity, title, message, ts, route
            - actionable_count (int): items whose severity is error/warning
        """
        try:
            env = request.env
            user = env.user
            partner = user.partner_id
            items = []
            items += self._subscription_items(env, partner)
            items += self._service_error_items(env, user)
            items += self._unpaid_invoice_items(env, partner)
            items += self._wallet_activity_items(env, partner)
            items.sort(key=lambda i: (_SEVERITY_ORDER.get(i['severity'], 9), -(_ts_key(i['ts']))))
            actionable = sum(1 for i in items if i['severity'] in ('error', 'warning'))
            return {'success': True, 'data': {'items': items, 'actionable_count': actionable}}
        except Exception:  # pragma: no cover — defensive top-level guard
            _logger.exception("Notifications failed for user=%s", request.env.user.id)
            return {'success': False, 'error': _("Failed to load notifications. Please try again.")}

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _subscription_items(self, env, partner) -> list[dict[str, Any]]:
        today = fields.Date.today()
        cutoff = today + timedelta(days=_LOOKAHEAD_DAYS)
        subs = env['sh.subscription.subscription'].sudo().search([
            ('sh_partner_id', '=', partner.id),
            ('state', '=', 'in_progress'),
            ('id', 'not in', self._hidden_subscription_ids(env)),
        ])
        balance = partner.wallet_balance or 0.0
        items = []
        for sub in subs:
            plan_name = sub.sh_subscription_plan_id.name or sub.name or ''
            total = _subscription_total_with_tax(sub)[3]
            if sub.sh_trial and sub.sh_trial_end_date and today <= sub.sh_trial_end_date <= cutoff:
                items.append({
                    'id': 'trial_ending:%s:%s' % (sub.id, sub.sh_trial_end_date.isoformat()),
                    'type': 'trial_ending',
                    'severity': 'warning',
                    'title': _("Trial ending soon"),
                    'message': _(
                        "%(plan)s trial ends on %(date)s. %(amount)s points will be charged after that.",
                        plan=plan_name, date=format_date(env, sub.sh_trial_end_date),
                        amount=_format_points(total)),
                    'ts': sub.sh_trial_end_date.isoformat(),
                    'route': 'billing/subscriptions',
                })
            next_payment = sub.sh_date_of_next_payment
            if next_payment and today <= next_payment <= cutoff and balance < total:
                items.append({
                    'id': 'renewal_low_balance:%s:%s' % (sub.id, next_payment.isoformat()),
                    'type': 'renewal_low_balance',
                    'severity': 'error',
                    'title': _("Not enough points for renewal"),
                    'message': _(
                        "%(plan)s renews on %(date)s and needs %(amount)s points; your balance is %(balance)s points.",
                        plan=plan_name, date=format_date(env, next_payment),
                        amount=_format_points(total), balance=_format_points(balance)),
                    'ts': next_payment.isoformat(),
                    'route': 'billing/topup',
                })
        return items

    def _accessible_workspace_ids(self, env, user) -> list[int]:
        rows = env['woow_paas_platform.workspace_access'].sudo().search([('user_id', '=', user.id)])
        hidden = set(self._hidden_workspace_ids(env))
        return [w.id for w in rows.mapped('workspace_id') if w.state == 'active' and w.id not in hidden]

    def _service_error_items(self, env, user) -> list[dict[str, Any]]:
        workspace_ids = self._accessible_workspace_ids(env, user)
        if not workspace_ids:
            return []
        sources = (
            ('woow_paas_platform.cloud_service', 'services', [('template_id', 'not in', self._hidden_template_ids(env))]),
            ('woow_paas_platform.smart_home', 'smarthome', []),
            ('woow_paas_platform.security_access', 'security-access', []),
        )
        items = []
        for model, segment, extra in sources:
            records = env[model].sudo().search(
                [('workspace_id', 'in', workspace_ids), ('state', '=', 'error')] + extra,
                order='write_date desc', limit=_ITEM_LIMIT)
            for rec in records:
                items.append({
                    'id': 'service_error:%s:%s:%s' % (model, rec.id, fields.Datetime.to_string(rec.write_date)),
                    'type': 'service_error',
                    'severity': 'error',
                    'title': _("Service needs attention"),
                    'message': _("%(name)s in workspace %(workspace)s is in an error state.",
                                 name=rec.name or '', workspace=rec.workspace_id.name or ''),
                    'ts': _iso_utc(rec.write_date),
                    'route': 'workspace/%s/%s/%s' % (rec.workspace_id.id, segment, rec.id),
                })
        return items

    def _unpaid_invoice_items(self, env, partner) -> list[dict[str, Any]]:
        moves = env['account.move'].sudo().search([
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ('not_paid', 'partial')),
        ], order='invoice_date desc, id desc', limit=_ITEM_LIMIT)
        return [{
            'id': 'invoice_unpaid:%s' % move.id,
            'type': 'invoice_unpaid',
            'severity': 'warning',
            'title': _("Unpaid invoice"),
            'message': _("Invoice %(name)s has %(amount)s outstanding.",
                         name=move.name or '', amount=_format_points(move.amount_residual)),
            'ts': move.invoice_date.isoformat() if move.invoice_date else None,
            'route': 'billing/invoices/%s' % move.id,
        } for move in moves]

    def _wallet_activity_items(self, env, partner) -> list[dict[str, Any]]:
        since = fields.Datetime.now() - timedelta(days=_ACTIVITY_DAYS)
        rows = env['wallet.transaction'].sudo().search(
            [('partner_id', '=', partner.id), ('create_date', '>=', since)],
            order='create_date desc, id desc', limit=_ACTIVITY_LIMIT)
        items = []
        for row in rows:
            amount = row.amount or 0.0
            if amount >= 0:
                title = _("Points added")
                message = _("%(amount)s points were added to your wallet.", amount=_format_points(amount))
            else:
                title = _("Points used")
                message = _("%(amount)s points were deducted from your wallet.", amount=_format_points(-amount))
            items.append({
                'id': 'wallet_activity:%s' % row.id,
                'type': 'wallet_activity',
                'severity': 'info',
                'title': title,
                'message': message,
                'ts': _iso_utc(row.create_date),
                'route': 'billing/transactions',
            })
        return items


def _iso_utc(value) -> str | None:
    """Odoo datetimes are naive UTC; send an explicit offset so the browser
    renders them in the viewer's local time instead of guessing."""
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _format_points(value) -> str:
    return '{:,.2f}'.format(value or 0.0)


def _ts_key(ts) -> float:
    """Sort key for ISO date/datetime strings (newest first); missing → oldest."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return 0.0

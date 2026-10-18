"""#1611：訂閱被取消（欠費）或到期之後，租戶在 /woow 自助續訂 Cloud Service。

續訂＝沿用上游 ``sh_subscription`` 的**續約鏈**，不另建一條平行的訂閱關係：

1. ``old._sh_renew_subscription(enforce_portal_rules=True)``：舊訂閱轉 ``renewed``、
   鑄出 draft 子訂閱（``sh_subscription_id``＝舊訂閱）——上游自助續約的同一個入口，
   ``sh_renewed`` 也替我們擋重複續約。
2. 子訂閱套上所選方案（同 ``_create_and_confirm_subscription`` 的 onchange 順序），再用
   ``_force_active_dates_from_plan`` 從**今天**起算新週期：不補繳欠費期間、不給試用。
3. ``sh_subscription_confirm()``：轉 in_progress、開票、錢包付款。#1599 的兩個觸發點
   （訂閱轉 in_progress、``_pay_from_wallet``）在這裡自動替停權中的服務寫
   ``resume_requested``＋``_trigger()``；commit 之後由付款後復權的 cron scale 1＋unpark。
4. 確認真的付清（``_paid_resume_block_reason``，與 #1599 同一個判準）才改綁：
   ``_follow_renewal_chain``（每日 cron 分支 1 抽出的 helper）。

能不能續訂的判準集中在 ``_renewal_status``；保留期（cancel 之後幾天會被清除）與每日 cron
分支 4 共用 ``subscription_lifecycle_cron._cancel_cleanup_date``。

只做 Cloud Service；Security Access／Smart Home 的停權語意不同（專屬 tunnel＋客戶端
connector），另議。
"""
import logging
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.addons.sh_subscription.models.sh_subscription_subscription import (
    SH_PORTAL_RENEWABLE_STATES,
)

_logger = logging.getLogger(__name__)

#: 可以自助續訂的訂閱狀態＝上游自助續約允許的來源狀態（cancel／close／expire）。
RENEWABLE_SUBSCRIPTION_STATES = SH_PORTAL_RENEWABLE_STATES

#: 從沒部署起來的服務沒有東西可以「續」——刪掉重開才是對的路。
_NOT_DEPLOYED_STATES = ('pending', 'deploying')

_CYCLE_DELTAS = {
    'day': lambda n: relativedelta(days=n),
    'week': lambda n: relativedelta(weeks=n),
    'month': lambda n: relativedelta(months=n),
    'year': lambda n: relativedelta(years=n),
}


class RenewalRefused(UserError):
    """自助續訂被拒（第一個寫入之前，或付款沒成功而整段回捲）。

    ``code`` 是給前端的穩定代碼（``_renewal_status`` 的 reason，或 ``busy``／
    ``insufficient_balance``／``plan_not_available``／``payment_failed``）；``data`` 是
    附帶的數字（例：錢包不足時的差額）。訊息本身已是寫給租戶看的句子。
    """

    def __init__(self, code, message, data=None):
        self.code = code
        self.data = data or {}
        super().__init__(message)


class CloudServiceRenewal(models.Model):
    _inherit = 'woow_paas_platform.cloud_service'

    # ------------------------------------------------------------------ #
    # 能不能續訂
    # ------------------------------------------------------------------ #

    def _renewal_status(self, today=None):
        """這台服務現在能不能自助續訂；前端的續訂入口與兩支端點都讀這一份。

        Returns:
            dict：
            * ``available``：可以續訂。
            * ``reason``：不行的原因代碼（見 ``_renewal_refusal_message``），可以時為 None。
            * ``subscription_state``：目前綁的訂閱狀態。
            * ``retention_until``：取消（cancel）的訂閱——最後一天還能續訂的日期（隔天起每日
              cron 會清除這台服務）。到期／結束的訂閱沒有清除計時，回 None。
            * ``period_end``：``period_not_ended`` 時，目前週期的最後一天。
            * ``resuming``：訂閱已恢復、付清了，服務仍在停權中＝恢復中（#1599 的 cron 會處理）。
        """
        self.ensure_one()
        service = self.sudo()
        lifecycle = self.env['woow_paas_platform.subscription_lifecycle_cron'].sudo()
        today = today or fields.Date.today()
        sub = service.subscription_id
        status = {
            'available': False,
            'reason': None,
            'subscription_state': sub.state or None,
            'retention_until': None,
            'period_end': None,
            'resuming': False,
        }
        if not sub:
            status['reason'] = 'no_subscription'
            return status
        if sub.state in ('in_progress', 'renewed'):
            # renewed：後台已續約，等 cron 沿續約鏈改綁；續約還沒確認（子訂閱仍 draft）時
            # 不能再自助續一次（上游 sh_renewed 也會擋）。
            target = lifecycle._resume_target_subscription(service)
            if target.state != 'in_progress':
                status['reason'] = 'already_renewed'
                return status
            status['reason'] = 'subscription_active'
            status['resuming'] = bool(
                service._is_halted()
                and lifecycle._paid_resume_block_reason(target) is None)
            return status
        if sub.state not in RENEWABLE_SUBSCRIPTION_STATES:
            status['reason'] = 'subscription_not_renewable'
            return status
        if sub.sh_renewed:
            status['reason'] = 'already_renewed'
            return status
        if service.state == 'deleting':
            status['reason'] = 'service_removed'
            return status
        if (service.state in _NOT_DEPLOYED_STATES
                or service._provision_rejected_nothing_created()):
            status['reason'] = 'service_not_deployed'
            return status
        if not service.helm_release_name:
            status['reason'] = 'service_removed'
            return status
        if sub.state != 'cancel' and sub.sh_end_date and sub.sh_end_date >= today:
            # 租戶自己結束（close）或剛到期、這一期還沒過完：這時續訂會從今天起算新的一期，
            # 等於重複付錢。週期結束後（寬限期內服務仍在跑）才開放。
            status['reason'] = 'period_not_ended'
            status['period_end'] = sub.sh_end_date.isoformat()
            return status
        if sub.state == 'cancel':
            _grace_days, cleanup_months = lifecycle._lifecycle_params()
            cleanup_date = lifecycle._cancel_cleanup_date(sub, cleanup_months, today)
            status['retention_until'] = (cleanup_date - timedelta(days=1)).isoformat()
            if today >= cleanup_date:
                # 保留期已過：每日 cron 下一輪就會清除（或已在清除），不再賣續訂。
                status['reason'] = 'retention_expired'
                return status
        status['available'] = True
        return status

    def _renewal_refusal_message(self, code, data=None):
        """``code`` 對應的租戶訊息（已翻譯）。"""
        data = data or {}
        messages = {
            'no_subscription': _('This service has no subscription to renew.'),
            'subscription_active': _(
                "This service's subscription is still active; there is nothing to renew."),
            'already_renewed': _(
                'This subscription has already been renewed. The service will be '
                'restored automatically once the renewal is complete.'),
            'subscription_not_renewable': _(
                'This subscription cannot be renewed in its current state.'),
            'service_removed': _(
                'This service has already been removed, so it cannot be renewed. '
                'Please create a new service.'),
            'service_not_deployed': _(
                'This service was never set up, so it cannot be renewed. Please delete it '
                'and create a new service.'),
            'period_not_ended': _(
                'The current billing period has not ended yet (it ends on %(date)s). You '
                'can renew after it ends.', date=data.get('period_end') or ''),
            'retention_expired': _(
                'The retention period of this service has ended, so it can no longer be '
                'renewed. Please create a new service.'),
            'plan_not_available': _('This plan cannot be used to renew this service.'),
            'busy': _(
                'This service is being processed right now. Please try again in a moment.'),
            'insufficient_balance': _(
                'Insufficient wallet balance. %(shortage)s more points are needed.',
                shortage=data.get('shortage', 0)),
            'payment_failed': _(
                'The payment could not be completed, so nothing was renewed or charged. '
                'Please try again.'),
        }
        return messages.get(code) or messages['subscription_not_renewable']

    # ------------------------------------------------------------------ #
    # 方案與報價
    # ------------------------------------------------------------------ #

    def _renewal_plans(self):
        """可以拿來續訂的方案：原方案優先，加上規格與原方案相同的同範本方案。

        * 原方案即使已從前台下架（``storefront_hidden``／不在範本清單）也可沿用——續訂是
          「繼續用原本那個」，不是新購。
        * 其他方案：同範本、前台有上架、Access 席數相同、而且套到這台服務上的 helm values
          與原方案相同（``_plan_values_for``）——也就是同一個等級的月繳／年繳。這樣續訂
          不必 helm upgrade、也不用重跑換方案的規格／磁碟預檢；要換規格請續訂後走「變更
          方案」。
        * 沒有訂閱產品的方案不列（``_subscription_product_for`` 會失敗）。
        """
        self.ensure_one()
        service = self.sudo()
        Plan = self.env['sh.subscription.plan'].sudo()
        old_plan = service.subscription_id.sh_subscription_plan_id
        if not old_plan:
            return Plan
        plans = Plan
        candidates = old_plan | service.template_id.subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden
            and p.ac_max_access_seats == old_plan.ac_max_access_seats)
        try:
            baseline = service._plan_values_for(old_plan)
        except ValueError:
            candidates = old_plan
            baseline = None
        for plan in candidates:
            if plan != old_plan:
                try:
                    if service._plan_values_for(plan) != baseline:
                        continue
                except ValueError:
                    continue
            try:
                service._subscription_product_for(plan)
            except UserError:
                continue
            plans |= plan
        return plans

    def _renewal_quote(self, plan, today=None):
        """用 ``plan`` 續訂的首期金額（不寫 DB）。

        金額的規則與實際開票同一套：用上游的 ``_sh_get_period_billing_amount``（每日扣款與
        發票共用的 SSOT），套在一筆**同樣方式**組出來的暫存訂閱上（所選方案的訂閱產品、
        ``_onchange_sh_subscription_plan_id``／``_onchange_sh_product_id``、數量沿用舊訂閱）
        ——``_renew_lapsed_subscription`` 對真的子訂閱做的是同一串步驟。
        """
        self.ensure_one()
        service = self.sudo()
        old = service.subscription_id
        today = today or fields.Date.today()
        product = service._subscription_product_for(plan)
        draft = self.env['sh.subscription.subscription'].sudo().new({
            'sh_partner_id': old.sh_partner_id.id,
            'product_id': product.id,
            'sh_subscription_plan_id': plan.id,
            'sh_qty': old.sh_qty or 1,
        })
        draft._onchange_sh_subscription_plan_id()
        draft._onchange_sh_product_id()
        currency = draft.currency_id or self.env.company.currency_id
        total = draft._sh_get_period_billing_amount()
        price = draft._sh_get_invoice_price_unit() * draft.sh_qty
        # 2026-10-14 政策 A（內含稅）：未稅額取 compute_all 的 total_excluded；原本直接拿單價
        # 當未稅，內含稅時會顯示「未稅 300、稅 0」。
        untaxed = currency.round(draft.sh_taxes_ids.compute_all(
            draft._sh_get_invoice_price_unit(), currency=currency, quantity=draft.sh_qty,
            product=draft.product_id, partner=draft.sh_partner_id,
        )['total_excluded'] if draft.sh_taxes_ids else price)
        tax_amount = currency.round(total - untaxed)
        delta = _CYCLE_DELTAS.get(plan.sh_unit, _CYCLE_DELTAS['month'])(plan.sh_duration or 1)
        return {
            'plan_id': plan.id,
            'untaxed': float(untaxed),
            'tax_amount': float(tax_amount),
            'tax_rate': float(tax_amount / untaxed) if untaxed else 0.0,
            'total': float(total),
            'billing_cycle_start': today.isoformat(),
            # 與 `_force_active_dates_from_plan` 同一個慣例：end = start + cycle − 1 天。
            'billing_cycle_end': (today + delta - relativedelta(days=1)).isoformat(),
        }

    # ------------------------------------------------------------------ #
    # 續訂
    # ------------------------------------------------------------------ #

    def _renew_lapsed_subscription(self, plan):
        """自助續訂：沿用上游續約鏈、錢包付款、付清才改綁；停權中的服務由 #1599 的 cron 復權。

        第一個寫入之前會擋掉所有能預判的失敗（不能續訂、方案不合格、錢包不足、另一筆交易
        正在處理這台服務）——那之後才有上游寄出的通知信。寫入全部包在一個 savepoint：付款
        鏈中途出錯、或錢沒有真的付清，整段回捲（舊訂閱維持原狀、沒有子訂閱、錢包不變、
        沒有復權旗標與 trigger）。

        Raises:
            RenewalRefused: 帶穩定代碼；訊息可直接給租戶看。
        Returns:
            dict：``subscription``＝新的（in_progress、已付清的）子訂閱；``invoice``＝這次付清的
            首期發票。
        """
        self.ensure_one()
        service = self.sudo()
        lifecycle = self.env['woow_paas_platform.subscription_lifecycle_cron'].sudo()

        status = service._renewal_status()
        if not status['available']:
            raise RenewalRefused(status['reason'], service._renewal_refusal_message(
                status['reason'], status), data=status)
        if plan not in service._renewal_plans():
            raise RenewalRefused('plan_not_available',
                                 service._renewal_refusal_message('plan_not_available'))
        old = service.subscription_id
        quote = service._renewal_quote(plan)
        partner = old.sh_partner_id
        balance = partner.wallet_balance or 0.0
        if balance < quote['total']:
            shortage = round(quote['total'] - balance, 2)
            raise RenewalRefused(
                'insufficient_balance',
                service._renewal_refusal_message(
                    'insufficient_balance', {'shortage': f'{shortage:,.2f}'}),
                data={'shortage': shortage, 'total': quote['total'], 'wallet_balance': balance})
        # 重複點擊／兩個分頁同時按：先拿到這一列的才繼續，另一個立刻回 busy（什麼都不做）。
        # 這把鎖也讓每日 cron 的停權／清除（`_lock_for_transition`）不會與續訂交錯。
        if not service._acquire_resume_lock():
            raise RenewalRefused('busy', service._renewal_refusal_message('busy'))

        _logger.info(
            "[CLOUD_SVC] renew request service=%s old_sub=%s state=%s plan=%s total=%s",
            service.id, old.id, old.state, plan.id, quote['total'],
        )
        with self.env.cr.savepoint():
            new = old._sh_renew_subscription(enforce_portal_rules=True)
            product = service._subscription_product_for(plan)
            new.write({'product_id': product.id, 'sh_subscription_plan_id': plan.id})
            new._onchange_sh_subscription_plan_id()
            new._onchange_sh_product_id()
            service._force_active_dates_from_plan(new, plan)
            new.sh_subscription_confirm()
            if lifecycle._paid_resume_block_reason(new):
                # 錢沒有真的付清（例如與另一筆扣款競爭、錢包在這之間被扣到不足，上游只過帳
                # 不付款）：不留一張「已恢復但沒付錢」的訂閱。
                _logger.warning(
                    "[CLOUD_SVC] renew payment not settled service=%s new_sub=%s; rolled back",
                    service.id, new.id,
                )
                raise RenewalRefused('payment_failed',
                                     service._renewal_refusal_message('payment_failed'))
            lifecycle._follow_renewal_chain(service)
        _logger.info(
            "[CLOUD_SVC] renewed service=%s old_sub=%s new_sub=%s halted=%s resume_requested=%s",
            service.id, old.id, new.id, service._is_halted(), service.resume_requested,
        )
        invoice = self.env['account.move'].sudo().search([
            ('sh_subscription_id', '=', new.id), ('move_type', '=', 'out_invoice'),
        ], order='id desc', limit=1)
        return {'subscription': new, 'invoice': invoice}

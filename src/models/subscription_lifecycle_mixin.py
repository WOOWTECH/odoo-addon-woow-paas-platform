import logging
from contextlib import nullcontext
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------- #
#  租戶直打 ORM（`/web/dataset/call_kw`）的生命週期守衛                       #
# ------------------------------------------------------------------------- #
#
# SA／SH 的 owner／admin 對自己的記錄有合法的 write（ir.rule
# `*_rule_owner_admin_manage`），而 `/web/dataset/call_kw` 不經過 /woow
# controller 的 `_check_operation_allowed`（訂閱閘）。下面兩組守衛補的是
# 「controller 有擋、ORM 沒擋」而且**後果是繞過計費**的那兩格：
#
# 1. `subscription_id` 綁定（#519 提案 B 在 cloud_service 上的同一個不變式，
#    該決策文件「明確不在本輪範圍」段點名 SA／SH 同款洞仍開著）：
#    - 清空：`_cron_enforce_subscription_lifecycle` 的 domain 是
#      `subscription_id != False` ⇒ 清空＝永久脫離停權／結算；而
#      `_is_subscription_active()` 對「沒有訂閱」回 True ⇒ 之後 controller 的
#      訂閱閘也一律放行。
#    - 改指：Many2one 寫入不查 comodel ACL ⇒ 任何存在的訂閱 id 都寫得進去。
# 2. 停權逃逸：把 `suspended`／`deleting` 改回會對外服務的 state。SA 的 route
#    同步只在 `active`／`active_no_route`／`error` 推送真 service_url，所以
#    「state 改回 active ＋ 隨便改一條 route」就把 503 凍結推掉。
#
# 豁免＝superuser／sudo 或 `base.group_system`（與 `workspace.py`
# `_is_privileged_writer`（#658）同一個判準）。這兩組屬於
# scope-flip-write-guard 家族——有合法的變更路徑（下單／change-plan 的綁定、
# cron 的續約鏈與 `_do_resume`），只是要設閘——不是「根本沒有合法路徑」的
# su-env-exempt-guard 家族。⚠ 正因為豁免 su，守線測試必須走 HttpCase＋真租戶
# 帳號（`TransactionCase` 是 su env，deny 分支在那裡結構上不可達）。
#
# ⚠ 刻意**不**改 cloud_service：它有自己對所有人（含 su）生效的 #519 版本
# （`CloudService._assert_subscription_binding` ＋ partial unique index）。
_LIFECYCLE_ENTITY_MODELS = (
    'woow_paas_platform.cloud_service',
    'woow_paas_platform.security_access',
    'woow_paas_platform.smart_home',
)

#: 停權／刪除中——離開這兩格回到其他 state 只能由平台（`_do_resume` 等）做。
_HALTED_STATES = ('suspended', 'deleting')


class EntitySubscriptionBindingError(AccessError):
    """SA／SH 的 `subscription_id` 綁定不合法（租戶直打 ORM）。

    獨立型別讓測試斷言**型別**而非文案，並能與 ACL／ir.rule 的 AccessError
    分開（同 `cloud_service.SubscriptionBindingError` 的理由）。
    """


class LifecycleHaltEscapeError(AccessError):
    """租戶把停權中／刪除中的 SA／SH 改回會對外服務的 state。"""


# #1310：每個計費週期換算成「月」的倍數，供工作區月費估算。
_MONTHS_PER_UNIT = {'day': 12 / 365, 'week': 12 / 52, 'month': 1.0, 'year': 12.0}


def _monthly_estimate(sub):
    """Tax-included cost of one billing cycle normalised to a month (rounded to 2dp)."""
    total = _subscription_total_with_tax(sub)[3]
    months = _MONTHS_PER_UNIT.get(sub.sh_unit or 'month', 1.0) * (sub.sh_recurrency or 1)
    return round(total / months, 2) if months else 0.0


def _subscription_total_with_tax(sub):
    """Compute (untaxed, tax_rate, tax_amount, total) for a subscription.

    Shared between `_skip_trial_estimate` (skip-trial confirmation modal)
    and the `/api/dashboard/summary` endpoint's `next_bill_estimate` so a
    single tax-computation rule serves both surfaces. Drift here would
    surface as "the modal said 573.85 but the dashboard said 499" — the
    same class of bug as the original Issue #9 root cause.

    Tax source matches what `_generate_invoice_vals` will write onto the
    invoice line: prefer `sh_taxes_ids`, fall back to the subscription's
    product taxes for subs whose onchange chain never populated
    `sh_taxes_ids` (legacy data path).

    Args:
        sub: a `sh.subscription.subscription` recordset (single record).

    Returns:
        tuple of floats: (untaxed, tax_rate, tax_amount, total).
        All zero when sub is empty / sh_plan_price is 0.
    """
    if not sub:
        return 0.0, 0.0, 0.0, 0.0
    untaxed = sub.sh_plan_price or 0.0
    if not untaxed:
        return 0.0, 0.0, 0.0, 0.0

    taxes = sub.sh_taxes_ids
    if not taxes and sub.product_id:
        taxes = sub.product_id.taxes_id

    currency = sub.currency_id or sub.env.company.currency_id

    if taxes:
        compute = taxes.compute_all(
            untaxed,
            partner=sub.sh_partner_id,
            currency=currency,
        )
        untaxed = compute['total_excluded']
        total = compute['total_included']
        tax_amount = total - untaxed
        tax_rate = (tax_amount / untaxed) if untaxed else 0.0
    else:
        total = untaxed
        tax_amount = 0.0
        tax_rate = 0.0

    return float(untaxed), float(tax_rate), float(tax_amount), float(total)


class SubscriptionLifecycleMixin(models.AbstractModel):
    _name = 'woow_paas_platform.subscription_lifecycle_mixin'
    _description = 'Subscription Lifecycle Mixin'

    subscription_id = fields.Many2one(
        'sh.subscription.subscription',
        string='Subscription',
        ondelete='set null',
        copy=False,
    )

    # Computed convenience fields for UI display
    subscription_state = fields.Selection(
        related='subscription_id.state',
        string='Subscription State',
        store=True,
    )
    subscription_plan_name = fields.Char(
        related='subscription_id.sh_subscription_plan_id.name',
        string='Plan',
    )
    subscription_end_date = fields.Date(
        related='subscription_id.sh_end_date',
        string='Subscription End Date',
    )
    subscription_next_payment = fields.Date(
        related='subscription_id.sh_date_of_next_payment',
        string='Next Payment Date',
    )
    subscription_is_trial = fields.Boolean(
        related='subscription_id.sh_trial',
        string='Is Trial',
    )
    subscription_trial_end = fields.Date(
        related='subscription_id.sh_trial_end_date',
        string='Trial End Date',
    )

    # Suspend/resume state tracking
    pre_suspend_state = fields.Char(
        string='Pre-Suspend State',
        help='State before suspension, used to restore on resume',
    )
    # #1610：訂閱已回到 in_progress、服務仍停權中，但「付清才復權」的判斷沒過
    # （`subscription_lifecycle_cron._paid_resume_block_reason`）——這裡寫明為什麼還沒恢復。
    # 只有生命週期 cron 會寫；復權、或判斷不再擋下時清掉。`groups='base.group_system'`：
    # 給維運看的說明，租戶經 ORM 讀不到也寫不進去。
    resume_hold_reason = fields.Text(
        string='Resume On Hold',
        readonly=True,
        copy=False,
        groups='base.group_system',
        help='Why this suspended service has not been resumed although its subscription '
             'is in progress again: it still has unpaid invoices. Written and cleared by '
             'the subscription lifecycle jobs.',
    )

    # ----------------------------------------------------------------
    # Subscription access
    # ----------------------------------------------------------------

    def _sub(self):
        """本記錄的訂閱（**sudo**）——mixin 內部唯一的 subscription traversal 入口。

        ``subscription_id`` 指向 ``sh.subscription.subscription``，而該 model 的
        read ACL 只發給 ``group_manager_sh_subscription`` /
        ``group_user_sh_subscription`` / ``base.group_portal``。workspace 擁有權是
        **平台側**概念，不會連帶給到那三個 Odoo 群組——所以 mixin 內部只要直接
        traversal（``self.subscription_id.state`` 之類），僅有
        ``base.group_user`` 的內部呼叫者就會在 env cache 落空時撞 AccessError，
        endpoint 回 "You are not allowed to access sh.subscription.subscription"
        （#29；#27 的 controller 端已於 PR #28 修過同一個 class 的缺陷）。

        「cache 命中所以看起來沒事」不是健全性——production 每個 HTTP request 都
        從空 cache 開始，所以這條路是必現而非偶發。

        這不是「租戶都是 internal」的定義：一般註冊租戶是 Portal；對外 PaaS
        由 private capability 驗證真實 uid／workspace／角色，不升級其群組。
        本 helper 只提權讀取已授權實例所綁訂閱。呼叫端仍須保留自己的讀取或
        異動守衛；readonly 成員路徑與 owner/admin 異動路徑不能混為一談。

        新增任何要讀訂閱的 mixin 方法都走這裡，不要再寫 ``self.subscription_id``
        ——``scripts/audit/repo-lint.py`` 的 ``check_mixin_subscription_sudo``
        會在編輯當下機械攔下。
        """
        self.ensure_one()
        return self.sudo().subscription_id

    # ----------------------------------------------------------------
    # Subscription creation
    # ----------------------------------------------------------------

    def _create_and_confirm_subscription(self, partner, plan, *, trial_only=False):
        """Create a subscription linked to this entity and confirm it.

        Args:
            partner: res.partner record (workspace owner's partner)
            plan: sh.subscription.plan record
        Returns:
            sh.subscription.subscription record
        """
        self.ensure_one()
        # #367 (per-entity): capture whether THIS entity already holds a
        # subscription BEFORE we create/link the new one. The 7-day trial is a
        # per-service (per-entity) one-time grant — NOT per-partner (Eugene's
        # 2026-07-19 #367 clarification): every new service gets its own trial;
        # only a re-subscription on a service that already had one (change-plan;
        # or a hypothetical suspend-resume that recreates a sub) must not
        # restart it.
        #
        # ``subscription_id`` is the entity's canonical "is subscribed" pointer
        # and is the reliable, entity-scoped signal here:
        #   • brand-new entity (incl. a partner's 2nd/3rd service) → empty → trial
        #   • change-plan → the wizard / HTTP endpoints close the old sub
        #     (state='close') but do NOT unlink subscription_id (no code path
        #     sets it False; ondelete='set null' only fires on delete, and
        #     change-plan closes — never deletes) → truthy → no trial
        #   • suspend/resume → _do_resume flips state without recreating a
        #     subscription, so it never reaches this method → trial untouched
        # Read here, before the create + the trailing self.write() that replaces
        # it at the end of this method, so it reflects the entity's PRIOR state.
        entity_has_prior_sub = bool(self._sub())
        if trial_only and (entity_has_prior_sub or not plan.sh_trial
                           or plan.sh_trial_unit != 'day'
                           or plan.sh_trial_duration != 7):
            raise UserError(_('此服務無法開通七天試用，請聯絡支援。'))
        _logger.info(
            "[SUB_MIXIN] _create_and_confirm_subscription entry entity=%s:%s partner=%s plan=%s prior_sub=%s",
            self._name, self.id, partner.id, plan.id, entity_has_prior_sub,
        )
        Subscription = self.env['sh.subscription.subscription']

        product = self._subscription_product_for(plan)

        # Validate subscription journal is configured
        # (sh_subscription uses company.sh_journal_id for invoice creation;
        #  post_init_hook sets a default, admin can override in Settings → Woow PaaS)
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            raise UserError(
                _("Subscription journal is not configured. "
                  "Please go to Settings → Woow PaaS → Subscription Lifecycle "
                  "and select a journal.")
            )

        sub_vals = {
            'sh_partner_id': partner.id,
            'product_id': product.id,
            'sh_subscription_plan_id': plan.id,
            'sh_source': 'manual',
            'woow_trial_only': trial_only,
        }
        subscription = Subscription.sudo().create(sub_vals)

        # Trigger onchanges to populate recurrence, prices, and dates.
        # Order matters: plan → product → trial dates → end date.
        subscription._onchange_sh_subscription_plan_id()
        subscription._onchange_sh_product_id()
        subscription._onchange_sh_trial_subcription_start_date()
        subscription._onchange_sh_trial_subcription_end_date()

        # Always override the onchange-chain-computed dates with plan-driven
        # values; the third-party onchange compute is unreliable on both
        # branches (issue #33 for trial, #39 for non-trial). See helper
        # docstrings for the specific arithmetic each branch corrects.
        #
        # #367 (per-entity): grant the trial only when the plan offers one AND
        # this entity had no prior subscription (``entity_has_prior_sub``,
        # captured at entry above). A brand-new service — including a partner's
        # 2nd/3rd — gets its own 7-day trial; a change-plan / resume on a
        # service that already had one enters the active (non-trial) state.
        if plan.sh_trial and not entity_has_prior_sub:
            self._force_trial_dates_from_plan(subscription, plan)
        else:
            self._force_active_dates_from_plan(subscription, plan)

        # Confirm → in_progress (starts trial if configured)
        subscription.sh_subscription_confirm()
        if trial_only:
            # No payment is scheduled until the user selects a paid plan.
            subscription.write({'sh_date_of_next_payment': False})

        self.write({'subscription_id': subscription.id})
        _logger.info(
            "[SUB_MIXIN] created subscription=%s name=%s plan=%s entity=%s:%s trial=%s",
            subscription.id, subscription.name, plan.name,
            self._name, self.id, subscription.sh_trial,
        )
        return subscription

    def _change_plan_balance_quote(self, plan, refund):
        """換到 ``plan`` 的首期應付與錢包是否付得起（不寫 DB）。

        AUDITFIX change-plan-insufficient-balance（#1677 A1）：三個 change-plan 入口原本沒有
        任何餘額檢查——錢包不足時照樣退點、關舊訂閱、建新訂閱，新訂閱 confirm 開出首期發票、
        錢包扣不到就留成未付。換方案時新訂閱一律不給試用（``entity_has_prior_sub`` 在這條路徑
        上恆為 True，見 ``_create_and_confirm_subscription``），confirm 當下就開首期發票。

        * ``required``：新方案首期應付（含稅）。用上游與發票同源的
          ``_sh_get_period_billing_amount``，套在一筆與 ``_create_and_confirm_subscription``
          同樣方式組出來的暫存訂閱上（skip-trial、自助續訂同一把尺）。
        * ``available``：錢包餘額＋這次換方案會先退的點（``refund``，呼叫端以舊訂閱的
          ``_compute_unused_refund`` 算好傳入——退點在建新訂閱之前入帳）。
        * ``shortfall``：還差多少（付得起時為 0）。

        Raises:
            UserError: 方案沒有訂閱產品（``_subscription_product_for``）。
        """
        self.ensure_one()
        old = self._sub()
        partner = old.sh_partner_id
        product = self._subscription_product_for(plan)
        draft = self.env['sh.subscription.subscription'].sudo().new({
            'sh_partner_id': partner.id,
            'product_id': product.id,
            'sh_subscription_plan_id': plan.id,
            'sh_source': 'manual',
        })
        draft._onchange_sh_subscription_plan_id()
        draft._onchange_sh_product_id()
        currency = draft.currency_id or self.env.company.currency_id
        required = currency.round(draft._sh_get_period_billing_amount() or 0.0)
        balance = currency.round(partner.sudo().wallet_balance or 0.0)
        refund = currency.round(refund or 0.0)
        available = currency.round(balance + refund)
        shortfall = currency.round(max(required - available, 0.0))
        return {
            'plan_id': plan.id,
            'required': float(required),
            'balance': float(balance),
            'refund': float(refund),
            'available': float(available),
            'sufficient': currency.compare_amounts(available, required) >= 0,
            'shortfall': float(shortfall),
        }

    def _change_plan_balance_gate(self, plan, refund, dry_run=False):
        """三個 change-plan 入口共用：錢包付不起就回 endpoint 的失敗回應；``dry_run`` 回估算。

        放在每個入口「所有既有閘之後、第一個寫入（退點）之前」——與 #773 席數閘同一個相對
        位置。錯誤碼與 ``/api/billing/invoices/<id>/pay`` 一致（頂層 ``code`` 與
        ``data.code`` 都是 ``insufficient_balance``）。

        Returns:
            ``None``＝放行（繼續換方案）；否則是 endpoint 要直接回傳的 dict。
        """
        try:
            quote = self._change_plan_balance_quote(plan, refund)
        except UserError as e:
            return {'success': False, 'error': str(e)}
        if dry_run:
            return {'success': True, 'data': dict(quote, dry_run=True)}
        if quote['sufficient']:
            return None
        _logger.info(
            "[SUB_MIXIN] change_plan refused (insufficient balance) entity=%s:%s plan=%s "
            "required=%s available=%s", self._name, self.id, plan.id,
            quote['required'], quote['available'],
        )
        return {
            'success': False,
            'error': _('Insufficient wallet balance. %(shortage)s more points are needed.',
                       shortage=f"{quote['shortfall']:,.2f}"),
            'code': 'insufficient_balance',
            'data': dict(quote, code='insufficient_balance'),
        }

    def _subscription_product_for(self, plan):
        """``plan`` 的訂閱產品（第一個 variant）；沒有就 raise UserError。

        新開訂閱（``_create_and_confirm_subscription``）與 #1611 自助續訂共用。
        """
        # sh_subscription_plan_id is on product.template; find product via template
        product_tmpl = self.env['product.template'].sudo().search([
            ('sh_product_subscribe', '=', True),
            ('sh_subscription_plan_id', '=', plan.id),
        ], limit=1)

        if not product_tmpl:
            raise UserError(
                _("No subscription product found for plan: %s") % plan.name
            )

        # Get the first variant of this template
        return product_tmpl.product_variant_id

    def _force_trial_dates_from_plan(self, subscription, plan):
        """Force trial fields and dates on ``subscription`` from ``plan``.

        Plan-driven explicit set; never trusts the chained onchange compute
        in ``_create_and_confirm_subscription`` to populate trial dates.
        Writes the full date triple — sh_trial_end_date, sh_start_date,
        sh_end_date — so downstream readers (UI trial countdown, refund
        compute) never see a partial state.
        """
        trial_delta = {
            'day': relativedelta(days=plan.sh_trial_duration),
            'week': relativedelta(weeks=plan.sh_trial_duration),
            'month': relativedelta(months=plan.sh_trial_duration),
            'year': relativedelta(years=plan.sh_trial_duration),
        }.get(plan.sh_trial_unit, relativedelta())
        cycle_delta = {
            'day': relativedelta(days=plan.sh_duration),
            'week': relativedelta(weeks=plan.sh_duration),
            'month': relativedelta(months=plan.sh_duration),
            'year': relativedelta(years=plan.sh_duration),
        }.get(plan.sh_unit, relativedelta())
        trial_end = date.today() + trial_delta - relativedelta(days=1)
        start_date = trial_end + relativedelta(days=1)
        end_date = start_date + cycle_delta - relativedelta(days=1)
        subscription.write({
            'sh_trial': True,
            'sh_trial_duration': plan.sh_trial_duration,
            'sh_trial_unit': plan.sh_trial_unit,
            'sh_trial_end_date': trial_end,
            'sh_start_date': start_date,
            'sh_end_date': end_date,
            'sh_date_of_next_payment': start_date,
        })
        _logger.info(
            "Forced trial for subscription %s: %s %s, billing %s → %s",
            subscription.name, plan.sh_trial_duration, plan.sh_trial_unit,
            start_date, end_date,
        )

    def _force_active_dates_from_plan(self, subscription, plan):
        """Force ``subscription`` into a coherent non-trial (active) state.

        Plan-driven (start_date=today, end_date=start+cycle_delta-1d) so
        downstream readers never see end < start; cycle_delta does NOT
        multiply by sh_no_of_billing_cycle (= -1 on every platform plan,
        which was the issue #39 root cause).

        ``sh_date_of_next_payment`` is seeded to ``start_date``;
        ``sh_subscription_confirm()`` advances it by one cycle to
        ``start + cycle_delta`` after creating the first invoice.

        #367: also clears ``sh_trial`` / ``sh_trial_end_date``. This helper is
        the active branch, reachable for a trial plan when the entity already
        holds a subscription (per-entity guard: a change-plan / resume on an
        existing service) — the onchange chain may have flipped
        ``sh_trial=True``, so the flag must be cleared to stay consistent with
        the active date triple.
        """
        cycle_delta = {
            'day': relativedelta(days=plan.sh_duration),
            'week': relativedelta(weeks=plan.sh_duration),
            'month': relativedelta(months=plan.sh_duration),
            'year': relativedelta(years=plan.sh_duration),
        }.get(plan.sh_unit, relativedelta())
        start_date = date.today()
        end_date = start_date + cycle_delta - relativedelta(days=1)
        subscription.write({
            # #367: the onchange chain may have left sh_trial=True; the active
            # path is non-trial by definition, so clear the flag and any
            # trial-end residue to keep sh_trial and the date triple consistent.
            'sh_trial': False,
            'sh_trial_end_date': False,
            'sh_start_date': start_date,
            'sh_end_date': end_date,
            'sh_date_of_next_payment': start_date,
        })
        _logger.info(
            "Forced active dates for subscription %s: billing %s → %s",
            subscription.name, start_date, end_date,
        )

    # ----------------------------------------------------------------
    # Subscription state checks
    # ----------------------------------------------------------------

    def _get_grace_period_days(self):
        return int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.grace_period_days', '7'
        ))

    def _get_cleanup_after_months(self):
        return int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.cleanup_after_months', '6'
        ))

    def _is_subscription_active(self):
        """Check if subscription allows normal operations."""
        self.ensure_one()
        sub = self._sub()
        if not sub:
            return True  # No subscription = unrestricted (admin/legacy)
        return sub.state == 'in_progress'

    def _is_in_grace_period(self):
        """Check if subscription is expired but within grace period."""
        self.ensure_one()
        sub = self._sub()
        if not sub or not sub.sh_end_date:
            return False
        if sub.state not in ('expire', 'close'):
            return False
        days_expired = (fields.Date.today() - sub.sh_end_date).days
        return 0 < days_expired <= self._get_grace_period_days()

    def _is_past_grace_period(self):
        """Check if subscription expired beyond grace period."""
        self.ensure_one()
        sub = self._sub()
        if not sub:
            return False
        if sub.state == 'cancel':
            return True
        if not sub.sh_end_date:
            return False
        if sub.state not in ('expire', 'close'):
            return False
        days_expired = (fields.Date.today() - sub.sh_end_date).days
        return days_expired > self._get_grace_period_days()

    def _check_operation_allowed(self, operation='write'):
        """Check if the current user can perform an operation.

        Args:
            operation: 'read', 'write' or 'delete'（#1424：這裡與 'write' 同級；
                CloudService 對開通被拒、訂閱已自動取消的服務放行 'delete'）
        Raises:
            UserError if not allowed
        """
        self.ensure_one()
        # Admin bypasses all restrictions
        if self.env.user.has_group('base.group_system'):
            return

        if self._is_subscription_active():
            return

        if self._is_in_grace_period():
            if operation == 'read':
                return
            raise UserError(
                _("Your subscription has expired. "
                  "You have a grace period to renew. "
                  "During this time, modifications are not allowed.")
            )

        if self._is_past_grace_period():
            raise UserError(
                _("Your subscription has expired and the service is suspended. "
                  "Please renew your subscription to restore access.")
            )

    # ----------------------------------------------------------------
    # Skip trial
    # ----------------------------------------------------------------

    def _skip_trial_estimate(self):
        """Compute the upcoming-invoice breakdown for skip-trial.

        Side-effect-free counterpart to ``_skip_trial``: returns the same
        amount that the actual skip-trial would invoice, broken down into
        untaxed / tax / total, plus wallet sufficiency info. Used by the
        confirm modal to render the breakdown before the user commits.

        Mirrors the same three preconditions as ``_skip_trial`` so the
        modal can surface the same UserError messages on load.

        Returns:
            dict with keys:
                untaxed, tax_rate, tax_amount, total, currency,
                wallet_balance, sufficient, shortage,
                billing_cycle_start, billing_cycle_end
        Raises:
            UserError when no subscription / not active / not in trial.
        """
        self.ensure_one()
        sub = self._sub()
        if not sub:
            raise UserError(_("No subscription linked to this service."))
        if sub.state != 'in_progress':
            raise UserError(_("Subscription is not active."))
        # 2026-10-10：試用已自然結束（已開始計費）的訂閱 sh_trial 仍可能是 True，
        # 只看旗標會讓它再「略過試用」一次＝重複扣當期，所以看實際試用期。
        if not sub._woow_in_trial():
            raise UserError(_("Subscription is not in trial period."))

        # Tax breakdown is computed by the shared helper so the dashboard's
        # next_bill_estimate uses the same rule as the skip-trial modal.
        untaxed, tax_rate, tax_amount, total = _subscription_total_with_tax(sub)

        today = fields.Date.today()
        delta = {
            'day': relativedelta(days=sub.sh_recurrency or 1),
            'week': relativedelta(weeks=sub.sh_recurrency or 1),
            'month': relativedelta(months=sub.sh_recurrency or 1),
            'year': relativedelta(years=sub.sh_recurrency or 1),
        }.get(sub.sh_unit, relativedelta(months=1))

        wallet_balance = sub.sh_partner_id.wallet_balance or 0.0
        return {
            'untaxed': float(untaxed),
            'tax_rate': float(tax_rate),
            'tax_amount': float(tax_amount),
            'total': float(total),
            'currency': 'pts',
            'wallet_balance': float(wallet_balance),
            'sufficient': wallet_balance >= total,
            'shortage': float(max(0.0, total - wallet_balance)),
            'billing_cycle_start': today.isoformat(),
            'billing_cycle_end': (today + delta).isoformat(),
        }

    def _skip_trial(self, before_mutation=None):
        """Skip the trial period and start paid billing immediately.

        Creates the first invoice and advances the next payment date
        by one billing cycle.

        Args:
            before_mutation: 選用的 side-effect-free guard；在既有 business
                precondition 之後、savepoint 與第一筆寫入之前呼叫。提供此 callback
                時，本方法會自行包住 mutation savepoint，讓 caller 不必把 guard
                放進 savepoint 內。
        Returns:
            dict with invoice info
        Raises:
            UserError if not in trial
        """
        self.ensure_one()
        sub = self._sub()
        _logger.info(
            "[SUB_MIXIN] _skip_trial entry entity=%s:%s sub=%s",
            self._name, self.id, sub.id if sub else None,
        )
        if not sub:
            raise UserError(_("No subscription linked to this service."))
        if sub.state != 'in_progress':
            raise UserError(_("Subscription is not active."))
        # 2026-10-10：試用已自然結束（已開始計費）的訂閱 sh_trial 仍可能是 True，
        # 只看旗標會讓它再「略過試用」一次＝重複扣當期，所以看實際試用期。
        if not sub._woow_in_trial():
            raise UserError(_("Subscription is not in trial period."))

        # Check wallet balance before proceeding
        #
        # 2026-10-14：門檻必須是**本期發票的含稅總額**，不是未稅 `sh_plan_price`。
        # 原本比未稅價：訂閱帶稅（例 300 未稅＋5%＝315）且錢包落在 [300, 315) 時
        # 這裡放行，接著發票 315 > 錢包 ⇒ `_handle_invoice_creation` 退回上游
        # （`sh_invoice_generated='draft'` 留草稿、`'paid'` 開假付款），而試用已結束、
        # 下次扣款日已推一期 ⇒ 沒扣點就開始付費週期。用上游與發票同源的
        # `_sh_get_period_billing_amount()`（單價×數量再套 sh_taxes_ids）。
        partner = sub.sh_partner_id
        required = sub._sh_get_period_billing_amount()
        _logger.info(
            "[SUB_MIXIN] _skip_trial balance check sub=%s partner=%s untaxed=%s required=%s balance=%s",
            sub.id, partner.id, sub.sh_plan_price, required, partner.wallet_balance,
        )
        if partner.wallet_balance < required:
            raise UserError(
                _("Insufficient wallet balance to skip trial. "
                  "Current balance: %.2f, required: %.2f. "
                  "Please top up your wallet first.")
                % (partner.wallet_balance, required)
            )

        # #980：controller 的 `/woow` payability guard 由這個 seam 注入。它必須
        # 排在既有 business precondition 之後（保留 #567 taxonomy），但仍早於
        # savepoint／trial write／建票／扣款與 action_post。
        if before_mutation is not None:
            before_mutation(sub)

        mutation_scope = (
            self.env.cr.savepoint()
            if before_mutation is not None
            else nullcontext()
        )
        with mutation_scope:
            today = fields.Date.today()

            # End trial and set billing to start today
            #
            # #1261：`sh_trial_end_date = 昨天` 是**刻意的**，不是 off-by-one。
            # 本模組的日期慣例是「sh_trial_end_date＝試用的**最後一天（含）**、
            # sh_start_date＝第一個計費日＝trial_end + 1」——建立時的
            # `_force_trial_dates_from_plan` 就是 `start_date = trial_end + 1`，
            # test_subscription_lifecycle／test_trial_reset_guard_367 也釘著
            # `sh_start_date == trial_end + 1`。skip 當天就是第一個計費日（今天
            # 付今天的錢），所以最後一個試用日是昨天；若寫成今天，今天會同時算
            # 試用與計費，與上面的慣例矛盾。
            # 本 repo 內讀它的只有序列化（`_get_subscription_info`、billing.py
            # 的訂閱 dict → trial_end_date），前端只在 `is_trial` 時才顯示（skip
            # 後 sh_trial=False 就不再出現）；續扣、到期、退點
            # （`_prorata_unused_refund`）都看 sh_date_of_next_payment／
            # sh_start_date，不讀 sh_trial_end_date。
            # 正式站 E2E 看到「9/29 skip → trial_end 9/28」即此慣例，不要改成今天。
            sub.write({
                'sh_trial': False,
                'sh_trial_end_date': today - relativedelta(days=1),
                'sh_start_date': today,
                'sh_date_of_next_payment': today,
            })

            # Generate and create invoice
            invoice_vals = sub._generate_invoice_vals()
            invoice = self.env['account.move'].sudo().create(invoice_vals)
            sub.compute_view_invoice()
            _logger.info(
                "[SUB_MIXIN] _skip_trial invoice created sub=%s invoice=%s amount=%s",
                sub.id, invoice.id, invoice.amount_total,
            )

            # Advance next payment by one billing cycle
            unit_deltas = {
                'day': relativedelta(days=sub.sh_recurrency),
                'week': relativedelta(weeks=sub.sh_recurrency),
                'month': relativedelta(months=sub.sh_recurrency),
                'year': relativedelta(years=sub.sh_recurrency),
            }
            delta = unit_deltas.get(sub.sh_unit)
            if delta:
                # #1562：週期終點跟著 next_payment 從今天重算，與
                # `_force_active_dates_from_plan`（換方案／直接開通）同一個慣例：
                # end = start + cycle − 1 天。原本只推進 next_payment，sh_end_date 留著
                # 試用推算的值（試用結束後才開始的那一期），比實際週期晚「略過的試用
                # 天數」——例 10-07 略過 7 天試用：end 11-13、next_payment 11-07。
                # 讀 sh_end_date 的有：上游 `_sh_auto_expired_subscription`
                # （sh_end_date == today → expire）、到期寬限期判斷
                # （`_is_in_grace_period`／生命週期 cron）、上游提醒／續約按鈕 cron、
                # 訂閱確認信與帳務頁的「結束日」；續扣與退點只看 next_payment。
                sub.write({
                    'sh_date_of_next_payment': today + delta,
                    'sh_end_date': today + delta - relativedelta(days=1),
                })

            # Post/pay invoice based on company settings
            sub._handle_invoice_creation(invoice)

            _logger.info(
                "[SUB_MIXIN] _skip_trial done entity=%s:%s sub=%s invoice=%s amount=%s next_payment=%s",
                self._name, self.id, sub.id, invoice.name,
                invoice.amount_total, sub.sh_date_of_next_payment,
            )

            return {
                'invoice_id': invoice.id,
                'amount': invoice.amount_total,
            }

    # ----------------------------------------------------------------
    # Tenant-direct ORM guards（SA／SH 的 create／write 呼叫；見模組頂端）
    # ----------------------------------------------------------------

    def _is_platform_lifecycle_writer(self):
        """豁免判準：superuser／sudo 或 `base.group_system`。

        與 `workspace.py::_is_privileged_writer`（#658）同形。豁免的兩種身分都
        不是租戶經 RPC 取得得了的身分（`call_kw` 換得掉 context、換不掉 uid／su）。
        """
        return (self.env.is_superuser()
                or self.env.user.has_group('base.group_system'))

    def _guard_tenant_lifecycle_write(self, vals):
        """write() 前置守衛：停權逃逸 ＋ `subscription_id` 改綁／清空。

        先 `check_access('write')`：讓「本來就不能寫這一列的人」（role=user／
        guest／非成員）照舊由 ir.rule 拒絕，歸因不被本守衛搶走；走得過 ir.rule
        的人（owner／admin）才進入下面兩格。兩格都比對「新值 != 現值」，整批送出
        未修改欄位的 no-op write 不會被誤擋。
        """
        if self._is_platform_lifecycle_writer() or not self:
            return
        if not ({'state', 'subscription_id'} & set(vals)):
            return
        self.check_access('write')
        if 'state' in vals:
            for rec in self:
                if (rec.state in _HALTED_STATES
                        and vals['state'] != rec.state
                        and vals['state'] not in _HALTED_STATES):
                    raise LifecycleHaltEscapeError(_(
                        '%(model)s #%(id)s is %(state)s; only the platform can '
                        'bring it back into service (renew the subscription '
                        'instead).',
                        model=rec._description, id=rec.id, state=rec.state))
        if 'subscription_id' in vals:
            new_sub_id = vals['subscription_id'] or False
            for rec in self:
                if (rec._sub().id or False) == new_sub_id:
                    continue
                if not new_sub_id:
                    raise EntitySubscriptionBindingError(_(
                        'A service cannot be unbound from its subscription.'))
                rec._assert_entity_subscription_binding(
                    self.env['sh.subscription.subscription'].sudo().browse(
                        new_sub_id))

    def _guard_tenant_lifecycle_create(self):
        """create() 事後守衛：讀**落地值**（`default_subscription_id` 與租戶可寫的
        `ir.default` 都在 `BaseModel.create` 內部才展開，看 raw vals 會漏）。

        呼叫端是 super().create() 之後的 recordset；擋下來時 raise 會讓整筆交易
        回捲。
        """
        if self._is_platform_lifecycle_writer():
            return
        for rec in self:
            sub = rec._sub()
            if sub:
                rec._assert_entity_subscription_binding(sub)

    def _assert_entity_subscription_binding(self, sub):
        """綁定當下判準（形狀同 #519 提案 B 的 `CloudService._assert_subscription_binding`）。

        - 歸屬：`sub.sh_partner_id == workspace_id.owner_id.partner_id`；
        - 狀態：`in_progress`——第三方 `/renew-subscription` 讓租戶鑄得出屬於自己
          的訂閱，但停在 draft（#519／#725）；只驗歸屬會被無限鑄幣打穿；
        - 唯一：同一筆訂閱不得已綁在任何其他 CS／SA／SH 上（#335 裁決：一服務＝
          一訂閱）。⚠ 這是 Python 層 check-then-act，擋得住同一租戶的循序改綁，
          不保證並發；CS 側的 DB partial unique index 不延伸到這兩個 model。

        全程 sudo：這是授權判斷，不可被呼叫者自己的讀取權限縮窄（租戶對訂閱
        model 零 ACL）。只在綁定**改變**時呼叫——訂閱到期後仍合法地綁著（停權
        流程靠它），不是常駐不變式。
        """
        self.ensure_one()
        sub = sub.sudo().exists()
        if not sub:
            raise EntitySubscriptionBindingError(_(
                'The subscription does not exist.'))
        owner_partner = self.sudo().workspace_id.owner_id.partner_id
        if not owner_partner or sub.sh_partner_id != owner_partner:
            raise EntitySubscriptionBindingError(_(
                "This subscription does not belong to the owner of this "
                "service's workspace and cannot be bound."))
        if sub.state != 'in_progress':
            raise EntitySubscriptionBindingError(_(
                'Only an active (in_progress) subscription can be bound. '
                'Current state: %(state)s.', state=sub.state or 'unset'))
        for model_name in _LIFECYCLE_ENTITY_MODELS:
            domain = [('subscription_id', '=', sub.id)]
            if model_name == self._name:
                domain.append(('id', '!=', self.id))
            if self.env[model_name].sudo().search_count(domain):
                raise EntitySubscriptionBindingError(_(
                    'This subscription is already bound to another service.'))

    # ----------------------------------------------------------------
    # Suspend / Resume / Cleanup (abstract — implemented by subclass)
    # ----------------------------------------------------------------

    def _is_halted(self):
        """這筆記錄是否處於「停權中」——lifecycle cron 決定要不要復權的判準。

        預設只認 ``state == 'suspended'``，與 SmartHome / SecurityAccess 自己
        ``_do_resume`` 的 guard 一致（對它們是零行為變化）。

        CloudService override 成 ``state == 'suspended' or
        bool(pre_suspend_state)``：它的 state 會被 upgrade 管線改寫，需要一個不受
        呼叫順序影響的事實源（#482）。cron 讀這個方法而不是直接比 state，是為了讓
        「停權中但 state 被改寫過」的服務仍走得到復權——否則租戶付了款也回不來
        （#497）。
        """
        self.ensure_one()
        return self.state == 'suspended'

    def _acquire_resume_lock(self):
        """復權前先鎖住這一列；拿不到（別的交易正在處理它）回 ``False``（#1599）。

        生命週期 cron 的 ``_enforce_single`` 在「停權中、訂閱可能已恢復」時先問這個 seam，
        拿不到就跳過這一輪。預設**不取鎖、恆回 True**：Smart Home／Security Access 的
        復權只有每日生命週期 cron 一個執行者，不會有兩筆交易同時復權同一台（#1599 的
        付款後即時復權只做 Cloud Service）——對它們是零行為變化。CloudService override
        成真的取鎖。
        """
        self.ensure_one()
        return True

    def _do_suspend(self):
        """Suspend the service. Implemented by CloudService / SmartHome."""
        raise NotImplementedError

    def _do_resume(self):
        """Resume the service. Implemented by CloudService / SmartHome."""
        raise NotImplementedError

    def _do_cleanup(self):
        """Permanently remove the service. Implemented by CloudService / SmartHome."""
        raise NotImplementedError

    # ----------------------------------------------------------------
    # Subscription info for API responses
    # ----------------------------------------------------------------

    def _get_subscription_info(self):
        """Return subscription data dict for API responses."""
        self.ensure_one()
        sub = self._sub()
        if not sub:
            return None
        return {
            'id': sub.id,
            'state': sub.state,
            # #368 PR-3：ChangePlanModal 用 plan_id 定位目前方案（cycle×group×seats
            # 由前端 PlanSelector 反推）並擋「churn 成同方案」。三 surface（CS/SA/SH）
            # 皆以此 dict 為 subscription 資料源，故補在 mixin 一處即全數受惠。
            'plan_id': sub.sh_subscription_plan_id.id if sub.sh_subscription_plan_id else None,
            'plan_name': sub.sh_subscription_plan_id.name if sub.sh_subscription_plan_id else None,
            'plan_price': sub.sh_plan_price,
            # #1310：工作區頁「預估費用」原本寫死 $0/month。含稅總額依計費週期
            # 換算成每月（年繳 ÷12、週繳 ×52/12…），前端加總即為工作區月費估算。
            'monthly_estimate': _monthly_estimate(sub),
            # #745：Access 頁籤的顯示條件（設計拍板 6：只有 `> 0` 才出現）。
            #
            # 欄位名逐字保留 `ac_` 前綴，不學 plans 端點剝掉：那邊剝得掉是因為
            # 整個 dict 的 scope 就是「這個方案的上限」，這裡是混合欄位的
            # subscription dict，剝掉之後 `max_access_seats` 讀起來像是「這筆
            # 訂閱已用/可用席數」，而它的語意是**方案上限**（`ac_` 家族＝
            # capability-orthogonal 的上限值，OQ-C 拍板）。
            #
            # 值恆為 int（無方案時 0）：前端的判準是 `> 0`，回 None 會讓
            # `null > 0` 在 JS 裡默默成立為 false 沒錯，但一旦有人寫成
            # `>= 1` 之外的比較就不再安全，且「沒方案」與「方案沒買席位」在
            # 這個判準下本來就該同義。
            'ac_max_access_seats': (
                sub.sh_subscription_plan_id.ac_max_access_seats
                if sub.sh_subscription_plan_id else 0),
            # #773：**已用**席數（與方案上限並列，前端才能在按下確認**之前**就標出
            # 「這個方案裝不下」）。
            #
            # 🔴 欄位名刻意是 `ac_used_*` 而不是沿用 `ac_max_*` 家族的命名：上面那格的
            # docstring 已警告過 `ac_max_*` 的語意是**方案上限**；用量若也叫 max 會被誤讀。
            #
            # 🔴 由後端算、⛔ 不讓前端自己數：detail payload 另有 `access.access_policies`
            # 原始陣列，前端若拿它重算就得把「只計 email_list」「strip+lower 折疊」
            # 「跨 policy 去重」整套規則在瀏覽器再實作一次 ＝ 長出**第三把尺**。
            # 這裡直接走守衛與 gate 共用的那把（`_seat_usage_for_anchor`）。
            'ac_used_access_seats': len(
                self.env['woow_paas_platform.security_access_access_policy']
                    ._seat_usage_for_anchor(self)),
            # 2026-10-10：「此刻在試用期內」，不是上游 sh_trial（＝方案有試用，試用結束後不會被清）。
            'is_trial': sub._woow_in_trial(),
            'trial_end_date': sub.sh_trial_end_date.isoformat() if sub.sh_trial_end_date else None,
            'start_date': sub.sh_start_date.isoformat() if sub.sh_start_date else None,
            'end_date': sub.sh_end_date.isoformat() if sub.sh_end_date else None,
            'next_payment': sub.sh_date_of_next_payment.isoformat() if sub.sh_date_of_next_payment else None,
            'last_payment_status': sub.sh_last_payment_status if hasattr(sub, 'sh_last_payment_status') else None,
        }

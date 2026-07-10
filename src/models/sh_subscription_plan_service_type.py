from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ShSubscriptionPlanServiceType(models.Model):
    """三大類 plan taxonomy discriminator（#330，OQ-A 拍板：3 值）。

    Add-only pattern（比照 sa_quota／sh_mcp，不覆寫 std 欄位）。MCP 類
    （Odoo/n8n MCP、Hermes）歸 cloud_service——MCP-ness 由
    cloud_app_template.category='mcp' 承載，不設第 4 值（拍板記錄見
    docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md §3.1）。

    為何「存」而非從 template M2M 衍生：orphan plan（AnythingLLM 2 筆，
    無任何 template record）反查不出來；backfill 與前端 filter 需要純量。
    一致性由既有三條 template 互斥 constraint＋下方條件式 guard 兜底。
    """

    _inherit = 'sh.subscription.plan'

    # xmlid（template model）→ 該 family 對應的 service_type 值
    _TEMPLATE_FAMILY = {
        'woow_paas_platform.cloud_app_template': 'cloud_service',
        'woow_paas_platform.smart_home_template': 'smart_home',
        'woow_paas_platform.security_access_template': 'security_access',
    }

    service_type = fields.Selection(
        [('cloud_service', 'Cloud Service'),
         ('smart_home', 'Smart Home'),
         ('security_access', 'Security Access')],
        string='Service Type',
        help='Which of the three service families this plan belongs to. '
             'MCP offerings (Odoo/n8n MCP, Hermes) are cloud_service — '
             'MCP-ness is carried by cloud_app_template.category, not by '
             'a fourth enum value.',
    )

    @api.constrains('service_type')
    def _check_service_type_matches_template_family(self):
        """條件式 guard（縱深防禦）：存下的 service_type 必須與 owning
        template family 一致；orphan plan（無任何 template 綁定，如
        AnythingLLM placeholder）放行，否則 backfill 會炸。

        主保證仍是三 create-flow 各掛自家 template＋既有三條互斥
        constraint；本 guard 抓 backfill 寫錯值、開發者手改、未來 seed
        掛錯 family。
        """
        for plan in self:
            families = {
                st for model_name, st in self._TEMPLATE_FAMILY.items()
                if self.env[model_name].sudo().search_count(
                    [('subscription_plan_ids', 'in', plan.id)])
            }
            if not families:
                continue  # orphan plan → 放行
            if plan.service_type and plan.service_type not in families:
                raise ValidationError(
                    "Plan '%s' 的 service_type=%s 與其 owning template "
                    "family %s 不一致。plan 分類必須跟隨掛載它的 template "
                    "家族（cloud_app→cloud_service／smart_home→smart_home／"
                    "security_access→security_access）。"
                    % (plan.name, plan.service_type, sorted(families))
                )

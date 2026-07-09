from odoo import fields, models


class ShSubscriptionPlanShMcp(models.Model):
    """Smart Home MCP capability on subscription plans.

    Follows the odoo-std-field-no-override pattern (add-only), mirroring
    sh_subscription_plan_sa_quota. The value NAMES which HA MCP integration
    the plan grants, so the HA component can branch on it. Empty = no MCP.
    """

    _inherit = 'sh.subscription.plan'

    mcp_integration = fields.Char(
        string='SH MCP Integration',
        default='',
        help="Smart Home: which HA MCP integration this plan grants "
             "(e.g. 'ha_mcp_tools'). Empty = no MCP. Read by the HA "
             "woow_paas_smart_home component to decide whether to expose "
             "the MCP sensor entity.",
    )

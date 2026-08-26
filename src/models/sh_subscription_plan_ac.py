from odoo import fields, models


class ShSubscriptionPlanAc(models.Model):
    """Access Control（AC）跨類加值能力欄位（#330，OQ-B 拍板定名）。

    Add-only pattern。ac_ 前綴＝capability-orthogonal（跨三類，OQ-C 拍板
    不綁 service_type——三大類服務皆有 Cloudflare Application route，
    加值刻意跨類）。

    語意守線（不可漂移）：本欄位是「上限／天花板（MAX）」、**不是**
    「內含席數」——定價＝內含 M=0＋在其上加購（500/seat/月）。seed 一律
    不寫此欄（全部 default 0）。

    Enforcement 已接線（#521）：``security_access_access_policy.py`` 的
    ``_check_access_seat_quota`` 拿本欄位當上限，與跨**該**訂閱全部 route
    去重後的 ``email_list`` 位址數比較（計數範圍是單一訂閱內，不跨訂閱；
    權威措辭見該檔 docstring），超過即擋寫入；0 ＝一席都不給，不是
    unlimited。
    """

    _inherit = 'sh.subscription.plan'

    ac_max_access_seats = fields.Integer(
        string='AC Max Access Seats',
        default=0,
        help='Access Control: cap on the number of distinct email_list '
             'addresses across every route of this subscription = 0 plus the '
             'AC seats purchased (500/seat/month). MAX/ceiling semantics, NOT '
             'an included seat count; 0 = no seats purchased (blocks the '
             'first address, not unlimited). Enforcement is wired in '
             'security_access_access_policy.py::_check_access_seat_quota '
             '(#521).',
    )

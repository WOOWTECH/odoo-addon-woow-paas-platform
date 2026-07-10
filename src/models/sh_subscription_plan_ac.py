from odoo import fields, models


class ShSubscriptionPlanAc(models.Model):
    """Access Control（AC）跨類加值能力欄位（#330，OQ-B 拍板定名）。

    Add-only pattern。ac_ 前綴＝capability-orthogonal（跨三類，OQ-C 拍板
    不綁 service_type——三大類服務皆有 Cloudflare Application route，
    加值刻意跨類）。

    語意守線（不可漂移）：本欄位是「上限／天花板（MAX）」、**不是**
    「內含席數」——定價＝內含 M=0＋在其上加購（500/seat/月）。seed 一律
    不寫此欄（全部 default 0）；「0＋已購 AC 席數」的執行期疊加與
    enforcement 接線屬 OQ-D carrier 實作（follow-up），seed 值本身不足以
    表達 MAX 語意。
    """

    _inherit = 'sh.subscription.plan'

    ac_max_access_seats = fields.Integer(
        string='AC Max Access Seats',
        default=0,
        help='Access Control：跨該訂閱全部 route 的 email_list 去重位址數'
             '上限＝0＋已購 AC 席數（500/seat/月）。MAX/上限語意、非內含'
             '席數；0＝未加購任何席位。enforcement 於 OQ-D carrier 落地後'
             '接線（欄位先行佔位）。',
    )

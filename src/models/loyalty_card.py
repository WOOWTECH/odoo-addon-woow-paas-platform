"""Extend 原生 loyalty.card：禮品碼 → eWallet 兌換核心。

Smart Home「買主機半年免費」epic Phase 1。
設計依據：docs/plans/2026-Q2/2026-06-25-smart-home-half-year-gift-points-{design,implementation}.md

兌換 = 對該 card row FOR UPDATE NOWAIT 鎖（防跨 request double-redeem，本 repo 首例
row-lock；風格對齊 agent_session._backfill_reaped_since_orphans 的 raw cr.execute、
NOWAIT 語意同 odoo ir_sequence.py:59 的單次性）→ validate → partner.add_wallet_points
灌進 ewallet 錢包 → 作廢卡（points=0 即「已用」，loyalty.card 無 state 欄位）。
"""
import psycopg2

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class GiftRedeemError(ValidationError):
    """ValidationError 帶機器可讀 code，供 endpoint 穩定分流（免比對 i18n 訊息）。

    code 是穩定 ASCII（already_used / expired / inactive / not_a_gift_card）。
    endpoint 經 getattr(e, 'code', ...) 取用、轉成 API 回應的 error code，避免對
    經 _() 翻譯的訊息字串做 substring 比對（在非 en 語系會失效）。
    因 GiftRedeemError ⊂ ValidationError ⊂ UserError，既有 assertRaises 守線不變。
    """

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class LoyaltyCard(models.Model):
    _inherit = 'loyalty.card'

    @api.model
    def _find_redeemable(self, code):
        """依 code 找一張 gift_card program 的卡（查不到回空 recordset）。

        只依 code + program_type='gift_card' 過濾，**不**在查詢就排除
        已用 / 過期 / inactive——那些交給 _redeem_to_wallet 的 validate 回明確
        錯誤（already_used / expired），而非一律「查無此卡」。

        .sudo()：portal user 對 loyalty.card 無 ORM 讀權，且查的是平台發放的
        gift_card 卡（非使用者自己的 ewallet 錢包卡）。
        """
        if not code:
            return self.browse()
        return self.sudo().search([
            ('code', '=', code),
            ('program_id.program_type', '=', 'gift_card'),
        ], limit=1)

    def _redeem_to_wallet(self, partner):
        """把本 gift 卡的點數兌進 partner 的 eWallet 錢包，並作廢本卡。原子操作。

        回傳實際入帳的點數（float）。失敗以 ValidationError（業務分流）/
        UserError（鎖衝突）拋出，由呼叫端（Phase 2 controller）轉成回應。

        ⚠️ 安全契約：本 method **不**驗證 partner 是否等於呼叫者身分——它無條件
        把點數灌給傳入的 partner。呼叫端（Phase 2 endpoint）**必須**令
        partner = request.env.user.partner_id，**絕不可**讓 client 指定 partner，
        否則攻擊者可把禮品碼兌進任意他人錢包（mass-assign 提權）。
        勿從 partner 可被攻擊者控制的情境呼叫。
        """
        self.ensure_one()

        # --- (1) row-lock：防跨 request double-redeem ---
        # 鎖本卡 row，NOWAIT → 並行第二位兌換者立即被拒（而非排隊等鎖），
        # 語意同 ir_sequence 單次性。鎖在 transaction commit/rollback 自動釋放，
        # 後續任何 raise 都會連同釋鎖，無孤兒鎖。
        try:
            self.env.cr.execute(
                "SELECT id FROM loyalty_card WHERE id = %s FOR UPDATE NOWAIT",
                (self.id,),
            )
        except psycopg2.errors.LockNotAvailable:
            raise UserError(_(
                "This gift card is being redeemed by another request. "
                  "Please retry."
            ))
        # 鎖只擋並發 writer，不刷新本 env 的 record cache → 重讀再 validate 才原子。
        self.invalidate_recordset(
            ['points', 'partner_id', 'active', 'expiration_date', 'program_id'],
        )

        # --- (2) validate（GiftRedeemError 帶 code 供 endpoint 穩定分流）---
        if self.program_id.program_type != 'gift_card':
            raise GiftRedeemError(_("Not a gift card."), 'not_a_gift_card')
        if self.points <= 0:
            raise GiftRedeemError(_("Gift card already used."), 'already_used')
        if not self.active:
            raise GiftRedeemError(_("Gift card is inactive."), 'inactive')
        if self.expiration_date and self.expiration_date < fields.Date.today():
            raise GiftRedeemError(_("Gift card expired."), 'expired')

        # --- (3) 灌點（官方 API，必 sudo + 必 invalidate wallet_balance 快取） ---
        amount = self.points
        # ledger description 用純字串（對齊 repo 既有 wallet description 慣例，如
        # wallet_topup / change_subscription_plan_wizard 皆不包 _()），讓稽核文字
        # 不隨兌換當下語系飄移；user-facing 的 Error 訊息才包 _() 翻譯。
        partner.sudo().add_wallet_points(
            amount,
            description="Gift card redeemed: %s" % self.code,
        )
        partner.invalidate_recordset(['wallet_balance'])

        # --- (4) 作廢卡（points=0 == 已用；記錄兌換者供稽核）。
        #         本卡已被 (1) FOR UPDATE 鎖住，可安全 write。 ---
        self.sudo().write({'points': 0.0, 'partner_id': partner.id})
        return amount

"""忘記密碼請求的排隊表（商用驗收 X-SEC-007-1／X-SEC-007-2）。

## 為什麼要排隊

原生 ``/web/reset_password`` 在請求當下查帳號並**同步寄信**：

* 帳號不存在 ⇒ 回「No account found for this login」，0.1–0.3 s；
* 帳號存在 ⇒ 回「已寄出」，2–6 s（SMTP 往返）。

文字與耗時兩條都能拿來枚舉註冊信箱。只把文字統一還不夠——耗時仍會洩露。所以請求路徑只做
「正規化 → 寫一列 → 叫 cron 起來」，不論帳號存不存在、是不是 email 都走同一條路、花同樣的
時間；查帳號與寄信都移到 ``_cron_process_requests``。

## 節流

* 同一個登入帳號（以雜湊比對）在 ``_WINDOW`` 內最多排 ``_QUEUE_LIMIT_PER_WINDOW`` 列，
  超過就不再寫入（回應仍相同）——表不會被灌爆。
* 同一個登入帳號在 ``_WINDOW`` 內最多真的寄 ``_SEND_LIMIT_PER_WINDOW`` 封，其餘標
  ``throttled``——受害者不會被洗信。
* 以 IP 為單位的限流**不在這裡**：平台 Odoo 前面是 Cloudflare Tunnel，Odoo 看到的對端
  位址不一定是使用者（``controllers/llm.py`` ``_peer_ip`` 也因此不採信轉送標頭）。per-IP
  限流交給 Cloudflare rate limiting 規則（見 PR 說明）。

## 隱私

明文 ``login`` 只活到 cron 處理完（處理後清空），之後只留 ``login_key``（SHA-256）供節流
計數；整列在 ``_RETENTION_DAYS`` 天後刪除。租戶零 ACL，只有系統管理員可讀。
"""
import hashlib
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PasswordResetRequest(models.Model):
    _name = 'woow_paas_platform.password_reset_request'
    _description = 'Queued Password Reset Request'
    _order = 'id'

    _WINDOW = timedelta(hours=1)
    _QUEUE_LIMIT_PER_WINDOW = 10
    _SEND_LIMIT_PER_WINDOW = 3
    _RETENTION_DAYS = 2
    _BATCH_SIZE = 200

    login = fields.Char(help='Normalized login as typed; cleared once processed.')
    login_key = fields.Char(required=True, index=True,
                            help='SHA-256 of the lower-cased login, kept for throttling.')
    state = fields.Selection([
        ('pending', 'Pending'),
        ('sent', 'Reset Email Sent'),
        ('no_account', 'No Matching Account'),
        ('throttled', 'Throttled'),
        ('failed', 'Failed'),
    ], required=True, default='pending', index=True)
    processed_at = fields.Datetime()

    @api.model
    def _woow_login_key(self, login):
        return hashlib.sha256((login or '').strip().lower().encode('utf-8')).hexdigest()

    @api.model
    def _woow_enqueue(self, login):
        """請求路徑的唯一動作。刻意不查 ``res.users``——查了就會因帳號存不存在而有不同耗時。"""
        login = (login or '').strip()[:254]
        if not login:
            return
        Request = self.sudo()
        key = self._woow_login_key(login)
        since = fields.Datetime.now() - self._WINDOW
        if Request.search_count([('login_key', '=', key), ('create_date', '>=', since)],
                                limit=self._QUEUE_LIMIT_PER_WINDOW) < self._QUEUE_LIMIT_PER_WINDOW:
            Request.create({'login': login, 'login_key': key})
        cron = self.env.ref('woow_paas_platform.ir_cron_password_reset_requests',
                            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def _woow_signup_token_is_valid(self, token):
        """``signup_email`` 只在帶著有效邀請／重設 token 時才可觸發「導去登入」捷徑。"""
        if not token:
            return False
        return bool(self.env['res.partner'].sudo()._signup_retrieve_partner(
            token, check_validity=True))

    @api.model
    def _woow_find_user(self, login):
        """同原生 ``res.users.reset_password``：先比登入帳號、再比 email；必須恰好一個。"""
        Users = self.env['res.users'].sudo()
        users = Users.search(Users._get_login_domain(login))
        if not users:
            users = Users.search(Users._get_email_domain(login))
        return users if len(users) == 1 else Users

    @api.model
    def _cron_process_requests(self):
        Request = self.sudo()
        now = fields.Datetime.now()
        since = now - self._WINDOW
        for row in Request.search([('state', '=', 'pending')], limit=self._BATCH_SIZE):
            sent_recently = Request.search_count([
                ('login_key', '=', row.login_key), ('state', '=', 'sent'),
                ('processed_at', '>=', since)])
            if sent_recently >= self._SEND_LIMIT_PER_WINDOW:
                state = 'throttled'
            else:
                user = self._woow_find_user(row.login)
                if not user:
                    state = 'no_account'
                else:
                    try:
                        with self.env.cr.savepoint():
                            user.action_reset_password()
                        state = 'sent'
                    except Exception:  # noqa: BLE001 — 單列失敗不可卡住整批
                        _logger.exception(
                            'Queued password reset failed for user #%s (request #%s)',
                            user.id, row.id)
                        state = 'failed'
            row.write({'state': state, 'processed_at': fields.Datetime.now(),
                           'login': False})
        Request.search([
            ('create_date', '<', now - timedelta(days=self._RETENTION_DAYS)),
            ('state', '!=', 'pending'),
        ]).unlink()

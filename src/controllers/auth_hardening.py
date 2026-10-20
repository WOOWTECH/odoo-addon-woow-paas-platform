"""原生 ``/web/reset_password``、``/web/signup`` 的帳號枚舉修補（商用驗收 X-SEC-007-1／-2）。

這是 CLAUDE.md「不在 /web 原生路由開新曝光點」的反方向：不新增頁面，只把原生頁既有的
洩露關掉。

1. **忘記密碼（無 token 的 POST）**：原生在請求當下查帳號、同步寄信，回應文字（No account
   found／已寄出）與耗時（0.1 s／2–6 s）都會洩露帳號是否存在。改成不論輸入什麼——存在、
   不存在、不是 email——都只排進 ``woow_paas_platform.password_reset_request`` 並回同一句話；
   查帳號與寄信由 cron 非同步處理（含每個帳號的寄信節流）。
2. **``?signup_email=<信箱>``**：原生在 GET 時若信箱已註冊就 303 到
   ``/web/login?login=<信箱>``，不需要任何憑證就能枚舉。這個捷徑原本是給「已啟用的邀請連結」
   用的，所以只在帶著**有效** token 時保留，其餘情況直接忽略該參數。

帶 token 的重設（真的設定新密碼）與註冊 POST 照原生流程；密碼強度由
``models/res_users_password_policy.py`` 在 ``signup()`` 擋。
"""
import logging

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.auth_signup.controllers.main import AuthSignupHome

_logger = logging.getLogger(__name__)


class WoowAuthEnumerationGuard(AuthSignupHome):

    def _woow_drop_unverified_signup_email(self, kw):
        if 'signup_email' not in request.params and 'signup_email' not in kw:
            return
        token = request.params.get('token') or request.session.get('auth_signup_token')
        if request.env['woow_paas_platform.password_reset_request']._woow_signup_token_is_valid(token):
            return
        request.params.pop('signup_email', None)
        kw.pop('signup_email', None)

    @http.route()
    def web_auth_signup(self, *args, **kw):
        self._woow_drop_unverified_signup_email(kw)
        return super().web_auth_signup(*args, **kw)

    @http.route()
    def web_auth_reset_password(self, *args, **kw):
        self._woow_drop_unverified_signup_email(kw)
        qcontext = self.get_auth_signup_qcontext()
        if (request.httprequest.method != 'POST' or qcontext.get('token')
                or 'error' in qcontext or not qcontext.get('reset_password_enabled')):
            return super().web_auth_reset_password(*args, **kw)

        try:
            if not request.env['ir.http']._verify_request_recaptcha_token('password_reset'):
                raise UserError(_("Suspicious activity detected by Google reCaptcha."))
            login = (qcontext.get('login') or '').strip()
            if not login:
                raise UserError(_("Please enter your login email."))
            _logger.info("Password reset request queued from %s",
                         request.httprequest.remote_addr)
            request.env['woow_paas_platform.password_reset_request']._woow_enqueue(login)
            qcontext['message'] = _(
                "If an account exists for this login, password reset instructions will be "
                "sent to its email address.")
        except UserError as e:
            qcontext['error'] = e.args[0]

        response = request.render('auth_signup.reset_password', qcontext)
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
        return response

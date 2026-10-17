"""``/web/signup`` 伺服器端必填檢查（商用驗收 X-SET-007-5）。

原生 ``AuthSignupHome`` 只靠 HTML ``required``：繞過前端送空欄位時，空的
login／name 在 ``_create_user_from_template`` 丟 ``ValueError``（控制器沒接 ⇒
HTTP 500 英文頁），空密碼則一路走到 ``session.authenticate`` 才被擋成英文的
「Access Denied」。這裡在建帳號之前先回中文欄位錯誤；``UserError`` 由原生
``web_auth_signup``／``web_auth_reset_password`` 接住、顯示在表單上。

公開註冊（沒有 token）另外驗 email 格式（X-SET-007-3）。大小寫正規化與重複信箱
檢查在 ``res.users._signup_create_user``（``src/models/res_users_login_identity.py``），
所有建帳號路徑都會經過那裡；email 格式與密碼必填只放在這一層，因為 OAuth 等非表單
路徑本來就不帶密碼、也可能沒有 email 形式的 login。
"""
from odoo import _
from odoo.exceptions import UserError
from odoo.tools.mail import single_email_re

from odoo.addons.auth_signup.controllers.main import AuthSignupHome


class WoowAuthSignupHome(AuthSignupHome):

    def _prepare_signup_values(self, qcontext):
        if not qcontext.get('token'):
            login = (qcontext.get('login') or '').strip()
            if not login:
                raise UserError(_("請填寫電子郵件。"))
            if not single_email_re.match(login):
                raise UserError(_("請輸入有效的電子郵件地址（例如 name@example.com）。"))
            if not (qcontext.get('name') or '').strip():
                raise UserError(_("請填寫姓名。"))
        if not qcontext.get('password'):
            raise UserError(_("請設定密碼。"))
        return super()._prepare_signup_values(qcontext)

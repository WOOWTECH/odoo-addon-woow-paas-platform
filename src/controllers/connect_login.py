"""Use the existing Odoo sign-in flow with woowtech connect presentation."""
from urllib.parse import unquote, urlsplit

from odoo.addons.auth_signup.controllers.main import AuthSignupHome
from odoo.http import request, route


class ConnectLogin(AuthSignupHome):
    def _connect_context(self, redirect, title):
        target = urlsplit(unquote(redirect or '')).path
        connect = target.startswith(('/oauth2/authorize', '/device', '/woow'))
        if connect:
            request.update_context(lang='zh_TW', woow_connect_login=True,
                                   woow_connect_title=title)
        return connect

    @route()
    def web_login(self, redirect=None, **kw):
        connect = self._connect_context(redirect, '登入並連結智慧平台')
        response = super().web_login(redirect=redirect, **kw)
        if connect and getattr(response, 'qcontext', None) is not None:
            self._connect_context(redirect, '登入並連結智慧平台')
            # Keep the standard login, CSRF, OAuth providers and redirect. Only
            # this page's lazy-render locale/brand change; no user/company edits.
            if response.qcontext.get('error') == 'Wrong login/password':
                response.qcontext['error'] = '帳號或密碼錯誤，請重新輸入。'
        return response

    @route()
    def web_auth_signup(self, *args, **kw):
        redirect = kw.get('redirect') or request.params.get('redirect')
        self._connect_context(redirect, '建立 woowtech 帳號')
        response = super().web_auth_signup(*args, **kw)
        self._connect_context(redirect, '建立 woowtech 帳號')
        return response

    @route()
    def web_auth_reset_password(self, *args, **kw):
        redirect = kw.get('redirect') or request.params.get('redirect')
        self._connect_context(redirect, '重設帳號密碼')
        response = super().web_auth_reset_password(*args, **kw)
        self._connect_context(redirect, '重設帳號密碼')
        return response

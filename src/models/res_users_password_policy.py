"""自助設定密碼的強度下限（商用驗收 X-SET-007-1）。

正式站實測：``/web/signup`` 用 ``123`` 註冊成功、``/my/security`` 也接受把密碼改成 ``123``。
平台 Odoo 沒裝 ``auth_password_policy``，原生完全不檢查。

## 為什麼不直接 depends ``auth_password_policy``

它把檢查掛在 ``res.users._set_password``——**所有**寫入密碼的路徑（含後台建立使用者、
資料匯入、測試 fixture）都會被擋，而且 ``data/defaults.xml`` 以 ``forcecreate`` 把
``auth_password_policy.minlength`` 設成 8，是**整個 Odoo 的全域預設**。這套 Odoo 同時供
官方網站與 ERP 使用（CLAUDE.md「不改全域預設，在本 addon 層隔離」），本 repo 的測試也有
大量短密碼 fixture。所以這裡只擋「使用者自己設定密碼」的入口：

* ``signup()``：``/web/signup`` 自助註冊、邀請連結與「忘記密碼」連結（帶 token）設定密碼
  都經這裡。OAuth（Google 登入）首次建帳也經這裡但**不帶密碼**——不帶就不檢查。
* ``_change_password()``：``/my/security``（portal ``change_password``）、後台「變更我的
  密碼」精靈、管理員的「變更密碼」精靈最後都經這裡。

## 規則（NIST SP 800-63B 精神：長度＋黑名單，不強迫大小寫／符號組合）

1. 至少 ``PASSWORD_MIN_LENGTH`` 個字元。
2. 不可是常見密碼、同一字元重複、連續遞增／遞減字元（``12345678``、``abcdefgh``）。
3. 不可與登入帳號相同，或與信箱 ``@`` 前段相同（不分大小寫）。
"""
from odoo import _, api, models
from odoo.exceptions import UserError

PASSWORD_MIN_LENGTH = 8

# 刻意只放「長度已達 8 仍極常見」的值；短於 8 的由長度規則擋。比對前先 lower()。
_COMMON_PASSWORDS = frozenset({
    'password', 'password1', 'password12', 'password123', 'passw0rd', 'p@ssw0rd',
    '12345678', '123456789', '1234567890', '0123456789', '87654321', '11223344',
    '12341234', '12121212', '11111111', '00000000', '88888888', '66666666',
    'qwertyui', 'qwertyuiop', 'qwerty12', 'qwerty123', 'asdfghjk', 'asdfghjkl',
    'zxcvbnm1', '1qaz2wsx', '1q2w3e4r', '1q2w3e4r5t', 'q1w2e3r4', 'abc12345',
    'abcd1234', 'a1234567', 'aa123456', 'iloveyou', 'sunshine', 'princess',
    'football', 'baseball', 'superman', 'welcome1', 'admin123', 'administrator',
    'changeme', 'letmein1', 'trustno1', 'woowtech', 'woowtech123',
})


def _is_monotonic_run(password):
    """整串都是 +1 或 -1 的連續字元（``abcdefgh``、``98765432``）。"""
    if len(password) < 3:
        return False
    steps = {ord(b) - ord(a) for a, b in zip(password, password[1:])}
    return steps in ({1}, {-1})


def password_strength_problem(password, login=None):
    """回傳違反的規則代碼（``'length'``／``'guessable'``／``'login'``），合格回 ``None``。

    空密碼交給原生處理（原生會擋「不可設定空密碼」）。訊息文字在 model method 裡翻譯——
    ``_()`` 要從呼叫端 frame 的 ``self.env`` 取語系，放在模組層函式會拿不到。
    """
    if not password:
        return None
    if len(password) < PASSWORD_MIN_LENGTH:
        return 'length'
    lowered = password.lower()
    if (lowered in _COMMON_PASSWORDS or len(set(lowered)) == 1
            or _is_monotonic_run(lowered)):
        return 'guessable'
    if isinstance(login, str) and login.strip():
        login_l = login.strip().lower()
        if lowered in {login_l, login_l.split('@', 1)[0]}:
            return 'login'
    return None


class ResUsersPasswordPolicy(models.Model):
    _inherit = 'res.users'

    @api.model
    def _woow_check_password_strength(self, password, login=None):
        problem = password_strength_problem(password, login)
        if problem == 'length':
            raise UserError(_("Your password must be at least %(min)s characters long.",
                              min=PASSWORD_MIN_LENGTH))
        if problem == 'guessable':
            raise UserError(_("This password is too easy to guess. Please choose a different one."))
        if problem == 'login':
            raise UserError(_("Your password must not be the same as your login or email."))

    @api.model
    def signup(self, values, token=None):
        password = values.get('password')
        if password:
            login = values.get('login')
            if not login and token:
                partner = self.env['res.partner'].sudo()._signup_retrieve_partner(token)
                login = (partner.user_ids[:1].login or partner.email) if partner else None
            self._woow_check_password_strength(password, login)
        return super().signup(values, token=token)

    def _change_password(self, new_passwd):
        for user in self:
            self._woow_check_password_strength((new_passwd or '').strip(), user.login)
        return super()._change_password(new_passwd)

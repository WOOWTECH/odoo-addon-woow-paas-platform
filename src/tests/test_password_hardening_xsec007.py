"""商用驗收 X-SET-007-1／X-SEC-007-1／X-SEC-007-2：密碼強度、忘記密碼不洩露帳號、重設請求節流。

正式站實測（2026-10-16）：

* X-SET-007-1：``/web/signup`` 用 ``123`` 註冊成功、``/my/security`` 也接受把密碼改成 ``123``。
* X-SEC-007-1：``/web/reset_password`` 對不存在的帳號回「No account found for this login」
  （0.1–0.3 s），存在的帳號回「已寄出」（2–6 s，含同步寄信）⇒ 文字與耗時都能枚舉帳號；
  另外 ``?signup_email=<信箱>`` 對已註冊信箱會 303 到 ``/web/login?login=<信箱>``，GET 就能枚舉。
* X-SEC-007-2：忘記密碼請求完全沒有節流，同一個信箱可以被連續寄信。

本檔每個 class 對應一個修補面；HttpCase 走真的 controller，不只測 ORM。
"""
import re
import uuid
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged

STRONG = 'Correct-Horse-7391'
GENERIC_RESET_MESSAGE = (
    "If an account exists for this login, password reset instructions will be "
    "sent to its email address.")


def _portal_user(env, suffix, password=STRONG, **extra):
    return env['res.users'].with_context(no_reset_password=True).create(dict({
        'login': f'xsec007-{suffix}@example.com',
        'email': f'xsec007-{suffix}@example.com',
        'name': f'XSEC007 {suffix}',
        'password': password,
        'groups_id': [(6, 0, [env.ref('base.group_portal').id])],
    }, **extra))


@tagged('post_install', '-at_install')
class TestPasswordStrengthXSet007(TransactionCase):
    """X-SET-007-1：自助註冊、憑 token 重設、自己改密碼都要擋弱密碼。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param('auth_signup.invitation_scope', 'b2c')
        cls.suffix = uuid.uuid4().hex[:8]

    @contextmanager
    def assertWeak(self):
        """AccessError／SignupError 也是 UserError 的子類——只認「強度規則」丟的那個。"""
        with self.assertRaises(UserError) as cm:
            yield
        self.assertIs(type(cm.exception), UserError, cm.exception)
        self.assertRegex(str(cm.exception), r'(?i)password')

    def _signup(self, password, login=None):
        login = login or f'xsec007-signup-{uuid.uuid4().hex[:8]}@example.com'
        return self.env['res.users'].sudo().signup(
            {'login': login, 'name': 'Signup Probe', 'password': password})

    def test_signup_rejects_short_password(self):
        with self.assertWeak():
            self._signup('123')

    def test_signup_rejects_common_and_trivial_passwords(self):
        for weak in ('12345678', 'password', 'aaaaaaaaaa', 'abcdefgh', '87654321', 'Qwerty123'):
            with self.subTest(password=weak), self.assertWeak():
                self._signup(weak)

    def test_signup_rejects_password_equal_to_login(self):
        login = f'probe{uuid.uuid4().hex[:6]}@example.com'
        with self.assertWeak():
            self._signup(login.upper(), login=login)
        with self.assertWeak():
            self._signup(login.split('@')[0], login=login)

    def test_signup_accepts_strong_password(self):
        login, _password = self._signup(STRONG)
        self.assertTrue(self.env['res.users'].sudo().search([('login', '=', login)]))

    def test_signup_without_password_is_not_blocked(self):
        """OAuth（Google 登入）首次建帳走 ``signup()`` 但不帶密碼——不能被強度檢查擋掉。"""
        login = f'xsec007-oauth-{uuid.uuid4().hex[:8]}@example.com'
        self.env['res.users'].sudo().signup({'login': login, 'name': 'OAuth Probe'})
        self.assertTrue(self.env['res.users'].sudo().search([('login', '=', login)]))

    def test_token_reset_rejects_weak_password(self):
        user = _portal_user(self.env, self.suffix + 'tok')
        user.partner_id.sudo().signup_prepare(signup_type='reset')
        token = user.partner_id.sudo()._generate_signup_token()
        with self.assertWeak():
            self.env['res.users'].sudo().signup({'password': '123'}, token)

    def test_change_own_password_rejects_weak_password(self):
        """``/my/security`` 與後台「變更密碼」最後都經 ``_change_password``。"""
        user = _portal_user(self.env, self.suffix + 'chg')
        own = user.with_user(user).env.user  # 原生 change_password() 也是經 env.user（sudo）呼叫
        with self.assertWeak():
            own._change_password('123')
        own._change_password('Another-Strong-Pass-42')


@tagged('post_install', '-at_install')
class TestPasswordResetQueueXSec007(TransactionCase):
    """X-SEC-007-1／-2：請求只排隊（不查帳號、不寄信）；cron 才查帳號並寄信，且每個登入帳號限流。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Request = cls.env['woow_paas_platform.password_reset_request']
        cls.user = _portal_user(cls.env, uuid.uuid4().hex[:8])

    def _rows(self, login):
        return self.Request.sudo().search([('login_key', '=', self.Request._woow_login_key(login))])

    def test_enqueue_does_not_touch_the_account_synchronously(self):
        with patch.object(type(self.env['res.users']), '_action_reset_password',
                          autospec=True) as spy:
            self.Request._woow_enqueue(self.user.login)
            self.Request._woow_enqueue('nobody-%s@example.com' % uuid.uuid4().hex[:6])
            self.Request._woow_enqueue('not-an-email')
        spy.assert_not_called()
        rows = self._rows(self.user.login)
        self.assertEqual(rows.mapped('state'), ['pending'])

    def test_cron_sends_reset_for_existing_account(self):
        self.Request._woow_enqueue('  %s ' % self.user.login)
        self.Request._cron_process_requests()
        row = self._rows(self.user.login)
        self.assertEqual(row.state, 'sent')
        self.assertFalse(row.login, '處理完就清掉明文登入帳號，只留雜湊')
        self.assertEqual(self.user.partner_id.sudo().signup_type, 'reset')

    def test_cron_matches_account_by_email_too(self):
        other = _portal_user(self.env, uuid.uuid4().hex[:8],
                             login='xsec007-login-%s' % uuid.uuid4().hex[:6])
        self.Request._woow_enqueue(other.email)
        self.Request._cron_process_requests()
        self.assertEqual(self._rows(other.email).state, 'sent')

    def test_cron_unknown_login_sends_nothing(self):
        login = 'nobody-%s@example.com' % uuid.uuid4().hex[:6]
        with patch.object(type(self.env['res.users']), '_action_reset_password',
                          autospec=True) as spy:
            self.Request._woow_enqueue(login)
            self.Request._cron_process_requests()
        spy.assert_not_called()
        self.assertEqual(self._rows(login).state, 'no_account')

    def test_cron_throttles_repeated_resets_per_login(self):
        cap = self.Request._SEND_LIMIT_PER_WINDOW
        for _i in range(cap + 2):
            self.Request._woow_enqueue(self.user.login)
        with patch.object(type(self.env['res.users']), '_action_reset_password',
                          autospec=True) as spy:
            self.Request._cron_process_requests()
        self.assertEqual(spy.call_count, cap)
        states = self._rows(self.user.login).mapped('state')
        self.assertEqual(states.count('sent'), cap)
        self.assertEqual(states.count('throttled'), 2)

    def test_enqueue_is_capped_per_login(self):
        cap = self.Request._QUEUE_LIMIT_PER_WINDOW
        for _i in range(cap + 5):
            self.Request._woow_enqueue(self.user.login)
        self.assertEqual(len(self._rows(self.user.login)), cap)

    def test_cron_purges_old_rows(self):
        self.Request._woow_enqueue(self.user.login)
        self.Request._cron_process_requests()
        row = self._rows(self.user.login)
        old = fields.Datetime.now() - timedelta(days=self.Request._RETENTION_DAYS + 1)
        self.env.cr.execute(
            "UPDATE woow_paas_platform_password_reset_request SET create_date = %s WHERE id = %s",
            (old, row.id))
        self.Request.invalidate_model()
        self.Request._cron_process_requests()
        self.assertFalse(row.exists())


@tagged('post_install', '-at_install')
class TestPasswordResetEndpointXSec007(HttpCase):
    """走真的 ``/web/reset_password``、``/web/signup``：回應不得因帳號存不存在而不同。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        icp = cls.env['ir.config_parameter'].sudo()
        icp.set_param('auth_signup.reset_password', 'True')
        icp.set_param('auth_signup.invitation_scope', 'b2c')
        cls.user = _portal_user(cls.env, uuid.uuid4().hex[:8])
        cls.Request = cls.env['woow_paas_platform.password_reset_request']

    def _csrf(self, path):
        resp = self.url_open(path, timeout=20)
        self.assertEqual(resp.status_code, 200)
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        self.assertTrue(match, 'reset page must carry a csrf token')
        return match.group(1)

    def _post_reset(self, login):
        return self.url_open('/web/reset_password', data={
            'login': login, 'csrf_token': self._csrf('/web/reset_password')}, timeout=20)

    def test_same_answer_for_existing_unknown_and_non_email_logins(self):
        logins = [self.user.login, 'nobody-%s@example.com' % uuid.uuid4().hex[:6], 'not-an-email']
        with patch.object(type(self.env['res.users']), '_action_reset_password',
                          autospec=True) as spy:
            responses = [self._post_reset(login) for login in logins]
        spy.assert_not_called()  # 寄信不在請求路徑上 ⇒ 耗時不再洩露帳號是否存在
        for login, resp in zip(logins, responses):
            with self.subTest(login=login):
                self.assertEqual(resp.status_code, 200)
                self.assertIn(GENERIC_RESET_MESSAGE, resp.text)
                self.assertNotIn('No account found', resp.text)
                self.assertNotIn('Multiple accounts', resp.text)
        key = self.Request._woow_login_key(self.user.login)
        self.assertEqual(
            self.Request.sudo().search([('login_key', '=', key)]).mapped('state'), ['pending'])

    def test_signup_email_param_does_not_reveal_registered_accounts(self):
        for path in ('/web/reset_password', '/web/signup'):
            with self.subTest(path=path):
                resp = self.url_open(
                    '%s?signup_email=%s' % (path, self.user.email),
                    allow_redirects=False, timeout=20)
                self.assertEqual(resp.status_code, 200, '已註冊信箱不可再被 303 導去 /web/login')
                self.assertNotIn('/web/login?login=', resp.headers.get('Location', ''))

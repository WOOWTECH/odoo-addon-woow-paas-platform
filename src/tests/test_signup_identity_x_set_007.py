"""註冊身分缺口（商用驗收 X-SET-007-2／-3／-5、X-W-002-1）。

正式站實測（2026-10-16）：
- X-SET-007-2：已有 ``folight2015+qa-final-1-…@gmail.com``（uid 227），用全大寫
  版本再註冊 ⇒ 建出 uid 236 並登入（``UNIQUE(login)`` 區分大小寫）。
- X-W-002-1：之後邀請 uid 227 進工作區 ⇒ ``invite_identity_ambiguous``（``=ilike``
  同時命中兩個帳號）。
- X-SET-007-3：``login=not-an-email`` ⇒ 建出 uid 240 並登入。
- X-SET-007-5：繞過 HTML required 送空 login／name ⇒ HTTP 500 英文頁；空密碼 ⇒
  英文「Access Denied」。

只用合成帳號（``@example.invalid``），不寄信、不碰 operator。

⚠ 「例外之後 DB 應該長怎樣」的斷言一律用裸 try/except，不用 ``assertRaises``：
``assertRaises`` 的 savepoint 會把區塊內建出來的帳號一起回捲，讓「沒建帳號」恆綠
（``docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md``）。
"""
import re
import uuid

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger


def _addr(label):
    return 'xset007-%s-%s@example.invalid' % (label, uuid.uuid4().hex[:12])


def _enable_public_signup(env):
    env['ir.config_parameter'].sudo().set_param('auth_signup.invitation_scope', 'b2c')
    if 'website' in env:
        env['website'].sudo().search([]).write({'auth_signup_uninvited': 'b2c'})


def _make_user(env, login, password=None):
    return env['res.users'].with_context(
        no_reset_password=True, tracking_disable=True,
        mail_create_nosubscribe=True, mail_create_nolog=True,
    )._create_user_from_template({
        'name': 'XSET007 %s' % login.split('@')[0],
        'login': login, 'email': login.strip().lower(),
        'password': password or uuid.uuid4().hex,
    })


def _count_logins_ci(env, login):
    env.flush_all()
    env.cr.execute(
        "SELECT count(*) FROM res_users WHERE lower(login) = lower(%s)", [login.strip()])
    return env.cr.fetchone()[0]


@tagged('post_install', '-at_install', 'x_set_007_signup')
class TestSignupIdentityXSet007(TransactionCase):

    def setUp(self):
        super().setUp()
        _enable_public_signup(self.env)
        self.Users = self.env['res.users'].sudo()

    # --- ORM 寫入正規化 ---------------------------------------------------
    def test_create_strips_and_lowercases_login(self):
        raw = '  ' + _addr('Create').upper() + ' '
        user = _make_user(self.env, raw)
        self.assertEqual(user.login, raw.strip().lower())

    def test_write_strips_and_lowercases_login(self):
        user = _make_user(self.env, _addr('write'))
        new = _addr('Rewrite')
        user.write({'login': ' %s ' % new.upper()})
        self.assertEqual(user.login, new.lower())

    # --- 大小寫不敏感唯一性 -----------------------------------------------
    def _legacy_mixed_case_user(self, label):
        """模擬修補前就存在的大小寫混合帳號（ORM 寫入會被正規化，只能直接改表）。"""
        user = _make_user(self.env, _addr(label))
        legacy = 'Legacy.%s-%s@Example.Invalid' % (label, uuid.uuid4().hex[:8])
        self.env.flush_all()
        self.env.cr.execute("UPDATE res_users SET login = %s WHERE id = %s", [legacy, user.id])
        self.env.invalidate_all()
        return user, legacy

    def test_case_variant_duplicate_rejected_on_create(self):
        # 正規化之後與既有帳號完全相同 ⇒ 原生 UNIQUE(login)／website 檢查就會擋
        # （例外型別依是否裝 website 而不同，這裡只要求「擋下」）。
        login = _addr('dup')
        _make_user(self.env, login)
        with mute_logger('odoo.sql_db'), self.assertRaises(Exception):
            _make_user(self.env, login.upper())
        # 既有的是大小寫混合帳號（修補前資料）⇒ 只有不分大小寫的檢查擋得住。
        _user, legacy = self._legacy_mixed_case_user('dupc')
        with self.assertRaises(ValidationError):
            self.Users.with_context(no_reset_password=True).create({
                'name': 'XSET007 dup direct', 'login': legacy.lower(),
            })

    def test_case_variant_duplicate_rejected_on_write(self):
        _user, legacy = self._legacy_mixed_case_user('dupw')
        other = _make_user(self.env, _addr('other'))
        with self.assertRaises(ValidationError):
            other.write({'login': legacy.upper()})

    # --- 登入查找 -------------------------------------------------------
    def test_login_domain_matches_any_case(self):
        login = _addr('lookup')
        user = _make_user(self.env, login)
        found = self.Users.search(self.Users._get_login_domain(login.upper()))
        self.assertEqual(found, user)

    def test_legacy_mixed_case_login_still_resolves(self):
        user, legacy = self._legacy_mixed_case_user('legacy')
        found = self.Users.search(self.Users._get_login_domain(legacy))
        self.assertEqual(found, user)

    def test_legacy_case_duplicates_each_keep_their_own_login(self):
        # 修補前留下的重複對（正式站 uid 227 小寫／236 大寫）：各自用自己的寫法
        # 必須解析到自己，不能被另一個帳號搶走。
        lower_user = _make_user(self.env, _addr('pair'))
        upper_user, _legacy = self._legacy_mixed_case_user('pairup')
        upper_login = lower_user.login.upper()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE res_users SET login = %s WHERE id = %s", [upper_login, upper_user.id])
        self.env.invalidate_all()
        self.assertEqual(
            self.Users.search(self.Users._get_login_domain(upper_login)), upper_user)
        self.assertEqual(
            self.Users.search(self.Users._get_login_domain(lower_user.login)), lower_user)

    # --- 自助註冊（model 層） -------------------------------------------
    def _signup_expect_user_error(self, values, needle):
        try:
            self.Users.signup(dict(values))
        except UserError as exc:
            self.assertNotIsInstance(exc, ValidationError)
            self.assertIn(needle, str(exc.args[0]))
            return
        except Exception as exc:  # noqa: BLE001 — 原生丟 ValueError ⇒ 500
            self.fail('signup 應回中文 UserError，實際 %r' % (exc,))
        self.fail('signup 應被拒絕：%r' % (values,))

    def test_signup_rejects_empty_login_and_name_with_user_error(self):
        self._signup_expect_user_error(
            {'login': '', 'name': 'XSET007', 'password': uuid.uuid4().hex}, '電子郵件')
        login = _addr('noname')
        self._signup_expect_user_error(
            {'login': login, 'name': '  ', 'password': uuid.uuid4().hex}, '姓名')
        self.assertEqual(_count_logins_ci(self.env, login), 0)

    def test_signup_rejects_case_variant_duplicate(self):
        login = _addr('signupdup')
        _make_user(self.env, login)
        self._signup_expect_user_error(
            {'login': login.upper(), 'name': 'XSET007', 'password': uuid.uuid4().hex},
            '已經註冊')
        self.assertEqual(_count_logins_ci(self.env, login), 1)
        # uid 227／236 的形狀：既有帳號是大小寫混合，新註冊用另一種大小寫。
        _user, legacy = self._legacy_mixed_case_user('signuplegacy')
        self._signup_expect_user_error(
            {'login': legacy.lower(), 'name': 'XSET007', 'password': uuid.uuid4().hex},
            '已經註冊')
        self.assertEqual(_count_logins_ci(self.env, legacy), 1)

    def test_signup_normalizes_login_and_email(self):
        raw = ' ' + _addr('Mixed').replace('xset007', 'XSet007') + ' '
        password = uuid.uuid4().hex
        login, returned_password = self.Users.signup(
            {'login': raw, 'name': 'XSET007 mixed', 'password': password})
        expected = raw.strip().lower()
        self.assertEqual(login, expected)
        self.assertEqual(returned_password, password)
        user = self.Users.search([('login', '=', expected)])
        self.assertEqual(len(user), 1)
        self.assertEqual(user.email, expected)


@tagged('post_install', '-at_install', 'x_set_007_signup')
class TestSignupFormXSet007(HttpCase):

    def setUp(self):
        super().setUp()
        _enable_public_signup(self.env)

    def _csrf(self):
        page = self.url_open('/web/signup')
        self.assertEqual(page.status_code, 200, '公開註冊頁應開啟')
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        self.assertTrue(match, '註冊頁找不到 csrf_token')
        return match.group(1)

    def _post(self, login, name, password, confirm=None):
        return self.url_open('/web/signup', data={
            'csrf_token': self._csrf(),
            'login': login, 'name': name,
            'password': password,
            'confirm_password': password if confirm is None else confirm,
        })

    def _assert_form_error(self, response, needle):
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertIn(needle, response.text)
        self.assertNotIn('Internal Server Error', response.text)
        self.assertNotIn('Access Denied', response.text)

    def test_empty_fields_show_chinese_field_errors(self):
        before = self.env['res.users'].sudo().with_context(active_test=False).search_count([])
        self._assert_form_error(self._post('', '', ''), '請填寫電子郵件')
        self._assert_form_error(self._post(_addr('noname'), '', 'x' * 12), '請填寫姓名')
        self._assert_form_error(self._post(_addr('nopw'), 'XSET007', ''), '請設定密碼')
        self.env.invalidate_all()
        after = self.env['res.users'].sudo().with_context(active_test=False).search_count([])
        self.assertEqual(after, before)

    def test_non_email_login_rejected(self):
        self._assert_form_error(
            self._post('not-an-email', 'XSET007', uuid.uuid4().hex), '有效的電子郵件')
        self.assertEqual(_count_logins_ci(self.env, 'not-an-email'), 0)

    def test_case_variant_duplicate_rejected(self):
        login = _addr('formdup')
        _make_user(self.env, login)
        self._assert_form_error(
            self._post(login.upper(), 'XSET007', uuid.uuid4().hex), '已經註冊')
        self.assertEqual(_count_logins_ci(self.env, login), 1)

    def test_mixed_case_signup_stores_lowercase_and_logs_in_any_case(self):
        login = _addr('formok')
        password = uuid.uuid4().hex
        response = self._post(' %s ' % login.upper(), 'XSET007 ok', password)
        self.assertNotEqual(response.status_code, 500)
        self.env.invalidate_all()
        user = self.env['res.users'].sudo().search([('login', '=', login)])
        self.assertEqual(len(user), 1, '應以小寫登入帳號建立一個帳號')
        self.assertEqual(_count_logins_ci(self.env, login), 1)
        # 不論大小寫都能登入（舊行為：只有註冊時打的那種大小寫才登得進去）。
        self.authenticate(login.upper(), password)
        self.assertEqual(self.session.uid, user.id)

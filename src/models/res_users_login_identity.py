"""登入帳號身分正規化（商用驗收 X-SET-007-2／-3／-5、X-W-002-1）。

Odoo 原生 ``res.users.login`` 的唯一性是 SQL ``UNIQUE(login)``——**區分大小寫**。
公開註冊 ``/web/signup`` 又不做任何正規化，於是同一個信箱換個大小寫就能再開一個
帳號（正式站 uid 227／236），之後邀請端以 ``=ilike`` 比對時兩個都命中，原帳號
再也邀請不進工作區（``invite_identity_ambiguous``）。

本檔只做三件事，全部在 ORM 層，所有建立／改登入帳號的路徑都會經過：

1. ``create``／``write`` 寫入 ``login`` 時一律 ``strip()``＋``lower()``。
2. ``@api.constrains('login')`` 以 ``lower(login)`` 檢查唯一性（同 website 範圍，
   與 website 模組的 ``UNIQUE(login, website_id)`` 對齊；含封存帳號，與原生
   ``login_key`` 一致）。只檢查**本次被寫入**的列，既有的大小寫重複帳號不會擋住
   模組升級——那批資料要由管理員封存（X-SET-007 待人工清單）。
   刻意**不加** ``lower(login)`` unique index：正式站目前就有重複列，建 index 會讓
   ``-u`` 失敗、整次發版中斷。
3. ``_get_login_domain``：輸入值與其正規化值任一相符即可登入，讓舊的大小寫混合
   帳號照舊能登入，新帳號則不論使用者怎麼打大小寫都登得進去。

所有「註冊新建帳號」路徑（``/web/signup``、邀請 token、OAuth）都經過
``_signup_create_user``：這裡把必填欄位缺漏與重複信箱轉成中文 ``UserError``——
原生在這裡丟的是 ``ValueError``，``/web/signup`` 沒接住就變 HTTP 500 英文頁
（X-SET-007-5）。email 格式只在 ``/web/signup`` 表單驗（``src/controllers/signup.py``）：
auth_oauth 在供應商沒給 email 時會用 ``provider_<id>_user_<uid>`` 當 login，
放在這一層會連帶擋掉那條既有路徑。

X-SET-007-4（要不要強制 email 驗證）屬產品決策，本檔不處理。
"""
from odoo import _, api, models
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression


def normalize_login(login):
    """``strip()``＋``lower()``；非字串原樣回傳（False／None 交給原生處理）。"""
    if isinstance(login, str):
        return login.strip().lower()
    return login


class ResUsersLoginIdentity(models.Model):
    _inherit = 'res.users'

    # ------------------------------------------------------------------
    # 1. 寫入時正規化
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'login' in vals:
                vals['login'] = normalize_login(vals['login'])
        return super().create(vals_list)

    def write(self, vals):
        if 'login' in vals:
            vals = dict(vals, login=normalize_login(vals['login']))
        return super().write(vals)

    # ------------------------------------------------------------------
    # 2. 大小寫不敏感的唯一性
    # ------------------------------------------------------------------
    def _woow_login_conflict_ids(self, login, website_id=None, exclude_ids=()):
        """回傳與 ``login``（不分大小寫）衝突的使用者 id，含封存帳號。

        website 模組允許不同 website 各有同名登入帳號（``UNIQUE(login, website_id)``），
        所以有 ``website_id`` 欄位時只比對同一個 website（NULL 視為相同）。
        """
        if not login:
            return []
        query = "SELECT id FROM res_users WHERE lower(login) = lower(%s)"
        params = [login]
        if 'website_id' in self._fields:
            query += " AND website_id IS NOT DISTINCT FROM %s"
            params.append(website_id or None)
        if exclude_ids:
            query += " AND id NOT IN %s"
            params.append(tuple(exclude_ids))
        self.env.cr.execute(query + " LIMIT 5", params)
        return [row[0] for row in self.env.cr.fetchall()]

    @api.constrains('login')
    def _check_login_case_insensitive_unique(self):
        fnames = ['login'] + (['website_id'] if 'website_id' in self._fields else [])
        self.flush_model(fnames)
        for user in self:
            website_id = user.website_id.id if 'website_id' in self._fields else None
            if self._woow_login_conflict_ids(user.login, website_id, exclude_ids=user.ids):
                raise ValidationError(
                    _("已有其他使用者使用相同的登入帳號（不分大小寫）：%s", user.login))

    # ------------------------------------------------------------------
    # 3. 登入查找不分大小寫
    # ------------------------------------------------------------------
    @api.model
    def _get_login_domain(self, login):
        domain = super()._get_login_domain(login)
        normalized = normalize_login(login)
        if not normalized or normalized == login:
            return domain
        return expression.OR([domain, super()._get_login_domain(normalized)])

    # ------------------------------------------------------------------
    # 4. 自助註冊新建帳號的伺服器端驗證
    # ------------------------------------------------------------------
    @api.model
    def _signup_create_user(self, values):
        raw_login = values.get('login')
        login = normalize_login(raw_login)
        if not login:
            raise UserError(_("請填寫電子郵件。"))
        if not values.get('partner_id') and not (values.get('name') or '').strip():
            raise UserError(_("請填寫姓名。"))
        if self.sudo()._woow_login_conflict_ids(login, values.get('website_id')):
            raise UserError(_("此電子郵件已經註冊過，請直接登入，或使用「忘記密碼」重設密碼。"))
        # 就地改寫：上游 signup() 回傳的是同一個 dict 的 login，接著拿它登入。
        values['login'] = login
        if normalize_login(values.get('email')) in (None, '', False, login):
            values['email'] = login
        return super()._signup_create_user(values)

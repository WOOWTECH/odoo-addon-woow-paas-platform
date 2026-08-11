from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


ROLE_OWNER = 'owner'
ROLE_ADMIN = 'admin'
ROLE_USER = 'user'
ROLE_GUEST = 'guest'

ROLE_SELECTION = [
    (ROLE_OWNER, 'Owner'),
    (ROLE_ADMIN, 'Admin'),
    (ROLE_USER, 'User'),
    (ROLE_GUEST, 'Guest'),
]

# Role hierarchy — owner > admin > user > guest. Only the grantor's own
# role determines which target roles they may assign (B-1 / PA-4 / PA-5).
# Admin cannot grant admin or owner; user / guest cannot grant anything.
ASSIGNABLE_ROLES_BY_ROLE = {
    ROLE_OWNER: [ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST],
    ROLE_ADMIN: [ROLE_USER, ROLE_GUEST],
    ROLE_USER: [],
    ROLE_GUEST: [],
}


class ProjectAccess(models.Model):
    _name = 'woow_paas_platform.project_access'
    _description = 'Project Member Access (first-class ACL)'
    _order = 'role desc, create_date desc'
    _rec_name = 'partner_id'

    project_id = fields.Many2one(
        'project.project',
        required=True,
        ondelete='cascade',
        index=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Member',
        required=True,
        ondelete='cascade',
        index=True,
    )
    user_id = fields.Many2one(
        'res.users',
        string='User',
        compute='_compute_user_id',
        store=True,
        index=True,
    )

    role = fields.Selection(
        ROLE_SELECTION,
        string='Role',
        required=True,
        default=ROLE_USER,
        index=True,
    )

    invited_by_uid = fields.Many2one(
        'res.users',
        readonly=True,
        default=lambda self: self.env.uid,
    )
    invited_at = fields.Datetime(
        readonly=True,
        default=fields.Datetime.now,
    )

    _sql_constraints = [
        (
            'unique_partner_per_project',
            'UNIQUE(project_id, partner_id)',
            'A partner can have only one access record per project.',
        ),
    ]

    @api.depends('partner_id')
    def _compute_user_id(self):
        for rec in self:
            user = self.env['res.users'].sudo().search(
                [('partner_id', '=', rec.partner_id.id)], limit=1,
            )
            rec.user_id = user.id if user else False

    def unlink(self):
        """(#657 gap 3) Two invariants, checked per record:

        (a) a non-owner caller may not unlink a row whose role is currently
            'owner' — closes the demote-then-self-promote chain;
        (b) the last owner row of a project cannot be removed at all, even
            by the owner themselves.

        Order matters: (a) authorization denial (``AccessError``) runs
        before (b) invariant denial (``ValidationError``), so the two stay
        distinguishable in tests (mirrors workspace_access.unlink(),
        src/models/workspace_access.py).
        """
        for rec in self:
            if rec.role != ROLE_OWNER:
                continue
            self._check_owner_row_protected(rec)
            other_owners = self.sudo().search_count([
                ('project_id', '=', rec.project_id.id),
                ('role', '=', ROLE_OWNER),
                ('id', '!=', rec.id),
            ])
            if not other_owners:
                raise ValidationError(
                    "Cannot remove the last owner of project '%s'. "
                    "Transfer ownership first." % (rec.project_id.display_name or rec.project_id.id)
                )
        return super().unlink()

    # ------------------------------------------------------------------ #
    # Role-hierarchy gate (B-1 / PA-5) + identity anchor + owner guard    #
    # (#657 — same shape as workspace_access.py's #556 guards)            #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # #657 gap 2 — 不可在 role 缺省時 early-return。
            # 缺省時 Odoo 會在 super().create() 內用 `default_get` 補值
            # （odoo/models.py :: BaseModel._prepare_create_values →
            #  _add_missing_default_values → default_get），而 default_get 的
            # 第一順位是 context 的 `default_role` ——那是 RPC 用戶端可控的。
            # 所以守衛必須用「Odoo 實際會寫進去的那個值」來判，而不是 raw vals，
            # 否則 with_context(default_role='owner') + 不放 role 就整道繞過。
            # 這裡刻意逐字重現 Odoo 的解析順序：key 存在就用它（即使是 False，
            # 那種 payload 會在 DB 層因 required 而失敗，fail-closed），
            # key 不存在才走 default_get。
            if 'role' in vals:
                effective_role = vals['role']
            else:
                effective_role = self.default_get(['role']).get('role') or ROLE_USER
            self._check_assignable_role(effective_role, vals.get('project_id'))
        return super().create(vals_list)

    def write(self, vals):
        # #657 gap 1 — identity anchors。與 workspace_access.write() 同型
        # （src/models/workspace_access.py 的 #556 註解區塊）。project_id /
        # partner_id 是成員關係的「身分錨點」：invite / 改 role / 移除成員
        # 這三條正常流程都只動 `role`，沒有任何合法流程會把一列既有的 access
        # 重新指向另一個 project 或另一個 partner。少了這道守衛，一個合法自建
        # project 的人（他真的是自己那列的 owner）可以 write([自己那列],
        # {'project_id': <受害 project>, 'role': 'admin'})：ir.rule 的 write
        # 檢查只評估 PRE-write 狀態（還在自己的 project、還是 owner ⇒ 放行）、
        # 永遠不會重新評估搬過去之後的目的地；而下面的 `_check_assignable_role`
        # 讀的是 `rec.project_id.id` ——也是來源而非目的地。兩層都沒守到。
        # 比對的是「新值 != 現值」而不是「key 有沒有出現」，這樣前端整批送出
        # 未修改欄位的 no-op write 不會被誤擋。
        if not self.env.is_superuser() and not self.env.user.has_group('base.group_system'):
            for rec in self:
                if 'project_id' in vals and vals['project_id'] != rec.project_id.id:
                    raise AccessError(
                        "Cannot re-point an access row (id=%s) to a "
                        "different project." % rec.id
                    )
                if 'partner_id' in vals and vals['partner_id'] != rec.partner_id.id:
                    raise AccessError(
                        "Cannot re-point an access row (id=%s) to a "
                        "different partner." % rec.id
                    )
        # #657 gap 3 — 每一次 write 都檢查目標列是否為 owner 列，不論改哪個
        # 欄位（見 _check_owner_row_protected docstring）。
        for rec in self:
            self._check_owner_row_protected(rec)
        if 'role' in vals:
            for rec in self:
                self._check_assignable_role(
                    vals['role'], rec.project_id.id,
                )
        return super().write(vals)

    @api.model
    def _check_owner_row_protected(self, rec):
        """非 owner 的呼叫者不得 write / unlink 一列「現值為 owner」的紀錄。

        ir.rule 表達不了這件事：record rule 只評估**呼叫者自己**的 pre-write
        狀態，從不評估**被動到的那一列**，所以少了這道守衛，admin 對 owner 那
        一列做 write({'role': 'user'}) 會直接通過 manage rule（該 rule 只檢查
        呼叫者是不是 owner/admin，不檢查被改的列屬於誰）——那是
        「先降級再自我提升」鏈的第一步。
        """
        if rec.role != ROLE_OWNER:
            return
        if self.env.is_superuser():
            return
        if self.env.user.has_group('base.group_system'):
            return
        caller_access = self.sudo().search([
            ('project_id', '=', rec.project_id.id),
            # env.uid 而非 context：call_kw 讓呼叫端可以整包替換 context，
            # 從 context 取身分等於讓身分可偽造（既有 _check_assignable_role
            # 也是用 env.uid，保持一致）。
            ('user_id', '=', self.env.uid),
        ], limit=1)
        if caller_access and caller_access.role == ROLE_OWNER:
            return
        raise AccessError(
            "Only the project owner may modify or remove the owner's own "
            "access row (project %s)." % rec.project_id.id
        )

    @api.model
    def _check_assignable_role(self, target_role, project_id):
        """Grantor may only assign target roles per ASSIGNABLE_ROLES_BY_ROLE.

        Bypassed for ``env.is_superuser()`` (covers uid == SUPERUSER_ID and
        any ``sudo()`` recordset — the project create bootstrap runs through
        sudo) and for ``base.group_system`` (parallel to the
        ``project_access_rule_admin_full`` ir.rule).

        ``target_role`` is resolved by the caller into the *effective* role
        (raw vals, or Odoo's own default resolution) before being passed in
        — this function no longer lets an empty value through on its own.
        """
        if not project_id:
            return  # 缺 project_id 由 required=True 擋掉
        if self.env.is_superuser():
            return
        if self.env.user.has_group('base.group_system'):
            return
        grantor = self.sudo().search([
            ('project_id', '=', project_id),
            ('user_id', '=', self.env.uid),
        ], limit=1)
        grantor_role = grantor.role if grantor else None
        allowed = ASSIGNABLE_ROLES_BY_ROLE.get(grantor_role, [])
        if target_role not in allowed:
            raise AccessError(
                "User %s (role=%s) cannot assign role '%s' on project %s. "
                "Assignable roles for %s: %s."
                % (
                    self.env.user.display_name,
                    grantor_role or '<none>',
                    target_role,
                    project_id,
                    grantor_role or '<none>',
                    allowed,
                )
            )

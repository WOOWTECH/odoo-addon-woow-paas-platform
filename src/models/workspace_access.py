from __future__ import annotations

from odoo import models, fields, api
from odoo.exceptions import AccessError, ValidationError


# Role constants
ROLE_OWNER = 'owner'
ROLE_ADMIN = 'admin'
ROLE_USER = 'user'
ROLE_GUEST = 'guest'

# Role hierarchy (low to high)
ROLE_HIERARCHY = [ROLE_GUEST, ROLE_USER, ROLE_ADMIN, ROLE_OWNER]

# Roles that can be assigned by admins (excludes owner). Used by the
# controller's payload-shape validation (paas.py _invite_member /
# _update_member_role) — independent of *who* is granting; see
# ASSIGNABLE_ROLES_BY_ROLE below for the grantor-aware check.
ASSIGNABLE_ROLES = [ROLE_ADMIN, ROLE_USER, ROLE_GUEST]

# Role selection for fields.Selection
ROLE_SELECTION = [
    (ROLE_OWNER, 'Owner'),
    (ROLE_ADMIN, 'Admin'),
    (ROLE_USER, 'User'),
    (ROLE_GUEST, 'Guest'),
]

# Grantor-role → assignable target roles (#556 B1). Mirrors
# project_access.ASSIGNABLE_ROLES_BY_ROLE (src/models/project_access.py)
# with ONE deliberate deviation: ROLE_ADMIN here includes ROLE_ADMIN itself
# (project_access's admin cannot grant admin). paas.py's _invite_member has
# always let an admin invite another admin; copying project_access's
# stricter table verbatim would break that existing product behaviour.
# It is not a privilege escalation — the grantor is already admin, so
# granting admin to someone else does not raise anyone above the grantor's
# own ceiling. Only owner may still grant owner. Do NOT "fix" this back to
# match project_access — see
# docs/tracking/landings/556-workspace-access-authz-landing.md.
ASSIGNABLE_ROLES_BY_ROLE = {
    ROLE_OWNER: [ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST],
    ROLE_ADMIN: [ROLE_ADMIN, ROLE_USER, ROLE_GUEST],
    ROLE_USER: [],
    ROLE_GUEST: [],
}


class WorkspaceAccess(models.Model):
    _name = 'woow_paas_platform.workspace_access'
    _description = 'Workspace User Access'
    _order = 'role desc, create_date desc'
    _rec_name = 'user_id'

    # Core fields
    workspace_id = fields.Many2one(
        'woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        index=True
    )
    user_id = fields.Many2one(
        'res.users',
        string='User',
        required=True,
        ondelete='cascade',
        index=True
    )

    # Role
    role = fields.Selection(
        ROLE_SELECTION,
        string='Role',
        required=True,
        default=ROLE_USER,
        index=True
    )

    # Related fields for convenience
    user_name = fields.Char(related='user_id.name', string='User Name', store=True)
    user_email = fields.Char(related='user_id.email', string='User Email', store=True)
    user_avatar = fields.Binary(related='user_id.avatar_128', string='Avatar')

    # Invitation fields
    invited_by_id = fields.Many2one(
        'res.users',
        string='Invited By',
        default=lambda self: self.env.user
    )
    invited_date = fields.Datetime(
        string='Invited Date',
        default=fields.Datetime.now
    )

    _sql_constraints = [
        ('unique_user_workspace', 'UNIQUE(workspace_id, user_id)',
         'A user can only have one access record per workspace.')
    ]

    @api.constrains('role', 'workspace_id')
    def _check_owner_count(self) -> None:
        """Ensure each workspace has exactly one owner"""
        for access in self:
            if access.role == ROLE_OWNER:
                # #556 B3: sudo() — this constraint must see every access row
                # in the workspace, not just the ones ir.rule lets the
                # caller see. It was previously unfiltered only by accident
                # (whoever could reach create() could already see every row
                # in the workspace); that accident is no longer guaranteed
                # once create()/write() are guarded below.
                owner_count = self.sudo().search_count([
                    ('workspace_id', '=', access.workspace_id.id),
                    ('role', '=', ROLE_OWNER),
                    ('id', '!=', access.id),
                ])
                if owner_count > 0:
                    raise ValidationError(
                        'A workspace can only have one owner. '
                        'Transfer ownership first before assigning a new owner.'
                    )

    # ------------------------------------------------------------------ #
    # Role-hierarchy + owner-row guard (#556 B1/B2)                       #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Do NOT early-return when 'role' is omitted from vals — that
            # would let a create-direction payload skip this guard entirely
            # and fall back on nothing but the ir.rule's create check
            # (which, per #556's root cause, is evaluated AFTER the row is
            # already inserted and can be self-satisfied by the very row
            # being created). Both an explicit role='admin' payload and an
            # omitted-role payload must go through the SAME check here.
            #
            # #664 — 守衛必須看「Odoo 實際會寫進去的值」而不是 raw vals。
            # context 的 `default_<field>` 是 RPC 用戶端可控的，而 default
            # 是在 super().create() 內部才補上的（odoo/models.py ::
            # BaseModel._prepare_create_values → _add_missing_default_values
            # → default_get，default_get 第一順位就是 context 的
            # `default_<field>`）。這裡直接呼叫 Odoo 自己那支解析函式
            # （`_add_missing_default_values`，`_prepare_create_values` 內部
            # 用的正是它），而不是逐欄位重演它的規則——逐欄位重演會漏：
            # 這個檔案先前的版本只手動重演了 role 半邊（且評論宣稱這樣「仍
            # 正確」，那是錯的——見下方 `_check_assignable_role` 的
            # docstring），`workspace_id` 半邊完全沒補，攻擊者用
            # with_context(default_workspace_id=<受害 workspace>,
            # default_role='owner') 且 vals 不放 workspace_id 就整道繞過
            # （#664，同型於 PR #666 留言 6583 §1 的 H1）。
            # single-source-of-truth 版本保證的是「同一個欄位」的解析方式
            # 不會漂（role／workspace_id 永遠照 Odoo 自己的順序解析，不會有
            # 人手動重演漏掉其中一步）——它不會自動涵蓋未來新增的授權相關
            # 欄位；日後若新增一個會影響授權判定的欄位，仍要手動把它加進
            # `_check_assignable_role` 的參數與呼叫端。
            # （`ProjectAccess.create`，`project_access.py`，是這個修法的
            # 樣板，已通過兩輪獨立對抗式覆核，PR #666 留言 6583/6595）。
            resolved = self._add_missing_default_values(vals)
            self._check_assignable_role(
                resolved.get('role') or ROLE_USER,
                resolved.get('workspace_id'),
            )
        return super().create(vals_list)

    def write(self, vals):
        # #556 write-pivot (found by independent verification, not in the
        # original analysis): workspace_id / user_id are membership
        # IDENTITY ANCHORS — no legitimate tenant flow ever re-points an
        # existing access row at a different workspace or a different
        # user (invite/role-change/remove all operate on `role` only, on
        # a row whose workspace_id/user_id stay fixed for its lifetime).
        # Without this, a caller who legitimately self-created their OWN
        # workspace (P6 bootstrap — they really are that row's owner) can
        # write([their_own_row_id], {'workspace_id': <victim_ws>, 'role':
        # 'admin'}) on their own row: ir.rule's write check evaluates only
        # the PRE-write state (still their own workspace, still owner ⇒
        # passes) and never re-evaluates the post-write target; and
        # `_check_assignable_role` below reads `rec.workspace_id.id` —
        # also the pre-write/SOURCE workspace, not the vals-supplied
        # target — so a self-owned row can be "moved" straight into a
        # victim workspace with an attacker-chosen role. This must run
        # first, before the source-workspace-scoped checks below, and
        # must compare the ACTUAL new value against the current one (not
        # merely whether the key is present) so a no-op write (UI
        # batch-send of unmodified fields) is not mistakenly blocked.
        if not self.env.is_superuser() and not self.env.user.has_group('base.group_system'):
            for rec in self:
                if 'workspace_id' in vals and vals['workspace_id'] != rec.workspace_id.id:
                    raise AccessError(
                        "Cannot re-point an access row (id=%s) to a "
                        "different workspace." % rec.id
                    )
                if 'user_id' in vals and vals['user_id'] != rec.user_id.id:
                    raise AccessError(
                        "Cannot re-point an access row (id=%s) to a "
                        "different user." % rec.id
                    )
        # Owner-row protection applies to every write on an owner row,
        # regardless of which fields are being changed — ir.rule cannot
        # express this because record rules evaluate the CALLER's pre-write
        # state, never the TARGET row's, so an admin's write({'role':
        # 'user'}) on the owner's own row would otherwise sail through the
        # manage rule outright (it only checks that the admin is
        # owner/admin, not that the row being touched belongs to someone
        # else who is the owner).
        for rec in self:
            self._check_owner_row_protected(rec)
        if 'role' in vals:
            for rec in self:
                self._check_assignable_role(vals['role'], rec.workspace_id.id)
        return super().write(vals)

    def unlink(self):
        """(#556 B2) Two invariants, checked per record:

        (a) a non-owner caller may not unlink a row whose role is currently
            'owner' — closes the demote-then-self-promote chain (an admin
            who cannot directly unlink the owner's row cannot open a
            0-owner window that way either);
        (b) the last owner row of a workspace cannot be removed at all,
            even by the owner themselves (mirrors
            `ProjectAccess.unlink` in project_access.py) — checked only
            once (a) has cleared, so the AUTHORIZATION denial
            (AccessError) and the INVARIANT denial (ValidationError) stay
            distinguishable.
        """
        for rec in self:
            if rec.role != ROLE_OWNER:
                continue
            self._check_owner_row_protected(rec)
            other_owners = self.sudo().search_count([
                ('workspace_id', '=', rec.workspace_id.id),
                ('role', '=', ROLE_OWNER),
                ('id', '!=', rec.id),
            ])
            if not other_owners:
                raise ValidationError(
                    "Cannot remove the last owner of workspace '%s'. "
                    "Transfer ownership first." % (
                        rec.workspace_id.display_name or rec.workspace_id.id)
                )
        return super().unlink()

    @api.model
    def _check_assignable_role(self, target_role, workspace_id):
        """Grantor may only assign target roles per ASSIGNABLE_ROLES_BY_ROLE.

        Bypassed for ``env.is_superuser()`` (covers uid == SUPERUSER_ID and
        any ``sudo()`` recordset — the workspace create bootstrap and the
        HA PaaS auto-provision path both run through sudo) and for
        ``base.group_system`` (parallel to the
        ``workspace_access_rule_admin_full`` ir.rule). Role assignment via
        invite / role-change / bootstrap is a normal, gated product flow —
        this is the scope-flip-write-guard-not-ir-rule family (a legitimate
        change path that only needs gating), not the su-env-exempt-guard
        family (no legitimate change path at all); the su/group_system
        exemption is therefore correct here, not a hole.

        This function has two callers with different resolution needs —
        the "already reflects the value Odoo will persist" property below
        is per-caller, not a blanket guarantee of this function itself:

        - ``create()`` passes ``target_role`` / ``workspace_id`` already
          resolved via ``_add_missing_default_values`` (Odoo's own
          default-resolution function — the same one
          ``_prepare_create_values`` uses internally), because ``create``
          is the only direction where an RPC caller can inject a value
          through context ``default_<field>`` (#664, mirrors
          `ProjectAccess._check_assignable_role`'s `project_id` fix in
          `project_access.py`, PR #666 comment 6583 "H1" / 6595).
        - ``write()`` passes the raw ``vals['role']`` / ``rec.workspace_id.id``
          straight through, unresolved — this is correct, not a gap:
          Odoo's own `write()` never applies `default_get`/context-default
          resolution (that machinery is wired into `_prepare_create_values`,
          which only `create()` goes through — verified against
          `odoo/models.py`, no `default_get`/`_add_missing_default_values`
          call anywhere in `BaseModel.write()`'s body), so there is no
          context-injected value for a write-direction caller to resolve.

        This function's own early-return below is therefore only a
        defensive guard against a genuinely absent value — on the create
        path, no vals key AND no context default AND no field-level
        default; that case still reaches the DB's ``required=True``
        constraint and fails closed.
        """
        if not workspace_id:
            return
        if self.env.is_superuser():
            return
        if self.env.user.has_group('base.group_system'):
            return
        grantor = self.sudo().search([
            ('workspace_id', '=', workspace_id),
            # env.uid, not context: call_kw lets the caller replace the
            # whole context wholesale (odoo/api.py::call_kw is
            # with_context(context) as a positional arg, not a merge), so a
            # context-sourced identity would be forgeable over RPC.
            ('user_id', '=', self.env.uid),
        ], limit=1)
        grantor_role = grantor.role if grantor else None
        allowed = ASSIGNABLE_ROLES_BY_ROLE.get(grantor_role, [])
        if target_role not in allowed:
            raise AccessError(
                "User %s (role=%s) cannot assign role '%s' on workspace %s. "
                "Assignable roles for %s: %s."
                % (
                    self.env.user.display_name,
                    grantor_role or '<none>',
                    target_role,
                    workspace_id,
                    grantor_role or '<none>',
                    allowed,
                )
            )

    @api.model
    def _check_owner_row_protected(self, rec):
        """A non-owner caller may not write/unlink a row whose CURRENT role
        is 'owner'. ir.rule cannot express this: the manage rule evaluates
        only the caller's own pre-write role, never the target row's, so
        without this an admin's write({'role': 'user'}) on the owner's row
        would pass the manage rule outright — the first step of a
        demote-then-self-promote chain into a transient 0-owner window.
        project_access has no equivalent guard; this is a deliberate
        addition, not parity with that model (see module docstring / #556
        landing doc for the three deviations from the project_access
        template).
        """
        if rec.role != ROLE_OWNER:
            return
        if self.env.is_superuser():
            return
        if self.env.user.has_group('base.group_system'):
            return
        caller_access = self.sudo().search([
            ('workspace_id', '=', rec.workspace_id.id),
            ('user_id', '=', self.env.uid),
        ], limit=1)
        if caller_access and caller_access.role == ROLE_OWNER:
            return
        raise AccessError(
            "Only the workspace owner may modify or remove the owner's own "
            "access row (workspace %s)." % rec.workspace_id.id
        )

    @api.model
    def _check_caller_owns_transfer(self, rec):
        """(#583) 授權閘：呼叫者必須是**這個 workspace 的 owner**，且操作的是他
        自己那一列。判準取自 `env.uid` 反查出來的 access 列，**不從被操作的記錄
        推導**。

        原本的守衛是 `if rec.role != ROLE_OWNER: raise ValidationError(...)` ——
        `rec` 是呼叫端指定的 access 列，攻擊者只要挑一筆 `role == 'owner'` 的記錄
        （也就是現任 owner 自己那筆）當 `self`，守衛就通過；它從頭到尾沒有問過
        `env.user` 是誰。⚠ **它讀起來完全像一個正確的守衛**，這是它活到現在的原因。

        真 RPC 實證（2026-08-14，`origin/develop` f55eccc6）：固定 payload
        `transfer_ownership([owner 那筆], <不存在的 uid>)`，guest / admin / owner
        三種身分的回應**逐字相同**（都倒在後段的 new_owner 商業驗證）⇒ 舊守衛對
        呼叫者身分的鑑別力是 **0**。行為守線見
        `src/tests/test_workspace_transfer_ownership_authz_583.py` 的
        `TestTransferOwnershipGateIsCallerScoped`（三條共用同一 payload、只換身分）。

        三個設計選擇，都刻意與本檔既有兩道守衛（`_check_assignable_role` /
        `_check_owner_row_protected`）對齊，不要各自漂：

        1. **用 `sudo()` 查呼叫者那一列**。授權閘的強度等於它依賴項的最弱保護——
           若改用呼叫者自己的讀取權限去查，這道閘就會隨 `ir.rule` 的鬆緊而變，
           而 rule 的收窄／放寬是另一條演進線。`_check_owner_row_protected` 用的
           也是 `self.sudo().search`。
        2. **`rec` 必須就是呼叫者自己那一列**。owner 拿別人的 access 列來當 `self`
           不是合法流程（語意是「我把我的 owner 身分交出去」），放行只會讓下面那段
           降級／升級的配對錯位、最後撞 `_check_owner_count` 拋出一個與授權無關的
           ValidationError。
           🔴 更根本的理由（覆核補上）：少了這個條件，這道閘的語意會退化成
           「**你是 owner 就能對任何一列做 transfer**」——它擋的是**身分**、擋不住
           **標的**。而 #583 的整個教訓正是「守衛要同時綁對呼叫者與標的」：只綁一邊
           就是同一類缺陷換個位置長出來（舊版綁了標的沒綁呼叫者，這裡若只綁呼叫者
           就是把它鏡像翻過來）。
        3. **豁免 `is_superuser()` 與 `base.group_system`**。這屬於
           scope-flip-write-guard 家族（有合法變更路徑、只需要設閘），不是
           su-env-exempt-guard 家族（根本沒有合法路徑）——理由與
           `_check_assignable_role` docstring 那段相同。`group_system` 本來就有
           `workspace_access_rule_admin_full` 的全 CRUD，走直接 write 也做得到同樣
           的事，在這裡擋它只會製造一道繞得過去的假防線。
        """
        if self.env.is_superuser():
            return
        if self.env.user.has_group('base.group_system'):
            return
        # sudo：閘門不依賴呼叫者的讀取權限（理由見 docstring 第 1 點）。
        # `rec.id` 不觸發讀取，所以非成員也走得到這裡、由本閘門 fail closed，
        # 而不是靠 ir.rule 碰巧先擋下來。
        workspace_id = rec.sudo().workspace_id.id
        caller_access = self.sudo().search([
            ('workspace_id', '=', workspace_id),
            # env.uid，不是 context：call_kw 讓呼叫者整包替換 context
            # （`odoo/api.py::call_kw` 是 with_context(context) 位置參數、非 merge），
            # 從 context 取身分等於讓攻擊者自己填。
            ('user_id', '=', self.env.uid),
        ], limit=1)
        if (caller_access
                and caller_access.role == ROLE_OWNER
                and caller_access.id == rec.id):
            return
        raise AccessError(
            "Only the workspace owner may transfer ownership, and only from "
            "their own access row (workspace %s)." % workspace_id
        )

    def transfer_ownership(self, new_owner_id: int) -> None:
        """Transfer ownership to another user.

        ⚠ **死碼，且對非 superuser 呼叫者已經壞了**（#556 landing doc 的 Not-in-scope
        段落已記載，#658 複驗仍成立，#583 的可達性實證再次復現）：`src/` 裡沒有任何
        controller／OWL 呼叫它。壞在下面連續兩次 write 的順序——第一次把呼叫者自己
        那列降成 admin，第二次要指派 `owner` 時 `_check_assignable_role` 查到的
        grantor 已經是 admin，而 `ASSIGNABLE_ROLES_BY_ROLE['admin']` 不含 `'owner'`
        ⇒ AccessError。

        #658 又多了一道：最後那行 `self.workspace_id.write({'owner_id': ...})` 會撞
        `Workspace.write()` 的 owner_id identity anchor。**這不是本方法今天壞掉的
        原因**（它在 #658 之前就已經走不到那一行了），但要復活這條方法時兩道都要處理：
        整段改走 `sudo()`，或重新設計成單一 atomic 呼叫。不要只修其中一道。

        ⚠ #583 只修**授權閘**（`_check_caller_owns_transfer`），刻意**不**順手修上面
        那兩道功能性缺陷——「復活它還是刪掉它」是產品決策。因此本方法今天對合法
        owner 仍然走不完；閘門本身的正確性由行為測試獨立守住（見
        `_check_caller_owns_transfer` 的 docstring）。
        """
        self.ensure_one()
        self._check_caller_owns_transfer(self)

        new_owner_access = self.search([
            ('workspace_id', '=', self.workspace_id.id),
            ('user_id', '=', new_owner_id),
        ], limit=1)

        if not new_owner_access:
            raise ValidationError('The new owner must be an existing member of the workspace.')

        # Demote current owner to admin
        self.write({'role': ROLE_ADMIN})
        # Promote new owner
        new_owner_access.write({'role': ROLE_OWNER})
        # Update workspace owner_id
        self.workspace_id.write({'owner_id': new_owner_id})

    @api.model
    def get_role_permissions(self, role: str) -> dict[str, bool]:
        """Get permissions for a given role"""
        permissions = {
            ROLE_OWNER: {
                'can_view': True,
                'can_edit': True,
                'can_delete': True,
                'can_manage_members': True,
                'can_manage_workspace': True,
                'can_transfer_ownership': True,
            },
            ROLE_ADMIN: {
                'can_view': True,
                'can_edit': True,
                'can_delete': False,
                'can_manage_members': True,
                'can_manage_workspace': True,
                'can_transfer_ownership': False,
            },
            ROLE_USER: {
                'can_view': True,
                'can_edit': True,
                'can_delete': False,
                'can_manage_members': False,
                'can_manage_workspace': False,
                'can_transfer_ownership': False,
            },
            ROLE_GUEST: {
                'can_view': True,
                'can_edit': False,
                'can_delete': False,
                'can_manage_members': False,
                'can_manage_workspace': False,
                'can_transfer_ownership': False,
            },
        }
        return permissions.get(role, {})

    def get_permissions(self) -> dict[str, bool]:
        """Get permissions for this access record"""
        self.ensure_one()
        return self.get_role_permissions(self.role)

"""#1679 Workspace quota 最低保障的 append-only 異動紀錄（只有 base.group_system 可讀）。

刻意不讓 workspace 繼承 mail.thread：那會替租戶可讀的 workspace 開出 follower、
``message_post``、``/mail`` 路由與通知信一整組新的外露面。這裡改用獨立 ledger：

- ``workspace_ref`` 用純量 id＋``ws_hash``／名稱快照，不用外鍵——workspace 刪除後紀錄仍在；
- 只有 quota 政策入口（模組私有 sentinel）能新增；write／unlink 一律拒絕，不豁免 su；
- 操作者存 uid 與 login 快照，不依賴 partner 名稱。
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .workspace_quota_policy import _is_internal_quota_policy_write


class WorkspaceQuotaAudit(models.Model):
    _name = 'woow_paas_platform.workspace_quota_audit'
    _description = 'Workspace Quota Minimum Change'
    _order = 'id desc'

    workspace_ref = fields.Integer(string='Workspace ID', required=True, index=True, readonly=True)
    ws_hash = fields.Char(string='Workspace Hash', readonly=True)
    workspace_name = fields.Char(string='Workspace', readonly=True)
    actor_uid = fields.Integer(string='Changed By (User ID)', required=True, readonly=True)
    actor_login = fields.Char(string='Changed By', readonly=True)
    target_version = fields.Integer(string='Policy Version', readonly=True)
    old_values = fields.Char(string='Previous Minimums', readonly=True)
    new_values = fields.Char(string='New Minimums', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not _is_internal_quota_policy_write(self.env):
            raise AccessError(_('Quota change records are written by the platform only.'))
        return super().create(vals_list)

    def write(self, values):
        raise AccessError(_('Quota change records cannot be modified.'))

    def unlink(self):
        raise AccessError(_('Quota change records cannot be deleted.'))

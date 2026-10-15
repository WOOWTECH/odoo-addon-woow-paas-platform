"""#1679 管理員設定 Workspace quota 最低保障的後台對話框。

只是 ``set_quota_minimums`` 的表單外殼：權限、驗證、降額拒絕、版本、稽核與立即同步都在
那個入口裡，這裡不重做任何一項。對話框的 ACL 只給 ``base.group_system``。
"""
from odoo import _, fields, models


class WorkspaceQuotaMinimumWizard(models.TransientModel):
    _name = 'woow_paas_platform.workspace_quota_minimum_wizard'
    _description = 'Set Workspace Quota Minimums'

    workspace_id = fields.Many2one(
        'woow_paas_platform.workspace', string='Workspace', required=True, readonly=True,
        ondelete='cascade')
    cpu = fields.Integer(
        string='CPU Minimum (cores)', help='Whole cores. 0 clears the minimum (the formula applies).')
    memory_gi = fields.Integer(
        string='Memory Minimum (Gi)', help='Whole Gi. 0 clears the minimum (the formula applies).')
    storage_gi = fields.Integer(
        string='Storage Minimum (Gi)', help='Whole Gi. 0 clears the minimum (the formula applies).')

    def action_apply(self):
        self.ensure_one()
        result = self.workspace_id.set_quota_minimums(
            cpu=self.cpu, memory_gi=self.memory_gi, storage_gi=self.storage_gi)
        state = result.get('state')
        if state == 'synced':
            message, kind = _('Quota minimums saved and applied to the cluster.'), 'success'
        elif state == 'pending_first_deploy':
            message, kind = _('Quota minimums saved. The namespace does not exist yet; they '
                              'apply on the first deployment.'), 'info'
        elif state == 'unconfirmed':
            message, kind = _('Quota minimums saved. The operator did not answer in time, so '
                              'the result is unconfirmed: %(reason)s',
                              reason=result.get('reason') or ''), 'warning'
        else:
            message, kind = _('Quota minimums saved but not applied: %(reason)s',
                              reason=result.get('reason') or ''), 'warning'
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Workspace quota'), 'message': message, 'type': kind,
                       'sticky': kind != 'success', 'next': {'type': 'ir.actions.act_window_close'}},
        }

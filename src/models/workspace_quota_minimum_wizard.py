"""#1679 管理員設定 Workspace quota 最低保障的後台對話框。

只是 ``set_quota_minimums`` 的表單外殼：權限、驗證、降額檢查、版本、稽核與立即同步都在
那個入口裡，這裡不重做任何一項。對話框的 ACL 只給 ``base.group_system``。

#1681：會降低叢集 quota 時分兩步——先 ``preview_quota_minimums`` 顯示每項降額與風險警示，
管理員按確認才帶確認碼呼叫 ``set_quota_minimums``。確認碼由伺服器簽章並綁定這一次的變更，
這裡的欄位被改過（包括經 RPC）就對不上、會被拒絕。
"""
from odoo import _, fields, models
from odoo.exceptions import UserError


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
    step = fields.Selection(
        [('edit', 'Edit'), ('confirm', 'Confirm Decrease')], default='edit', required=True,
        readonly=True)
    confirmation = fields.Char(readonly=True)
    decrease_summary = fields.Text(string='Quota Decrease', readonly=True)
    decrease_warning = fields.Text(string='Risk', readonly=True)

    def _values(self):
        return {'cpu': self.cpu, 'memory_gi': self.memory_gi, 'storage_gi': self.storage_gi}

    def action_apply(self):
        self.ensure_one()
        preview = self.workspace_id.preview_quota_minimums(**self._values())
        state = preview.get('state')
        if state == 'rejected':
            raise UserError(_(
                'The quota cannot be lowered safely, so the minimums were not saved: '
                '%(reasons)s', reasons='; '.join(preview.get('reasons') or ())))
        if state == 'confirmation_required':
            lines = ['%(namespace)s %(key)s: %(cluster)s -> %(target)s (used %(used)s)' % item
                     for item in preview['decreases']]
            self.write({'step': 'confirm', 'confirmation': preview['confirmation'],
                        'decrease_summary': '\n'.join(lines),
                        'decrease_warning': preview['warning']})
            return {'type': 'ir.actions.act_window', 'name': _('Confirm Quota Decrease'),
                    'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
                    'target': 'new'}
        return self._apply(None)

    def action_confirm_decrease(self):
        self.ensure_one()
        if self.step != 'confirm' or not self.confirmation:
            raise UserError(_('Review the change before confirming a quota decrease.'))
        return self._apply(self.confirmation)

    def _apply(self, confirmation):
        result = self.workspace_id.set_quota_minimums(confirmation=confirmation, **self._values())
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

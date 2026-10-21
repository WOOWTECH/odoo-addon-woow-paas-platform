"""System-user-only dialogs for manual credits and needs_review decisions."""
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.llm_backoffice import _named_system_operator
from ..models.llm_credit_order import ORDER_MODEL


class LlmManualCreditWizard(models.TransientModel):
    _name = 'woow_paas_platform.llm_manual_credit_wizard'
    _description = 'Manual AI credit'

    account_id = fields.Many2one('woow_paas_platform.llm_account', required=True,
                                 domain="[('state', 'in', ('active', 'suspended'))]")
    points = fields.Integer(required=True)
    reason = fields.Text(required=True)

    @api.model
    def default_get(self, names):
        _named_system_operator(self.env)
        return super().default_get(names)

    def action_create(self):
        operator = _named_system_operator(self.env)
        self.ensure_one()
        operation = self.env['woow_paas_platform.llm_backoffice_operation']._enqueue(
            operation='manual_credit', request_id='manual-%d' % self.id,
            operator=operator, account=self.account_id, points=self.points, reason=self.reason)
        return {'type': 'ir.actions.act_window',
                'res_model': 'woow_paas_platform.llm_backoffice_operation',
                'view_mode': 'form', 'res_id': operation.id, 'target': 'current'}


class LlmReviewWizard(models.TransientModel):
    _name = 'woow_paas_platform.llm_review_wizard'
    _description = 'Review AI credit order'

    order_id = fields.Many2one(ORDER_MODEL, required=True, readonly=True)
    order_source = fields.Selection(related='order_id.source', readonly=True)
    reason = fields.Text(required=True)
    evidence_summary = fields.Text(compute='_compute_evidence_summary', readonly=True)

    @api.depends('order_id.evidence_json')
    def _compute_evidence_summary(self):
        for wizard in self:
            wizard.evidence_summary = json.dumps(
                wizard.order_id.evidence_json or {}, ensure_ascii=False, sort_keys=True)

    @api.model
    def default_get(self, names):
        _named_system_operator(self.env)
        return super().default_get(names)

    def _apply(self, decision):
        operator = _named_system_operator(self.env)
        self.ensure_one()
        operation = self.env['woow_paas_platform.llm_backoffice_operation']._enqueue(
            operation='review', request_id='review-%d-%s' % (self.id, decision), operator=operator,
            order=self.order_id, decision=decision, reason=self.reason)
        return {'type': 'ir.actions.act_window',
                'res_model': 'woow_paas_platform.llm_backoffice_operation',
                'view_mode': 'form', 'res_id': operation.id, 'target': 'current'}

    def action_mark_credited(self):
        return self._apply('credited')

    def action_retry(self):
        return self._apply('retry')

    def action_void_user_order(self):
        _named_system_operator(self.env)
        if self.order_id.source != 'user':
            raise UserError(_('This action only applies to user conversions.'))
        return self._apply('void')

    def action_cancel_adjustment(self):
        _named_system_operator(self.env)
        if self.order_id.source not in ('manual', 'dr'):
            raise UserError(_('This action only applies to manual or recovery adjustments.'))
        return self._apply('void')


class LlmRechargeWizard(models.TransientModel):
    _name = 'woow_paas_platform.llm_recharge_wizard'
    _description = 'Recharge a late credited AI conversion'

    order_id = fields.Many2one(ORDER_MODEL, required=True, readonly=True)
    reason = fields.Text(required=True)

    @api.model
    def default_get(self, names):
        _named_system_operator(self.env)
        return super().default_get(names)

    def action_recharge(self):
        operator = _named_system_operator(self.env)
        self.ensure_one()
        operation = self.env['woow_paas_platform.llm_backoffice_operation']._enqueue(
            operation='recharge', request_id='recharge-%d' % self.id,
            operator=operator, order=self.order_id, reason=self.reason)
        return {'type': 'ir.actions.act_window',
                'res_model': 'woow_paas_platform.llm_backoffice_operation',
                'view_mode': 'form', 'res_id': operation.id, 'target': 'current'}


class LlmDisableAllKeysWizard(models.TransientModel):
    _name = 'woow_paas_platform.llm_disable_all_keys_wizard'
    _description = 'Disable all mirrored AI API keys'

    reason = fields.Text(required=True)

    @api.model
    def default_get(self, names):
        _named_system_operator(self.env)
        return super().default_get(names)

    def action_enqueue(self):
        operator = _named_system_operator(self.env)
        self.ensure_one()
        operation = self.env['woow_paas_platform.llm_backoffice_operation']._enqueue(
            operation='disable_all_keys', request_id='disable-all-%d' % self.id,
            operator=operator, reason=self.reason)
        return {'type': 'ir.actions.act_window',
                'res_model': 'woow_paas_platform.llm_backoffice_operation',
                'view_mode': 'form', 'res_id': operation.id, 'target': 'current'}

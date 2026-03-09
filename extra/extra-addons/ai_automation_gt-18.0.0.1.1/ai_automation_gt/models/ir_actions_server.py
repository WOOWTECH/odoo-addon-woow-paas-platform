from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.addons.ai_base_gt.models.tools import after_commit


class IrActionsServer(models.Model):
    _inherit = 'ir.actions.server'

    state = fields.Selection(selection_add=[
        ('ai_action', 'AI Action')
    ], ondelete={'ai_action': 'cascade'})

    ai_assistant_id = fields.Many2one(
        'ai.assistant',
        string='AI Assistant',
        required=False,
        help="AI Assistant that will execute this action"
    )
    ai_run_with_sudo = fields.Boolean(
        string='Run as Superuser',
        default=False,
        help="If True, this action will run in superuser mode, bypass all security restrictions."
    )
    ai_prompt = fields.Text(
        string='AI Prompt',
        help="Prompt to send to AI. You can tag records using $model/id syntax. "
             "The triggered records will be automatically attached to the prompt."
    )

    @api.constrains('state', 'ai_assistant_id', 'ai_prompt')
    def _check_ai_action_required_fields(self):
        """Ensure required fields are set when state is ai_action"""
        for action in self:
            if action.state == 'ai_action':
                if not action.ai_assistant_id:
                    raise UserError(_("AI Assistant is required for AI Action."))
                if not action.ai_prompt:
                    raise UserError(_("AI Prompt is required for AI Action."))

    def _run_action_ai_action_multi(self, eval_context=None):
        """
        Execute AI action: Create one thread, tag all records in prompt
        Runs after commit in a separate thread to avoid blocking the main transaction
        """
        self.ensure_one()
        Model = self.env[self.model_id.model]

        # Get all records from context (can be single or multiple records)
        records = Model.browse(self._context.get('active_ids', self._context.get('active_id'))).exists()

        # Prepare prompt and thread
        prompt = self._prepare_prompt(records)
        thread = self._create_ai_thread(records)

        # Execute after commit in a separate thread
        self._execute_ai_action_after_commit(thread.id, prompt)

        return False  # Return False to indicate action completed successfully

    @after_commit(wait=False)
    def _execute_ai_action_after_commit(self, thread_id, prompt):
        """
        Execute AI action after commit in a separate thread
        """
        self.ensure_one()
        thread = self.env['ai.thread'].browse(thread_id)
        if self.ai_run_with_sudo:
            thread = thread.sudo()
        thread._send_request(prompt)

    def _create_ai_thread(self, records):
        """
        Create new thread for this automation action (always create new, don't search for existing)
        """
        self.ensure_one()
        return self.env['ai.thread'].create({
            'name': self.name,
            'assistant_id': self.ai_assistant_id.id,
            'res_model': self._name,
            'res_id': self.id,
            'is_automation': True,
        })

    def _prepare_prompt(self, records):
        """
        Prepare prompt: tag all records in the prompt
        """
        self.ensure_one()
        prompt = self.ai_prompt

        # Collect all record tags
        record_tags = []
        for record in records:
            tag = f"${record._name}/{record.id}"
            if tag not in prompt:
                record_tags.append(tag)

        # Add all tags to beginning of prompt if any are missing
        if record_tags:
            tags_text = " ".join(record_tags)
            prompt = f"Records that trigger this action: {tags_text}.\n\n{prompt}"

        return prompt

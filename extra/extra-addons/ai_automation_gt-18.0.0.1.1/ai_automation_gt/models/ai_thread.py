from odoo import fields, models


class AIThread(models.Model):
    _inherit = 'ai.thread'

    is_automation = fields.Boolean(
        string='Is Automation',
        default=False,
        help="Indicates that this thread was created by an automation rule. "
             "AI should execute in non-interactive mode."
    )

    def _get_thread_context(self):
        """Override to add automation context when is_automation is True"""
        context = super()._get_thread_context()

        if self.is_automation:
            automation_context = (
                "\n\n"
                "IMPORTANT: You have been triggered through an automation rule. "
                "This means you are running in non-interactive mode. "
                "You should:\n"
                "- Execute the requested action directly without asking for confirmation\n"
                "- Do not ask questions or request clarification\n"
                "- Complete the task based on the information available\n"
                "- If you cannot complete the task with available information, "
                "use tool calls to gather necessary data, but do not ask the user\n"
                "- Act autonomously and make decisions based on the context provided"
            )
            context += automation_context

        return context

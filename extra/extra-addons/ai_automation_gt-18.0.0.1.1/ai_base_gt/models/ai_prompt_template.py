from odoo import models, fields


class AIPromptTemplate(models.Model):
    _name = 'ai.prompt.template'
    _description = 'AI Prompt Template'

    name = fields.Char(string="Name", required=True)
    template_content = fields.Text(string="Template Content", required=True)

    def generate_prompt(self, prompt, **kwargs):
        """
        Generate a complete prompt by replacing markers in the template with prompt and kwargs.
        :param str prompt: User prompt
        """
        try:
            return self.template_content.format(prompt=prompt, **kwargs)
        except KeyError as e:
            raise ValueError(f"Missing required template argument: {e}")

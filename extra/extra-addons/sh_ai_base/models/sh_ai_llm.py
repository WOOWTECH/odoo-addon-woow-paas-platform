# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo import fields, models, api
from odoo.exceptions import ValidationError

class ShAiLlm(models.Model):
    _name = 'sh.ai.llm'
    _description = 'SH AI LLM Provider'
    _order = 'name'

    name = fields.Char(string="LLM Name", required=True,
                      help="Display name for this AI model (e.g. 'Gemini 2.5 Pro')")
    sh_company = fields.Char(string="Provider", required=True,
                            help="AI provider company (e.g. 'Google', 'OpenAI', 'Anthropic')")
    sh_model_code = fields.Char(string="Model Code", required=True,
                               help="Technical model identifier for API calls (e.g. 'gemini-2.5-pro' or 'gpt-4o')")

    def _detect_provider_type(self):
        """Auto-detect provider type based on model code"""
        self.ensure_one()
        model_code = (self.sh_model_code or '').lower()

        # OpenAI model patterns
        if any(pattern in model_code for pattern in ['gpt-', 'o1-', 'o3-', 'chatgpt']):
            return 'openai'

        # Default to Gemini
        return 'gemini'
    sh_api_key = fields.Char(string="API Key", groups="sh_ai_base.group_sh_ai_manager",
                            help="Your API key from the provider. This will be used to authenticate API requests.")
    image = fields.Image(string="LLM Icon",
                        help="Upload an icon/logo for this LLM provider. This will be displayed in the chat interface.")
    active = fields.Boolean(string="Active", default=True,
                           help="Uncheck to disable this provider temporarily")
    is_default = fields.Boolean(string="Default LLM", default=False,
                               help="Mark this as the default LLM for new chat sessions")
    temperature = fields.Selection([
        ('precise', 'Precise (0.2) - Best for data queries and accuracy'),
        ('balanced', 'Balanced (0.5) - Good for general use'),
        ('creative', 'Creative (0.8) - Best for text generation'),
    ], string="Response Style", default='precise',
       help="Controls AI creativity vs consistency. Use Precise for accurate data queries.")
    
    
    is_gpt_5_model = fields.Boolean(string="Is GPT-5" , compute="_compute_is_gpt_5_model" , store=True)

    @api.depends('sh_model_code')
    def _compute_is_gpt_5_model(self):
        for llm in self : 
            if llm.sh_model_code and 'gpt-5' in llm.sh_model_code :
                llm.is_gpt_5_model = True 
            else : 
                llm.is_gpt_5_model = False 
                
    def write(self, vals):
        res = super().write(vals)
        if 'is_default' in vals and vals['is_default'] is True:
            for rec in self:
                rec._unset_others_default()            
        return res

    def _unset_others_default(self):
        """Ensure this is the only default"""
        self.search([
            ('id', '!=', self.id),
        ]).write({'is_default': False})

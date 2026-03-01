# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo import api, fields, models
import logging
from ...sh_ai_base.provider.odoo_tools import (
    get_search_records_declaration,
    get_models_list_declaration, get_models_list,
    get_model_fields_declaration,
    get_aggregate_records_declaration,
    get_selection_values_declaration,
    get_current_date_info_declaration,
    get_open_view_declaration,
    fuzzy_lookup_declaration
)
from ...sh_ai_base.provider.prompt_builder import PromptBuilder
from ..ai_processing import AiEngineFactory
from ..ai_processing.utils import sanitize_for_json

_logger = logging.getLogger(__name__)

MAX_FUNCTION_CALLS = 20 # Number of total Function calls per User requests

class ShAiChatMessage(models.Model):
    _name = 'sh.ai.chat.message'
    _description = 'AI Chat Message'
    _order = 'create_date asc'

    session_id = fields.Many2one('sh.ai.chat.session', string="Chat Session", required=True, ondelete='cascade')
    message_type = fields.Selection([
        ('user', 'User Message'),
        ('assistant', 'Assistant Response'),
        ('system', 'System Message'),
        ('error', 'Error Message'),
        ('tool', 'Tool Execution'),
        ('thought', 'AI Reasoning')
    ], string="Message Type", required=True, default='user')
    content = fields.Text(string="Message Content", required=True)
    llm_id = fields.Many2one('sh.ai.llm', string="LLM Provider", help="The LLM provider that generated this message (for assistant messages)")
    action_data = fields.Json(string="Action Data", help="Store action data for 'View All' button (model, domain, fields)")
    query_details = fields.Json(string="Query Details", help="The primary database operation performed (for UI display)")
    debug_info = fields.Json(string="Debug Information", help="Store query details (model, domain, fields, tool calls) for debugging")
    debug_info_formatted = fields.Text(string="Debug Info (Formatted)", compute="_compute_debug_info_formatted", help="Formatted JSON for display")

    # Tracking & Analytics
    prompt_tokens = fields.Integer(string="Prompt Tokens")
    completion_tokens = fields.Integer(string="Completion Tokens")
    total_tokens = fields.Integer(string="Total Tokens")
    tool_call_count = fields.Integer(string="Tool Calls")
    execution_time = fields.Float(string="Execution Time (s)")

    # Keep our original fields for backward compatibility
    user_query = fields.Text(string="User Query", help="User's question or request")
    ai_response = fields.Text(string="AI Response", help="AI's response to the user query")

    @api.depends('debug_info')
    def _compute_debug_info_formatted(self):
        """Format debug_info JSON for display in ace editor"""
        import json
        for record in self:
            if record.debug_info:
                try:
                    record.debug_info_formatted = json.dumps(record.debug_info, indent=2, ensure_ascii=False)
                except Exception:
                    record.debug_info_formatted = str(record.debug_info)
            else:
                record.debug_info_formatted = "{}"

    @api.model
    def create_user_message(self, session_id, content):
        """Helper method to create a user message"""
        return self.create({
            'session_id': session_id,
            'message_type': 'user',
            'content': content,
        })


    def action_view_debug_info(self):
        """Open debug information popup"""
        self.ensure_one()

        if not self.debug_info:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'message': 'No debug information available for this message.',
                    'type': 'warning',
                    'sticky': False,
                }
            }

        return {
            'name': 'AI Response Debug Info',
            'type': 'ir.actions.act_window',
            'res_model': 'sh.ai.chat.message',
            'res_id': self.id,
            'view_mode': 'form',
            'view_id': self.env.ref('sh_ai_assistance.view_sh_ai_chat_message_debug_form').id,
            'target': 'new',
            'context': {'debug_mode': True}
        }

    def open_records_action(self, view_type='list'):
        """Open records in specified view type"""
        self.ensure_one()

        if not self.action_data:
            return {'type': 'ir.actions.act_window_close'}

        action_data = self.action_data
        model_name = action_data.get('model')
        domain = action_data.get('domain', [])
        group_by = action_data.get('group_by')

        _logger.info(f"Opening {view_type} view: model={model_name}, domain={domain}, group_by={group_by}")

        # Ensure domain is a proper list format
        if not isinstance(domain, list):
            domain = []

        # Get model display name
        try:
            model_obj = self.env[model_name]
            model_display_name = model_obj._description or model_name
        except Exception:
            model_display_name = model_name

        # Build context with group_by if provided
        context = {'create': False}
        if group_by:
            context['group_by'] = [group_by]
            _logger.info(f"Applying group_by: {group_by}")

        # Action with specified view type
        action = {
            'type': 'ir.actions.act_window',
            'name': model_display_name,
            'res_model': model_name,
            'view_mode': view_type,
            'views': [(False, view_type)],
            'domain': domain,
            'target': 'new',  # Open in main view, not popup
            'context': context,
        }

        _logger.info(f"Returning {view_type} action: {action}")
        return action

    @api.model
    def migrate_existing_messages(self):
        """Migrate existing assistant messages to have LLM info from their session"""
        assistant_messages = self.search([
            ('message_type', '=', 'assistant'),
            ('llm_id', '=', False)
        ])

        for message in assistant_messages:
            if message.session_id and message.session_id.llm_id:
                message.llm_id = message.session_id.llm_id

        return len(assistant_messages)

    @api.model
    def process_user_message(self, session_id, user_message):
        """Process user message using modular AI processing engines"""
        from datetime import datetime, timedelta
        session = self.env['sh.ai.chat.session'].browse(session_id)
        if not session.exists() or not session.llm_id:
            return {'error': 'Invalid session or LLM provider'}

        llm = session.llm_id.sudo()
        provider_type = llm._detect_provider_type()

        # 1. Update session catalog if needed
        catalog = session.model_catalog
        if not catalog or (datetime.now() - session.catalog_timestamp > timedelta(hours=1)):
            catalog_result = get_models_list(self.env)
            catalog = catalog_result.get('models', [])
            session.write({'model_catalog': catalog, 'catalog_timestamp': datetime.now()})

        # 2. Build configuration for engine
        tool_declarations = [
            get_search_records_declaration(),
            get_models_list_declaration(),
            get_model_fields_declaration(),
            get_aggregate_records_declaration(),
            get_selection_values_declaration(),
            get_current_date_info_declaration(),
            get_open_view_declaration(),
            fuzzy_lookup_declaration(),
        ]
        
        # OPTIMIZATION: Use condensed prompt for follow-up queries
        # First query in session gets full prompt with Model Catalog and examples
        # Follow-up queries get a much smaller prompt to save tokens
        is_followup = session.is_context_initialized
        
        # 3. Load history for context and Technical Memory
        previous_messages = self.search([
            ('session_id', '=', session_id),
            ('message_type', 'in', ['user', 'assistant', 'error'])
        ], order='id asc')

        prompt_builder = PromptBuilder(self.env)
        system_instruction = prompt_builder.build_complete_prompt(
            system_prompt=llm.system_prompt,
            tool_declarations=tool_declarations,
            model_catalog=catalog,
            instructions={
                'context': llm.context_instruction,
                'workflow': llm.workflow_instruction,
                'tool': llm.tool_instruction,
                'formatting': llm.formatting_instruction,
                'security': llm.security_instruction,
                'critical': llm.critical_instruction,
                'examples': llm.examples_instruction,
                'previous_messages': previous_messages, # For Technical Memory extraction
            },
            is_followup=is_followup
        )
        
        # Mark context as initialized after first query
        if not session.is_context_initialized:
            session.write({'is_context_initialized': True})


        temperature_map = {'precise': 0.2, 'balanced': 0.5, 'creative': 0.8}
        
        llm_config = {
            'api_key': llm.sh_api_key,
            'model_code': llm.sh_model_code,
            'system_instruction': system_instruction,
            'temperature': temperature_map.get(llm.temperature or 'precise', 0.2),
            'tool_declarations': tool_declarations,
        }

        # 4. Execute Engine
        start_time = datetime.now()
        engine = AiEngineFactory.get_engine(self.env, provider_type)
        result = engine.execute(session, user_message, previous_messages, llm_config)
        execution_time = (datetime.now() - start_time).total_seconds()

        # 5. Create response message in Odoo
        usage = result.get('usage', {})
        msg_vals = {
            'session_id': session_id,
            'message_type': 'assistant' if result.get('success') else 'error',
            'content': result.get('content') if result.get('success') else result.get('error'),
            'llm_id': llm.id,
            'action_data': result.get('action_data'),
            'query_details': result.get('query_details'),
            'debug_info': result.get('debug_info'),
            'prompt_tokens': usage.get('prompt_tokens', 0),
            'completion_tokens': usage.get('completion_tokens', 0),
            'total_tokens': usage.get('total_tokens', 0),
            'tool_call_count': result.get('tool_call_count', 0),
            'execution_time': execution_time,
        }
        
        # Add metadata to debug info
        if 'debug_info' in msg_vals and msg_vals['debug_info']:
            msg_vals['debug_info']['model_catalog_cached'] = True
            msg_vals['debug_info']['execution_time'] = execution_time
            msg_vals['debug_info']['usage'] = usage
        
        # FINAL SAFETY: Sanitize all JSON fields to prevent serialization errors (like datetime objects)
        for field in ['action_data', 'query_details', 'debug_info']:
            if msg_vals.get(field):
                msg_vals[field] = sanitize_for_json(msg_vals[field])
            
        self.create(msg_vals)
        
        # Auto-rename session if it's the first message
        user_msg_count = self.search_count([('session_id', '=', session_id), ('message_type', '=', 'user')])
        if user_msg_count == 1:
            session.auto_rename_from_message(user_message)

        return result


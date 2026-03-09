# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo import api, fields, models, _
import secrets
import json
import base64


class ShAiChatSession(models.Model):
    _name = 'sh.ai.chat.session'
    _description = 'AI Chat Session'
    _order = 'last_message_date desc, id desc'
    _rec_name = 'display_name'

    name = fields.Char(string="Session Title", required=True, default="New Chat")
    display_name = fields.Char(string="Display Name", compute='_compute_display_name', store=True)
    access_token = fields.Char(string="Access Token", required=True, index=True, help="Unique token for secure session access")
    user_id = fields.Many2one('res.users', string="User", required=True, default=lambda self: self.env.user, ondelete='cascade')
    llm_id = fields.Many2one('sh.ai.llm', string="LLM Provider")
    state = fields.Selection([
        ('active', 'Active'),
        ('archived', 'Archived'),
        ('error', 'Error')
    ], string="Status", default='active')
    is_active = fields.Boolean(string="Active", default=True)
    last_message_date = fields.Datetime(string="Last Message", compute='_compute_message_stats', store=True)
    message_count = fields.Integer(string="Messages", compute='_compute_message_stats', store=True)
    message_ids = fields.One2many('sh.ai.chat.message', 'session_id', string="Messages")

    # Model Catalog Cache for performance and context
    model_catalog = fields.Json(string="Model Catalog Cache", help="Cached list of available Odoo models for this session")
    catalog_timestamp = fields.Datetime(string="Catalog Last Updated", help="When the model catalog was last refreshed")
    
    # Context Optimization: Track if full prompt has been sent
    is_context_initialized = fields.Boolean(
        string="Context Initialized",
        default=False,
        help="True if the full system prompt (with Model Catalog) has been sent. "
             "On subsequent queries, a condensed prompt is used to save tokens."
    )

    # Analytics (Cumulative for session)
    total_prompt_tokens = fields.Integer(string="Total Prompt Tokens", compute='_compute_message_stats', store=True)
    total_completion_tokens = fields.Integer(string="Total Completion Tokens", compute='_compute_message_stats', store=True)
    total_session_tokens = fields.Integer(string="Total Session Tokens", compute='_compute_message_stats', store=True)
    total_tool_calls = fields.Integer(string="Total Tool Calls", compute='_compute_message_stats', store=True)
    total_execution_time = fields.Float(string="Total Execution Time", compute='_compute_message_stats', store=True)

    @api.depends('name')
    def _compute_display_name(self):
        for session in self:
            session.display_name = session.name or "Start New Conversation"

    @api.depends('message_ids', 'message_ids.create_date', 'message_ids.prompt_tokens', 'message_ids.completion_tokens', 'message_ids.tool_call_count', 'message_ids.execution_time')
    def _compute_message_stats(self):
        for session in self:
            messages = session.message_ids
            session.message_count = len(messages)
            session.last_message_date = max(messages.mapped('create_date')) if messages else fields.Datetime.now()
            
            # Sum up analytics
            assistant_messages = messages.filtered(lambda m: m.message_type in ['assistant', 'error'])
            session.total_prompt_tokens = sum(assistant_messages.mapped('prompt_tokens'))
            session.total_completion_tokens = sum(assistant_messages.mapped('completion_tokens'))
            session.total_session_tokens = sum(assistant_messages.mapped('total_tokens'))
            session.total_tool_calls = sum(assistant_messages.mapped('tool_call_count'))
            session.total_execution_time = sum(assistant_messages.mapped('execution_time'))

    def _generate_access_token(self):
        """Generate a unique access token for the session"""
        return secrets.token_urlsafe(32)

    @api.model_create_multi
    def create(self, vals_list):
        import logging
        _logger = logging.getLogger(__name__)

        for vals in vals_list:
            # Force user_id to current user for security (users can only create sessions for themselves)
            vals['user_id'] = self.env.user.id

            # Generate unique access token
            if not vals.get('access_token'):
                new_token = self._generate_access_token()
                vals['access_token'] = new_token
                _logger.info(f"🔑 Generated new token: {new_token}")

            # Auto-generate session name based on timestamp if not provided
            if not vals.get('name') or vals.get('name') in [_('Start New Conversation'), _('New Conversation'), 'New Chat']:
                vals['name'] = _("Chat %s") % fields.Datetime.now().strftime('%Y-%m-%d %H:%M')

            # Assign default LLM if not specified
            if not vals.get('llm_id'):
                default_llm = self.env['sh.ai.llm'].search([('is_default', '=', True)], limit=1)
                if default_llm:
                    vals['llm_id'] = default_llm.id

        sessions = super().create(vals_list)
        for session in sessions:
            _logger.info(f"✅ Session created: ID={session.id}, Token={session.access_token}, User={session.user_id.id}")

        return sessions

    @api.model
    def create_new_session(self):
        """API method to create a new chat session for current user"""
        session = self.create({
            'name': 'New Chat',
            'user_id': self.env.user.id
        })

        return {
            'id': session.id,
            'name': session.name,
            'access_token': session.access_token,
            'display_name': session.display_name,
            'llm_id': [session.llm_id.id, session.llm_id.name] if session.llm_id else False,
        }

    @api.model
    def find_by_token(self, access_token):
        """Find session by access token for current user"""
        import logging
        _logger = logging.getLogger(__name__)

        _logger.info(f"🔍 Searching for session with token: {access_token}")
        _logger.info(f"🔍 Current user: {self.env.user.id} - {self.env.user.name}")

        # Search without user filter first to see if session exists at all
        all_sessions = self.search([('access_token', '=', access_token)])
        _logger.info(f"🔍 Found {len(all_sessions)} session(s) with this token (any user)")

        session = self.search([
            ('access_token', '=', access_token),
            ('user_id', '=', self.env.user.id)
        ], limit=1)

        _logger.info(f"🔍 Found {len(session)} session(s) for current user")

        if session:
            _logger.info(f"✅ Session found: ID={session.id}, Name={session.name}")
            return {
                'id': session.id,
                'name': session.name,
                'access_token': session.access_token,
                'message_count': session.message_count,
                'last_message_date': session.last_message_date,
                'llm_id': [session.llm_id.id, session.llm_id.name] if session.llm_id else False,
            }

        _logger.warning(f"❌ Session not found for token: {access_token}")
        return False

    def auto_rename_from_message(self, message_content):
        """
        Auto-rename session based on first user message.
        Takes first 2 words from message and appends '...'
        Only renames if current name is auto-generated (starts with 'Chat 20')
        """
        self.ensure_one()

        # Only rename if current name is auto-generated timestamp format
        if not self.name or not self.name.startswith('Chat'):
            return False

        # Extract first 4 words from message
        words = message_content.strip().split()
        if len(words) == 0:
            return False

        # Take first 4 words (or just 1 if message is short)
        num_words = min(4, len(words))
        title_words = words[:num_words]
        new_name = ' '.join(title_words)

        # Add ellipsis if there are more words or if we took 4 words
        if len(words) > num_words or num_words == 4:
            new_name += '...'

        # Update session name
        self.name = new_name
        return True
    
    def get_demo_questions(self):
        """
        Get 3 random demo questions based on installed modules in the database.
        Strategy: Select ONE random question from each installed module (mixed approach).
        This ensures diversity and shows features from multiple modules.

        Example:
        - If Sale + Purchase + HR are installed:
          Returns 1 question from Sale, 1 from Purchase, 1 from HR
        - If only 1-2 modules installed:
          Returns all available module questions (less than 3)
        """
        import random

        # Dictionary of all possible questions organized by module
        # NOTE: Using BASIC, beginner-friendly questions focused on counts, totals, and simple summaries
        # These are easier to understand and execute compared to advanced analytics
        questions_by_module = {
            'sale': [
                'How many sales orders do we have?',
                'What is the total number of customers?',
                'How many orders are pending?',
                'Show me the sales order list.',
                'What is the total sales amount?',
            ],
            'purchase': [
                'How many purchase orders do we have?',
                'What is the total number of vendors?',
                'How many purchase orders are pending?',
                'Show me the purchase order list.',
                'What is the total purchase amount?',
            ],
            'crm': [
                'How many leads do we have?',
                'How many opportunities are open?',
                'What is the total number of customers?',
                'Show me the leads list.',
                'How many deals are in progress?',
            ],
            'hr': [
                'What is the total number of employees?',
                'How many employees are active?',
                'How many leave requests are pending?',
                'Show me the employee list.',
                'What is the total headcount?',
            ],
            'account': [
                'How many invoices are pending?',
                'What is the total invoice amount?',
                'How many bills do we have?',
                'Show me the invoice list.',
                'How many customers owe us money?',
            ],
            'inventory': [
                'How many products do we have?',
                'What is the total stock value?',
                'How many items are out of stock?',
                'Show me the product list.',
                'How many stock movements today?',
            ],
            'project': [
                'How many projects do we have?',
                'How many active projects?',
                'How many tasks are pending?',
                'Show me the project list.',
                'How many tasks are overdue?',
            ],
            'product': [
                'How many products do we have?',
                'What is the total product count?',
                'Show me the product list.',
                'How many products are out of stock?',
                'What is the average product price?',
            ],
            'calendar': [
                'How many meetings scheduled?',
                'What events are coming up?',
                'Show me my calendar.',
                'How many meetings today?',
                'What is my schedule?',
            ],
            'contact': [
                'How many contacts do we have?',
                'How many customers are there?',
                'How many vendors do we have?',
                'Show me the contacts list.',
                'How many companies are registered?',
            ],
        }

        # Get list of installed modules
        installed_modules = self.env['ir.module.module'].search([
            ('state', '=', 'installed')
        ]).mapped('name')

        # Strategy: Select ONE random question from each installed module
        # This ensures we get diverse questions from different areas
        selected_questions = []
        modules_with_questions = []

        for module in installed_modules:
            if module in questions_by_module:
                # Pick ONE random question from this module
                question = random.choice(questions_by_module[module])
                selected_questions.append(question)
                modules_with_questions.append(module)

        # Limit to 3 questions
        if len(selected_questions) > 3:
            # If more than 3 modules, pick 3 random ones
            selected_indices = random.sample(range(len(selected_questions)), 3)
            selected_questions = [selected_questions[i] for i in selected_indices]
            modules_with_questions = [modules_with_questions[i] for i in selected_indices]

        # Return success only if we have questions from at least one module
        if selected_questions:
            return {
                'success': True,
                'questions': selected_questions,
                'modules_used': modules_with_questions,
                'total_modules': len([m for m in installed_modules if m in questions_by_module.keys()]),
            }
        else:
            # No relevant modules installed - return fallback questions for new databases
            # IMPORTANT: These questions must be accessible to ALL users (no permission restrictions)
            # Only use questions that access data the current user can view by default
            fallback_questions = [
                'How can I use this AI assistant?',
                'Show me my profile information?',
                'What companies am I associated with?',
            ]

            return {
                'success': True,
                'questions': fallback_questions,
                'modules_used': ['base'],  # These questions use base module features only
                'total_modules': 0,
                'is_fallback': True,
                'message': 'Showing fallback questions for new database',
            }

    def create_session_snapshot(self):
        """
        Creates a snapshot of the current session's messages and stores it
        in a binary field (in the filestore).
        Returns the UUID of the snapshot.
        """
        self.ensure_one()

        # ✅ NEW (public-safe URLs)
        user_avatar_url = f"/ai/avatar/user/{self.user_id.id}"
        llm_image_url = f"/ai/avatar/llm/{self.llm_id.id}"

        messages_data = []
        for msg in self.message_ids.sorted('create_date'):
            # Basic message data
            msg_dict = {
                'id': msg.id,
                'content': msg.content,
                'create_date': msg.create_date.strftime('%Y-%m-%d %H:%M:%S'),
                'type': msg.message_type,  
                'author': 'user' if msg.message_type == 'user' else 'assistant',
            }

            # Set Avatar URL based on type
            if msg.message_type == 'user':
                msg_dict['avatar_url'] = user_avatar_url
            else:
                msg_dict['avatar_url'] = llm_image_url
            
            messages_data.append(msg_dict)


        messages_json_str = json.dumps(messages_data, indent=2)
        messages_base64 = base64.b64encode(messages_json_str.encode('utf-8'))

        snapshot = self.env['sh.ai.chat.session.snapshot'].create({
            'name': self.name,
            'original_session_id': self.id,
            'messages_json': messages_base64,
        })

        return snapshot.snapshot_uuid

    @api.model
    def import_session_from_json(self, messages_data):
        """
        Creates a new session from imported JSON message data.
        """
        if not messages_data or not isinstance(messages_data, list):
            return False

        # Create a new session
        session_vals = {
            'name': _("Imported Chat %s") % fields.Datetime.now().strftime('%Y-%m-%d %H:%M'),
            'user_id': self.env.user.id,
        }
        
        found_llm_id = False
        default_llm = self.env['sh.ai.llm'].search([('is_default', '=', True)], limit=1)
        if default_llm:
            found_llm_id = default_llm.id

        def find_llm(xml_id, code, name):
            if xml_id:
                rec = self.env.ref(xml_id, raise_if_not_found=False)
                if rec and rec._name == 'sh.ai.llm':
                    return rec.id
            if code:
                rec = self.env['sh.ai.llm'].search([('sh_model_code', '=', code)], limit=1)
                if rec: return rec.id
            if name:
                rec = self.env['sh.ai.llm'].search([('name', '=', name)], limit=1)
                if rec: return rec.id
            return False

        for msg in messages_data:
            detected = find_llm(msg.get('llm_xml_id'), msg.get('llm_code'), msg.get('llm'))
            if detected:
                found_llm_id = detected
                break
        
        session_vals['llm_id'] = found_llm_id
        session = self.create(session_vals)

        Message = self.env['sh.ai.chat.message']
        for msg in messages_data:
            content = msg.get('content', '')
            if not content:
                continue

            msg_type = msg.get('author') or msg.get('type')
            if msg_type not in ['user', 'assistant', 'system', 'error']:
                msg_type = 'user' 

            msg_llm_id = found_llm_id
            detected_specific = find_llm(msg.get('llm_xml_id'), msg.get('llm_code'), msg.get('llm'))
            if detected_specific:
                msg_llm_id = detected_specific

            action_data = msg.get('action_data')
            if action_data and isinstance(action_data, dict):
                action_data_value = action_data
            else:
                action_data_value = None

            Message.create({
                'session_id': session.id,
                'content': content,
                'message_type': msg_type,
                'llm_id': msg_llm_id if msg_type != 'user' else False,
                'action_data': action_data_value,
            })

        return {
            'id': session.id,
            'name': session.name,
            'access_token': session.access_token,
        }
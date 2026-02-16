# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

import logging
from datetime import datetime
from .utils import sanitize_for_json
from ...sh_ai_base.provider.odoo_tools import (
    search_records, get_current_date_info, get_models_list,
    get_model_fields, aggregate_records, get_selection_values, open_view,
    fuzzy_lookup
)

_logger = logging.getLogger(__name__)

class BaseAiEngine:
    """
    Base class for AI Processing Engines.
    Provides common methods for tool execution and context management.
    """

    def __init__(self, env):
        self.env = env
        self.max_iterations = 10

    def _execute_tool_call(self, tool_name, tool_args, iteration):
        """Execute Odoo tool and return results"""
        result = None
        last_search_call = None
        last_search_result = None
        action_data = None

        # Sanitize tool_args: Remove None values to allow Odoo defaults to take over
        # This is critical for OpenAI Strict Mode where ALL params are sent as null if unused.
        tool_args = {k: v for k, v in tool_args.items() if v is not None}

        _logger.info(f"🔧 AI Tool Called: {tool_name} with args: {tool_args}")

        try:
            if tool_name == "search_records":
                if not tool_args.get('count_only', False):
                    last_search_call = tool_args
                result = search_records(self.env, **tool_args)
                last_search_result = result
            elif tool_name == "get_current_date_info":
                result = get_current_date_info(self.env)
            elif tool_name == "get_models_list":
                result = get_models_list(self.env)
            elif tool_name == "get_model_fields":
                result = get_model_fields(self.env, **tool_args)
            elif tool_name == "aggregate_records":
                last_search_call = {
                    'model': tool_args.get('model'),
                    'domain': tool_args.get('domain', []),
                    'fields': [tool_args.get('field')] if tool_args.get('field') else [],
                    'group_by': tool_args.get('group_by'),
                    'operation': tool_args.get('operation'),
                }
                result = aggregate_records(self.env, **tool_args)
                last_search_result = result
            elif tool_name == "get_selection_values":
                result = get_selection_values(self.env, **tool_args)
            elif tool_name == "open_view":
                result = open_view(self.env, **tool_args)
                if result.get('success') and result.get('action_data'):
                    action_data = result['action_data']
            elif tool_name == "fuzzy_lookup":
                result = fuzzy_lookup(self.env, **tool_args)
            else:
                result = {"error": f"Unknown function: {tool_name}"}
        except Exception as e:
            _logger.error(f"Error executing tool {tool_name}: {str(e)}", exc_info=True)
            result = {"error": str(e), "success": False}

        # Sanitize everything for JSON storage (converts datetimes to strings etc.)
        return (
            sanitize_for_json(result), 
            sanitize_for_json(last_search_call), 
            sanitize_for_json(last_search_result), 
            sanitize_for_json(action_data)
        )

    def _build_action_data(self, last_search_call, last_search_result):
        """Construct action data for 'View All' buttons based on search results"""
        if not (last_search_call and last_search_result):
            return None

        total_count = last_search_result.get('total_count', 0)
        count = last_search_result.get('count', 0)
        success = last_search_result.get('success', False)

        # Show button if records were truncated or there are many records
        if success and total_count > count and total_count > 0:
            action = {
                'model': last_search_call.get('model'),
                'domain': last_search_call.get('domain', []),
                'total_count': total_count,
            }
            group_by = last_search_call.get('group_by') or last_search_result.get('group_by')
            if group_by:
                action['group_by'] = group_by
            return action
        
        return None

    def execute(self, session, user_message, previous_messages, llm_config):
        """Must be implemented by child classes"""
        raise NotImplementedError("Subclasses must implement execute()")

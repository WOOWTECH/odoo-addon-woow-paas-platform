# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

import logging
from datetime import datetime
from google.genai import types
from .base_engine import BaseAiEngine
from .utils import clean_ai_response, get_friendly_error_message, OdooJSONEncoder
from ...sh_ai_base.provider.gemini_provider import GeminiProvider

_logger = logging.getLogger(__name__)

class GeminiEngine(BaseAiEngine):
    """
    Processing engine for Google Gemini models with multi-tool turn support.
    """

    def _convert_tools(self, tool_declarations):
        """Clean tool declarations for Gemini compatibility"""
        cleaned = []
        for t in tool_declarations:
            # Create a copy and remove OpenAI-specific keys that might confuse Gemini
            t_copy = dict(t)
            if "strict" in t_copy: del t_copy["strict"]
            if "parameters" in t_copy and "additionalProperties" in t_copy["parameters"]:
                t_copy["parameters"] = dict(t_copy["parameters"])
                del t_copy["parameters"]["additionalProperties"]
            cleaned.append(t_copy)
        return cleaned

    def execute(self, session, user_message, previous_messages, llm_config):
        api_key = llm_config.get('api_key')
        model_code = llm_config.get('model_code')
        system_instruction = llm_config.get('system_instruction')
        temperature = llm_config.get('temperature', 0.2)
        tool_declarations = llm_config.get('tool_declarations', [])

        provider = GeminiProvider(api_key=api_key)
        
        # Clean and convert tools
        google_tools = types.Tool(function_declarations=self._convert_tools(tool_declarations))
        
        # Tool configuration for reliability
        tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="AUTO" # Using AUTO as VALIDATED is still in preview, but Gemini 1.5 is very strict with schemas.
            )
        )

        config = types.GenerateContentConfig(
            tools=[google_tools],
            tool_config=tool_config,
            system_instruction=system_instruction,
            temperature=temperature
        )

        # Build conversation history with strict role alternation for Gemini
        contents = []
        for msg in previous_messages:
            role = "user" if msg.message_type == 'user' else "model"
            
            # Gemini strictly requires turn alternation (User -> Model -> User -> Model)
            if contents and contents[-1].role == role:
                # If consecutive same-role messages, merge their content into the previous turn
                prev_text = contents[-1].parts[0].text if contents[-1].parts else ""
                contents[-1].parts = [types.Part(text=f"{prev_text}\n\n{msg.content}")]
            else:
                contents.append(types.Content(role=role, parts=[types.Part(text=msg.content)]))
        
        # Ensure the conversation starts with a User turn (Gemini requirement)
        if contents and contents[0].role != 'user':
            contents.pop(0)

        contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

        ai_response = None
        action_data = None
        last_search_call = None
        last_search_result = None
        debug_tool_calls = []
        iteration = 0
        
        # Tracking tokens across multi-turn calls
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0

        try:
            for iteration in range(self.max_iterations):
                response = provider.generate_content(model=model_code, contents=contents, config=config)
                
                # Accmulate tokens
                usage = getattr(response, 'usage_metadata', None)
                if usage:
                    total_prompt_tokens += (usage.prompt_token_count or 0)
                    total_completion_tokens += (usage.candidates_token_count or 0)
                    total_tokens += (usage.total_token_count or 0)

                has_function_call = False
                if response.candidates:
                    candidate = response.candidates[0]
                    if hasattr(candidate, 'content') and candidate.content.parts:
                        # Find all function calls in the current candidate
                        function_calls = [p.function_call for p in candidate.content.parts if hasattr(p, 'function_call') and p.function_call]
                        
                        if function_calls:
                            has_function_call = True
                            _logger.info(f"⚡ Gemini multi-call: Processing {len(function_calls)} tools")
                            
                            # 1. Add the model's call to history
                            contents.append(candidate.content)
                            
                            # 2. Execute all calls and collect response parts
                            response_parts = []
                            for function_call in function_calls:
                                tool_name = function_call.name
                                tool_args = dict(function_call.args)

                                # Execute Tool (sequentially for Odoo safety)
                                result, s_call, s_res, a_data = self._execute_tool_call(
                                    tool_name, tool_args, iteration
                                )

                                if s_call: last_search_call = s_call
                                if s_res: last_search_result = s_res
                                if a_data: action_data = a_data

                                debug_tool_calls.append({
                                    'tool': tool_name, 'args': tool_args, 'iteration': iteration + 1,
                                    'result': {'success': result.get('success'), 'count': result.get('count'), 'error': result.get('error')},
                                    'raw_result': result # Full technical memory for follow-up turns
                                })

                                response_parts.append(types.Part.from_function_response(
                                    name=tool_name, 
                                    response={"result": result}
                                ))
                            
                            # 3. Add all responses as a single turn from 'user'
                            contents.append(types.Content(role="user", parts=response_parts))
                            continue # Check if more steps are needed

                # If no function calls, it's the final text response
                if not has_function_call:
                    ai_response = response.text if hasattr(response, 'text') else None
                    if not ai_response and response.candidates:
                        parts = [p.text for p in response.candidates[0].content.parts if hasattr(p, 'text') and p.text]
                        ai_response = ' '.join(parts) if parts else "I've processed your request."
                    break

            # Build detailed query information for UI (regardless of 'View All' logic)
            query_details = None
            if last_search_call:
                query_details = {
                    'model': last_search_call.get('model'),
                    'domain': last_search_call.get('domain', []),
                    'operation': last_search_call.get('operation', 'search'),
                    'fields': last_search_call.get('fields', []),
                    'group_by': last_search_call.get('group_by'),
                }

            return {
                'success': True,
                'content': clean_ai_response(ai_response),
                'action_data': action_data,
                'query_details': query_details,
                'usage': {
                    'prompt_tokens': total_prompt_tokens,
                    'completion_tokens': total_completion_tokens,
                    'total_tokens': total_tokens,
                },
                'tool_call_count': len(debug_tool_calls),
                'debug_info': {
                    'tool_calls': debug_tool_calls,
                    'total_iterations': iteration + 1,
                    'final_query': last_search_call,
                    'timestamp': datetime.now().isoformat()
                }
            }

        except Exception as e:
            _logger.error(f"Gemini Engine Error: {str(e)}", exc_info=True)
            return {
                'success': False,
                'error': get_friendly_error_message(e),
                'debug_info': {'error': str(e), 'total_iterations': iteration + 1, 'tool_calls': debug_tool_calls}
            }

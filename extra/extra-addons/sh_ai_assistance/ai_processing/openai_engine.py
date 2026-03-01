import json
import logging
from datetime import datetime
from .base_engine import BaseAiEngine
from .utils import clean_ai_response, get_friendly_error_message, OdooJSONEncoder
from ...sh_ai_base.provider.openai_provider import OpenAIProvider

_logger = logging.getLogger(__name__)

class OpenAiEngine(BaseAiEngine):
    """
    Processing engine for OpenAI models.
    """

    def _convert_tools(self, tool_declarations):
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
                "strict": t.get("strict", False)
            }
        } for t in tool_declarations]

    def execute(self, session, user_message, previous_messages, llm_config):
        api_key = llm_config.get('api_key')
        model_code = llm_config.get('model_code')
        system_instruction = llm_config.get('system_instruction')
        temperature = llm_config.get('temperature', 0.2)
        tool_declarations = llm_config.get('tool_declarations', [])

        provider = OpenAIProvider(api_key=api_key)
        openai_tools = self._convert_tools(tool_declarations)

        messages = [{"role": "system", "content": system_instruction}]
        
        # Build simple conversation history
        # Technical Memory is now automatically injected into the 'system_instruction' 
        # via PromptBuilder for all providers, ensuring consistent performance.
        for msg in previous_messages:
            role = "user" if msg.message_type == 'user' else "assistant"
            # OpenAI is flexible with roles, but we maintain clean alternation for consistency
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n\n{msg.content}"
            else:
                messages.append({"role": role, "content": msg.content})
        
        messages.append({"role": "user", "content": user_message})

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
                response = provider.generate_content(
                    model=model_code, messages=messages, 
                    tools=openai_tools, temperature=temperature
                )
                
                # Accumulate tokens
                usage = getattr(response, 'usage', None)
                if usage:
                    total_prompt_tokens += usage.prompt_tokens
                    total_completion_tokens += usage.completion_tokens
                    total_tokens += usage.total_tokens

                message = response.choices[0].message

                if message.tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} 
                            for tc in message.tool_calls
                        ]
                    })

                    # Odoo Safety: Use sequential execution for database thread safety
                    for tool_call in message.tool_calls:
                        tool_name = tool_call.function.name
                        tool_args = json.loads(tool_call.function.arguments)

                        result, s_call, s_res, a_data = self._execute_tool_call(tool_name, tool_args, iteration)

                        if s_call: last_search_call = s_call
                        if s_res: last_search_result = s_res
                        if a_data: action_data = a_data

                        debug_tool_calls.append({
                            'tool': tool_name, 'args': tool_args, 'iteration': iteration + 1,
                            'result': {'success': result.get('success'), 'count': result.get('count'), 'error': result.get('error')},
                            'raw_result': result, # Full technical memory
                            'response_id': tool_call.id # Required for OpenAI history continuity
                        })

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result, cls=OdooJSONEncoder)
                        })
                    continue

                ai_response = message.content
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

            if not action_data:
                action_data = self._build_action_data(last_search_call, last_search_result)

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
            _logger.error(f"OpenAI Engine Error: {str(e)}", exc_info=True)
            return {
                'success': False,
                'error': get_friendly_error_message(e),
                'debug_info': {'error': str(e), 'total_iterations': iteration + 1, 'tool_calls': debug_tool_calls}
            }

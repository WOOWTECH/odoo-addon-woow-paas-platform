import json
from openai import APIError, AuthenticationError, RateLimitError
from odoo import models, _
from odoo.exceptions import UserError


class AIThread(models.Model):
    _inherit = 'ai.thread'

    def _get_tools_spec_chatgpt(self):
        """Convert tool specifications to ChatGPT function calling format.

        Returns:
            list: List of tool definitions wrapped with type='function'
        """
        return [
            dict(type='function', **tool)
            for tool in self._get_tools_spec()
        ]

    def _do_request_chatgpt(self, message):
        """Handle ChatGPT API request.

        Builds request parameters and sends to OpenAI Responses API.
        Includes thread context as instructions and tool specifications
        if available.

        Args:
            message: The message record triggering this request

        Returns:
            Response: OpenAI API response object

        Raises:
            UserError: If API call fails with descriptive error message
        """
        self.ensure_one()
        config = self.sudo().config_id
        client = config._get_ai_client()

        params = {
            "model": config.model,
            "input": self._prepare_message_history_chatgpt(self.message_ids),
            "temperature": config.temperature,
            "max_output_tokens": config.max_tokens,
        }
        if thread_context := self._get_thread_context():
            params["instructions"] = thread_context
        if tools_spec := self._get_tools_spec_chatgpt():
            params["tools"] = tools_spec
            # Disable parallel tool calls - base module only handles one at a time
            params["parallel_tool_calls"] = False

        try:
            return client.responses.create(**params)
        except AuthenticationError:
            raise UserError(_("ChatGPT Authentication Error: Invalid API key. Please check your configuration."))
        except RateLimitError as e:
            raise UserError(_("ChatGPT Rate Limit Error: %s") % str(e))
        except APIError as e:
            raise UserError(_("ChatGPT API Error: %s") % str(e))
        except Exception as e:
            raise UserError(_("ChatGPT Error: %s") % str(e))

    def _prepare_message_history_chatgpt(self, messages):
        """Convert Odoo message records to ChatGPT message format.

        Args:
            messages: Recordset of ai.message records

        Returns:
            list: Flattened list of message content dictionaries
        """
        import logging
        _logger = logging.getLogger(__name__)

        self.ensure_one()
        message_history = []

        # Track function calls to ensure each has a corresponding output
        pending_function_calls = {}  # call_id -> True

        for msg in messages:
            items = msg._prepare_message_content_chatgpt()
            for item in items:
                item_type = item.get('type', '')

                # Track function calls
                if item_type == 'function_call':
                    call_id = item.get('call_id')
                    if call_id:
                        pending_function_calls[call_id] = True
                        _logger.info("ChatGPT: Found function_call with call_id=%s", call_id)

                # Track function call outputs
                elif item_type == 'function_call_output':
                    call_id = item.get('call_id')
                    if call_id:
                        if call_id in pending_function_calls:
                            del pending_function_calls[call_id]
                            _logger.info("ChatGPT: Found matching function_call_output for call_id=%s", call_id)
                        else:
                            _logger.warning("ChatGPT: function_call_output for unknown call_id=%s", call_id)

            message_history.extend(items)

        # Log any unmatched function calls
        for call_id in pending_function_calls:
            _logger.error("ChatGPT: No function_call_output found for call_id=%s", call_id)

        return message_history

    def _dump_response_json_chatgpt(self, response):
        """Serialize ChatGPT response to JSON for storage.

        Clears large fields (instructions, tools) that don't need to be stored
        but keeps them as empty values to maintain schema compatibility.

        Args:
            response: OpenAI Response object

        Returns:
            str: JSON string representation of the response
        """
        self.ensure_one()
        data = response.model_dump()
        # Clear large fields but keep them for schema compatibility
        data['instructions'] = None
        data['tools'] = []
        return json.dumps(data, indent=2)

    def _parse_response_tool_chatgpt(self, response):
        """Extract function call from ChatGPT response if present.

        Args:
            response: OpenAI Response object

        Returns:
            dict: Function call dictionary if found, False otherwise
        """
        for item in response.output:
            if item.type == 'function_call':
                return item.model_dump()
        return False

    def _execute_tool_chatgpt(self, func_call):
        """Execute a tool/function with arguments from ChatGPT response.

        Args:
            func_call: Dictionary containing function name and arguments

        Returns:
            Result of the tool execution
        """
        func_name = func_call['name']
        arguments = json.loads(func_call['arguments'])
        return self._run_tool(func_name, **arguments)

    def _parse_response_text_chatgpt(self, response):
        """Extract text content from ChatGPT response.

        Args:
            response: OpenAI Response object

        Returns:
            str: The text content of the response
        """
        return response.output_text

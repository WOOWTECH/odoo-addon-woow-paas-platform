import json
import logging
from odoo import models
from odoo.addons.ai_base_gt.models.ai_thread import MESSAGE_TYPE_ROLE_MAP

_logger = logging.getLogger(__name__)

# Supported attachment MIME types for ChatGPT
SUPPORTED_IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp')
SUPPORTED_FILE_TYPES = ('application/pdf',)


class AIMessage(models.Model):
    _inherit = 'ai.message'

    def _filter_legit_attachments(self):
        """Filter attachments to only include ChatGPT-supported types.

        ChatGPT currently supports images (JPEG, PNG, GIF, WebP) and PDFs.

        Returns:
            Recordset: Filtered attachment records
        """
        if self.thread_id.sudo().config_id.type == 'chatgpt':
            return self.message_attachment_ids.filtered(
                lambda att: (
                    att.mimetype.startswith('image/') or
                    att.mimetype in SUPPORTED_FILE_TYPES
                )
            )
        return super()._filter_legit_attachments()

    def _prepare_message_content_chatgpt(self):
        """Convert Odoo message to ChatGPT API format.

        Handles three types of content:
        - Stored responses: Parsed from JSON and returned as-is
        - Function call results: Formatted with call_id reference
        - Regular messages: Formatted with role and content array

        Also handles multimodal content (images, PDFs) as attachments.

        Returns:
            list: List of content dictionaries in ChatGPT format
        """
        self.ensure_one()

        # Handle stored responses - use plain JSON parsing for robustness
        if self.response:
            try:
                response_data = json.loads(self.response)
                output_items = response_data.get('output', [])
                # Filter out None values from each output item
                return [
                    {k: v for k, v in item.items() if v is not None}
                    for item in output_items
                ]
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                _logger.warning(
                    "Failed to parse stored response for message %s: %s",
                    self.id, e
                )
                return []

        content = {
            'role': MESSAGE_TYPE_ROLE_MAP[self.message_type],
            'content': [],
        }

        if self.content:
            content["content"].append({
                "type": "input_text",
                "text": self.content_full
            })

        # Handle function call results
        if self.func_result:
            call_id = None
            # Find the call_id from the previous message
            prev_message = self.thread_id.message_ids.filtered(lambda m: m.id < self.id)[-1:]

            if prev_message:
                # First try: look in func_call field
                if prev_message.func_call:
                    func_call = json.loads(prev_message.func_call)
                    call_id = func_call.get('call_id')

                # Second try: look in stored response's output for function_call
                if not call_id and prev_message.response:
                    try:
                        response_data = json.loads(prev_message.response)
                        for item in response_data.get('output', []):
                            if item.get('type') == 'function_call':
                                call_id = item.get('call_id')
                                break
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass

            if not call_id:
                _logger.warning(
                    "Missing call_id in func_call for message %s, using fallback",
                    self.id
                )
                call_id = f"call_{prev_message.id}" if prev_message else "call_unknown"

            content = {
                "type": "function_call_output",
                "call_id": call_id,
                "output": self.func_result
            }
        # Handle legacy function calls (backward compatibility with Chat Completion API)
        elif self.func_call:
            func_call = json.loads(self.func_call)
            func_type = func_call.get('type', '')
            if func_type == 'function':
                # Convert old Chat Completion API format to Response API format
                func_data = func_call.get('function', {})
                content = {
                    "type": "function_call",
                    "call_id": func_call.get('id', f"call_{self.id}"),
                    "name": func_data.get('name', 'unknown'),
                    "arguments": func_data.get('arguments', '{}')
                }
            elif 'call_id' in func_call:
                # Already in Response API format
                content = func_call
            else:
                _logger.warning(
                    "Unknown func_call format for message %s: %s",
                    self.id, func_type
                )
                content = func_call

        contents = [content]

        # Handle attachments (multimodal content)
        att_contents = []
        for attachment in self.legit_attachment_ids:
            if attachment.mimetype.startswith('image/'):
                att_contents.append({
                    "type": "input_image",
                    "image_url": f"data:{attachment.mimetype};base64,{attachment.datas.decode('utf-8')}"
                })
            elif attachment.mimetype in SUPPORTED_FILE_TYPES:
                att_contents.append({
                    "type": "input_file",
                    "filename": attachment.name,
                    "file_data": f"data:{attachment.mimetype};base64,{attachment.datas.decode('utf-8')}"
                })
            else:
                _logger.warning(
                    "Unsupported attachment type %s for message %s, skipping",
                    attachment.mimetype, self.id
                )

        if att_contents:
            if 'content' in content:
                content['content'].extend(att_contents)
            else:
                # Create separate user message for attachments
                # (required because non-user roles don't allow files)
                contents.append({
                    'role': 'user',
                    'content': att_contents
                })

        return contents

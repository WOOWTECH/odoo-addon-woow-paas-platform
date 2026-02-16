import base64
import hashlib
import json
import textwrap
import re
import inspect
from typing import Any, Optional

import odoo.release
from odoo import api, models, fields, Command, _
from odoo.osv import expression
from odoo.tools import json_default, DEFAULT_SERVER_DATETIME_FORMAT, ormcache
from odoo.exceptions import UserError, AccessError

from odoo.addons.iap.tools import iap_tools
from odoo.addons.web_editor.controllers.main import DEFAULT_OLG_ENDPOINT

from .tools import ai_spec, ai_tool, dict_list_to_csv


MESSAGE_TYPE_ROLE_MAP = {
    'system': 'system',
    'prompt': 'user',
    'response': 'assistant',
}


class AIThread(models.Model):
    _name = 'ai.thread'
    _description = 'AI Conversation Thread'
    _order = 'id desc'

    name = fields.Char(string="Thread Name", required=True)
    assistant_id = fields.Many2one('ai.assistant', string="Assistant", required=True, index=True)
    config_id = fields.Many2one('ai.config', string="Configuration", related='assistant_id.config_id',
                                groups='base.group_system')
    ai_user_id = fields.Many2one('res.users', string="AI User", related='assistant_id.user_id')
    ai_partner_id = fields.Many2one('res.partner', string="AI Partner", related='assistant_id.partner_id')
    context_id = fields.Many2one('ai.context', string="Thread Context", compute='_compute_context_id',
                                 precompute=True, store=True, readonly=False)
    prompt_template_id = fields.Many2one('ai.prompt.template', string="Prompt Template")
    prompt = fields.Text(string="Prompt")
    message_ids = fields.One2many('ai.message', 'thread_id', string="Messages")
    res_model = fields.Char(string='Res Model', index='btree_not_null', readonly=True)
    res_id = fields.Integer(string='Res ID', index='btree_not_null', readonly=True)
    last_error = fields.Text(string='Last Error', readonly=True)

    @api.depends('assistant_id')
    def _compute_context_id(self):
        for r in self:
            r.context_id = r.assistant_id.context_id

    def _get_tools(self):
        self.ensure_one()
        result = []
        for attr in dir(self):
            if attr in self._fields:
                continue
            method = getattr(self, attr, None)
            if callable(method) and getattr(method, 'ai_tool', False):
                condition = getattr(method, 'ai_condition', None)
                if condition is None or condition(self):
                    result.append(method)
        return result

    @ormcache('method.__name__')
    def _generate_tool_spec(self, method):
        return ai_spec(method)

    def _get_tools_spec(self):
        self.ensure_one()
        result = []
        for method in self._get_tools():
            result.append(self._generate_tool_spec(method))
        return result

    def _get_thread_context(self):
        """Build system prompt: identity, communication rules, focus record, instructions, capabilities."""
        self.ensure_one()
        config = self.sudo().config_id
        contexts = []

        # -------------------------------------------------------------------------
        # Identity & environment
        # -------------------------------------------------------------------------
        contexts.append((
            "You are an AI assistant named %s, integrated in an Odoo system.\n"
            "System: Odoo %s | Company: %s (ID: %s) | Time: %s (UTC)"
        ) % (
            self.assistant_id.name,
            odoo.release.version,
            self.env.company.name,
            self.env.company.id,
            fields.Datetime.now().strftime(DEFAULT_SERVER_DATETIME_FORMAT),
        ))

        contexts.append((
            "Current user: %s | Type: %s | Partner ID: %s | Email: %s | Phone: %s | Company: %s"
        ) % (
            self.env.user.name,
            self.env.user._get_type(),
            self.env.user.partner_id.id,
            self.env.user.email or '-',
            self.env.user.phone or '-',
            self.env.user.partner_id.parent_name or '-',
        ))

        env_context = dict(self.env.context)
        env_context.pop('lang', None)
        env_context.pop('__action_done', None)
        contexts.append("Environment context: %s" % json.dumps(
            env_context, ensure_ascii=False, default=json_default
        ))

        # -------------------------------------------------------------------------
        # Communication (user-facing behavior)
        # -------------------------------------------------------------------------
        contexts.append("Respond in the same language as the user. Use standard Markdown.")
        contexts.append(
            "When explaining your plan or results to the user, use plain language only: "
            "say what you will do or what you found (e.g. 'I will look up…', 'I found …') "
            "and do not mention technical or internal names (model names, field names, IDs)."
        )

        # -------------------------------------------------------------------------
        # Thread focus & custom instructions
        # -------------------------------------------------------------------------
        if self.res_model and self.res_id:
            rec = self.env[self.res_model].browse(self.res_id)
            contexts.append(
                "This conversation is about: %s (record type: %s, ID: %s)"
                % (rec.display_name, rec._name, rec.id)
            )

        if self.context_id:
            contexts.append("Instructions: " + self.context_id.context.strip())

        # -------------------------------------------------------------------------
        # Capabilities (reference)
        # -------------------------------------------------------------------------
        if tools_spec := self._get_tools_spec():
            contexts.append(
                "You can use the system capabilities below to look up or change data when needed; "
                "use them as many times as needed. If something is missing, try another way; "
                "if you still cannot answer, say so. If the request is unclear, ask for clarification."
            )

            if config.type == 'odooai':
                contexts.append(
                    "Available capabilities (OpenAPI spec):\n```\n%s\n```\n"
                    "To use a capability, include in your response a JSON block: "
                    '{"tool_call": {"name": "…", "kwargs": {…}}}'
                    % json.dumps(tools_spec, ensure_ascii=False, indent=2)
                )

            if data_sources_info := self.assistant_id._get_searchable_data_sources_info():
                contexts.append(
                    "Searchable data sources (search by meaning):\n```csv\n%s\n```"
                    % dict_list_to_csv(data_sources_info)
                )

            if models_info := self.assistant_id._get_accessible_models_info():
                contexts.append((
                    "Record types you can work with:\n```csv\n%s\n```\n"
                    "You can retrieve the structure of a record type with '_get_model_fields_spec' tool when needed. "
                    "When you mention a record, include a clickable link when available (e.g. Markdown: [label](url))."
                ) % dict_list_to_csv(models_info))

        return "\n\n".join(contexts)

    def action_send_request(self):
        if not self.prompt:
            raise UserError(_("Please enter a prompt."))
        res = self._send_request(self.prompt, template_id=self.prompt_template_id.id)
        self.prompt = False
        return res

    def _send_request(self, prompt, prompt_message_id=None, template_id=None, **kwargs):
        """
        Send a request to the AI and return the response content.
        :param str prompt: User prompt
        :param int template_id: ID of the prompt template to use
        :return: tuple of prompt message and response message
        """
        self.ensure_one()
        config = self.sudo().config_id

        try:
            if not prompt_message_id:
                prompt_message = self._create_prompt_message(prompt, template_id, **kwargs)
            else:
                prompt_message = self.env['ai.message'].sudo().browse(prompt_message_id)

            if not self.env.su:
                prompt_message = prompt_message.sudo(False)

            response = getattr(self, f'_do_request_{config.type}')(prompt_message)
            response_json = getattr(self, f'_dump_response_json_{config.type}')(response)
            func_call = getattr(self, f'_parse_response_tool_{config.type}')(response)
            text = getattr(self, f'_parse_response_text_{config.type}')(response)
            response_message = self.env['ai.message'].create({
                'thread_id': self.id,
                'message_type': 'response',
                'content': text and text.strip() or "",
                'response': response_json,
                'func_call': json.dumps(func_call, ensure_ascii=False, indent=2, default=json_default) if func_call else False,
                'author_id': self.ai_partner_id.id,
            })

            while func_call:
                func_res, extra_vals = getattr(self, f'_execute_tool_{config.type}')(func_call)
                result_message = self._create_tool_result(prompt, func_res, extra_vals)
                response = getattr(self, f'_do_request_{config.type}')(result_message)
                response_json = getattr(self, f'_dump_response_json_{config.type}')(response)
                func_call = getattr(self, f'_parse_response_tool_{config.type}')(response)
                text = getattr(self, f'_parse_response_text_{config.type}')(response)
                response_message = self.env['ai.message'].create({
                    'thread_id': self.id,
                    'message_type': 'response',
                    'response': response_json,
                    'content': text and text.strip() or "",
                    'func_call': json.dumps(func_call, ensure_ascii=False, indent=2, default=json_default) if func_call else False,
                    'author_id': self.ai_partner_id.id,
                })

            self._post_request_hook(prompt_message, response_message)

        except Exception as e:
            self.env.cr.rollback()
            self.last_error = str(e)
            self.env.cr.commit()
            raise
        else:
            self.last_error = False
            return prompt_message, response_message

    def _get_attachments_to_request(self, kwargs):
        self.ensure_one()
        return kwargs.get('attachments', False)

    def _create_prompt_message(self, prompt, template_id=None, **kwargs):
        self.ensure_one()
        if template_id:
            prompt = self.env['ai.prompt.template'].browse(template_id).generate_prompt(
                prompt,
                **kwargs.pop('template_kwargs', {})
            )
        message_vals = {
            'thread_id': self.id,
            'message_type': 'prompt',
            'content': prompt.strip(),
            'author_id': self.env.user.partner_id.id,
        }
        attachments = self._get_attachments_to_request(kwargs)
        if attachments:
            message_vals['message_attachment_ids'] = [Command.create({'attachment_id': att.id}) for att in attachments]
        return self.env['ai.message'].sudo().create(message_vals)

    def _do_request_odooai(self, message):
        self.ensure_one()
        message_history = self._prepare_message_history_odooai(self.message_ids - message)
        if thread_context := self._get_thread_context():
            message_history.insert(0, {'role': 'system', 'content': thread_context})

        try:
            IrConfigParameter = self.env['ir.config_parameter'].sudo()
            olg_api_endpoint = IrConfigParameter.get_param('web_editor.olg_api_endpoint', DEFAULT_OLG_ENDPOINT)
            database_id = IrConfigParameter.get_param('database.uuid')
            response = iap_tools.iap_jsonrpc(olg_api_endpoint + "/api/olg/1/chat", params={
                'prompt': message.content,
                'conversation_history': message_history or [],
                'database_id': database_id,
            }, timeout=30)
            if response['status'] == 'success':
                return response
            elif response['status'] == 'error_prompt_too_long':
                raise UserError(_("Sorry, your prompt is too long. Try to say it in fewer words."))
            elif response['status'] == 'limit_call_reached':
                raise UserError(
                    _("You have reached the maximum number of requests for this service. Try again later."))
            else:
                raise UserError(_("Sorry, we could not generate a response. Please try again later."))
        except AccessError:
            raise AccessError(_("Oops, it looks like our AI is unreachable!"))

    def _prepare_message_history_odooai(self, messages):
        self.ensure_one()
        message_history = [
            {'role': MESSAGE_TYPE_ROLE_MAP[msg.message_type], 'content': msg.content_full}
            for msg in messages
        ]
        return message_history

    def _dump_response_json_odooai(self, response):
        self.ensure_one()
        return json.dumps(response, ensure_ascii=False, indent=2, default=json_default)

    def _parse_response_tool_odooai(self, response):
        self.ensure_one()
        match = re.search(r'\{\s*"tool_call":\s*{.*}\s*}', response['content'], re.DOTALL)
        if match:
            try:
                func_call = json.loads(match.group(0))
                return func_call.get('tool_call')
            except json.JSONDecodeError:
                return False
        return False

    def _execute_tool_odooai(self, func_call):
        return self._run_tool(func_call['name'], **func_call['kwargs'])

    def _run_tool(self, tool_name, **kwargs):
        self.ensure_one()
        try:
            method = getattr(self, tool_name, None)
            if not method or not getattr(method, 'ai_tool', False):
                raise AccessError(_("Tool %s does not exist.") % tool_name)
            res = method(**kwargs)
            extra_vals = self._prepare_tool_result_extra_vals(tool_name, kwargs, res)
            return res, extra_vals
        except Exception as e:
            return {'error': str(e)}, None

    def _prepare_tool_result_extra_vals(self, tool_name, kwargs, result):
        """Check and convert binary data to attachment to send to AI."""
        self.ensure_one()
        extra_vals = {}
        if tool_name == '_read_binary_field' and result.get('datas'):
            datas = result['datas']
            model = kwargs['model_name']
            res_id = kwargs['res_id']
            fname = kwargs['field_name']
            
            # If model is ir.attachment, res_id is the attachment ID itself
            if model == 'ir.attachment':
                old_att = self.env['ir.attachment'].sudo().browse(res_id)
            else:
                # Try to find existing attachment to copy metadata (mimetype, etc.)
                old_att = self.env['ir.attachment'].sudo().search([
                    ('res_model', '=', model),
                    ('res_field', '=', fname),
                    ('res_id', '=', res_id),
                ], limit=1)
            
            if old_att.exists():
                # Copy existing attachment to preserve data
                new_att = old_att.copy(default={
                    'res_model': self._name,
                    'res_id': self.id,
                })
            else:
                # Create new attachment
                new_att = self.env['ir.attachment'].sudo().create({
                    'name': fname,
                    'res_model': self._name,
                    'res_id': self.id,
                    'datas': datas,
                })
            extra_vals['message_attachment_ids'] = [Command.create({
                'attachment_id': new_att.id,
            })]
            result['datas'] = "<see following attachment for the binary data>"
        return extra_vals

    def _create_tool_result(self, prompt, func_res, extra_vals=None):
        self.ensure_one()
        config = self.sudo().config_id
        vals = {
            'thread_id': self.id,
            'message_type': 'system',
            'func_result': json.dumps(func_res, ensure_ascii=False, indent=2, default=json_default),
            'author_id': self.env.ref('base.partner_root').id,
        }
        if config.type == 'odooai':
            vals['content'] = (
                "Here is the result of your tool call:\n"
                "```\n"
                "%s\n"
                "```\n\n"
                "From that result, make a response for the previous user's prompt, "
                "or make another tool call to get further information. The previous "
                "user's prompt is:\n"
                "%s"
            ) % (
                json.dumps(func_res, ensure_ascii=False, indent=2, default=json_default),
                textwrap.indent(prompt.strip(), '> ')
            )
        if extra_vals:
            vals.update(extra_vals)
        return self.env['ai.message'].create(vals)

    def _parse_response_text_odooai(self, response):
        self.ensure_one()
        # Remove JSON function call from response text (with or without markdown code blocks)
        cleaned_text = re.sub(
            r'```\w*\s*\{\s*"tool_call":\s*{.*}\s*\}\s*```|\{\s*"tool_call":\s*{.*}\s*\}', '',
            response['content'], flags=re.DOTALL
        )
        return cleaned_text.strip()

    def _post_request_hook(self, prompt_message, response_message):
        """
        Hook that is called after the request is processed.
        """
        pass

    @ai_tool(condition=lambda thread: thread.assistant_id.has_vector_access)
    def _semantic_search(self, query: str, top_k: int = 5, data_source_ids: Optional[list[int]] = None) -> list[dict]:
        """
        Search all data sources associated with the assistant for items semantically similar to the given query.
        Can only be used with the data sources that have indexed data.

        When to use this tool:
        - When searching for content based on meaning, context, or semantic similarity
        - When looking for documents, text, or unstructured data related to a concept
        - When you need to find information that might be described in different words but has similar meaning
        - When doing research or knowledge discovery across text-based content
        - When the user asks questions like "find information about...", "what do you know about...", "search for content related to..."

        Do NOT use this tool for:
        - Structured data queries with specific filters or conditions
        - Exact record lookups by ID or specific field values
        - Statistical analysis or aggregated data retrieval

        Args:
            query (str): The query string to search for. Should be concise and to the point.
            top_k (int, optional): The number of top similar results to return. Default is 5. Should be between 1 and 10.
            data_source_ids (list[int], optional): The IDs of the specific data sources to filter results. If not provided, all data sources will be queried.

        Returns:
            list[dict]: A list of dictionaries containing the search results, sorted by similarity.
        """

        self.ensure_one()
        data_sources = self.assistant_id.searchable_data_source_ids
        if data_source_ids:
            data_sources = data_sources.filtered(lambda ds: ds.id in data_source_ids)
        if not data_sources:
            return []

        return self.env['ai.data.item']._search_similar(
            query=query,
            data_sources=data_sources,
            limit=top_k
        )

    def _format_tool_record_values(self, model_name: str, values: list[dict]) -> list[dict]:
        base_url = self.get_base_url()
        return [
            {
                'model': model_name,
                'id': value['id'],
                'url': f"{base_url}/web#id={value['id']}&model={model_name}",
                'data': value
            } for value in values
        ]

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_read_access)
    def _model_search(self, model_name: str, domain: list[Any], fields: list[str], offset: int = 0, limit: int = 10, order: Optional[str] = None) -> dict:
        """
        Similar to Odoo's `search_read` method, but with access restricted to the models and fields
        defined in data sources of type 'model'.

        Note: Binary fields cannot be read using this tool. If you need to read a binary field,
        use the `_read_binary_field` tool instead.

        When to use this tool:
        - When you need to query structured data from Odoo models with specific criteria
        - When looking for records that match exact conditions or filters
        - When you need to retrieve specific fields from database records
        - When doing data analysis, reporting, or statistical queries
        - When the user asks for specific records, lists, or data with conditions like "show me all...", "find records where...", "get data from..."
        - When you need to sort, limit, or paginate results from structured data

        Do NOT use this tool for:
        - Content-based or semantic searches across unstructured text
        - When you're looking for information based on meaning rather than exact field matches
        - When searching for general knowledge or concepts

        Args:
            model_name (str): The technical name of the Odoo model to search in.
            domain (list): The search domain to filter records. The data type of the value element must be the same as the field type.
            fields (list): The list of field names to return in the results. If empty, all allowed fields will be returned.
            offset (int, optional): The number of records to skip. Default is 0.
            limit (int, optional): The maximum number of records to return. Default is 10.
            order (str, optional): The field to sort results by, must be a stored field. If not provided, default model order will be used.

        Returns:
            dict:
                - total_records (int): The total number of records matching the domain.
                - limit (int): The limit value provided.
                - offset (int): The offset value provided.
                - order (str): The order of the records.
                - records (list): A list of dictionaries containing the requested fields for each matching record.
        """
        self.ensure_one()
        allowed_fields = self.assistant_id._check_model_fields_access(model_name, 'read', fields)
        if not fields:
            fields = self.env[model_name].check_field_access_rights('read', allowed_fields)
        domain = expression.AND([self.assistant_id._get_model_domain_access(model_name, 'read'), domain])
        record_values = self.env[model_name].search_read(domain, fields, offset, limit, order)
        return {
            'total_records': self.env[model_name].search_count(domain),
            'limit': limit,
            'offset': offset,
            'order': order or self.env[model_name]._order,
            'records': self._format_tool_record_values(model_name, record_values)
        }

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_read_access)
    def _model_read(self, model_name: str, res_ids: list[int], fields: list[str]) -> list[dict]:
        """
        Similar to Odoo's `read` method, but with access restricted to the models and fields
        defined in data sources of type 'model'.

        Note: Binary fields cannot be read using this tool. If you need to read a binary field,
        use the `_read_binary_field` tool instead.

        Args:
            model_name (str): The technical name of the Odoo model to read from.
            res_ids (list): The list of record IDs to read.
            fields (list): The list of field names to return in the result. If empty, all allowed fields will be returned.

        Returns:
            list[dict]: A list of dictionaries, each containing the requested fields for a record.
        """
        self.ensure_one()
        if not res_ids:
            return []
        allowed_fields = self.assistant_id._check_model_fields_access(model_name, 'read', fields)
        if not fields:
            fields = self.env[model_name].check_field_access_rights('read', allowed_fields)
        domain = expression.AND([self.assistant_id._get_model_domain_access(model_name, 'read'), [('id', 'in', res_ids)]])
        recs = self.env[model_name].search(domain)
        missing_ids = set(res_ids) - set(recs.ids)
        if missing_ids:
            raise UserError(_("Records with ids %s in model %s do not exist or cannot be accessed.") % (', '.join(map(str, missing_ids)), model_name))
        record_values = recs.read(fields)
        return self._format_tool_record_values(model_name, record_values)

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_read_access and thread.assistant_id.allow_files)
    def _read_binary_field(self, model_name: str, res_id: int, field_name: str) -> dict:
        """
        Read a binary field value from a specific record.
        The binary data will be sent as an attachment in the message for AI to process.

        Args:
            model_name (str): The technical name of the Odoo model to read from.
            res_id (int): The record ID to read the binary field from.
            field_name (str): The name of the binary field to read.

        Returns:
            dict:
                - datas (str): Base64 encoded binary data.
                - checksum (str): Checksum of the binary data.
        """
        self.ensure_one()
        if not res_id:
            raise UserError(_("res_id is required."))
        if not field_name:
            raise UserError(_("field_name is required."))

        Model = self.env[model_name]

        # Check if field exists and is binary type
        if field_name not in Model._fields:
            raise UserError(_("Field '%s' does not exist on model '%s'.") % (field_name, model_name))
        field = Model._fields[field_name]
        if field.type != 'binary':
            raise UserError(_("Field '%s' is not a binary field.") % field_name)

        # Check field access with include_binary=True (requires allow_files=True)
        self.assistant_id._check_model_fields_access(model_name, 'read', [field_name], include_binary=True)

        # Check domain access
        domain = expression.AND([self.assistant_id._get_model_domain_access(model_name, 'read'), [('id', '=', res_id)]])
        rec = Model.search(domain, limit=1)
        if not rec:
            raise UserError(_("Record with id %s in model %s does not exist or cannot be accessed.") % (res_id, model_name))

        # Read binary field
        binary_data = rec[field_name]
        if not binary_data:
            raise UserError(_("Field '%s' of record %s is empty.") % (field_name, res_id))

        # Decode base64 to binary for checksum calculation
        try:
            bin_data = base64.b64decode(binary_data) if binary_data else b''
        except Exception:
            bin_data = binary_data.encode('utf-8') if isinstance(binary_data, str) else binary_data

        # Calculate checksum
        checksum = hashlib.sha1(bin_data or b'').hexdigest()

        return {
            'datas': binary_data,
            'checksum': checksum,
        }

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_read_access)
    def _model_read_group(
        self, model_name: str, domain: list[Any], fields: list[str], groupby: list[str],
        offset: Optional[int] = None, limit: Optional[int] = None, orderby: Optional[str] = None
    ) -> list[dict]:
        """
        Similar to Odoo's `read_group` method, but with access restricted to the models and fields
        defined in data sources of type 'model'.

        Args:
            model_name (str): The technical name of the Odoo model to group on.
            domain (list): The search domain to filter records. The data type of the value element must be the same as the field type.
            fields (list): The list of field names to return in the results.
            groupby (list): The list of field names to group by.
            offset (int, optional): The number of records to skip. Default is None (no offset).
            limit (int, optional): The maximum number of records to return. Default is None (no limit).
            orderby (str, optional): The field to sort results by. If not provided, default model order will be used.

        Returns:
            list[dict]: A list of dictionaries containing the grouped results, each dict containing the requested fields for each group.
        """
        self.ensure_one()
        # Check both fields and groupby fields
        check_fields = (fields or []) + (groupby or [])
        self.assistant_id._check_model_fields_access(model_name, 'read', check_fields)
        domain = expression.AND([self.assistant_id._get_model_domain_access(model_name, 'read'), domain])
        return self.env[model_name].read_group(domain, fields, groupby, offset or 0, limit, orderby, lazy=False)

    @ai_tool(
        condition=lambda thread: thread.assistant_id.has_model_create_access,
        params_aliases={
            'vals_list': ['vals', 'values']
        }
    )
    def _model_create(self, model_name: str, vals_list: list[dict]) -> dict:
        """
        Create new records in the specified Odoo model. Similar to Odoo's `create` method,
        but with access restricted to the models and fields defined in data sources of type 'model'.

        Args:
            model_name (str): The technical name of the Odoo model to create records in.
            vals_list (list[dict]): A list of dictionaries, each containing field names and values for the new record(s).

        Returns:
            dict:
                - success (bool): Whether the creation was successful.
                - created_records (list[dict]): List of dictionaries containing the created records with their values.
                - error (str, optional): Error message if creation failed.
        """
        self.ensure_one()
        if not vals_list:
            raise UserError(_("No values provided for creation"))

        Model = self.env[model_name]

        # Get all fields that will be written
        all_fields = set()
        for vals in vals_list:
            all_fields.update(vals.keys())

        # Check model and field access (Odoo will check create permissions automatically)
        self.assistant_id._check_model_fields_access(model_name, 'create', list(all_fields))

        # Create records
        created_records = Model.create(vals_list)

        # Check domain access to ensure created records are within allowed domain
        domain = self.assistant_id._get_model_domain_access(model_name, 'create')
        if domain:
            accessible_records = created_records.filtered_domain(domain)
            if len(accessible_records) != len(created_records):
                # If any record is not in allowed domain, raise error (Odoo will rollback)
                raise AccessError(_("Created records do not match the allowed domain for model %s.") % model_name)

        # Read created records - only return fields that were written + essential fields (id, name, display_name)
        fields_to_read = list(all_fields)
        # Add essential fields if they exist
        if 'id' not in fields_to_read:
            fields_to_read.append('id')
        if 'name' in Model._fields and 'name' not in fields_to_read:
            fields_to_read.append('name')
        if 'display_name' in Model._fields and 'display_name' not in fields_to_read:
            fields_to_read.append('display_name')

        created_values = self._model_read(model_name, created_records.ids, fields_to_read)

        return {
            'success': True,
            'created_records': created_values,
        }

    @ai_tool(
        condition=lambda thread: thread.assistant_id.has_model_write_access,
        params_aliases={
            'vals': ['values']
        }
    )
    def _model_write(self, model_name: str, res_ids: list[int], vals: dict) -> dict:
        """
        Update existing records in the specified Odoo model. Similar to Odoo's `write` method,
        but with access restricted to the models and fields defined in data sources of type 'model'.

        Args:
            model_name (str): The technical name of the Odoo model to update records in.
            res_ids (list[int]): List of record IDs to update.
            vals (dict): Dictionary containing field names and new values to set.

        Returns:
            dict:
                - success (bool): Whether the update was successful.
                - updated_records (list[dict]): List of dictionaries containing the updated records with their values.
                - error (str, optional): Error message if update failed.
        """
        self.ensure_one()
        if not res_ids:
            raise UserError(_("No record IDs provided"))
        if not vals:
            raise UserError(_("No values provided for update"))

        Model = self.env[model_name]

        # Check model and field access (Odoo will check write permissions automatically)
        self.assistant_id._check_model_fields_access(model_name, 'write', list(vals.keys()))

        # Check domain access to ensure we can only write records within allowed domain
        domain = expression.AND([
            self.assistant_id._get_model_domain_access(model_name, 'write'),
            [('id', 'in', res_ids)]
        ])
        accessible_records = Model.search(domain)
        accessible_ids = set(accessible_records.ids)

        # Check if all requested IDs are accessible
        missing_ids = set(res_ids) - accessible_ids
        if missing_ids:
            raise AccessError(_("Records with ids %s in model %s do not exist or cannot be accessed for writing.") % (', '.join(map(str, missing_ids)), model_name))

        accessible_records.write(vals)

        # Read updated records - only return fields that were written + essential fields (id, name, display_name)
        fields_to_read = list(vals.keys())
        Model = self.env[model_name]
        # Add essential fields if they exist
        if 'id' not in fields_to_read:
            fields_to_read.append('id')
        if 'name' in Model._fields and 'name' not in fields_to_read:
            fields_to_read.append('name')
        if 'display_name' in Model._fields and 'display_name' not in fields_to_read:
            fields_to_read.append('display_name')

        updated_values = self._model_read(model_name, accessible_records.ids, fields_to_read)

        return {
            'success': True,
            'updated_records': updated_values,
        }

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_unlink_access)
    def _model_unlink(self, model_name: str, res_ids: list[int]) -> dict:
        """
        Delete records from the specified Odoo model. Similar to Odoo's `unlink` method,
        but with access restricted to the models defined in data sources of type 'model'.

        IMPORTANT: Use this tool with caution. Always confirm with the user before deleting records,
        especially if the deletion might have significant consequences or cannot be undone.

        Args:
            model_name (str): The technical name of the Odoo model to delete records from.
            res_ids (list[int]): List of record IDs to delete.

        Returns:
            dict:
                - success (bool): Whether the deletion was successful.
                - deleted_records (list[dict]): List of dictionaries containing the deleted records with their values (read before deletion).
                - error (str, optional): Error message if deletion failed.
        """
        self.ensure_one()
        if not res_ids:
            raise UserError(_("No record IDs provided"))

        Model = self.env[model_name]

        # Check model access (Odoo will check unlink permissions automatically)
        self.assistant_id._check_model_fields_access(model_name, 'unlink')

        # Check domain access to ensure we can only delete records within allowed domain
        domain = expression.AND([
            self.assistant_id._get_model_domain_access(model_name, 'unlink'),
            [('id', 'in', res_ids)]
        ])
        accessible_records = Model.search(domain)
        accessible_ids = set(accessible_records.ids)

        # Check if all requested IDs are accessible
        missing_ids = set(res_ids) - accessible_ids
        if missing_ids:
            raise AccessError(_("Records with ids %s in model %s do not exist or cannot be accessed for deletion.") % (', '.join(map(str, missing_ids)), model_name))

        # Read records before deletion - only return essential fields (id, name, display_name) to save tokens
        Model = self.env[model_name]
        fields_to_read = ['id']
        if 'name' in Model._fields:
            fields_to_read.append('name')
        if 'display_name' in Model._fields:
            fields_to_read.append('display_name')

        deleted_values = self._model_read(model_name, accessible_records.ids, fields_to_read)

        accessible_records.unlink()

        return {
            'success': True,
            'deleted_records': deleted_values,
        }

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_read_access)
    def _get_model_fields_spec(self, models: list[str], field_attrs: Optional[list[str]] = None) -> list[dict]:
        """
        Get all accessible fields specifications of the models. The specification includes the model technical name,
        description, accessible fields details and access domain.

        Args:
            models (list[str]): The list of model technical names to get the specification for.
            field_attrs (list[str], optional): The list of field attributes to include in the specification.
                                               If not provided, only essential field attributes will be included:
                                               (type, string, required, readonly, store, searchable, relation, domain, selection, help).
        Returns:
            list[dict]: A list of dictionaries containing the specification for each model.
        """
        self.ensure_one()

        # Only include essential field attributes to reduce token usage
        field_attrs = field_attrs or [
            'type',
            'string',
            'required',
            'readonly',
            'store',
            'searchable',
            'relation',
            'domain',
            'selection',
            'help',
        ]

        result = []
        is_super_assistant = self.assistant_id.is_super_assistant
        for model in models:
            if model not in self.env.registry:
                continue
            Model = self.env[model]
            if is_super_assistant:
                result.append({
                    'model': model,
                    'name': Model._description,
                    'fields': Model.fields_get(attributes=field_attrs),
                    'domain': [],
                })
            else:
                data_sources = self.assistant_id._get_model_data_sources_for_operation(model, 'read')
                if not data_sources:
                    continue
                access_fields = set()
                domains = []
                for ds in data_sources:
                    access_fields.update(ds._get_access_fields(include_binary=True))
                    domains.append(ds._get_model_domain())
                domain = expression.OR(domains)
                result.append({
                    'model': model,
                    'name': Model._description,
                    'fields': access_fields and Model.fields_get(list(access_fields), attributes=field_attrs) or {},
                    'domain': domain,
                })
        return result

    def _get_blacklisted_methods(self):
        """
        Get the list of methods that are blacklisted from being called via _call_model_method.
        These are methods that have dedicated AI tools or are useless methods for AI to call.
        """
        return {
            'create', 'write', 'update', 'unlink', 'copy', 'copy_multi', 'read', 'search',
            'search_read', 'read_group', 'sudo', 'with_context', 'with_user', 'with_company',
            'with_env', 'env', 'init', 'pool', 'onchange', 'export_data', 'is_transient',
            'web_override_translations', 'web_search_read', 'web_read', 'web_read_group',
            'web_save',
        }

    def _check_model_method_access(self, model_name: str, method_name: str):
        """
        Check if a method can be accessed/called by AI.
        Raises errors if the method is not accessible.
        """
        Model = self.env[model_name]
        blacklisted_methods = self._get_blacklisted_methods()

        if not hasattr(Model, method_name):
            raise UserError(_("Method '%s' does not exist on model '%s'.") % (method_name, model_name))

        method = getattr(Model, method_name, None)
        if method is None:
            raise UserError(_("Method '%s' does not exist on model '%s'.") % (method_name, model_name))

        if not callable(method):
            raise UserError(_("'%s' is not a callable method on model '%s'.") % (method_name, model_name))

        if method_name.startswith('_'):
            raise AccessError(_("Private methods (starting with '_') cannot be called."))

        if getattr(method, '_api_private', False):
            raise AccessError(_("Method '%s' is marked as private and cannot be called.") % method_name)

        api_type = getattr(method, '_api', None)
        if api_type == 'model':
            raise AccessError(_("Method '%s' is a @api.model method and cannot be called. Only methods that work on records are allowed.") % method_name)

        if method_name in blacklisted_methods:
            raise AccessError(_("Method '%s' is blacklisted. Use dedicated AI tools instead (e.g., _model_create, _model_write).") % method_name)

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_write_access)
    def _get_model_methods_spec(self, model_name: str) -> list[dict]:
        """
        Get specifications of all public methods of a model that operate on records.
        Only methods that work on records (not @api.model methods) are returned.
        All CRUD methods are also excluded. AI should use dedicated AI tools instead.
        This allows AI to discover available methods and their parameters.

        Args:
            model_name (str): The technical name of the Odoo model.

        Returns:
            list[dict]: List of method specifications, each containing:
                - name (str): Method name
                - signature (dict): Method signature with parameters
                - docstring (str): Method documentation if available
        """
        self.ensure_one()
        # Check model access (write: method may modify records)
        self.assistant_id._check_model_fields_access(model_name, 'write')

        Model = self.env[model_name]
        methods_spec = []

        # Get all methods from the model class
        for name in dir(Model):
            try:
                self._check_model_method_access(model_name, name)
            except UserError:
                continue

            # Get the method after check passes
            method = getattr(Model, name)

            try:
                # Get method signature
                sig = inspect.signature(method)
                parameters = []
                for param_name, param in sig.parameters.items():
                    # Skip 'self' parameter
                    if param_name == 'self':
                        continue
                    param_info = {
                        'name': param_name,
                        'kind': str(param.kind),
                    }
                    if param.default != inspect.Parameter.empty:
                        # Try to serialize default value
                        try:
                            param_info['default'] = json.loads(json.dumps(param.default, ensure_ascii=False, default=json_default))
                        except (TypeError, ValueError):
                            param_info['default'] = str(param.default)
                    parameters.append(param_info)

                # Get docstring
                docstring = inspect.getdoc(method) or ''

                methods_spec.append({
                    'name': name,
                    'signature': {
                        'parameters': parameters,
                        'return_annotation': str(sig.return_annotation) if sig.return_annotation != inspect.Parameter.empty else None,
                    },
                    'docstring': docstring,
                })
            except (ValueError, TypeError):
                # Skip methods that can't be inspected (e.g., built-in methods)
                continue

        return methods_spec

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_write_access)
    def _call_model_method(
        self,
        model_name: str,
        method_name: str,
        res_ids: list[int],
        args: Optional[list] = None,
        kwargs: Optional[dict] = None
    ) -> dict:
        """
        Call a public method on specific records of a model.
        Only methods that work on records (not @api.model methods) can be called.
        All CRUD methods are also forbidden. AI should use dedicated AI tools instead.
        This allows AI to execute custom business logic methods on records.

        Args:
            model_name (str): The technical name of the Odoo model.
            method_name (str): The name of the method to call (must be public, not starting with '_').
            res_ids (list[int]): List of record IDs to call the method on. Required.
            args (list, optional): Positional arguments to pass to the method.
            kwargs (dict, optional): Keyword arguments to pass to the method.

        Returns:
            dict:
                - success (bool): Whether the call was successful.
                - result: The return value of the method (serialized to JSON-compatible format).
                - error (str, optional): Error message if call failed.
        """
        self.ensure_one()
        # Check model access (write: method may modify records)
        self.assistant_id._check_model_fields_access(model_name, 'write')

        # Validate res_ids is provided
        if not res_ids:
            raise UserError(_("res_ids is required. This tool only supports calling methods on specific records."))

        Model = self.env[model_name]

        # Check method access
        self._check_model_method_access(model_name, method_name)

        # Prepare arguments
        args = args or []
        kwargs = kwargs or {}

        # Call on specific records
        # Check domain access before calling
        domain = self.assistant_id._get_model_domain_access(model_name, 'write')
        if domain:
            search_domain = expression.AND([domain, [('id', 'in', res_ids)]])
        else:
            search_domain = [('id', 'in', res_ids)]

        records = Model.search(search_domain)
        accessible_ids = set(records.ids)
        missing_ids = set(res_ids) - accessible_ids

        if missing_ids:
            raise AccessError(_("Records with ids %s in model %s do not exist or cannot be accessed.") %
                            (', '.join(map(str, missing_ids)), model_name))

        # Read field values before calling method to detect changes
        allowed_fields = self.assistant_id._check_model_fields_access(model_name, 'write')
        fields_to_check = [f for f in allowed_fields if f not in ['id', 'create_uid', 'create_date', 'write_uid', 'write_date']]

        # Read values before method call
        values_before = {}
        if fields_to_check:
            read_results = records.read(fields_to_check)
            values_before = {vals['id']: vals for vals in read_results}

        # Call method on records using Odoo's call_kw
        result = api.call_kw(Model, method_name, [res_ids, *args], kwargs)

        # Refresh records to get latest values
        records.invalidate_recordset()
        records = Model.browse(res_ids).exists()

        # Check domain access after calling method
        if domain:
            accessible_records_after = records.filtered_domain(domain)
            if records - accessible_records_after:
                # If any record is not in allowed domain after method call, raise error
                raise AccessError(
                    _("After calling method '%s', records do not match the allowed domain for model %s.")
                    % (method_name, model_name)
                )

        # Check if any fields were changed that are not in allowed fields
        if values_before and fields_to_check:
            changed_fields = set()
            read_results_after = records.read(fields_to_check)
            values_after = {vals['id']: vals for vals in read_results_after}

            for record_id in values_before.keys():
                if record_id not in values_after:
                    continue
                before = values_before[record_id]
                after = values_after[record_id]
                for field_name in fields_to_check:
                    if before.get(field_name) != after.get(field_name):
                        changed_fields.add(field_name)

            # Check if any changed field is not in allowed fields
            if changed_fields:
                not_allowed_fields = changed_fields - set(allowed_fields) if allowed_fields else changed_fields
                if not_allowed_fields:
                    raise AccessError(
                        _("Method '%s' changed fields %s which are not allowed for this assistant.")
                        % (method_name, ', '.join(not_allowed_fields))
                    )

        # Serialize result to JSON-compatible format
        try:
            serialized_result = json.loads(json.dumps(result, ensure_ascii=False, default=json_default))
        except (TypeError, ValueError):
            # If can't serialize, convert to string
            serialized_result = str(result)

        return {
            'success': True,
            'result': serialized_result,
        }

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_write_access)
    def _model_get_field_translations(
        self,
        model_name: str,
        res_id: int,
        field_name: str,
        langs: Optional[list[str]] = None
    ) -> dict:
        """
        Get the current translations of a translated field for a specific record.
        This tool is the first step in translating a field - you must call this before updating translations.

        IMPORTANT: To translate a field, you must use BOTH tools in this order:
        1. First, call `_model_get_field_translations` to get the current translations and understand the field's translation type
        2. Then, call `_model_update_field_translations` with the properly formatted translations based on the context returned

        Note:
        - If translation_show_source is False (translate=True):
          The 'source' is the English value, 'value' is the translation for each language
        - If translation_show_source is True (translate is callable):
          The 'source' is each individual term, 'value' is the translation of that term
        - For ir.ui.view model: Use field 'arch_db' (not 'arch') for translations. The 'arch' field is not translatable.

        Args:
            model_name (str): The technical name of the Odoo model.
            res_id (int): The record ID to get translations for.
            field_name (str): The name of the field to get translations for.
            langs (list[str], optional): List of language codes to get translations for (e.g., ['fr_FR', 'de_DE']).
                                        If not provided, returns translations for all installed languages.

        Returns:
            dict:
                - translations (list[dict]): List of translation dictionaries, each containing:
                    - lang (str): Language code (e.g., 'fr_FR')
                    - source (str): The source term/value (English or original term)
                    - value (str): The translated value for this language (empty if not translated)
                - context (dict): Translation context containing:
                    - translation_type (str): 'text' for text/html fields, 'char' for char fields
                    - translation_show_source (bool): True if field uses term-based translation (translate is callable),
                                                      False if field uses simple translation (translate=True)
                - record_id (int): The record ID translations are for
        """
        self.ensure_one()
        Model = self.env[model_name]

        # Check model and field access (write: tool is only useful when user can later update translations)
        self.assistant_id._check_model_fields_access(model_name, 'write', [field_name])

        # Check domain access
        domain = expression.AND([
            self.assistant_id._get_model_domain_access(model_name, 'write'),
            [('id', '=', res_id)]
        ])
        record = Model.search(domain)
        if not record:
            raise AccessError(_("Record with id %s in model %s does not exist or cannot be accessed.") % (res_id, model_name))

        # Get translations using Odoo's method
        translations, context = record.get_field_translations(field_name, langs=langs)

        return {
            'translations': translations,
            'context': context,
        }

    @ai_tool(condition=lambda thread: thread.assistant_id.has_model_write_access)
    def _model_update_field_translations(
        self,
        model_name: str,
        res_id: int,
        field_name: str,
        translations: dict
    ) -> dict:
        """
        Update the translations of a translated field for a specific record.
        This tool is the second step in translating a field - you must call `_model_get_field_translations` first.

        IMPORTANT: To translate a field, you must use BOTH tools in this order:
        1. First, call `_model_get_field_translations` to get the current translations and understand the field's translation type
        2. Then, call `_model_update_field_translations` with the properly formatted translations based on the context returned

        The format of the `translations` parameter depends on the field's translation type:
        - If translation_show_source is False (translate=True):
          translations = {"lang_code": "translated_value"} or {"lang_code": False} to remove translation
          Example: {"fr_FR": "Bonjour", "de_DE": "Hallo"}
        - If translation_show_source is True (translate is callable):
          translations = {"lang_code": {"source_term": "translated_term"}}
          Example: {"fr_FR": {"Hello": "Bonjour", "World": "Monde"}, "de_DE": {"Hello": "Hallo"}}

        Note:
        - Only languages that are installed in the system can be used
        - To determine the correct format, always call `_model_get_field_translations` first and check the context
        - For ir.ui.view model: Use field 'arch_db' (not 'arch') for translations. The 'arch' field is not translatable.

        Args:
            model_name (str): The technical name of the Odoo model.
            res_id (int): The record ID to update translations for.
            field_name (str): The name of the field to update translations for.
            translations (dict): Dictionary of translations to update. Format depends on field's translation type:
                - For simple translation (translate=True): {lang_code: translated_value}
                - For term-based translation (translate is callable): {lang_code: {source_term: translated_term}}
                - Use False as value to remove a translation: {lang_code: False}

        Returns:
            dict:
                - success (bool): Whether the update was successful.
                - updated_record (dict): Dictionary containing the updated record with its values.
                - error (str, optional): Error message if update failed.
        """
        self.ensure_one()
        if not translations:
            raise UserError(_("translations dictionary is required."))

        Model = self.env[model_name]

        # Check model and field access (write access for updating translations)
        self.assistant_id._check_model_fields_access(model_name, 'write', [field_name])

        # Check domain access
        domain = expression.AND([
            self.assistant_id._get_model_domain_access(model_name, 'write'),
            [('id', '=', res_id)]
        ])
        record = Model.search(domain)
        if not record:
            raise AccessError(_("Record with id %s in model %s does not exist or cannot be accessed for writing.") % (res_id, model_name))

        # Update translations using Odoo's method
        record.update_field_translations(field_name, translations)

        # Read updated record - return essential fields
        fields_to_read = ['id', field_name]
        if 'name' in Model._fields and 'name' not in fields_to_read:
            fields_to_read.append('name')
        if 'display_name' in Model._fields and 'display_name' not in fields_to_read:
            fields_to_read.append('display_name')

        updated_values = self._model_read(model_name, [res_id], fields_to_read)
        if updated_values:
            updated_record = updated_values[0]
        else:
            updated_record = {'id': res_id}

        return {
            'success': True,
            'updated_record': updated_record,
        }

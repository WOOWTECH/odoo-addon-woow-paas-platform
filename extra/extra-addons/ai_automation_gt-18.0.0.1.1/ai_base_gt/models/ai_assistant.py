import random
import string
from collections import defaultdict
from odoo import api, models, fields, _
from odoo.exceptions import AccessError
from odoo.osv import expression


class AIAssistant(models.Model):
    _name = 'ai.assistant'
    _description = 'AI Assistant'
    _inherits = {'res.partner': 'partner_id'}
    _order = 'sequence, id'

    sequence = fields.Integer(default=10)
    description = fields.Text(string="Description")
    config_id = fields.Many2one('ai.config', string="Configuration", required=True, groups='base.group_system')
    partner_id = fields.Many2one('res.partner', required=True, ondelete='restrict', auto_join=True, index=True)
    user_id = fields.Many2one('res.users', compute='_compute_user_id', precompute=True, store=True)
    context_id = fields.Many2one('ai.context', string="Assistant Context")
    is_super_assistant = fields.Boolean(string="Is Super Assistant", default=False,
                                  help="If True, the assistant will be able to access all data sources and models.")
    assistant_data_source_ids = fields.One2many(
        'ai.assistant.data.source',
        'assistant_id',
        string="Data Source Permissions",
    )
    data_source_ids = fields.Many2many(
        'ai.data.source',
        string="Data Sources",
        compute='_compute_data_source_ids',
        inverse='_inverse_data_source_ids',
    )
    searchable_data_source_ids = fields.Many2many('ai.data.source', string="Searchable Data Sources",
                                                  compute='_compute_searchable_data_source_ids')
    allow_files = fields.Boolean(related='config_id.allow_files', string="Allow Files", related_sudo=True)
    has_vector_access = fields.Boolean(string="Has Vector Access", compute='_compute_has_vector_access')
    has_model_read_access = fields.Boolean(string="Has Model Read Access", compute='_compute_has_model_access')
    has_model_create_access = fields.Boolean(string="Has Model Create Access", compute='_compute_has_model_access')
    has_model_write_access = fields.Boolean(string="Has Model Write Access", compute='_compute_has_model_access')
    has_model_unlink_access = fields.Boolean(string="Has Model Unlink Access", compute='_compute_has_model_access')
    data_item_count = fields.Integer(string="Data Item Count", compute='_compute_data_item_count')
    group_ids = fields.Many2many('res.groups', string="Groups",
                                 help="Restrict the availability of this assistant to specific groups.")

    @api.depends('partner_id.user_ids')
    def _compute_user_id(self):
        for r in self:
            r.user_id = r.partner_id.with_context(active_test=False).user_ids[:1]

    @api.depends('assistant_data_source_ids.data_source_id')
    def _compute_data_source_ids(self):
        for r in self:
            r.data_source_ids = r.assistant_data_source_ids.data_source_id

    def _inverse_data_source_ids(self):
        for r in self:
            current = r.assistant_data_source_ids.data_source_id
            target = r.data_source_ids
            to_add = target - current
            to_remove = current - target
            if to_remove:
                r.assistant_data_source_ids.filtered(
                    lambda line: line.data_source_id in to_remove
                ).unlink()
            for ds in to_add:
                self.env['ai.assistant.data.source'].create({
                    'assistant_id': r.id,
                    'data_source_id': ds.id,
                })

    @api.depends('is_super_assistant', 'data_source_ids')
    def _compute_searchable_data_source_ids(self):
        for r in self:
            if r.is_super_assistant:
                r.searchable_data_source_ids = self.env['ai.data.source'].search([])
            else:
                read_model_sources = r.assistant_data_source_ids.filtered('allow_read').data_source_id
                r.searchable_data_source_ids = r.data_source_ids.filtered(
                    lambda ds: ds.type != 'model' or ds in read_model_sources
                )

    @api.depends('searchable_data_source_ids')
    def _compute_data_item_count(self):
        items_per_source_count = dict(
            (res['id'], res['data_item_count'])
            for res in self.searchable_data_source_ids.read(['data_item_count'])
        )
        for r in self:
            r.data_item_count = sum(items_per_source_count.get(_id, 0) for _id in r.searchable_data_source_ids.ids)

    @api.depends('searchable_data_source_ids')
    def _compute_has_vector_access(self):
        for r in self:
            r.has_vector_access = bool(r.searchable_data_source_ids.filtered('data_item_count'))

    @api.depends(
        'is_super_assistant',
        'assistant_data_source_ids.data_source_id',
        'assistant_data_source_ids.data_source_id.type',
        'assistant_data_source_ids.allow_read',
        'assistant_data_source_ids.allow_create',
        'assistant_data_source_ids.allow_write',
        'assistant_data_source_ids.allow_unlink',
    )
    def _compute_has_model_access(self):
        for r in self:
            model_lines = r.assistant_data_source_ids.filtered(
                lambda line: line.data_source_id.type == 'model'
            )
            r.has_model_read_access = r.is_super_assistant or bool(model_lines.filtered('allow_read'))
            r.has_model_create_access = r.is_super_assistant or bool(model_lines.filtered('allow_create'))
            r.has_model_write_access = r.is_super_assistant or bool(model_lines.filtered('allow_write'))
            r.has_model_unlink_access = r.is_super_assistant or bool(model_lines.filtered('allow_unlink'))

    def _create_user(self):
        self.ensure_one()
        user = self.env['res.users'].create({
            'login': '__%s__' % (''.join(random.choice(string.ascii_letters + string.digits) for i in range(10))),
            'partner_id': self.partner_id.id,
            'groups_id': [(4, self.env.ref('base.group_user').id)],
        })
        user.action_archive()
        return user

    @api.model_create_multi
    def create(self, vals_list):
        configs = super().create(vals_list)
        for config in configs.with_context(active_test=False):
            if not config.partner_id.user_ids:
                config._create_user()
        self.env.registry.clear_cache()
        return configs

    def write(self, vals):
        res = super().write(vals)
        self.env.registry.clear_cache()
        return res

    def unlink(self):
        res = super().unlink()
        self.env.registry.clear_cache()
        return res

    def action_view_user(self):
        action = self.env['ir.actions.act_window']._for_xml_id('base.action_res_users')
        action['view_mode'] = 'form'
        action['views'] = [(self.env.ref('base.view_users_form').id, 'form')]
        action['res_id'] = self.user_id.id
        return action

    def action_view_data_items(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('ai_base_gt.action_ai_data_item')
        if self.data_source_ids:
            action['domain'] = [('data_source_id', 'in', self.data_source_ids.ids)]
        return action

    def _get_searchable_data_sources_info(self):
        self.ensure_one()
        data_sources = self.searchable_data_source_ids.filtered('data_item_count')
        specs = []
        for source in data_sources:
            specs.append({
                'id': source.id,
                'name': source.name,
                'description': source.description,
                'type': source.type,
            })
        return specs

    def _get_accessible_models_info(self):
        self.ensure_one()
        if self.is_super_assistant:
            models = self.env['ir.model'].sudo().search([])
        else:
            models = self.data_source_ids.filtered(lambda ds: ds.type == 'model').sudo().model_id
        specs = []
        for model in models.with_context(lang='en_US'):
            if model.model not in self.env.registry:
                continue
            Model = self.env[model.model]
            if Model._auto and not Model._transient:
                specs.append({
                    'name': model.name,
                    'model': model.model,
                })
        return specs

    def _get_model_data_sources_for_operation(self, model_name, operation):
        """
        Return data sources (type=model, model=model_name) that grant the given
        operation (read/create/write/unlink) for this assistant.
        """
        self.ensure_one()
        flag_map = {
            'read': 'allow_read',
            'create': 'allow_create',
            'write': 'allow_write',
            'unlink': 'allow_unlink',
        }
        flag = flag_map.get(operation, 'allow_read')
        lines = self.assistant_data_source_ids.filtered(
            lambda line: line.data_source_id.type == 'model'
            and line.data_source_id.model == model_name
            and getattr(line, flag, True)
        )
        return lines.data_source_id

    def _check_model_fields_access(self, model_name, operation='read', requested_fields=None, include_binary=False):
        """
        Check if the model and requested fields are allowed for this assistant
        for the given operation (read/create/write/unlink).
        Returns the allowed fields list for the model if valid, otherwise raises AccessError.
        """
        self.ensure_one()
        Model = self.env[model_name]
        include_binary = include_binary and self.allow_files
        if self.env.su or self.is_super_assistant:
            if requested_fields:
                return [
                    f for f in requested_fields
                    if f in Model._fields and (include_binary or Model._fields[f].type != 'binary')
                ]
            return [
                fname for fname, field in Model._fields.items()
                if include_binary or field.type != 'binary'
            ]
        data_sources = self._get_model_data_sources_for_operation(model_name, operation)
        if not data_sources:
            raise AccessError(_("Model %s is not allowed for this assistant (operation: %s).") % (model_name, operation))
        allowed_models = defaultdict(set)
        for ds in data_sources:
            allowed_models[ds.model].update(ds._get_access_fields(include_binary))
        allowed_fields = allowed_models.get(model_name, set())
        if requested_fields:
            not_allowed = set(requested_fields) - allowed_fields
            if not_allowed:
                raise AccessError(_("Fields %s of model %s are not allowed for this assistant.") % (', '.join(not_allowed), model_name))
        return list(allowed_fields)

    def _get_model_domain_access(self, model_name, operation='read'):
        self.ensure_one()
        if self.env.su or self.is_super_assistant:
            return []
        data_sources = self._get_model_data_sources_for_operation(model_name, operation)
        if not data_sources:
            raise AccessError(_("Model %s is not allowed for this assistant (operation: %s).") % (model_name, operation))
        domain = expression.OR([ds._get_model_domain() for ds in data_sources])
        return domain

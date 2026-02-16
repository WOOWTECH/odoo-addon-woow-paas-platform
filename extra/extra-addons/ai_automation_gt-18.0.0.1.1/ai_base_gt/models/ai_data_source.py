import yaml
import requests
import re
import logging
from urllib.parse import urlsplit, urljoin
from xml.etree import ElementTree
from ast import literal_eval
from markdownify import markdownify
from odoo import models, fields, api, _
from odoo.tools import split_every
from odoo.exceptions import ValidationError

logger = logging.getLogger(__name__)


def process_sitemap(sitemap_url, path_regex):
    """Process a sitemap URL and return all matching URLs recursively.

    :param sitemap_url: URL of the sitemap to process
    :param path_regex: Regex pattern to match against URL paths
    :return: List of matching URLs
    """
    try:
        response = requests.get(sitemap_url, timeout=30)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)

        # Initialize list to store all matching URLs
        matching_urls = []

        # Process sitemap index files (collection of other sitemaps)
        sitemaps = root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap')
        if sitemaps:
            for sitemap in sitemaps:
                loc = sitemap.find('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
                if loc is not None and loc.text:
                    # Recursively process each sub-sitemap
                    matching_urls.extend(process_sitemap(loc.text, path_regex))

        # Process URLs in current sitemap
        urls = root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}url')
        for url in urls:
            loc = url.find('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
            if loc is not None and loc.text:
                urlsplitted = urlsplit(loc.text)
                base_url = f"{urlsplitted.scheme}://{urlsplitted.netloc}"
                url_path = loc.text[len(base_url):]
                if re.match(path_regex, url_path):
                    matching_urls.append(loc.text)

        return matching_urls

    except Exception as e:
        logger.warning(f"Error processing sitemap {sitemap_url}: {str(e)}")
        return []


class AIDataSource(models.Model):
    _name = 'ai.data.source'
    _description = 'AI Data Source'
    _order = 'sequence, id'

    active = fields.Boolean(string="Active", default=True)
    sequence = fields.Integer(string="Sequence", default=10)
    name = fields.Char(string="Name", required=True)
    description = fields.Text(string="Description")
    type = fields.Selection([
        ('model', 'Model'),
        ('url', 'URL'),
        ('text', 'Text')
    ], required=True, default='model', inverse='_inverse_type' )
    # Model type
    model_id = fields.Many2one('ir.model', string="Model", domain=[('transient', '=', False)])
    model = fields.Char(string="Model Name", related='model_id.model', store=True, precompute=True)
    model_field_ids = fields.Many2many('ir.model.fields', string="Model Fields",
                                       compute='_compute_model_field_ids', store=True, precompute=True, readonly=False,
                                       domain="[('model_id', '=', model_id)]",
                                       help="Restrict CRUD access to the selected fields. Leave empty to allow all fields.")
    model_domain = fields.Char(string="Record Domain")
    # URL type
    url_regex = fields.Text(string="URL Regex",
                            help="System will crawl the website's sitemap and index the sites that match these URL regex, "
                                 "separated by breaklines, eg:\n"
                                 "/contactus\n"
                                 "/services/*\n"
                                 "https://odoo.com/industries/*\n")

    data_item_ids = fields.One2many('ai.data.item', 'data_source_id', string="Data Items")
    data_item_count = fields.Integer(string="Data Item Count", compute='_compute_data_item_count', compute_sudo=True)
    assistant_data_source_ids = fields.One2many(
        'ai.assistant.data.source',
        'data_source_id',
        string="Assistant Permissions",
    )
    assistant_ids = fields.Many2many(
        'ai.assistant',
        string="AI Assistants",
        compute='_compute_assistant_ids',
    )

    @api.depends('assistant_data_source_ids.assistant_id')
    def _compute_assistant_ids(self):
        for rec in self:
            rec.assistant_ids = rec.assistant_data_source_ids.assistant_id

    @api.depends('model_id')
    def _compute_name(self):
        for r in self:
            r.name = r.model_id.name

    @api.depends('model_id')
    def _compute_model_field_ids(self):
        for r in self:
            r.model_field_ids = False

    @api.depends('data_item_ids')
    def _compute_data_item_count(self):
        data = self.env['ai.data.item']._read_group([('data_source_id', 'in', self.ids)], ['data_source_id'], ['__count'])
        mapped_data = {source.id: count for source, count in data}
        for r in self:
            r.data_item_count = mapped_data.get(r.id, 0)

    def _inverse_type(self):
        for r in self:
            if r.type != 'model':
                # Reset model fields and domain
                r.model_id = False
                r.model_field_ids = False
                r.model_domain = False
            if r.type != 'url':
                # Reset URL regex
                r.url_regex = False
            if r.type != 'text':
                # Remove all text data items (data items that are not associated with a model or URL)
                r.data_item_ids.filtered_domain([
                    ('res_model', '=', False),
                    ('res_id', '=', 0),
                    ('res_url', '=', False),
                ]).unlink()

    def action_view_data_items(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('ai_base_gt.action_ai_data_item')
        action['domain'] = [('data_source_id', '=', self.id)]
        return action

    def action_index(self):
        self.check_access('write')

        for r in self:
            r.sudo()._index()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'message': _("Successfully indexed data sources: %s") % ', '.join(self.mapped('name')),
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            }
        }

    def _index(self):
        self.ensure_one()

        DataItem = self.env['ai.data.item']
        documents_count = 0

        # Get existing data items for this source
        existing_items = DataItem.search([('data_source_id', '=', self.id)])
        existing_data_ids = set(existing_items.mapped('source'))
        new_data_ids = set()

        # Process new data in batches
        for data_list in self._prepare_data_for_index():
            if not data_list:
                break

            documents_count += len(data_list)
            for item in data_list:
                new_data_ids.add(item['source'])
                # Create or update data item
                data_item = DataItem.search([
                    ('source', '=', item['source']),
                    ('data_source_id', '=', self.id)
                ], limit=1)

                if data_item:
                    if data_item.data != item['data']:
                        data_item.write(item)
                else:
                    DataItem.create({
                        'data_source_id': self.id,
                        **item
                    })

            logger.info(f"Updated {len(data_list)} documents for source: {self.name}")

        # Remove obsolete items
        obsolete_data_ids = existing_data_ids - new_data_ids
        if obsolete_data_ids:
            obsolete_items = DataItem.search([
                ('source', 'in', list(obsolete_data_ids)),
                ('data_source_id', '=', self.id)
            ])
            obsolete_items.unlink()
            logger.info(f"Deleted {len(obsolete_data_ids)} obsolete documents for source: {self.name}")

        # Generate vectors for items without embeddings
        items_without_vector = DataItem.search([
            ('data_source_id', '=', self.id),
            ('vector_generated', '=', False)
        ])
        if items_without_vector:
            for item in items_without_vector:
                item._index()
            logger.info(f"Generated vectors for {len(items_without_vector)} documents for source: {self.name}")

        logger.info(f"Successfully processed {documents_count} documents for source: {self.name}")

    def _prepare_data_for_index(self, batch_size=100):
        """
        Prepare data for indexing based on the data source type.

        This method yields batches of prepared data for indexing. The data preparation
        varies depending on the type of data source (model, URL, or text).

        :param batch_size: The number of records to process in each batch
        :type batch_size: int
        :return: Generator yielding lists of prepared data
        :rtype: Generator[List[Dict], None, None]
        """
        self.ensure_one()
        if self.type == 'model' and self.model and self.env[self.model]._auto:
            for records in self._get_records_to_index(batch_size):
                yield [self._prepare_record_data_for_index(record) for record in records]
        elif self.type == 'url':
            for urls in self._get_urls_to_index(batch_size):
                yield [self._prepare_url_data_for_index(url) for url in urls]
        elif self.type == 'text':
            for data_items_batch in split_every(batch_size, self.data_item_ids):
                data_items = self.env['ai.data.item'].concat(*data_items_batch)
                yield [self._prepare_text_data_for_index(data_item) for data_item in data_items]
        return []

    def _get_model_domain(self):
        self.ensure_one()
        return self.model_domain and literal_eval(self.model_domain) or [(1, '=', 1)]

    def _get_records_to_index(self, batch_size=100):
        self.ensure_one()
        offset = 0
        while True:
            records = self.env[self.model].search(
                self._get_model_domain(), limit=batch_size, offset=offset)
            if not records:
                break
            yield records
            offset += batch_size

    def _get_urls_to_index(self, batch_size=100):
        self.ensure_one()
        if not self.url_regex:
            return []

        urls = []
        for url in self.url_regex.splitlines():
            url = url.strip()
            if not url:
                continue

            urlsplitted = urlsplit(url)
            base_url = f"{urlsplitted.scheme}://{urlsplitted.netloc}" if urlsplitted.netloc else self.get_base_url()
            path = url[len(base_url):] if url.startswith(base_url) else url

            if path.find('*') == -1:  # Absolute URL
                urls.append(urljoin(base_url, path))
            else:  # URL pattern
                path_regex = f"^{path.replace('*', '.*')}$"
                sitemap_url = f"{base_url}/sitemap.xml"
                urls.extend(process_sitemap(sitemap_url, path_regex))

        for i in range(0, len(urls), batch_size):
            yield urls[i:i + batch_size]

    def _get_record_fields_to_index(self):
        self.ensure_one()
        return (self.model_field_ids or self.model_id.field_id).filtered_domain([
            ('ttype', '!=', 'binary'),
            ('name', 'not in', ['id', 'display_name']),
        ])

    def _prepare_record_data_for_index(self, record, data_fields=None):
        self.ensure_one()
        record.ensure_one()
        data_fields = data_fields or self._get_record_fields_to_index()
        record_data = {}
        for field in data_fields:
            label = f"{field.field_description} ({field.name})"
            if field.ttype == 'binary':
                raise ValidationError(_("Binary field %s is not supported for embedding.") % field.name)
            elif field.ttype == 'many2one':
                corecord = record[field.name]
                value = f"{corecord.display_name} ({corecord._name}({corecord.id}))" if corecord else ''
            elif field.ttype in ('one2many', 'many2many'):
                value = [f"{r.display_name} ({r._name}({r.id}))" for r in record[field.name]]
            elif field.ttype == 'text':
                value = (record[field.name] or '').strip()
            elif field.ttype == 'html':
                content = (record[field.name] or '').replace(u'\xa0', u' ').strip()
                value = markdownify(content)
            elif field.ttype == 'selection':
                descriptions = {elem[0]: elem[1] for elem in record._fields[field.name]._description_selection(self.env)}
                value = descriptions.get(record[field.name]) or record[field.name] or ''
            elif field.ttype == 'monetary':
                currency_field = record._fields[field.name].get_currency_field(record)
                value = record[currency_field].format(record[field.name])
            else:
                value = str(getattr(record, field.name))
            record_data[label] = value
        data = {
            'source': f"{record._name}({record.id})",
            'res_model': record._name,
            'res_id': record.id,
            'data': yaml.dump({
                _('Display Name'): record.display_name,
                _('Data'): record_data,
            }, default_flow_style=False, allow_unicode=True, sort_keys=False)
        }
        return data

    def _prepare_url_data_for_index(self, url):
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        html = response.text
        data = {
            'source': url,
            'res_url': url,
            'data': markdownify(html)
        }
        return data

    def _prepare_text_data_for_index(self, data_item):
        data_item.ensure_one()
        return {
            'source': data_item.source,
            'data': data_item.data
        }

    def _get_access_fields(self, include_binary=False):
        self.ensure_one()
        self_sudo = self.sudo()
        basic_fields = self_sudo.model_id.field_id.filtered(
            lambda f: f.name in ['id', 'display_name']
        )
        access_fields = basic_fields | (self_sudo.model_field_ids or self_sudo.model_id.field_id)
        if not include_binary:
            access_fields = access_fields.filtered(lambda f: f.ttype != 'binary')
        return access_fields.mapped('name')

from fastembed import TextEmbedding
import numpy as np
import psycopg2.extensions
from odoo import fields, models, api
from odoo.tools import ormcache, groupby

# Register the vector type with psycopg2
def adapt_numpy_array(numpy_array):
    return psycopg2.extensions.adapt(','.join(map(str, numpy_array.flatten()))).getquoted()

psycopg2.extensions.register_adapter(np.ndarray, adapt_numpy_array)


class AiDataItem(models.Model):
    _name = 'ai.data.item'
    _description = 'AI Data Item'
    _rec_name = 'source'

    source = fields.Char(string="Source", required=True, index=True)
    res_model = fields.Char(string="Resource Model", index='btree_not_null')
    res_id = fields.Integer(string="Resource ID", index='btree_not_null')
    res_url = fields.Char(string="Resource URL", index='btree_not_null')
    data = fields.Text(string="Data")
    data_source_id = fields.Many2one('ai.data.source', string="Data Source", required=True, index=True, ondelete='cascade')
    vector = fields.Binary(string="Vector Embedding", attachment=False)
    vector_generated = fields.Boolean(string="Vector Generated", default=False)

    _sql_constraints = [
        ('data_id_unique', 'unique (source, data_source_id)', 'Source must be unique within a data source')
    ]

    def init(self):
        """Initialize pgvector when installing the module"""
        super().init()
        self._init_pgvector()

    @api.model
    def _init_pgvector(self):
        """Initialize pgvector extension and add vector column if not exists"""
        self.env.cr.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self.env.cr.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'ai_data_item'
                    AND column_name = 'embedding'
                ) THEN
                    ALTER TABLE ai_data_item ADD COLUMN embedding vector(384);
                END IF;
            END $$;
        """)
        # Create index for faster vector search
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS ai_data_item_embedding_idx
            ON ai_data_item
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100);
        """)

    @ormcache()
    @api.model
    def _get_embedding_model(self):
        """Get or initialize the embedding model"""
        return TextEmbedding(
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            max_length=512
        )

    def _index(self):
        """Index the current data item by generating and storing its vector embedding."""
        self.ensure_one()
        if not self.vector_generated:
            model = self._get_embedding_model()
            # FastEmbed returns a generator of embeddings, we take the first one
            embedding = next(model.embed([self.data]))

            # Store embedding in the database
            self.env.cr.execute("""
                UPDATE ai_data_item
                SET embedding = %s
                WHERE id = %s
            """, (str(embedding.tolist()), self.id))

            self.vector_generated = True
            self.vector = embedding.tobytes()

    @api.model
    def _search_similar(self, query, data_sources, limit=5):
        """Search for similar items using vector similarity"""
        EmbeddingModel = self._get_embedding_model()
        query_embedding = next(EmbeddingModel.embed([query]))
        embedding = str(query_embedding.tolist())

        model_sources = data_sources.filtered(lambda ds: ds.type == 'model' and ds.model_id)
        non_model_sources = data_sources - model_sources

        sub_queries = []
        params = []

        # For non-model sources, users can access all data items.
        if non_model_sources:
            sub_queries.append("""
                SELECT id, data,
                       1 - (embedding <=> %s)::numeric as similarity
                FROM ai_data_item
                WHERE data_source_id IN %s
                ORDER BY similarity DESC
                LIMIT %s
            """)
            params.extend([embedding, tuple(non_model_sources.ids), limit])

        # For model sources, users can only access data items that are related to the records
        # they can access.
        for model, sources in groupby(model_sources, lambda ds: ds.model):
            source_ids = tuple(source.id for source in sources)
            Model = self.env[model]
            query = Model._where_calc([])
            Model._apply_ir_rules(query, 'read')
            table, where_clauses, where_params = query.get_sql()
            sub_queries.append(f"""
                SELECT ai_data_item.id, ai_data_item.data,
                       1 - (ai_data_item.embedding <=> %s)::numeric as similarity
                FROM {table}
                JOIN ai_data_item ON ai_data_item.data_source_id IN %s
                    AND ai_data_item.res_model = '{model}'
                    AND ai_data_item.res_id = {Model._table}.id
                {"" if not where_clauses else "WHERE " + where_clauses}
                ORDER BY similarity DESC
                LIMIT %s
            """)
            params.extend([embedding, source_ids, *where_params, limit])

        if not sub_queries:
            return []

        if len(sub_queries) == 1:
            sub_query = sub_queries[0]
        else:
            sub_query = " UNION ALL ".join(f"({q})" for q in sub_queries)

        query = f"""
            SELECT * FROM (
                {sub_query}
            ) results
            ORDER BY similarity DESC
            LIMIT %s
        """
        params.append(limit)

        self.env.cr.execute(query, tuple(params))

        results = self.env.cr.dictfetchall()
        for r in results:
            item = self.sudo().browse(r.pop('id'))
            if url := item._get_access_url():
                r['url'] = url
        return results

    def _get_access_url(self):
        """Get the URL of the current data item."""
        self.ensure_one()
        base_url = self.get_base_url()
        url = False
        if self.res_url:
            url = self.res_url
        elif self.res_model and self.res_id:
            record = self.env[self.res_model].browse(self.res_id)
            if hasattr(record, 'website_url') and getattr(record, 'is_published', False):
                return f"{base_url}{record.website_url}"
            else:
                url = f"{base_url}/web#id={self.res_id}&model={self.res_model}"
        return url

    def write(self, vals):
        if 'data' in vals:
            vals['vector_generated'] = False  # Force regenerate vector if data changed
        return super().write(vals)

"""Tenant usage endpoints: cache-only aggregates, bounded synchronous log reads."""
import re
import time
from datetime import datetime, timedelta, timezone

from odoo import api, models

from ..services import llm_usage as usage
from ..services import newapi_client as nac
from .llm_common import (
    ERR_LLM_KEY_NOT_FOUND, ERR_LLM_UNAVAILABLE, LlmError, llm_config,
)
from .llm_swr import iso

MODEL_ID = re.compile(r'[A-Za-z0-9._:/-]{1,128}', re.ASCII)
TAIPEI = timezone(timedelta(hours=8))


def _days(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _range(params):
    start = params.get('start', params.get('start_date'))
    end = params.get('end', params.get('end_date'))
    name = params.get('range', 'custom' if start is not None or end is not None else 'month')
    try:
        return usage.parse_range(name, start, end)
    except usage.UsageInvalid as exc:
        raise LlmError(exc.code) from None


def _model(params):
    value = params.get('model')
    if value is not None and (not isinstance(value, str) or not MODEL_ID.fullmatch(value)):
        raise LlmError('llm_invalid_filter')
    return value


class LlmAccountUsage(models.Model):
    _inherit = 'woow_paas_platform.llm_account'

    @api.model
    def _llm_usage_context(self, partner, params):
        account = self._llm_account_for_partner(partner)
        account._llm_require_active()
        start, end = _range(params)
        model = _model(params)
        key_id = params.get('key_id')
        key = self.env['woow_paas_platform.llm_api_key'].sudo().browse()
        if key_id is not None:
            if type(key_id) is not int or key_id <= 0:
                raise LlmError(ERR_LLM_KEY_NOT_FOUND)
            key = account.api_key_ids.filtered(lambda item: item.id == key_id)
            if not key or not key.newapi_name:
                raise LlmError(ERR_LLM_KEY_NOT_FOUND)
        return account, start, end, model, key

    @api.model
    def _llm_usage_aggregate(self, partner, params, *, chart=False):
        account, start, end, model, key = self._llm_usage_context(partner, params)
        group_by, metric = params.get('group_by', 'model'), params.get('metric', 'spend')
        if group_by not in ('model', 'key') or metric not in ('spend', 'requests', 'tokens'):
            raise LlmError('llm_invalid_filter')
        requested = _days(start, end)
        today = datetime.now(TAIPEI).date()
        recent_days = _days(today - timedelta(days=6), today) if not chart else []
        snapshots = self.env['woow_paas_platform.llm_swr_cache']._llm_usage_day_snapshots(
            account, requested + recent_days)
        selected = [snapshots[day.isoformat()] for day in requested if day.isoformat() in snapshots]
        complete = len(selected) == len(requested)
        # Resolve labels again at read time: a renamed key must not keep its old label
        # for the lifetime of a historical snapshot. Ownership is always local.
        names = {item.id: item.key_label or None for item in account.api_key_ids}
        rows = []
        for snapshot in selected:
            for row in snapshot['items']:
                if (key and row['key_id'] != key.id) or (model and row['model'] != model):
                    continue
                rows.append({**row, 'key_name': names.get(row['key_id'])})
        meta = {
            'start': start.isoformat(), 'end': end.isoformat(), 'complete': complete,
            'stale': any(item['stale'] for item in selected),
            'data_as_of': iso(min(item['fetched_at'] for item in selected)) if complete else None,
        }
        if chart:
            data = usage.daily_series(rows, start, end, group_by, metric)
            # A partial first load is not a zero-spend day. The UI waits for complete.
            if not complete:
                data['series'] = []
            return {**data, **meta}
        recent_complete = all(day.isoformat() in snapshots for day in recent_days)
        recent = [row for day in recent_days for row in snapshots.get(day.isoformat(), {}).get('items', [])]
        free = self.env['woow_paas_platform.llm_free_usage']._llm_today(account.id)
        data = usage.summarize(rows, balance_quota=account.quota_cached,
                               recent_rows=recent if recent_complete else None,
                               free_today_count=free['today_count'])
        if not complete:
            for name in ('quota', 'spend_points', 'requests', 'tokens', 'input_tokens', 'output_tokens'):
                data[name] = None
        if not recent_complete:
            data['days_left'] = None
        return {**data, **meta}

    @api.model
    def _llm_usage_logs(self, partner, params):
        account, start, end, model, key = self._llm_usage_context(partner, params)
        page, size = params.get('page', 1), params.get('page_size', 20)
        status = params.get('status', 'success')
        # Each page selects one exact log type. Mixing all log types would expose
        # unrelated account operations and make paging totals misleading.
        if (type(page) is not int or not 1 <= page <= 10000
                or type(size) is not int or not 1 <= size <= 50
                or status not in ('success', 'error')):
            raise LlmError('llm_invalid_filter')
        first, _ = usage.day_bounds(start)
        _, last = usage.day_bounds(end)
        query = {'type': 2 if status == 'success' else 5, 'start_timestamp': first,
                 'end_timestamp': last, 'p': page, 'page_size': size}
        if model:
            query['model_name'] = model
        if key:
            query['token_name'] = key.newapi_name
        # Only after all input/ownership validation do we read credentials and call
        # the upstream. One request, one three-second end-to-end client budget.
        deadline = time.monotonic() + 3
        client = account._llm_client(nac.ROLE_WEB)
        result = client.user_log_self(account._llm_tenant_ip(llm_config(self.env)),
                                      account._llm_up_token(role=nac.ROLE_WEB), query,
                                      timeout=nac.TIMEOUT_INTERACTIVE_READ, deadline=deadline)
        if not result.ok:
            self.env['woow_paas_platform.llm_swr_cache']._label(result, account)
            raise LlmError(ERR_LLM_UNAVAILABLE)
        names = {item.newapi_name: {'id': item.id, 'name': item.key_label or None,
                                    'kind': item.kind}
                 for item in account.api_key_ids if item.newapi_name}
        try:
            data = usage.normalize_logs(result.data, names)
        except usage.UsageInvalid:
            raise LlmError(ERR_LLM_UNAVAILABLE) from None
        if len(data['items']) > size:
            raise LlmError(ERR_LLM_UNAVAILABLE)
        # Verify the exact key filter before releasing any row, even if an upstream
        # regression were to ignore the supplied token_name.
        if key and any(row['key_id'] != key.id for row in data['items']):
            raise LlmError(ERR_LLM_UNAVAILABLE)
        return {**data, 'page': page, 'page_size': size,
                'data_as_of': datetime.now(timezone.utc).isoformat()}

/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { LlmUsageChart } from "../../../components/llm/LlmUsageChart";
import { llmService } from "../../../services/llm_service";
import { llmErrorMessage } from "../../../services/llm_errors";
import { copyText } from "../../../utils/clipboard";
import {
  formatAiPoints, formatCount, formatSpendPoints, formatTaipeiDateTime, formatTokens, quotaToPointsText,
} from "../../../utils/llm_format";
import { rangeDates, taipeiToday, usageRequestParams, validateUsageDates } from "./llm_usage_helpers";

const PAGE_SIZE = 20;
const FILL_RETRIES_MAX = 5;
const FILL_RETRY_MS = 30000;
const ERROR_LABELS = Object.assign(Object.create(null), {
  invalid_request: () => _t("Invalid request"),
  authentication_error: () => _t("Authentication failed"),
  permission_error: () => _t("Permission denied"),
  not_found: () => _t("Resource not found"),
  rate_limit: () => _t("Rate limit exceeded"),
  insufficient_quota: () => _t("Insufficient quota"),
  context_length_exceeded: () => _t("Context length exceeded"),
  content_policy_violation: () => _t("Content policy restriction"),
  timeout: () => _t("Request timed out"),
  upstream_error: () => _t("Service temporarily unavailable"),
});

export function usageErrorLabel(code) {
  return typeof code === "string" && Object.prototype.hasOwnProperty.call(ERROR_LABELS, code)
    ? ERROR_LABELS[code]() : _t("Request failed");
}

export class LlmUsageTab extends Component {
  static template = "woow_paas_platform.LlmUsageTab";
  static components = { LlmUsageChart };
  static props = {};

  setup() {
    this.llm = useState(llmService);
    const dates = rangeDates("30d");
    this.state = useState({
      range: "30d", start: dates.start, end: dates.end, keyId: "", model: "", status: "success",
      groupBy: "model", metric: "spend", summary: null, daily: null, items: [], total: 0, page: 1,
      loading: false, logsLoading: false, error: null, logsError: null, copyId: null, copyFailed: false,
      refreshing: false,
    });
    this.summaryEpoch = 0;
    this.dailyEpoch = 0;
    this.logsEpoch = 0;
    this.alive = true;
    this.fillRetries = 0;
    this.fillTimer = null;
    onMounted(() => {
      if (!llmService.keysLoaded) llmService.fetchKeys();
      if (!llmService.modelsLoaded) llmService.fetchModels();
      this.loadAll(1);
    });
    onWillUnmount(() => {
      this.alive = false;
      this.summaryEpoch += 1;
      this.dailyEpoch += 1;
      this.logsEpoch += 1;
      clearTimeout(this.fillTimer);
    });
  }

  get keys() {
    return (this.llm.keys || []).map((key) => ({
      id: String(key.id),
      label: key.display_name || (key.kind === "free_trial" ? _t("Free trial") : _t("Unknown key")),
    }));
  }
  get models() {
    const items = this.llm.models?.items || [];
    return items.map((item) => typeof item === "string" ? item : item.model).filter(Boolean);
  }
  get dateError() {
    const issue = this.state.range === "custom"
      ? validateUsageDates(this.state.start, this.state.end)
      : validateUsageDates(...Object.values(rangeDates(this.state.range) || {}));
    if (issue === "range") return _t("Choose a date range of at most 31 days within the last 180 days.");
    if (issue) return _t("Please check the usage filters and page size.");
    return "";
  }
  get summarySpend() {
    const summary = this.state.summary;
    const exact = summary && quotaToPointsText(summary.quota, 2);
    return exact === null ? formatAiPoints(summary?.spend_points) : _t("%(amount)s pts", { amount: exact });
  }
  get requestCount() { return formatCount(this.state.summary?.requests); }
  get tokenCount() { return formatTokens(this.state.summary?.tokens); }
  get inputTokenCount() { return this.state.summary?.input_tokens == null ? "—" : formatTokens(this.state.summary.input_tokens); }
  get outputTokenCount() { return this.state.summary?.output_tokens == null ? "—" : formatTokens(this.state.summary.output_tokens); }
  get daysLeft() {
    const value = this.state.summary?.days_left;
    return typeof value === "number" && Number.isFinite(value) ? _t("About %(days)s days", { days: String(Math.floor(value * 10) / 10) }) : "—";
  }
  get dataTime() {
    const time = this.state.summary?.data_as_of || this.state.daily?.data_as_of;
    return time ? _t("Data as of %(time)s (Taipei time)", { time: formatTaipeiDateTime(time) }) : _t("Data time unavailable");
  }
  errorMessage(code) { return llmErrorMessage({ code }); }
  get pageCount() { return Math.max(1, Math.ceil(this.state.total / PAGE_SIZE)); }
  get pageText() { return _t("Page %(page)s of %(pages)s", { page: this.state.page, pages: this.pageCount }); }
  get rangeLabel() { return _t("Date range"); }
  get noRowsText() { return _t("No usage records for these filters."); }
  get footerText() {
    return _t("Statistics are delayed by about 5 minutes; logs are retained for 180 days; concurrent requests may cause actual spend to exceed an API key’s total limit.");
  }
  get logLabels() {
    return { time: _t("Time (Taipei)"), key: _t("API key"), model: _t("Model"), tokens: _t("Tokens"),
      spend: _t("Spend"), status: _t("Status"), requestId: _t("Request ID") };
  }
  get rows() {
    return this.state.items.map((item, index) => ({
      ...item,
      key: item.request_id || `${item.time}-${index}`,
      displayKeyName: item.key_name || this.keys.find((key) => key.id === String(item.key_id))?.label || _t("Unknown key"),
      displayTime: formatTaipeiDateTime(item.time_tpe),
      displayInput: formatTokens(item.input_tokens),
      displayOutput: formatTokens(item.output_tokens),
      displayCache: formatTokens(item.cache_tokens),
      displaySpend: Number.isInteger(item.quota) && item.quota > 0 && item.quota < 2
        ? formatSpendPoints(item.quota / 15625)
        : Number.isInteger(item.quota)
          ? _t("%(amount)s pts", { amount: quotaToPointsText(item.quota, 4) })
          : formatSpendPoints(item.spend_points),
      displayStatus: item.status === "error" ? _t("Failed") : _t("Successful"),
      safeError: item.status === "error" ? usageErrorLabel(item.error_code) : "",
    }));
  }

  params(page = this.state.page, includeStatus = false) {
    const filters = {
      range: this.state.range, start: this.state.start, end: this.state.end,
      keyId: this.state.keyId, model: this.state.model,
      ...(includeStatus ? { status: this.state.status } : {}),
    };
    return usageRequestParams(filters, page);
  }

  async loadAll(page = this.state.page) {
    this.state.page = page;
    this.state.error = null;
    this.state.logsError = null;
    clearTimeout(this.fillTimer);
    this.fillTimer = null;
    this.fillRetries = 0;
    const summaryEpoch = ++this.summaryEpoch;
    const dailyEpoch = ++this.dailyEpoch;
    const logsEpoch = ++this.logsEpoch;
    this.state.loading = false;
    this.state.logsLoading = false;
    if (this.dateError) {
      this.state.summary = null;
      this.state.daily = null;
      this.state.items = [];
      this.state.total = 0;
      this.state.error = "llm_invalid_range";
      this.state.logsError = "llm_invalid_range";
      return;
    }
    const params = this.params(page);
    this.state.loading = true;
    this.state.logsLoading = true;
    const [summary, daily, logs] = await Promise.all([
      llmService.fetchUsageSummary(params),
      llmService.fetchUsageDaily({ ...params, group_by: this.state.groupBy, metric: this.state.metric }),
      llmService.fetchUsageLogs(this.params(page, true)),
    ]);
    if (!this.alive) return;
    if (summaryEpoch === this.summaryEpoch) {
      this.state.loading = false;
      if (summary?.success) {
        this.state.summary = summary.data;
        this.state.error = null;
        if (summary.data?.complete === false && summary.data?.data_as_of == null && this.fillRetries < FILL_RETRIES_MAX) {
          this.fillRetries += 1;
          this.fillTimer = setTimeout(() => this.loadSummaryFill(), FILL_RETRY_MS);
        }
      } else {
        this.state.error = summary?.code || "llm_unavailable";
      }
    }
    if (dailyEpoch === this.dailyEpoch) {
      if (daily?.success) this.state.daily = daily.data;
      else this.state.error = daily?.code || "llm_unavailable";
    }
    if (logsEpoch === this.logsEpoch) {
      this.state.logsLoading = false;
      if (logs?.success) {
        this.state.items = Array.isArray(logs.data?.items) ? logs.data.items : [];
        this.state.total = Number.isFinite(logs.data?.total) ? logs.data.total : 0;
        this.state.logsError = null;
      } else {
        this.state.logsError = logs?.code || "llm_unavailable";
      }
    }
  }

  async loadSummaryFill() {
    this.fillTimer = null;
    if (!this.alive || this.dateError) return;
    const summaryEpoch = ++this.summaryEpoch;
    const dailyEpoch = ++this.dailyEpoch;
    const [result, daily] = await Promise.all([
      llmService.fetchUsageSummary(this.params(this.state.page)),
      llmService.fetchUsageDaily({ ...this.params(this.state.page), group_by: this.state.groupBy, metric: this.state.metric }),
    ]);
    if (!this.alive || summaryEpoch !== this.summaryEpoch) return;
    if (result?.success) this.state.summary = result.data;
    if (dailyEpoch === this.dailyEpoch && daily?.success) this.state.daily = daily.data;
    if (result?.success && result.data?.complete === false && result.data?.data_as_of == null && this.fillRetries < FILL_RETRIES_MAX) {
      this.fillRetries += 1;
      this.fillTimer = setTimeout(() => this.loadSummaryFill(), FILL_RETRY_MS);
    }
  }

  setFilter(name, event) {
    const value = event.target.value;
    if (name === "range") {
      this.state.range = value;
      const dates = rangeDates(value);
      if (dates) { this.state.start = dates.start; this.state.end = dates.end; }
      this.fillRetries = 0;
      this.loadAll(1);
      return;
    }
    this.state[name] = value;
    if (name === "start" || name === "end") this.state.range = "custom";
    this.loadAll(1);
  }
  setChart(name, event) {
    this.state[name] = event.target.value;
    this.loadAll(this.state.page);
  }
  setStatus(event) {
    this.state.status = event.target.value;
    this.loadAll(1);
  }
  refresh() { this.fillRetries = 0; return this.loadAll(this.state.page); }
  previousPage() { if (this.state.page > 1) this.loadAll(this.state.page - 1); }
  nextPage() { if (this.state.page < this.pageCount) this.loadAll(this.state.page + 1); }
  chartValue(value) {
    return this.state.metric === "spend" ? formatSpendPoints(value)
      : this.state.metric === "tokens" ? formatTokens(value) : formatCount(value);
  }
  copyLabel(requestId) { return _t("Copy request ID %(id)s", { id: requestId }); }
  metricLabel(metric) {
    return metric === "spend" ? _t("Spend (points)") : metric === "requests" ? _t("Requests") : _t("Tokens");
  }
  async copyRequestId(requestId) {
    const copied = await copyText(requestId);
    if (!this.alive) return;
    this.state.copyId = requestId;
    this.state.copyFailed = !copied;
  }
}

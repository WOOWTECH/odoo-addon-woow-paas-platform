/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { llmService } from "../../../services/llm_service";
import { copyText } from "../../../utils/clipboard";
import { formatTaipeiTime } from "../../../utils/llm_format";

const TAG_LABELS = {
  text: () => _t("Text"), vision: () => _t("Vision"), reasoning: () => _t("Reasoning"),
  tools: () => _t("Tool use"), embedding: () => _t("Embeddings"),
  image: () => _t("Images"), audio: () => _t("Audio"),
};

/** Rates arrive as exact decimal strings; do not round small non-zero rates to free. */
export function priceText(value) {
  return typeof value === "string" && /^(0|[1-9]\d*)(\.\d+)?$/.test(value) ? value : "—";
}

export function modelRow(item) {
  const price = item.price || {};
  return {
    ...item,
    vendor: item.vendor || _t("Not specified"),
    tags: (item.tags || []).filter((tag) => Object.hasOwn(TAG_LABELS, tag))
      .map((id) => ({ id, label: TAG_LABELS[id]() })),
    input: priceText(price.input), output: priceText(price.output),
    cacheRead: priceText(price.cache_read), cacheWrite: priceText(price.cache_write),
    note: item.pricing_mode === "tiered" ? _t("Usage-based pricing")
      : item.pricing_mode === "fixed" ? _t("%(points)s points per call", { points: priceText(price.per_call) }) : "",
  };
}

export function filterModels(items, query = "", tag = "") {
  const search = query.trim().toLowerCase();
  return items.filter((item) => (!tag || (item.tags || []).includes(tag))
    && (!search || [item.model, item.vendor || ""].join(" ").toLowerCase().includes(search)));
}

/** Up to four representative models, preferring a range of vendors plus one free model. */
export function representativeModels(items) {
  const paid = items.filter((item) => item.group === "default");
  const picked = [], vendors = new Set();
  for (const item of paid) {
    if (!vendors.has(item.vendor) && picked.length < 3) {
      picked.push(item); vendors.add(item.vendor);
    }
  }
  for (const item of paid) {
    if (picked.length < 3 && !picked.includes(item)) picked.push(item);
  }
  const free = items.find((item) => item.group === "free");
  if (free) picked.push(free);
  return picked.map(modelRow);
}

export class LlmModelsTab extends Component {
  static template = "woow_paas_platform.LlmModelsTab";
  static props = {};

  setup() {
    this.llm = useState(llmService);
    this.state = useState({ query: "", tag: "", loading: false, announcement: "" });
    this.alive = true;
    onMounted(() => this.load());
    onWillUnmount(() => { this.alive = false; });
  }

  async load() {
    if (this.state.loading) return;
    this.state.loading = true;
    await llmService.fetchModels();
    if (this.alive) this.state.loading = false;
  }

  get items() { return this.llm.models && this.llm.models.state === "ok" ? this.llm.models.items : []; }
  get rows() { return filterModels(this.items, this.state.query, this.state.tag).map(modelRow); }
  get unavailable() { return !this.llm.models || this.llm.models.state !== "ok" || !this.items.length; }
  get tags() {
    const tags = new Set(this.items.flatMap((item) => item.tags || []));
    return Object.entries(TAG_LABELS).filter(([id]) => tags.has(id)).map(([id, label]) => ({ id, label: label() }));
  }
  get labels() {
    return { model: _t("Model ID"), vendor: _t("Provider"), tags: _t("Capabilities"),
      input: _t("Input"), output: _t("Output"), cacheRead: _t("Cache read"), cacheWrite: _t("Cache write") };
  }
  get loadingText() { return _t("Loading model prices…"); }
  get unavailableText() { return _t("Models and prices are unavailable right now. Please try again later."); }
  get emptyText() { return _t("No models match your search or capability filter."); }
  get freeText() { return _t("Free trial"); }
  get freeNote() { return _t("10 requests per minute, about 200 per day (resets at 00:00 Taipei time). Free trial keys only; capacity may change."); }
  get footerText() { return _t("Prices are in points per million tokens. 1 point = NT$1 (tax included). Price increases will be announced 7 days in advance."); }
  get cacheNote() { return _t("A dash means no separate price is published. For Claude usage, when the upstream reports 1-hour cache-creation tokens, cache writes are billed at 1.6× the displayed cache-write rate."); }
  get dataTime() { return this.llm.models && this.llm.models.data_as_of ? _t("Data as of %(time)s", { time: formatTaipeiTime(this.llm.models.data_as_of) }) : ""; }
  get copyLabel() { return _t("Copy model ID"); }

  async copyModel(model) {
    const ok = await copyText(model);
    if (this.alive) this.state.announcement = ok ? _t("Copied model ID: %(model)s", { model }) : _t("Copy failed. Please select and copy the model ID.");
  }
}

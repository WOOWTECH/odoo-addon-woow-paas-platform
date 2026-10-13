/** @odoo-module **/
import { _t } from "@web/core/l10n/translation";

const GIB = 1024 ** 3;
export const RESOURCE_STALE_AFTER_MS = 180000;

export function resourceNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function formatResourceNumber(value, unit) {
  const number = resourceNumber(value);
  if (number === null) return "—";
  const converted = unit === "bytes" ? number / GIB : number;
  if (converted > 0 && converted < 0.01) return "<0.01";
  return Number(converted.toFixed(2)).toString();
}

/** A missing sample is never zero; a partially observed sum is never a total. */
export function resourceRow(key, metric = {}, { now = Date.now(), refreshFailed = false } = {}) {
  metric = metric || {};
  const labels = { cpu: _t("CPU"), memory: _t("Memory"), storage: _t("Persistent storage") };
  const unit = key === "cpu" ? "cores" : "bytes";
  const usage = resourceNumber(metric.usage);
  const capacity = resourceNumber(key === "storage" ? metric.capacity : metric.limit);
  const sampledAt = typeof metric.sampled_at === "string" && Number.isFinite(Date.parse(metric.sampled_at))
    ? metric.sampled_at : null;
  let status = metric.status || "unavailable";
  if (["ok", "partial", "stale"].includes(status) && usage !== null &&
    (refreshFailed || (sampledAt && now - Date.parse(sampledAt) > RESOURCE_STALE_AFTER_MS))) {
    status = "stale";
  }
  const incomplete = status === "partial" ||
    (resourceNumber(metric.coverage?.expected) !== null && metric.coverage.expected > (metric.coverage.observed ?? 0));
  const usable = !["stopped", "unsupported", "unavailable"].includes(status);
  const usedLabel = usable ? formatResourceNumber(usage, unit) : "—";
  const unitLabel = unit === "cores" ? _t("cores") : "GiB";
  const unbounded = key !== "storage" && metric.limit_status === "unbounded";
  const bounded = key === "storage" || metric.limit_status === "bounded";
  const percent = usable && usage !== null && capacity !== null && capacity > 0 && bounded && !incomplete
    ? usage / capacity * 100 : null;
  let note = "";
  if (status === "stale") note = _t("Outdated sample");
  else if (status === "unsupported") note = _t("Not supported");
  else if (status === "stopped") note = _t("Service stopped");
  else if (incomplete) note = _t("Partial data (%s/%s)", metric.coverage?.observed ?? 0, metric.coverage?.expected ?? 0);
  else if (usage === null || !usable) note = _t("No usage data");
  else if (unbounded) note = _t("No limit set");
  else if (capacity === null || !bounded) note = _t("Limit unavailable");
  return {
    key, label: labels[key], status, sampledAt, incomplete, note,
    usage: usedLabel,
    capacity: bounded && capacity !== null ? formatResourceNumber(capacity, unit) : "—",
    unit: unitLabel,
    percent: percent === null ? null : Math.round(percent),
    width: percent === null ? 0 : Math.min(100, Math.max(0, percent)),
    tone: status === "stale" ? "stale" : percent >= 90 ? "high" : percent >= 75 ? "warning" : "normal",
  };
}

export function resourceRows(observation, options) {
  return ["cpu", "memory", "storage"].map((key) => resourceRow(key, observation?.[key], options));
}

/** Retain a last real sample during telemetry failure, explicitly marked stale. */
export function retainResourceSamples(previous, current) {
  if (!previous || !current || ["stopped", "unsupported"].includes(current.status)) return current;
  const result = { ...current };
  for (const key of ["cpu", "memory", "storage"]) {
    if (current[key]?.status === "unavailable" && resourceNumber(previous[key]?.usage) !== null) {
      result[key] = { ...previous[key], status: "stale" };
    }
  }
  return result;
}

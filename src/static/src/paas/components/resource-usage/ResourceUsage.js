/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { resourceRows } from "../../utils/resource_observations";
import { formatUserDateTime } from "../../utils/datetime_helpers";

export class ResourceUsage extends Component {
  static template = "woow_paas_platform.ResourceUsage";
  static props = {
    observation: { type: Object, optional: true },
    compact: { type: Boolean, optional: true },
    loading: { type: Boolean, optional: true },
    refreshFailed: { type: Boolean, optional: true },
    now: { type: Number, optional: true },
  };
  get rows() {
    return resourceRows(this.props.observation, { now: this.props.now, refreshFailed: this.props.refreshFailed });
  }
  get statusLabel() {
    if (this.props.loading && !this.props.observation) return _t("Waiting for data");
    if (this.props.refreshFailed || this.rows.some((row) => row.status === "stale")) return _t("Data outdated");
    const status = this.props.observation?.status;
    if (status === "unsupported") return _t("Metrics not supported");
    if (status === "stopped") return _t("Service stopped");
    if (status === "partial") return _t("Partial data");
    if (!status || status === "unavailable") return _t("Data unavailable");
    return _t("Updates every 30 seconds");
  }
  get hasWarning() {
    return this.props.refreshFailed || this.rows.some((row) => row.status !== "ok");
  }
  get sampledAt() {
    const timestamps = this.rows.map((row) => row.sampledAt).filter(Boolean).sort((a, b) => Date.parse(a) - Date.parse(b));
    return timestamps.length ? formatUserDateTime(timestamps[0], { showSeconds: true }, "") : "";
  }
  get title() { return _t("Resource usage"); }
  get scopeHint() {
    if (this.props.observation?.workload_type === "vm") {
      return _t("Virtual machine capacity is shown when available. Live guest usage is not yet supported.");
    }
    return _t("Includes this service's replicas, sidecars and databases. CPU is a 2-minute average. Storage counts each persistent volume once.");
  }
  get valueHint() { return _t("Usage / effective limit or capacity"); }
  get observedLabel() { return _t("Sampled at %s", this.sampledAt); }
}

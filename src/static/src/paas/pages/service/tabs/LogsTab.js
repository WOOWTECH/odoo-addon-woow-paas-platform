/** @odoo-module **/
import { Component, useState, useRef, onMounted, onPatched, onWillDestroy } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { cloudService } from "../../../services/cloud_service";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { createLogPoller, filterLogLines, downloadLogSnapshot, logDownloadFilename } from "./service_logs";

export class LogsTab extends Component {
  static template = "woow_paas_platform.LogsTab";
  static components = { WoowIcon };
  static props = { service: Object, workspaceId: Number, canManage: Boolean };

  setup() {
    this.state = useState({ loading: true, error: false, paused: false, wrap: true,
      query: "", pod: "", container: "", sources: [], text: "", available: false,
      reason: "", previous: false, truncated: false, atBottom: true, updatedAt: "" });
    this.viewport = useRef("viewport");
    this._scrollAfterPatch = false;
    this.poller = createLogPoller({
      load: async () => {
        const result = await cloudService.fetchLogs(this.props.workspaceId, this.props.service.id, {
          pod: this.state.pod || undefined,
          container: this.state.container || undefined,
          previous: this.state.previous,
        });
        if (!result.success) throw new Error("logs_unavailable");
        return result.data;
      },
      onData: (data) => this.acceptLogs(data),
      onError: () => { this.state.loading = false; this.state.error = true; },
    });
    onMounted(() => { if (this.props.canManage) this.poller.start(); });
    onPatched(() => {
      if (this._scrollAfterPatch) {
        this._scrollAfterPatch = false;
        this.scrollToLatest();
      }
    });
    onWillDestroy(() => this.poller.stop());
  }

  acceptLogs(data) {
    // Preserve the reader's position. New snapshots only follow when at the end.
    this._scrollAfterPatch = this.state.atBottom;
    this.state.loading = false;
    this.state.error = false;
    this.state.sources = data.sources || [];
    this.state.pod = data.pod || "";
    this.state.container = data.container || "";
    if (data.reason === "source_not_found") {
      this.state.pod = "";
      this.state.container = "";
    } else if (data.reason === "container_not_found") {
      this.state.container = "";
    }
    this.state.available = data.available === true;
    this.state.reason = data.reason || "";
    this.state.text = typeof data.text === "string" ? data.text : "";
    this.state.truncated = data.truncated === true;
    this.state.updatedAt = new Date().toLocaleTimeString();
  }

  get containers() {
    return this.state.sources.find((source) => source.pod === this.state.pod)?.containers || [];
  }

  get filteredText() {
    return filterLogLines(this.state.text, this.state.query);
  }

  get updateLabel() {
    return this.state.paused ? _t("Auto-refresh paused") : _t("Auto-refresh every 5 seconds");
  }

  get lastUpdatedLabel() {
    return _t("Last updated: %s", this.state.updatedAt);
  }

  get emptyMessage() {
    if (this.state.paused && !this.state.updatedAt) {
      return _t("Auto-refresh is paused. Resume or refresh to load logs.");
    }
    if (this.state.available) {
      return this.state.text ? _t("No log lines match your search.") : _t("No log output yet.");
    }
    if (["no_pods", "not_deployed"].includes(this.state.reason)) {
      return _t("Waiting for the service to start producing logs.");
    }
    if (this.state.reason === "unsupported_workload") return _t("Logs are not available for this service type.");
    if (["source_not_found", "container_not_found"].includes(this.state.reason)) {
      return _t("This log source has changed. Refresh to load an available source.");
    }
    if (this.state.previous && this.state.reason === "logs_unavailable") {
      return _t("No logs from a previous container are available.");
    }
    return _t("Logs are temporarily unavailable. Retry in a moment.");
  }

  togglePause() {
    this.state.paused = !this.state.paused;
    if (this.state.paused) this.state.loading = false;
    this.poller.setPaused(this.state.paused);
  }

  refresh() {
    this.poller.refresh();
  }

  resetSource() {
    this.state.loading = true;
    this.state.error = false;
    this.state.text = "";
    this.state.available = false;
    this.state.updatedAt = "";
    this.state.atBottom = true;
    this.poller.refresh();
  }

  changePod(event) {
    this.state.pod = event.target.value;
    this.state.container = this.containers[0] || "";
    this.resetSource();
  }

  changeContainer(event) {
    this.state.container = event.target.value;
    this.resetSource();
  }

  changePrevious(event) {
    this.state.previous = event.target.checked;
    this.resetSource();
  }

  onScroll() {
    const el = this.viewport.el;
    if (el) this.state.atBottom = el.scrollHeight - el.clientHeight - el.scrollTop < 32;
  }

  scrollToLatest() {
    const el = this.viewport.el;
    if (el) el.scrollTop = el.scrollHeight;
    this.state.atBottom = true;
  }

  download() {
    if (!this.state.text) return;
    downloadLogSnapshot(this.state.text, logDownloadFilename(
      this.props.service.id, this.state.pod, this.state.container,
    ));
  }
}

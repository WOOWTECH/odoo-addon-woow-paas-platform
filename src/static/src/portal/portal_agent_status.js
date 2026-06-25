/** @odoo-module **/
import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { aiService } from "../paas/services/ai_service";
import { podStateLabel } from "../paas/services/agent_pod_state";

const POLL_MS = 10000;

export class PortalAgentStatus extends Component {
  static template = "woow_paas_platform.PortalAgentStatus";
  static props = { projectId: Number };

  setup() {
    this.state = useState({ agent: null, pod: null, error: null });
    this._timer = null;
    this._onVisibility = () => {
      if (document.hidden) {
        this._stopPolling();
      } else {
        this._refreshPod();
        this._startPolling();
      }
    };
    onWillStart(async () => {
      const res = await aiService.fetchAgentStatus(this.props.projectId);
      if (res.success) {
        this.state.agent = res.data;
      } else {
        this.state.error = res.error || "無法取得 Agent 狀態";
      }
    });
    onMounted(() => {
      this._refreshPod();
      this._startPolling();
      document.addEventListener("visibilitychange", this._onVisibility);
    });
    onWillUnmount(() => {
      this._stopPolling();
      document.removeEventListener("visibilitychange", this._onVisibility);
    });
  }

  get podLabel() {
    return podStateLabel(this.state.pod?.state);
  }

  get podStateClass() {
    const s = this.state.pod?.state;
    if (s === "active") return "text-bg-success";
    if (s === "spawning") return "text-bg-warning";
    return "text-bg-secondary";
  }

  async _refreshPod() {
    const res = await aiService.fetchAgentPodStatus(this.props.projectId);
    if (res.success) {
      this.state.pod = res.data;
    }
  }

  _startPolling() {
    if (!this._timer) {
      this._timer = setInterval(() => this._refreshPod(), POLL_MS);
    }
  }

  _stopPolling() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }
}

registry
  .category("public_components")
  .add("woow_paas_platform.PortalAgentStatus", PortalAgentStatus);

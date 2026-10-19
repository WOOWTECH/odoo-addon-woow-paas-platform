/** @odoo-module **/
import { Component, useState, onMounted, onWillDestroy } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { cloudService } from "../../../services/cloud_service";
import { parseMarkdown } from "../../../services/markdown_parser";
import { WoowIcon } from "../../../components/icon/WoowIcon";

export class InformationTab extends Component {
  static template = "woow_paas_platform.InformationTab";
  static components = { WoowIcon };
  static props = { service: Object, workspaceId: Number };

  setup() {
    this.state = useState({
      loading: true, error: false, title: "", content: "", url: "", tutorialUrl: "",
      deployedChartVersion: "", currentTemplateChartVersion: "",
    });
    this._disposed = false;
    onMounted(() => this.load());
    onWillDestroy(() => { this._disposed = true; });
  }

  async load() {
    this.state.loading = true;
    this.state.error = false;
    try {
      const result = await cloudService.fetchDocumentation(this.props.workspaceId, this.props.service.id);
      if (this._disposed) return;
      if (!result.success) throw new Error("documentation_unavailable");
      this.state.title = result.data.title || this.props.service.name;
      this.state.content = parseMarkdown(result.data.content || "");
      const url = result.data.documentation_url || "";
      this.state.url = /^https?:\/\//i.test(url) ? url : "";
      const tutorialUrl = result.data.tutorial_url || "";
      this.state.tutorialUrl = /^https?:\/\//i.test(tutorialUrl) ? tutorialUrl : "";
      this.state.deployedChartVersion = result.data.deployed_chart_version || "";
      this.state.currentTemplateChartVersion = result.data.current_template_chart_version || "";
    } catch {
      if (!this._disposed) this.state.error = true;
    } finally {
      if (!this._disposed) this.state.loading = false;
    }
  }

  get errorText() {
    return _t("Documentation could not be loaded. Please try again.");
  }

  get hasVersionDifference() {
    return !!this.state.deployedChartVersion && !!this.state.currentTemplateChartVersion
      && this.state.deployedChartVersion !== this.state.currentTemplateChartVersion;
  }
}

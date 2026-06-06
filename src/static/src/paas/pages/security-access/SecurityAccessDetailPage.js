/** @odoo-module **/

import { Component, useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { StatusBadge } from "../../components/common/StatusBadge";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { securityAccessService } from "../../services/security_access";
import { router } from "../../core/router";

const TABS = [
  { key: "overview", label: "Overview", icon: "info" },
  { key: "configuration", label: "Configuration", icon: "settings" },
  { key: "application", label: "Application", icon: "route" },
  { key: "access", label: "Access", icon: "shield", comingSoon: true },
  { key: "email", label: "Email", icon: "mail", comingSoon: true },
  { key: "domains", label: "Domains", icon: "language", comingSoon: true },
];

/**
 * SecurityAccessDetailPage Component
 *
 * Workspace-scoped detail page for a Security Access (Zero Trust Tunnel)
 * instance. Six tabs; three active in P1 (Overview / Configuration /
 * Application) and three placeholders (Access / Email / Domains).
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - saId (number): Security Access ID to display
 *   - tab (string, optional): Active tab key (defaults to "overview")
 */
export class SecurityAccessDetailPage extends Component {
  static template = "woow_paas_platform.SecurityAccessDetailPage";
  static components = {
    WoowCard, WoowButton, WoowIcon, StatusBadge, SkipTrialModal,
  };
  static props = {
    workspaceId: { type: Number },
    saId: { type: Number },
    tab: { type: String, optional: true },
  };

  setup() {
    this.tabs = TABS;
    this.router = useState(router);
    this.state = useState({
      sa: null,
      loading: true,
      error: null,
      activeTab: this.props.tab || "overview",
      editingName: false,
      nameDraft: "",
      // Application tab — add route form
      newRoutePrefix: "",
      newRouteServiceUrl: "",
      prefixCheck: null, // null | {available, reason}
      prefixChecking: false,
      showSkipTrialModal: false,
      showDeleteConfirm: false,
    });
    this.pollingInterval = null;
    this.prefixCheckTimer = null;
    onWillStart(() => this.loadDetail());
    onWillUnmount(() => {
      this.stopPolling();
      clearTimeout(this.prefixCheckTimer);
    });
  }

  async loadDetail() {
    const res = await securityAccessService.fetchDetail(
      this.props.workspaceId, this.props.saId);
    this.state.loading = false;
    if (res.success) {
      this.state.sa = res.data;
      this.startPollingIfNeeded();
    } else {
      this.state.error = res.error;
    }
  }

  // ---- polling（鏡像 SmartHomeDetailPage） ----
  startPollingIfNeeded() {
    const pendingStates = ["pending", "provisioning", "deleting"];
    if (this.state.sa && pendingStates.includes(this.state.sa.state)) {
      this.stopPolling();
      this.pollingInterval = setInterval(() => this.loadDetail(), 5000);
    } else {
      this.stopPolling();
    }
  }

  stopPolling() {
    if (this.pollingInterval) {
      clearInterval(this.pollingInterval);
      this.pollingInterval = null;
    }
  }

  // ---- tabs（tab 為路徑段，對齊 cloud service 規則） ----
  switchTab(tabKey) {
    this.state.activeTab = tabKey;
    this.router.navigate(
      `workspace/${this.props.workspaceId}` +
      `/security-access/${this.props.saId}/${tabKey}`);
  }

  // ---- header actions ----
  startEditName() {
    this.state.nameDraft = this.state.sa.name;
    this.state.editingName = true;
  }

  async saveName() {
    const res = await securityAccessService.update(
      this.props.workspaceId, this.props.saId,
      { name: this.state.nameDraft.trim() });
    if (res.success) {
      this.state.sa = res.data;
      this.state.editingName = false;
    } else {
      this.state.error = res.error;
    }
  }

  async deleteSa() {
    const res = await securityAccessService.remove(
      this.props.workspaceId, this.props.saId);
    if (res.success) {
      this.router.navigate(`workspace/${this.props.workspaceId}`);
    } else {
      this.state.error = res.error;
      this.state.showDeleteConfirm = false;
    }
  }

  async provision() {
    const res = await securityAccessService.provision(
      this.props.workspaceId, this.props.saId);
    if (res.success) {
      this.state.sa = res.data;
      this.startPollingIfNeeded();
    } else {
      this.state.error = res.error;
    }
  }

  copyToken() {
    // tunnel token 只給 system group 看（field-mask），前端顯示
    // Configuration tab 的接入教學以 primary_hostname 與 docker 指令為主；
    // token 由 HA component / 後台管道取得。此按鈕複製 hostname。
    navigator.clipboard.writeText(this.state.sa.primary_hostname);
  }

  // ---- Application tab：routes ----
  onPrefixInput(ev) {
    this.state.newRoutePrefix = ev.target.value.trim().toLowerCase();
    this.state.prefixCheck = null;
    clearTimeout(this.prefixCheckTimer);
    if (this.state.newRoutePrefix.length >= 3) {
      // debounce 500ms — 即時 availability 反饋（spec：設定前可反饋）
      this.prefixCheckTimer = setTimeout(() => this.checkPrefix(), 500);
    }
  }

  async checkPrefix() {
    this.state.prefixChecking = true;
    const res = await securityAccessService.checkRoutePrefix(
      this.state.newRoutePrefix);
    this.state.prefixChecking = false;
    if (res.success) {
      this.state.prefixCheck = res.data;
    }
  }

  async addRoute() {
    if (!this.state.newRoutePrefix) {
      return;
    }
    const vals = { subdomain_prefix: this.state.newRoutePrefix };
    if (this.state.newRouteServiceUrl) {
      vals.service_url = this.state.newRouteServiceUrl;
    }
    const res = await securityAccessService.createRoute(
      this.props.workspaceId, this.props.saId, vals);
    if (res.success) {
      this.state.newRoutePrefix = "";
      this.state.newRouteServiceUrl = "";
      this.state.prefixCheck = null;
      await this.loadDetail();
    } else {
      this.state.error = res.error;
    }
  }

  async removeRoute(route) {
    const res = await securityAccessService.deleteRoute(
      this.props.workspaceId, this.props.saId, route.id);
    if (res.success) {
      await this.loadDetail();
    } else {
      this.state.error = res.error;
    }
  }

  // ---- SkipTrialModal 接線（鏡像 SmartHomeDetailPage） ----
  loadSkipTrialEstimate = () => {
    return securityAccessService.getSkipTrialEstimate(
      this.props.workspaceId, this.props.saId);
  };

  confirmSkipTrial = () => {
    return securityAccessService.skipTrial(
      this.props.workspaceId, this.props.saId);
  };

  onTrialSkipped = () => {
    this.state.showSkipTrialModal = false;
    this.loadDetail();
  };
}

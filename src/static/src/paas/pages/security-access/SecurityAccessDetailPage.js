/** @odoo-module **/

import { Component, useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { StatusBadge } from "../../components/common/StatusBadge";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { AddPolicyModal } from "../../components/modal/AddPolicyModal";
import { securityAccessService } from "../../services/security_access";
import { router } from "../../core/router";

const TABS = [
  { key: "overview", label: "Overview", icon: "info" },
  { key: "configuration", label: "Configuration", icon: "settings" },
  { key: "application", label: "Application", icon: "route" },
  { key: "access", label: "Access", icon: "shield" },
  { key: "email", label: "Email", icon: "mail", comingSoon: true },
  { key: "domains", label: "Domains", icon: "language", comingSoon: true },
];

// Cloudflare-internal IdP type strings (as persisted in
// access_unresolved_idp_types by the operator / _build_access_payload) →
// the SAME user-facing labels the login-method toggles use (XML li_google /
// li_otp). The fail-open widening warning must name the methods the user
// actually selected by those labels; surfacing the raw 'onetimepin' (a string
// the user never picked by that name) weakens the warning's actionability.
const IDP_TYPE_LABELS = {
  onetimepin: "Email OTP",
  google: "Google",
};

// Subdomain prefix = ONE DNS label. MUST mirror the backend _PREFIX_RE
// (models/security_access.py) + _subdomain_prefix_error exactly: 3-63 chars of
// lowercase letters, digits and hyphens only — NO dots/symbols/uppercase, and
// no leading/trailing hyphen. Front-end check gives immediate feedback; the
// model constraint + availability endpoint enforce the same rule server-side.
const SUBDOMAIN_RE = /^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/;
const SUBDOMAIN_HINT =
  "Use lowercase letters, digits and hyphens only (no dots or symbols); " +
  "3-63 chars, no leading/trailing hyphen.";

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
    AddPolicyModal,
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
      // Access tab (P2.5 per-route) — policy modal + which route it targets,
      // and which protected route cards are expanded (keyed by route id).
      showPolicyModal: false,
      editingPolicy: null,
      policyModalRoute: null,
      expandedRoutes: {},
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

  onNameKeydown(ev) {
    // OWL has no .enter event modifier (that's Vue syntax) — key check
    // lives in the handler. Blur delegates the save to t-on-blur=saveName
    // so there is exactly one save path.
    if (ev.key === "Enter") {
      ev.target.blur();
    }
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

  // ---- Application tab：routes ----
  // 平台 domain 後綴；後端 to_dict 不暴露 domain，從既有 route hostname 推回，
  // 無 route 時 fallback 平台預設（與後端 _get_paas_domain 一致）。
  get paasDomainSuffix() {
    const routes = this.state.sa && this.state.sa.routes;
    if (routes && routes.length && routes[0].hostname) {
      const dot = routes[0].hostname.indexOf(".");
      if (dot > -1) {
        return routes[0].hostname.slice(dot + 1);
      }
    }
    return "woowtech.io";
  }

  onPrefixInput(ev) {
    this.state.newRoutePrefix = ev.target.value.trim().toLowerCase();
    this.state.prefixCheck = null;
    clearTimeout(this.prefixCheckTimer);
    const prefix = this.state.newRoutePrefix;
    if (prefix.length < 3) {
      return;
    }
    // Format gate BEFORE the availability call (mirrors the server fail-fast):
    // a bad-format prefix (dots, symbols, uppercase, leading/trailing hyphen)
    // gets immediate "not available" feedback instead of a green tick that the
    // create call would then reject.
    if (!SUBDOMAIN_RE.test(prefix)) {
      this.state.prefixCheck = { available: false, reason: SUBDOMAIN_HINT };
      return;
    }
    // debounce 500ms — 即時 availability 反饋（spec：設定前可反饋）
    this.prefixCheckTimer = setTimeout(() => this.checkPrefix(), 500);
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
    // Block submit on bad format (defense in depth with onPrefixInput + the
    // server constraint) so a forced click can't bypass the front-end gate.
    if (!SUBDOMAIN_RE.test(this.state.newRoutePrefix)) {
      this.state.prefixCheck = { available: false, reason: SUBDOMAIN_HINT };
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

  // ---- Access tab (P2.5)：per-route Protected Applications ----
  // Each Application Route is its own Cloudflare Access app, so protection,
  // login methods, session and policies are configured per route. A protected
  // route's card expands to its own settings.
  //
  // Map a route's persisted comma-joined CF type string to user-facing labels
  // so the fail-open widening warning names the SAME methods the toggles do
  // (e.g. 'onetimepin' → 'Email OTP'). Unknown types pass through verbatim
  // rather than being dropped, so a future CF type still surfaces (unmapped).
  // 卡頭副標 "N of M routes protected" 的 N。
  get protectedRouteCount() {
    const routes = (this.state.sa && this.state.sa.routes) || [];
    return routes.filter((r) => r.is_protected).length;
  }

  unresolvedIdpLabels(route) {
    const raw = route && route.access_unresolved_idp_types;
    if (!raw) {
      return "";
    }
    return raw
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t)
      .map((t) => IDP_TYPE_LABELS[t] || t)
      .join(", ");
  }

  // Per-route Access config write → route-pinned BFF endpoint. Reloads the SA
  // so every route card reflects the new state (and the operator's server-owned
  // signals like access_unresolved_idp_types).
  async saveRouteAccess(route, vals) {
    const res = await securityAccessService.updateRouteAccess(
      this.props.workspaceId, this.props.saId, route.id, vals);
    if (res.success) {
      await this.loadDetail();
    } else {
      this.state.error = res.error;
    }
  }

  // A protected route's settings panel is expanded by default; the user may
  // collapse it. expandedRoutes[id] === false means explicitly collapsed;
  // undefined/true → expanded (only matters when the route is protected).
  isRouteExpanded(route) {
    return this.state.expandedRoutes[route.id] !== false;
  }

  toggleRouteExpand(route) {
    this.state.expandedRoutes[route.id] = !this.isRouteExpanded(route);
  }

  onRouteProtectedToggle(route, ev) {
    // Expand the card when a route is freshly protected so its settings are
    // immediately visible.
    if (ev.target.checked) {
      this.state.expandedRoutes[route.id] = true;
    }
    this.saveRouteAccess(route, { is_protected: ev.target.checked });
  }

  // Login methods 以 toggle pill 呈現；pill 點擊讀目前 route[method] 取反。
  toggleRouteLogin(route, method) {
    this.saveRouteAccess(route, { [method]: !route[method] });
  }

  onRouteSessionChange(route, ev) {
    this.saveRouteAccess(route, { session_duration: ev.target.value });
  }

  openAddPolicy(route) {
    this.state.policyModalRoute = route;
    this.state.editingPolicy = null;
    this.state.showPolicyModal = true;
  }

  openEditPolicy(route, policy) {
    this.state.policyModalRoute = route;
    this.state.editingPolicy = policy;
    this.state.showPolicyModal = true;
  }

  async onPolicySaved() {
    this.state.showPolicyModal = false;
    this.state.policyModalRoute = null;
    await this.loadDetail();
  }

  async deletePolicy(route, policy) {
    // Policy delete is by policy id (route is passed only for call-site
    // symmetry / future per-route disambiguation; the backend resolves the
    // policy under the gated SA).
    const res = await securityAccessService.deletePolicy(
      this.props.workspaceId, this.props.saId, policy.id);
    if (res.success) {
      await this.loadDetail();
    } else {
      this.state.error = res.error;
    }
  }

  // ---- P-rule: decision badge/mark, rule summary, drag-to-reorder ---- //
  // Cloudflare evaluates Bypass/Service-Auth ahead of Allow/Block regardless of
  // list position; the flat draggable list mirrors CF's own policy UI (the
  // tiering is CF's, not re-implemented here).

  policyBadgeClass(pol) {
    if (pol.decision === "deny") return "o_woow_badge o_woow_badge_gray";
    if (pol.decision === "bypass") return "o_woow_badge o_woow_badge_warning";
    return "o_woow_badge o_woow_badge_green";
  }

  policyBadgeLabel(pol) {
    if (pol.decision === "deny") return "Block";
    if (pol.decision === "bypass") return "Bypass";
    return "Allow";
  }

  policyMarkClass(pol) {
    const base = "o_woow_sa_policy__mark ";
    if (pol.decision === "deny") return base + "o_woow_sa_policy__mark--deny";
    if (pol.decision === "bypass") return base + "o_woow_sa_policy__mark--bypass";
    return base + "o_woow_sa_policy__mark--allow";
  }

  policyMarkIcon(pol) {
    if (pol.decision === "deny") return "block";
    if (pol.decision === "bypass") return "bolt";
    return "check";
  }

  policySummary(pol) {
    const fmt = (r) => (r.rule_type === "everyone" ? "everyone" : r.rule_value);
    const inc = (pol.include || []).map(fmt).join(", ");
    const extra = [];
    if ((pol.require || []).length) extra.push(`+${pol.require.length} require`);
    if ((pol.exclude || []).length) extra.push(`-${pol.exclude.length} exclude`);
    return extra.length ? `${inc} · ${extra.join(" · ")}` : inc;
  }

  onPolicyDragStart(route, pol, ev) {
    this._dragRouteId = route.id;
    this._dragPolId = pol.id;
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = "move";
    }
  }

  onPolicyDragOver(ev) {
    ev.preventDefault(); // required to allow a drop
    if (ev.dataTransfer) {
      ev.dataTransfer.dropEffect = "move";
    }
  }

  async onPolicyDrop(route, targetPol, ev) {
    ev.preventDefault();
    const dragId = this._dragPolId;
    this._dragPolId = null;
    // Reorder only within the same route, and never onto itself.
    if (this._dragRouteId !== route.id || !dragId || dragId === targetPol.id) {
      return;
    }
    const ids = route.access_policies.map((p) => p.id);
    const from = ids.indexOf(dragId);
    const to = ids.indexOf(targetPol.id);
    if (from < 0 || to < 0) {
      return;
    }
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    const res = await securityAccessService.reorderPolicies(
      this.props.workspaceId, this.props.saId, ids);
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

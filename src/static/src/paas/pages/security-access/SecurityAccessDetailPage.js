/** @odoo-module **/

import { Component, useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { StatusBadge } from "../../components/common/StatusBadge";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { ChangePlanModal } from "../../components/modal/ChangePlanModal";
import { AddPolicyModal } from "../../components/modal/AddPolicyModal";
import { AccessAppCard } from "../../components/access/AccessAppCard";
import { securityAccessService } from "../../services/security_access";
import { router } from "../../core/router";

const TABS = [
  { key: "overview", label: _t("Overview"), icon: "info" },
  { key: "configuration", label: _t("Configuration"), icon: "settings" },
  { key: "application", label: _t("Application"), icon: "route" },
  { key: "access", label: _t("Access"), icon: "shield" },
  { key: "email", label: _t("Email"), icon: "mail", comingSoon: true },
  { key: "domains", label: _t("Domains"), icon: "language", comingSoon: true },
];

// Subdomain prefix = ONE DNS label. MUST mirror the backend _PREFIX_RE
// (models/security_access.py) + _subdomain_prefix_error exactly: 3-63 chars of
// lowercase letters, digits and hyphens only — NO dots/symbols/uppercase, and
// no leading/trailing hyphen. Front-end check gives immediate feedback; the
// model constraint + availability endpoint enforce the same rule server-side.
const SUBDOMAIN_RE = /^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/;
const SUBDOMAIN_HINT = _t("Use lowercase letters, digits and hyphens only (no dots or symbols); 3-63 chars, no leading/trailing hyphen.");

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
    ChangePlanModal, AddPolicyModal, AccessAppCard,
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
      // #1196 第一層：就地編輯既有 route 的後端位址。沒有這個，改一個打錯的
      // service_url 只能刪掉重建 —— 那會放掉 subdomain、斷線、換掉 route id。
      editingRouteId: null,
      editRoutePrefix: "",
      editRouteServiceUrl: "",
      editRouteHostHeader: true,
      savingRoute: false,
      // #1196 第二層：tunnel token 自助複製。預設遮蔽——它等同密碼，而這一頁
      // 常常是開著給人看的。
      tunnelToken: null,
      tunnelTokenVisible: false,
      tunnelTokenLoading: false,
      tunnelTokenCopied: false,
      showSkipTrialModal: false,
      showChangePlanModal: false,
      showDeleteConfirm: false,
      // Access tab (P2.5 per-route) — policy modal + which route it targets,
      // and which protected route cards are expanded (keyed by route id).
      showPolicyModal: false,
      editingPolicy: null,
      policyModalRoute: null,
      expandedRoutes: {},
      // P-rule drag-to-reorder UX: the route whose order is being persisted
      // (saving spinner — the reorder API re-syncs Cloudflare so it is not
      // instant). 拖曳中的那一列由 AccessAppCard 自己記（純 UI 狀態）。
      reorderingRouteId: null,
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

  // ---- #1196 第一層：就地編輯 route ----
  // subdomain 仍然不可改（平台是先到先得的扁平命名空間，改名＝刪除+重建），
  // 這裡只開後端已經接受的 service_url / http_host_header。

  startEditRoute(route) {
    this.state.editingRouteId = route.id;
    this.state.editRoutePrefix = route.subdomain_prefix || "";
    this.state.editRouteServiceUrl = route.service_url || "";
    this.state.editRouteHostHeader = route.http_host_header !== false;
  }

  cancelEditRoute() {
    this.state.editingRouteId = null;
    this.state.editRoutePrefix = "";
    this.state.editRouteServiceUrl = "";
    this.state.savingRoute = false;
  }

  // #1196：改名。與新增那一格同樣的正規化（小寫、去空白），使用者貼上帶大寫的
  // 值時不會被格式檢查擋下來卻不知道為什麼。
  onEditRoutePrefix(ev) {
    this.state.editRoutePrefix = ev.target.value.trim().toLowerCase();
  }

  onEditRouteServiceUrl(ev) {
    this.state.editRouteServiceUrl = ev.target.value.trim();
  }

  onEditRouteHostHeader(ev) {
    this.state.editRouteHostHeader = ev.target.checked;
  }

  async saveRoute(route) {
    if (!this.state.editRouteServiceUrl) {
      return;
    }
    const prefix = this.state.editRoutePrefix;
    // 與新增那一格同一道前端閘（SUBDOMAIN_RE），純粹是為了即時回饋；真正的
    // 判準在後端（格式 + DB + Cloudflare 三層），前端擋不住也不會放行錯的值。
    if (prefix && prefix !== route.subdomain_prefix
        && !SUBDOMAIN_RE.test(prefix)) {
      this.state.error = SUBDOMAIN_HINT;
      return;
    }
    this.state.savingRoute = true;
    const vals = {
      service_url: this.state.editRouteServiceUrl,
      http_host_header: this.state.editRouteHostHeader,
    };
    // 只有真的改了才送——沒改就不要讓後端跑一次 Cloudflare 可用性查詢
    // （那會對自己現有的 DNS 記錄回「已被佔用」）。
    if (prefix && prefix !== route.subdomain_prefix) {
      vals.subdomain_prefix = prefix;
    }
    const res = await securityAccessService.updateRoute(
      this.props.workspaceId, this.props.saId, route.id, vals,
    );
    this.state.savingRoute = false;
    if (res.success) {
      this.cancelEditRoute();
      await this.loadDetail();
    } else {
      this.state.error = res.error;
    }
  }

  // ---- #1196 第二層：tunnel token 自助取用 ----
  // tunnel 可任意轉移是刻意的定位，而「複製得到 token」是轉移的前提。

  async toggleTunnelToken() {
    if (this.state.tunnelTokenVisible) {
      this.state.tunnelTokenVisible = false;
      return;
    }
    if (!this.state.tunnelToken) {
      this.state.tunnelTokenLoading = true;
      const res = await securityAccessService.getTunnelToken(
        this.props.workspaceId, this.props.saId);
      this.state.tunnelTokenLoading = false;
      if (!res.success) {
        this.state.error = res.error;
        return;
      }
      this.state.tunnelToken = res.data.tunnel_token;
    }
    this.state.tunnelTokenVisible = true;
  }

  async copyTunnelToken() {
    if (!this.state.tunnelToken) {
      const res = await securityAccessService.getTunnelToken(
        this.props.workspaceId, this.props.saId);
      if (!res.success) {
        this.state.error = res.error;
        return;
      }
      this.state.tunnelToken = res.data.tunnel_token;
    }
    try {
      await navigator.clipboard.writeText(this.state.tunnelToken);
      this.state.tunnelTokenCopied = true;
      // 只是給個「有複製到」的回饋，兩秒後自己收掉。
      setTimeout(() => { this.state.tunnelTokenCopied = false; }, 2000);
    } catch (err) {
      // 非 HTTPS／權限被拒時 clipboard API 會擋；此時至少把值顯示出來讓人手動選取。
      this.state.tunnelTokenVisible = true;
      this.state.error = err.message;
    }
  }

  // 遮蔽顯示：頭尾各留幾碼方便比對是不是同一組，中間不出現。
  get maskedTunnelToken() {
    const t = this.state.tunnelToken;
    if (!t) {
      return "••••••••••••••••••••••••";
    }
    return t.length <= 16 ? "••••••••" : `${t.slice(0, 6)}${"•".repeat(24)}${t.slice(-4)}`;
  }

  // docker 那條指令要能整段複製貼上就能跑。
  get dockerRunCommand() {
    const t = this.state.tunnelToken || "<TUNNEL_TOKEN>";
    return `docker run -d --restart unless-stopped --network host \\\n  cloudflare/cloudflared:latest tunnel --no-autoupdate run --token ${t}`;
  }

  // ---- Access tab (P2.5)：per-route Protected Applications ----
  // Each Application Route is its own Cloudflare Access app, so protection,
  // login methods, session and policies are configured per route. A protected
  // route's card expands to its own settings.
  //
  // #746：卡片本體（版面＋互動）抽成 components/access/AccessAppCard，與 Cloud
  // Service 的 Access 頁籤共用。留在這裡的是**SA 專屬的部分**：per-route 的展開
  // 狀態、route-pinned 的 API 呼叫，以及卡頭那句 "N of M routes protected"。
  //
  // 卡頭副標 "N of M routes protected" 的 N。
  get protectedRouteCount() {
    const routes = (this.state.sa && this.state.sa.routes) || [];
    return routes.filter((r) => r.is_protected).length;
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

  onRouteProtectedToggle(route, checked) {
    // Expand the card when a route is freshly protected so its settings are
    // immediately visible.
    if (checked) {
      this.state.expandedRoutes[route.id] = true;
    }
    this.saveRouteAccess(route, { is_protected: checked });
  }

  // Login methods 以 toggle pill 呈現；pill 點擊讀目前 route[method] 取反。
  toggleRouteLogin(route, method) {
    this.saveRouteAccess(route, { [method]: !route[method] });
  }

  onRouteSessionChange(route, value) {
    this.saveRouteAccess(route, { session_duration: value });
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

  // ---- AddPolicyModal 接線（#746：modal 不再自己 import service） ----
  // 建立 vs 編輯由 modal 依 `policy` prop 判定並把 policyId 明給回來——這裡不
  // 從 state.editingPolicy 再推一次（同一件事判兩次，遲早漂移）。
  savePolicy = (vals, policyId) => {
    if (policyId) {
      return securityAccessService.updatePolicy(
        this.props.workspaceId, this.props.saId, policyId, vals);
    }
    return securityAccessService.createPolicy(
      this.props.workspaceId, this.props.saId,
      this.state.policyModalRoute.id, vals);
  };

  // ---- P-rule：precedence 寫入 ---- //
  // 拖曳的互動（即時預覽、原地放下不寫、取消還原）在 AccessAppCard 裡；這裡只
  // 負責把卡片算好的最終順序寫回 route-pinned 端點，並持有 saving 狀態。
  async commitPolicyOrder(route, ids) {
    // Saving feedback: the reorder endpoint re-syncs Cloudflare Access, so it
    // takes a moment — keep the (already reflowed) list visible but mark it busy.
    this.state.reorderingRouteId = route.id;
    const res = await securityAccessService.reorderPolicies(
      this.props.workspaceId, this.props.saId, ids);
    this.state.reorderingRouteId = null;
    if (!res.success) {
      this.state.error = res.error;
    }
    // 成功：確認伺服器順序＋更新後的 CF id；失敗：把樂觀順序還原。兩條都重載。
    await this.loadDetail();
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

  // ---- ChangePlanModal 接線（改方案／改席數） ----
  // SA to_dict 不吐 template_id，故循 CreateSecurityAccessModal 路徑先取唯一
  // SA template（本 surface 僅一個）再抓 plans；plans 已含 C1 席數變體。
  loadSaPlans = async () => {
    const t = await securityAccessService.fetchTemplates();
    if (!t.success || !t.data.length) {
      return {
        success: false,
        error: t.error || _t("No Security Access template available"),
      };
    }
    return securityAccessService.fetchPlans(t.data[0].id);
  };

  confirmSaChangePlan = (planId) => {
    return securityAccessService.changePlan(
      this.props.workspaceId, this.props.saId, planId);
  };

  onPlanChanged = () => {
    this.state.showChangePlanModal = false;
    this.loadDetail();
  };
}

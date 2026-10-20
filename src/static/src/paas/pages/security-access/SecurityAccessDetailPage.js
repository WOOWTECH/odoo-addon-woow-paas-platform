/** @odoo-module **/

import { Component, useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { BrandIcon } from "../../components/brand-icon/BrandIcon";
import { SECURITY_ACCESS_BRAND } from "../../utils/brand_icons";
import { StatusBadge } from "../../components/common/StatusBadge";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { ChangePlanModal } from "../../components/modal/ChangePlanModal";
import { AddPolicyModal } from "../../components/modal/AddPolicyModal";
import { AccessAppCard } from "../../components/access/AccessAppCard";
import { securityAccessService } from "../../services/security_access";
import { router } from "../../core/router";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { workspaceService } from "../../services/workspace_service";
import { formatUserDate, formatUserDateTime } from "../../utils/datetime_helpers";

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
// #1234：route 健康狀態的顯示文字。狀態語意見 services/route_probe.py 的 STATUSES。
// #1261：三張表原本是裸字串，不會被翻譯（187 正式站實測中文介面仍顯示
// 「Reachable (200)」「Blocked by Access」）——一律包 _t。
const ROUTE_HEALTH_LABELS = {
  ok: _t("Reachable"),
  access_login: _t("Login required"),
  access_missing: _t("Not protected"),
  access_unexpected: _t("Blocked by Access"),
  tunnel_down: _t("Connector offline"),
  not_routed: _t("Not live yet"),
  dns_missing: _t("DNS not found"),
  origin_error: _t("Origin error"),
  proxy_no_site: _t("Proxy has no site"),
  timeout: _t("No response"),
  unknown: _t("Unknown"),
};
const ROUTE_HEALTH_HINTS = {
  ok: _t("Requests pass through Cloudflare and the tunnel, and your service answers."),
  access_login: _t("Protected: visitors see the Cloudflare Access login first. This is expected."),
  access_missing: _t("This route is set to protected, but no login page is in front of it. Toggle protection off and on in the Access tab, or contact support."),
  access_unexpected: _t("This route is public here, but a Cloudflare Access application that this platform does not manage is blocking it. Contact support."),
  tunnel_down: _t("No connector is running this tunnel. Check the machine that runs it, or start a connector with the tunnel token."),
  not_routed: _t("The hostname does not point to this tunnel yet. Right after creating or renaming a route this can take up to a minute — check again shortly."),
  dns_missing: _t("Public DNS has no record for this hostname yet. Wait a minute and check again."),
  origin_error: _t("The request reached your machine, but your reverse proxy or service returned an error. Check that the proxy has this hostname and the service is running."),
  proxy_no_site: _t("The request reached your machine, but your reverse proxy answered with its default page — it has no site for this hostname. Add this hostname in your proxy (for example Nginx Proxy Manager). This is expected right after renaming a route."),
  timeout: _t("Your service did not answer in time. Check that it is running and not overloaded."),
  unknown: _t("The check could not complete. Try again in a moment."),
};
// #1234：流量拿不到時的說明（reason 由後端 _route_traffic 給）。
const TRAFFIC_UNAVAILABLE_HINTS = {
  permission: _t("Traffic data is not available yet: the platform's Cloudflare token lacks analytics access."),
  operator_outdated: _t("Traffic data is not available yet: the platform service needs an update."),
  disabled: _t("Traffic data is not available on this platform."),
  error: _t("Traffic data could not be loaded. Try again later."),
};

// 「3 min ago」式的相對時間。刻意不用 Intl／toLocale*（見 locale guard #107）。
function relativeTime(iso) {
  if (!iso) {
    return "";
  }
  const minutes = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
  if (minutes < 1) {
    return _t("just now");
  }
  if (minutes < 60) {
    return _t("%s min ago", minutes);
  }
  const hours = Math.round(minutes / 60);
  return hours < 48 ? _t("%s h ago", hours) : _t("%s d ago", Math.round(hours / 24));
}

// 黃色（使用者多半只要等一下）vs 紅色（要動手處理）。
const WARN_HEALTH_STATUSES = ["not_routed", "dns_missing", "timeout", "unknown"];
const SUBDOMAIN_HINT = _t("Use lowercase letters, digits and hyphens only (no dots or symbols); 3-63 chars, no leading/trailing hyphen.");

// #1261：開啟 route 保護前的「明示後果」。CS（`pages/service/tabs/AccessTab.js`）
// 與 SH（`pages/smart-home/tabs/SmartHomeAccessTab.js`）都有傳 protectConfirm 給
// AccessAppCard，只有 SA 漏了——2026-09-29 正式站 E2E 實測：勾下去就直接生效，
// 沒有任何提示。SA 的 route 背後常常是 API／webhook／腳本／手機 App 直連這類
// **不會跳出瀏覽器登入頁**的用戶端（它們是 SA 最典型的用途之一），開保護等於
// 直接把它們擋在外面；這句必須在按下去之前出現，不是事後看流量歸零才發現。
// 語境是「這條 route 的網址」（SA 是 per-route 多錨點，保護的單位是一條 route），
// 不是 CS 的「這個服務」。標題沿用既有 msgid（已有中文），確認鍵用 SH 那條
// "Turn protection on"（同樣已有中文），不新增同義 msgid。
const PROTECT_CONFIRM = {
  title: _t("Turn on Access protection?"),
  message: _t(
    "Once protection is on, every visitor to this route's address has to pass Access verification first, including you. Only identities matching an Access policy below get in; clients that cannot show a browser login page (API clients, webhooks, scripts or apps connecting directly) will be blocked."
  ),
  confirmLabel: _t("Turn protection on"),
};

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
    WoowCard, WoowButton, WoowIcon, BrandIcon, StatusBadge, SkipTrialModal,
    ChangePlanModal, AddPolicyModal, AccessAppCard,
  };

  brandKey = SECURITY_ACCESS_BRAND;
  static props = {
    workspaceId: { type: Number },
    saId: { type: Number },
    tab: { type: String, optional: true },
  };

  setup() {
    // #1310：Email／Domains 尚未實作，點進去只有「Coming Soon」卡，頁籤上的圓點
    // 還像未讀標記。實作前不顯示這兩個頁籤（深連結仍落到原本的 Coming Soon 卡）。
    this.tabs = TABS.filter((tab) => !tab.comingSoon);
    // #1261：Access 分頁每張 AccessAppCard 共用同一份開保護確認文案（見檔頭）。
    this.protectConfirm = PROTECT_CONFIRM;
    this.router = useState(router);
    this.state = useState({
      sa: null,
      loading: true,
      // error＝整頁載不出來（取代整頁）；actionError＝某個操作失敗（頁內提示，
      // 可關閉）。以前兩者共用 error，任何操作失敗（例如 route 額度已滿）都會把
      // 整頁換成一行錯誤、而且之後不會自己恢復——#1234 實測時撞到。
      error: null,
      actionError: null,
      // #1234：route 健康探測。key 是 route id，值見 services/route_probe.py。
      routeHealth: {},
      healthLoading: false,
      // #1234：route 流量（null＝還沒問過）。
      routeTraffic: null,
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
      // 刪除 route 前的確認：待刪的那條 route（null＝沒在問）。刪 route 會立刻
      // 拿掉它在 Cloudflare 上的 DNS／ingress 與 Access app，舊網址當場失效、
      // subdomain 被放掉，沒有復原鍵——以前按一下垃圾桶就直接送出。
      confirmDeleteRoute: null,
      deletingRoute: false,
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
    onWillStart(async () => {
      await this.loadDetail();
      if (this.state.activeTab === "application") {
        this.loadRouteHealth();
      }
    });
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
      this._publishBreadcrumb();
      this.startPollingIfNeeded();
    } else {
      this.state.error = res.error;
    }
  }

  /**
   * #1310：此頁原本沒發布麵包屑，header 只剩「Home > Security Access Detail」，
   * 桌機手機都回不到所屬工作區。比照 SmartHomeDetailPage 的軌跡。
   */
  _publishBreadcrumb() {
    const ws = workspaceService.workspaces.find(w => w.id === this.props.workspaceId);
    breadcrumbService.set("security-access-detail", [
      { label: _t("Workspaces"), path: "workspaces" },
      { label: ws?.name || _t("Workspace"), path: `workspace/${this.props.workspaceId}` },
      { label: this.state.sa?.name || _t("Security Access"), path: null },
    ]);
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

  dismissActionError() {
    this.state.actionError = null;
  }

  // ---- #1234：route 健康狀態 ----
  // 從公開網路探每條 route，把「打不開」拆成使用者能行動的幾種原因。探測在
  // 後端做（同一個 SA 多人同看有快取），這裡只負責何時問、怎麼顯示。

  async loadRouteHealth(force = false) {
    if (!this.state.sa || !(this.state.sa.routes || []).length) {
      this.state.routeHealth = {};
      return;
    }
    this.state.healthLoading = true;
    const res = await securityAccessService.fetchRouteHealth(
      this.props.workspaceId, this.props.saId, force);
    this.state.healthLoading = false;
    if (res.success) {
      this.state.routeHealth = res.data || {};
    }
    // 探測失敗不打擾使用者：狀態欄維持「—」即可，不值得一個錯誤提示。
    this.loadRouteTraffic();
  }

  async loadRouteTraffic() {
    const res = await securityAccessService.fetchRouteTraffic(
      this.props.workspaceId, this.props.saId);
    this.state.routeTraffic = res.success ? res.data : { available: false, reason: "error" };
  }

  routeTrafficLabel(route) {
    const t = this.state.routeTraffic;
    if (!t || !t.available) {
      return "—";
    }
    const r = (t.routes || {})[route.id];
    if (!r || !r.requests) {
      return _t("No traffic");
    }
    return `${r.requests} · ${relativeTime(r.last_request_at)}`;
  }

  routeTrafficHint(route) {
    const t = this.state.routeTraffic;
    if (!t) {
      return "";
    }
    if (!t.available) {
      return TRAFFIC_UNAVAILABLE_HINTS[t.reason] || TRAFFIC_UNAVAILABLE_HINTS.error;
    }
    const r = (t.routes || {})[route.id];
    if (!r || !r.requests) {
      return _t("No requests reached this hostname in the last %s hours.", t.window_hours || 24);
    }
    return _t(
      "Last %(hours)s h: %(ok)s ok (2xx), %(redirect)s redirect, %(client)s client error (4xx), %(server)s server error (5xx).",
      {
        hours: t.window_hours || 24, ok: r.status_2xx, redirect: r.status_3xx,
        client: r.status_4xx, server: r.status_5xx,
      }
    );
  }

  // #1261：Host header 欄原本在樣板裡寫 `'Yes' : 'No'`，樣板運算式裡的字串
  // 不會被抽成翻譯。
  hostHeaderLabel(route) {
    return route.http_host_header ? _t("Yes") : _t("No");
  }

  routeHealthOf(route) {
    return this.state.routeHealth[route.id] || null;
  }

  routeHealthLabel(route) {
    const h = this.routeHealthOf(route);
    if (!h) {
      return this.state.healthLoading ? _t("Checking…") : "—";
    }
    const code = h.http_code ? ` (${h.http_code})` : "";
    return (ROUTE_HEALTH_LABELS[h.status] || ROUTE_HEALTH_LABELS.unknown) +
      (h.status === "ok" || h.status === "origin_error" ? code : "");
  }

  routeHealthHint(route) {
    const h = this.routeHealthOf(route);
    return h ? (ROUTE_HEALTH_HINTS[h.status] || ROUTE_HEALTH_HINTS.unknown) : "";
  }

  routeHealthClass(route) {
    const h = this.routeHealthOf(route);
    if (!h) {
      return "o_woow_sa_health";
    }
    return "o_woow_sa_health o_woow_sa_health--" + (h.healthy ? "ok"
      : (WARN_HEALTH_STATUSES.includes(h.status) ? "warn" : "error"));
  }

  /** #1310：訂閱／建立日期依 Odoo 使用者語系顯示（原本直接吐 ISO 字串）。 */
  formatDate(value) {
    return formatUserDate(value, "-");
  }

  formatDateTime(value) {
    return formatUserDateTime(value, { showSeconds: false }, "-");
  }

  // ---- #1234：route 額度 ----
  get routeCount() {
    return ((this.state.sa && this.state.sa.routes) || []).length;
  }

  get atRouteQuota() {
    const quota = this.state.sa && this.state.sa.route_quota;
    return quota !== null && quota !== undefined && this.routeCount >= quota;
  }

  // ---- tabs（tab 為路徑段，對齊 cloud service 規則） ----
  switchTab(tabKey) {
    this.state.activeTab = tabKey;
    if (tabKey === "application") {
      this.loadRouteHealth();
    }
    this.router.navigate(
      `workspace/${this.props.workspaceId}` +
      `/security-access/${this.props.saId}/${tabKey}`);
  }

  // ---- header actions ----
  startEditName() {
    this.state.nameDraft = this.state.sa.name;
    this.nameEditCancelled = false;
    this.state.editingName = true;
  }

  onNameKeydown(ev) {
    // OWL has no .enter event modifier (that's Vue syntax) — key check
    // lives in the handler. Blur delegates the save to t-on-blur=saveName
    // so there is exactly one save path.
    if (ev.key === "Enter") {
      ev.target.blur();
    } else if (ev.key === "Escape") {
      // #1261：原本只處理 Enter——按 Esc 輸入框還開著，之後任何 blur 都會把
      // 打錯的名稱存進去（2026-09-30 正式站實測）。先立旗標再關編輯：輸入框
      // 被移除時瀏覽器可能仍送出 blur，saveName 看到旗標就不送出。
      this.nameEditCancelled = true;
      this.state.nameDraft = this.state.sa.name;
      this.state.editingName = false;
    }
  }

  async saveName() {
    const name = (this.state.nameDraft || "").trim();
    if (this.nameEditCancelled || !name || name === this.state.sa.name) {
      // 取消、空白或沒改：不打 API，直接收起編輯。
      this.nameEditCancelled = false;
      this.state.editingName = false;
      return;
    }
    const res = await securityAccessService.update(
      this.props.workspaceId, this.props.saId,
      { name });
    if (res.success) {
      this.state.sa = res.data;
      this.state.editingName = false;
    } else {
      this.state.actionError = res.error;
    }
  }

  async deleteSa() {
    const res = await securityAccessService.remove(
      this.props.workspaceId, this.props.saId);
    if (res.success) {
      this.router.navigate(`workspace/${this.props.workspaceId}`);
    } else {
      this.state.actionError = res.error;
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
      this.state.actionError = res.error;
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
      this.loadRouteHealth(true);
    } else {
      this.state.actionError = res.error;
    }
  }

  requestRemoveRoute(route) {
    this.state.confirmDeleteRoute = route;
  }

  cancelRemoveRoute() {
    if (this.state.deletingRoute) {
      return;
    }
    this.state.confirmDeleteRoute = null;
  }

  // 確認視窗的「Delete」。視窗在請求期間**保持開著、按鈕鎖住**，而不是先關再
  // 送（AccessAppCard 刪 policy 那種）：刪 route 要同步 Cloudflare（DNS、
  // ingress、Access app 都要拆），常常要好幾秒；先關的話這段時間畫面毫無動靜，
  // 使用者會以為沒按到、再去點一次垃圾桶。與整個 SA 的刪除確認同一個作法。
  async removeRoute() {
    const route = this.state.confirmDeleteRoute;
    if (!route || this.state.deletingRoute) {
      return;
    }
    this.state.deletingRoute = true;
    try {
      const res = await securityAccessService.deleteRoute(
        this.props.workspaceId, this.props.saId, route.id);
      if (res.success) {
        await this.loadDetail();
      } else {
        this.state.actionError = res.error;
      }
    } finally {
      this.state.deletingRoute = false;
      this.state.confirmDeleteRoute = null;
    }
  }

  // ---- #1196：就地編輯 route（service_url / http_host_header / subdomain 改名） ----

  // 正在改名的那一條：只有編輯中、且 prefix 真的跟現值不同才算。用來在表格下方
  // 提醒改名的後果——舊網址立刻失效、不轉址；後端若依主機名分流要先加新名字。
  // 這些都是平台做不到的事（我們碰不到使用者的 origin），只能在按下儲存前講清楚。
  get pendingRename() {
    const id = this.state.editingRouteId;
    const prefix = this.state.editRoutePrefix;
    if (!id || !prefix) {
      return null;
    }
    const route = (this.state.sa.routes || []).find((r) => r.id === id);
    if (!route || prefix === route.subdomain_prefix) {
      return null;
    }
    return {
      from: route.hostname,
      to: `${prefix}.${this.paasDomainSuffix}`,
    };
  }

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
      this.state.actionError = SUBDOMAIN_HINT;
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
      this.loadRouteHealth(true);
    } else {
      this.state.actionError = res.error;
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
        this.state.actionError = res.error;
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
        this.state.actionError = res.error;
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
      this.state.actionError = err.message;
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
  //
  // #794：`access_protection_lost` 的 route **不算**。那一格的 is_protected 只
  // 剩意圖——Cloudflare 上的 app 已經被拆掉、hostname 免登入就打得開；把它算
  // 進「已保護」正是那個看不出來的謊（卡片本身另有紅色警示）。
  get protectedRouteCount() {
    const routes = (this.state.sa && this.state.sa.routes) || [];
    return routes.filter(
      (r) => r.is_protected && !r.access_protection_lost).length;
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
      this.state.actionError = res.error;
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
    // 確認視窗在 AccessAppCard 裡（三個 surface 共用），走到這裡＝使用者已經
    // 確認過了。
    // Policy delete is by policy id (route is passed only for call-site
    // symmetry / future per-route disambiguation; the backend resolves the
    // policy under the gated SA).
    const res = await securityAccessService.deletePolicy(
      this.props.workspaceId, this.props.saId, policy.id);
    if (res.success) {
      await this.loadDetail();
    } else {
      this.state.actionError = res.error;
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
      this.state.actionError = res.error;
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

  estimateSaChangePlan = (planId) => {
    return securityAccessService.changePlan(
      this.props.workspaceId, this.props.saId, planId, { dryRun: true });
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

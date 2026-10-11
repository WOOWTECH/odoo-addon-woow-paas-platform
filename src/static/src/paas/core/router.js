/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { showProjects } from "./platform_config";
import { captureInvitationFragment, clearInvitationToken, getInvitationRevision } from "./invitation_context";

/** @type {Array<{path: string, name: string, routeName?: string, pattern?: RegExp}>} */
const routes = [
    { path: "dashboard", name: _t("Overview"), routeName: "dashboard" },
    { path: "workspaces", name: _t("Workspaces"), routeName: "workspaces" },
    { path: "workspace-invitation/accept", name: _t("Accept Workspace Invitation"), routeName: "workspace-invitation-accept" },
    { path: "workspace/:id", name: _t("Workspace Detail"), routeName: "workspace-detail", pattern: /^workspace\/(\d+)$/ },
    { path: "workspace/:id/team", name: _t("Workspace Team"), routeName: "workspace-team", pattern: /^workspace\/(\d+)\/team$/ },
    // #768：Smart Home 也長出頁籤（Overview / Access）。與 CS 逐字同型的**雙路由**：
    // 帶 tab 的在前（比對更長的 path，否則不帶 tab 那條會先吃掉
    // `.../smarthome/12/access`），不帶 tab 的留著——既有的深連結／麵包屑／
    // `workspace/:id` 卡片點擊全部都是 `.../smarthome/:homeId`，只加帶 tab 那條
    // 會讓它們**整批 404**。兩條共用同一個 routeName，由 params.tab 決定分頁。
    // 白名單 `(overview|access)`：頁籤自己 setTab 改的 hash 也走這裡，不在白名單
    // 的話換頁後重新整理就掉到 NotFoundPage。
    { path: "workspace/:id/smarthome/:homeId/:tab", name: _t("Smart Home Detail Tab"), routeName: "smarthome-detail", pattern: /^workspace\/(\d+)\/smarthome\/(\d+)\/(overview|access)$/ },
    { path: "workspace/:id/smarthome/:homeId", name: _t("Smart Home Detail"), routeName: "smarthome-detail", pattern: /^workspace\/(\d+)\/smarthome\/(\d+)$/ },
    { path: "workspace/:id/security-access/:saId/:tab", name: _t("Security Access Detail"), routeName: "security-access-detail", pattern: /^workspace\/(\d+)\/security-access\/(\d+)\/(overview|configuration|application|access|email|domains)$/ },
    { path: "workspace/:id/services/marketplace", name: _t("App Marketplace"), routeName: "marketplace", pattern: /^workspace\/(\d+)\/services\/marketplace$/ },
    { path: "workspace/:id/services/configure/:templateId", name: _t("Configure Service"), routeName: "configure", pattern: /^workspace\/(\d+)\/services\/configure\/(\d+)$/ },
    // `access`（#746）：CS Access Control 頁籤。白名單沒有它的話，深連結
    // .../services/:id/access 會掉到 NotFoundPage——頁籤自己 setTab 改的 hash
    // 也一樣，換頁後重新整理就進不去。
    { path: "workspace/:id/services/:serviceId/:tab", name: _t("Service Detail Tab"), routeName: "service-detail", pattern: /^workspace\/(\d+)\/services\/(\d+)\/(overview|configuration|addons|mcp-servers|access)$/ },
    { path: "workspace/:id/services/:serviceId", name: _t("Service Detail"), routeName: "service-detail", pattern: /^workspace\/(\d+)\/services\/(\d+)$/ },
    // D4=C: `/woow#/projects` is the new entry for the Projects list page; the
    //        legacy `/woow#/ai-assistant/projects` route below stays active and
    //        shares the same `ai-projects` routeName so existing bookmarks
    //        continue to resolve to the same component.
    { path: "projects", name: _t("Projects"), routeName: "ai-projects" },
    { path: "projects/:id", name: _t("Project Detail"), routeName: "project-detail", pattern: /^projects\/(\d+)$/ },
    { path: "ai-assistant/projects/:id", name: _t("Project Kanban"), routeName: "ai-project-kanban", pattern: /^ai-assistant\/projects\/(\d+)$/ },
    { path: "ai-assistant/projects", name: _t("Projects"), routeName: "ai-projects" },
    { path: "ai-assistant/tasks/:taskId", name: _t("Task Detail"), routeName: "ai-task-detail", pattern: /^ai-assistant\/tasks\/(\d+)$/ },
    { path: "ai-assistant/tasks", name: _t("Tasks"), routeName: "ai-tasks" },
    { path: "ai-assistant/chat/:conversationId", name: _t("AI Chat"), routeName: "ai-chat", pattern: /^ai-assistant\/chat\/(\d+)$/ },
    { path: "deployments", name: _t("Deployments"), routeName: "deployments" },
    { path: "billing", name: _t("Billing"), routeName: "billing" },
    { path: "billing/topup", name: _t("Top-up Store"), routeName: "billing-topup" },
    { path: "billing/invoices", name: _t("Invoices"), routeName: "billing-invoices" },
    { path: "billing/invoices/:id", name: _t("Invoice Detail"), routeName: "billing-invoice-detail", pattern: /^billing\/invoices\/(\d+)$/ },
    { path: "billing/subscriptions", name: _t("Subscriptions"), routeName: "billing-subscriptions" },
    { path: "billing/transactions", name: _t("Transactions"), routeName: "billing-transactions" },
    { path: "settings", name: _t("Settings"), routeName: "settings" },
    { path: "settings/reconcile-report", name: _t("Orphan Reconciliation Report"), routeName: "reconcile-report" },
    { path: "settings/profile", name: _t("Profile"), routeName: "settings-profile" },
    { path: "settings/payment-methods", name: _t("Payment Methods"), routeName: "settings-payment-methods" },
    { path: "settings/payment-methods/add", name: _t("Add Payment Method"), routeName: "settings-payment-methods-add" },
];

// #275：flag OFF 時這些 routeName 一律 redirect 到 dashboard（cosmetic gate）。
// `projects` 與 `ai-assistant/projects` 共用 `ai-projects` routeName（見 routes 註解）。
const PROJECT_ROUTE_NAMES = new Set([
    "ai-projects", "project-detail", "ai-project-kanban",
    "ai-tasks", "ai-task-detail", "ai-chat",
]);

export const router = reactive({
    current: "dashboard",
    params: {},
    routes,

    _initialized: false,

    init() {
        if (this._initialized) return;
        this._initialized = true;
        this.parseRoute();
        window.addEventListener("hashchange", () => {
            this.parseRoute();
        });
        if (!window.location.hash) {
            window.location.hash = "#/dashboard";
        }
    },

    parseRoute() {
        captureInvitationFragment();
        if (window.location.hash !== "#/workspace-invitation/accept") {
            clearInvitationToken();
        }
        // Strip "#/" and split off query string — routing is path-based, but
        // query params (e.g. ?bind_status=ok) are preserved in this.params.query.
        const raw = window.location.hash.slice(2);
        const qIdx = raw.indexOf("?");
        const path = qIdx === -1 ? raw : raw.slice(0, qIdx);
        const queryStr = qIdx === -1 ? "" : raw.slice(qIdx + 1);

        this.params = {};
        if (path === "workspace-invitation/accept") {
            this.current = "workspace-invitation-accept";
            this.params.invitationRevision = getInvitationRevision();
            return;
        }
        if (queryStr) {
            const usp = new URLSearchParams(queryStr);
            this.params.query = Object.fromEntries(usp.entries());
        }

        // Check for pattern-based routes first (more specific patterns)
        for (const route of routes) {
            if (route.pattern) {
                const match = path.match(route.pattern);
                if (match) {
                    this.current = route.routeName;
                    this.params.id = match[1];
                    // Handle additional params for routes with multiple captures
                    if (match[2]) {
                        if (route.routeName === 'configure') {
                            this.params.templateId = match[2];
                        } else if (route.routeName === 'service-detail') {
                            this.params.serviceId = match[2];
                            if (match[3]) {
                                this.params.tab = match[3];
                            }
                        } else if (route.routeName === 'smarthome-detail') {
                            this.params.homeId = match[2];
                            // 不帶 tab 那條沒有 match[3]，params.tab 因此不存在
                            // ——由頁面自己退守 'overview'（同 service-detail）。
                            if (match[3]) {
                                this.params.tab = match[3];
                            }
                        } else if (route.routeName === 'security-access-detail') {
                            this.params.saId = match[2];
                            if (match[3]) {
                                this.params.tab = match[3];
                            }
                        }
                    }
                    // For AI routes, map the first capture to the correct param name
                    if (route.routeName === 'ai-task-detail') {
                        this.params.taskId = match[1];
                    } else if (route.routeName === 'ai-chat') {
                        this.params.conversationId = match[1];
                    } else if (route.routeName === 'ai-project-kanban') {
                        this.params.id = match[1];
                    }
                    this._applyProjectGate();
                    return;
                }
            }
        }

        // Fall back to exact path match (query-stripped). Unknown paths land
        // on the NotFoundPage rather than silently rendering the dashboard.
        const route = routes.find(r => r.path === path);
        this.current = route ? route.routeName : "not-found";
        this._applyProjectGate();
    },

    _applyProjectGate() {
        // #275：flag OFF 時把 project 路由收斂到 dashboard。同步 gate → 直接
        // hash 載入 /woow#/projects 也不會閃現 project 頁。改 hash 只是同步 URL；
        // this.current 已即時設為 dashboard，hashchange 再跑一次為冪等。
        if (!showProjects() && PROJECT_ROUTE_NAMES.has(this.current)) {
            this.current = "dashboard";
            this.params = {};
            if (window.location.hash !== "#/dashboard") {
                window.location.hash = "#/dashboard";
            }
        }
    },

    getRouteFromHash() {
        return this.current;
    },

    navigate(path) {
        window.location.hash = `#/${path}`;
    },

    back() {
        window.history.back();
    },
});

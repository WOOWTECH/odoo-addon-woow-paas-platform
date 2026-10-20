/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { showLlm, showProjects } from "./platform_config";
import { buildLlmRoutes, llmGateRedirect } from "./llm_routes";
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
    { path: "workspace/:id/services/:serviceId/:tab", name: _t("Service Detail Tab"), routeName: "service-detail", pattern: /^workspace\/(\d+)\/services\/(\d+)\/(overview|configuration|information|logs|addons|mcp-servers|access)$/ },
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
    // LLM-01c：「AI 模型」分頁。單一 routeName `llm` 的雙路由（`llm/<子頁>` 在前、exact `llm`
    // 在後；子頁白名單與 pattern 在 core/llm_routes.js，node 測試直接驗那一份）。
    ...buildLlmRoutes(_t("AI Models")),
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

    /**
     * LLM-01c：離開守衛。一次性金鑰對話框在「建立中／顯示中」時登記一個函式；hash 一變（側欄、
     * Header、程式呼叫 navigate、瀏覽器上一頁跨到別的 hash），守衛回 true 就**不換頁**：hash 用
     * replaceState 還原（不再觸發 hashchange），由守衛自己提示（對話框顯示關閉確認）。Root 依
     * router.current 換分支，換頁就會卸載整串元件、明文跟著丟掉，所以要擋在 parseRoute 之前。
     */
    _leaveGuard: null,
    _currentHash: "",

    init() {
        if (this._initialized) return;
        this._initialized = true;
        this.parseRoute();
        window.addEventListener("hashchange", () => {
            this.onHashChange();
        });
        if (!window.location.hash) {
            window.location.hash = "#/dashboard";
        }
    },

    /** 登記離開守衛；回傳移除函式（只移除自己登記的那一個）。 */
    setLeaveGuard(guard) {
        this._leaveGuard = guard;
        return () => {
            if (this._leaveGuard === guard) {
                this._leaveGuard = null;
            }
        };
    },

    onHashChange() {
        const guard = this._leaveGuard;
        if (guard && window.location.hash !== this._currentHash && guard()) {
            window.history.replaceState(window.history.state, "", this._currentHash || "#/dashboard");
            return;
        }
        this.parseRoute();
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
        this._currentHash = window.location.hash;
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
                    } else if (route.routeName === 'llm') {
                        // 唯一的 capture 是子頁，不是記錄 id。
                        this.params.tab = match[1];
                        delete this.params.id;
                    }
                    this._applyProjectGate();
                    this._applyLlmGate();
                    return;
                }
            }
        }

        // Fall back to exact path match (query-stripped). Unknown paths land
        // on the NotFoundPage rather than silently rendering the dashboard.
        const route = routes.find(r => r.path === path);
        this.current = route ? route.routeName : "not-found";
        this._applyProjectGate();
        this._applyLlmGate();
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
            this._currentHash = "#/dashboard";
        }
    },

    _applyLlmGate() {
        // LLM-01c：開關關（或不在 pilot 名單）時 `#/llm`、`#/llm/<子頁>` 都導回總覽，
        // 判準同 _applyProjectGate（同步讀 session，不會閃現分頁）。後端 /api/llm/* 另回
        // llm_disabled——這裡只是外觀。
        if (llmGateRedirect(this.current, showLlm())) {
            this.current = "dashboard";
            this.params = {};
            if (window.location.hash !== "#/dashboard") {
                window.location.hash = "#/dashboard";
            }
            this._currentHash = "#/dashboard";
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

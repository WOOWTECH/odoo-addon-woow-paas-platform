/** @odoo-module **/
import { reactive } from "@odoo/owl";

/** @type {Array<{path: string, name: string, routeName?: string, pattern?: RegExp}>} */
const routes = [
    { path: "dashboard", name: "Dashboard", routeName: "dashboard" },
    { path: "workspaces", name: "Workspaces", routeName: "workspaces" },
    { path: "workspace/:id", name: "Workspace Detail", routeName: "workspace-detail", pattern: /^workspace\/(\d+)$/ },
    { path: "workspace/:id/team", name: "Workspace Team", routeName: "workspace-team", pattern: /^workspace\/(\d+)\/team$/ },
    { path: "workspace/:id/smarthome/:homeId", name: "Smart Home Detail", routeName: "smarthome-detail", pattern: /^workspace\/(\d+)\/smarthome\/(\d+)$/ },
    { path: "workspace/:id/security-access/:saId/:tab", name: "Security Access Detail", routeName: "security-access-detail", pattern: /^workspace\/(\d+)\/security-access\/(\d+)\/(overview|configuration|application|access|email|domains)$/ },
    { path: "workspace/:id/services/marketplace", name: "App Marketplace", routeName: "marketplace", pattern: /^workspace\/(\d+)\/services\/marketplace$/ },
    { path: "workspace/:id/services/configure/:templateId", name: "Configure Service", routeName: "configure", pattern: /^workspace\/(\d+)\/services\/configure\/(\d+)$/ },
    { path: "workspace/:id/services/:serviceId/:tab", name: "Service Detail Tab", routeName: "service-detail", pattern: /^workspace\/(\d+)\/services\/(\d+)\/(overview|configuration|addons|mcp-servers)$/ },
    { path: "workspace/:id/services/:serviceId", name: "Service Detail", routeName: "service-detail", pattern: /^workspace\/(\d+)\/services\/(\d+)$/ },
    // D4=C: `/woow#/projects` is the new entry for the Projects list page; the
    //        legacy `/woow#/ai-assistant/projects` route below stays active and
    //        shares the same `ai-projects` routeName so existing bookmarks
    //        continue to resolve to the same component.
    { path: "projects", name: "Projects", routeName: "ai-projects" },
    { path: "projects/:id", name: "Project Detail", routeName: "project-detail", pattern: /^projects\/(\d+)$/ },
    { path: "ai-assistant/projects/:id", name: "Project Kanban", routeName: "ai-project-kanban", pattern: /^ai-assistant\/projects\/(\d+)$/ },
    { path: "ai-assistant/projects", name: "Projects", routeName: "ai-projects" },
    { path: "ai-assistant/tasks/:taskId", name: "Task Detail", routeName: "ai-task-detail", pattern: /^ai-assistant\/tasks\/(\d+)$/ },
    { path: "ai-assistant/tasks", name: "Tasks", routeName: "ai-tasks" },
    { path: "ai-assistant/chat/:conversationId", name: "AI Chat", routeName: "ai-chat", pattern: /^ai-assistant\/chat\/(\d+)$/ },
    { path: "deployments", name: "Deployments", routeName: "deployments" },
    { path: "billing", name: "Billing", routeName: "billing" },
    { path: "billing/topup", name: "Top-up Store", routeName: "billing-topup" },
    { path: "billing/invoices", name: "Invoices", routeName: "billing-invoices" },
    { path: "billing/invoices/:id", name: "Invoice Detail", routeName: "billing-invoice-detail", pattern: /^billing\/invoices\/(\d+)$/ },
    { path: "billing/subscriptions", name: "Subscriptions", routeName: "billing-subscriptions" },
    { path: "billing/transactions", name: "Transactions", routeName: "billing-transactions" },
    { path: "settings", name: "Settings", routeName: "settings" },
    { path: "settings/reconcile-report", name: "孤兒對帳報表", routeName: "reconcile-report" },
    { path: "settings/profile", name: "Profile", routeName: "settings-profile" },
    { path: "settings/payment-methods", name: "Payment Methods", routeName: "settings-payment-methods" },
    { path: "settings/payment-methods/add", name: "Add Payment Method", routeName: "settings-payment-methods-add" },
];

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
        // Strip "#/" and split off query string — routing is path-based, but
        // query params (e.g. ?bind_status=ok) are preserved in this.params.query.
        const raw = window.location.hash.slice(2);
        const qIdx = raw.indexOf("?");
        const path = qIdx === -1 ? raw : raw.slice(0, qIdx);
        const queryStr = qIdx === -1 ? "" : raw.slice(qIdx + 1);

        this.params = {};
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
                    return;
                }
            }
        }

        // Fall back to exact path match (query-stripped). Unknown paths land
        // on the NotFoundPage rather than silently rendering the dashboard.
        const route = routes.find(r => r.path === path);
        this.current = route ? route.routeName : "not-found";
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

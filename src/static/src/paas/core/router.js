/** @odoo-module **/
import { reactive } from "@odoo/owl";

/** @type {Array<{path: string, name: string, routeName?: string, pattern?: RegExp}>} */
const routes = [
    { path: "dashboard", name: "Dashboard", routeName: "dashboard" },
    { path: "workspaces", name: "Workspaces", routeName: "workspaces" },
    { path: "workspace/:id", name: "Workspace Detail", routeName: "workspace-detail", pattern: /^workspace\/(\d+)$/ },
    { path: "workspace/:id/team", name: "Workspace Team", routeName: "workspace-team", pattern: /^workspace\/(\d+)\/team$/ },
    { path: "workspace/:id/services/marketplace", name: "App Marketplace", routeName: "marketplace", pattern: /^workspace\/(\d+)\/services\/marketplace$/ },
    { path: "workspace/:id/services/configure/:templateId", name: "Configure Service", routeName: "configure", pattern: /^workspace\/(\d+)\/services\/configure\/(\d+)$/ },
    { path: "workspace/:id/services/:serviceId/:tab", name: "Service Detail Tab", routeName: "service-detail", pattern: /^workspace\/(\d+)\/services\/(\d+)\/(overview|configuration)$/ },
    { path: "workspace/:id/services/:serviceId", name: "Service Detail", routeName: "service-detail", pattern: /^workspace\/(\d+)\/services\/(\d+)$/ },
    { path: "ai-assistant", name: "AI Assistant", routeName: "ai-assistant" },
    { path: "ai-assistant/projects/:id", name: "Project Kanban", routeName: "ai-project-kanban", pattern: /^ai-assistant\/projects\/(\d+)$/ },
    { path: "ai-assistant/projects", name: "Support Projects", routeName: "ai-projects" },
    { path: "ai-assistant/tasks/:taskId", name: "Task Detail", routeName: "ai-task-detail", pattern: /^ai-assistant\/tasks\/(\d+)$/ },
    { path: "ai-assistant/tasks", name: "Support Tasks", routeName: "ai-tasks" },
    { path: "ai-assistant/chat/:conversationId", name: "AI Chat", routeName: "ai-chat", pattern: /^ai-assistant\/chat\/(\d+)$/ },
    { path: "deployments", name: "Deployments", routeName: "deployments" },
    { path: "billing", name: "Billing", routeName: "billing" },
    { path: "settings", name: "Settings", routeName: "settings" },
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
        const hash = window.location.hash.slice(2); // Remove "#/"
        this.params = {};

        // Check for pattern-based routes first (more specific patterns)
        for (const route of routes) {
            if (route.pattern) {
                const match = hash.match(route.pattern);
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

        // Fall back to exact match
        const route = routes.find(r => r.path === hash);
        this.current = route ? route.routeName : "dashboard";
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

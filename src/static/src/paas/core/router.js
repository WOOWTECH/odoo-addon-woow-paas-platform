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
    { path: "deployments", name: "Deployments", routeName: "deployments" },
    { path: "billing", name: "Billing", routeName: "billing" },
    { path: "settings", name: "Settings", routeName: "settings" },
];

export const router = reactive({
    current: "dashboard",
    params: {},
    routes,

    init() {
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

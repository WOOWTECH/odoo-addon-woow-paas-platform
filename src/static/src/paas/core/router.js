/** @odoo-module **/
import { reactive } from "@odoo/owl";

/** @type {Array<{path: string, name: string, routeName?: string, pattern?: RegExp}>} */
const routes = [
    { path: "dashboard", name: "Dashboard", routeName: "dashboard" },
    { path: "workspaces", name: "Workspaces", routeName: "workspaces" },
    { path: "workspace/:id", name: "Workspace Detail", routeName: "workspace-detail", pattern: /^workspace\/(\d+)$/ },
    { path: "workspace/:id/team", name: "Workspace Team", routeName: "workspace-team", pattern: /^workspace\/(\d+)\/team$/ },
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

/** @odoo-module **/
import { reactive } from "@odoo/owl";

const routes = [
    { path: "dashboard", name: "Dashboard" },
    { path: "workspaces", name: "Workspaces" },
    { path: "workspace/:id", name: "Workspace Detail", pattern: /^workspace\/(\d+)$/ },
    { path: "workspace/:id/team", name: "Workspace Team", pattern: /^workspace\/(\d+)\/team$/ },
    { path: "deployments", name: "Deployments" },
    { path: "billing", name: "Billing" },
    { path: "settings", name: "Settings" },
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

        // Check for pattern-based routes first
        for (const route of routes) {
            if (route.pattern) {
                const match = hash.match(route.pattern);
                if (match) {
                    this.current = route.path.split("/")[0]; // e.g., "workspace"
                    this.params.id = match[1];
                    if (hash.includes("/team")) {
                        this.current = "workspace-team";
                    } else {
                        this.current = "workspace-detail";
                    }
                    return;
                }
            }
        }

        // Fall back to exact match
        const route = routes.find(r => r.path === hash);
        this.current = route ? route.path : "dashboard";
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

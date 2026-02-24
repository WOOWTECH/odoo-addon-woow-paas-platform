/** @odoo-module **/
import { Component } from "@odoo/owl";

const WORKSPACE_ROUTES = [
    "workspace-detail", "workspace-team", "marketplace", "configure", "service-detail",
];
const AI_ROUTES = [
    "ai-assistant", "ai-projects", "ai-tasks", "ai-task-detail", "ai-chat", "ai-project-kanban",
];

export class Header extends Component {
    static template = "woow_paas_platform.Header";
    static props = { router: Object };

    get breadcrumbItems() {
        const route = this.props.router.current;
        const items = [{ label: "Home", path: "dashboard" }];

        if (WORKSPACE_ROUTES.includes(route)) {
            items.push({ label: "Workspaces", path: "workspaces" });
        } else if (AI_ROUTES.includes(route)) {
            items.push({ label: "AI Assistant", path: "ai-assistant" });
        }

        const currentRoute = this.props.router.routes.find(r => r.routeName === route);
        const currentLabel = currentRoute ? currentRoute.name : "Dashboard";
        items.push({ label: currentLabel, path: null });

        return items;
    }

    onBreadcrumbClick(path) {
        if (path) {
            this.props.router.navigate(path);
        }
    }
}

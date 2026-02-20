/** @odoo-module **/
import { Component } from "@odoo/owl";

export class BottomNav extends Component {
    static template = "woow_paas_platform.BottomNav";
    static props = { router: Object };

    navItems = [
        { path: "dashboard", name: "Dashboard", icon: "dashboard" },
        { path: "workspaces", name: "Workspaces", icon: "workspaces" },
        { path: "ai-assistant", name: "AI", icon: "smart_toy" },
        { path: "billing", name: "Billing", icon: "payments" },
        { path: "settings", name: "Settings", icon: "settings" },
    ];

    isActive(item) {
        const current = this.props.router.current;
        if (item.path === "ai-assistant") {
            return current === "ai-assistant"
                || current === "ai-projects"
                || current === "ai-tasks"
                || current === "ai-task-detail"
                || current === "ai-chat"
                || current === "ai-project-kanban";
        }
        if (item.path === "workspaces") {
            return current === "workspaces"
                || current === "workspace-detail"
                || current === "workspace-team"
                || current === "marketplace"
                || current === "configure"
                || current === "service-detail";
        }
        return current === item.path;
    }
}

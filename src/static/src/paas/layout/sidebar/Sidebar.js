/** @odoo-module **/
import { Component } from "@odoo/owl";

export class Sidebar extends Component {
    static template = "woow_paas_platform.Sidebar";
    static props = { router: Object };

    navItems = [
        { path: "dashboard", name: "Dashboard", icon: "dashboard" },
        { path: "workspaces", name: "Workspaces", icon: "workspaces" },
        { path: "ai-assistant", name: "AI Assistant", icon: "smart_toy" },
        { path: "billing", name: "Billing", icon: "payments" },
        { path: "settings", name: "Settings", icon: "settings" },
    ];

    /**
     * Check if a nav item is active.
     * For ai-assistant, all sub-routes (ai-projects, ai-tasks, etc.) should highlight this item.
     * @param {Object} item - Navigation item
     * @returns {boolean}
     */
    isActive(item) {
        const current = this.props.router.current;
        if (item.path === "ai-assistant") {
            return current === "ai-assistant"
                || current === "ai-projects"
                || current === "ai-tasks"
                || current === "ai-task-detail"
                || current === "ai-chat";
        }
        return current === item.path;
    }

    logout() {
        window.location.href = "/web/session/logout";
    }

    openHelp() {
        window.open("https://docs.woowtech.io", "_blank");
    }
}

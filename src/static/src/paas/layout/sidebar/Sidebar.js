/** @odoo-module **/
import { Component } from "@odoo/owl";

export class Sidebar extends Component {
    static template = "woow_paas_platform.Sidebar";
    static props = { router: Object };

    navItems = [
        { path: "dashboard", name: "Dashboard", icon: "dashboard" },
        { path: "workspaces", name: "Workspaces", icon: "workspaces" },
        { path: "projects", name: "Projects", icon: "folder" },
        { section: "Finance" },
        { path: "billing", name: "Billing", icon: "payments" },
        { path: "billing/invoices", name: "Invoices", icon: "receipt_long" },
        { path: "billing/topup", name: "Top-up Store", icon: "storefront" },
        { section: "System" },
        { path: "settings", name: "Settings", icon: "settings" },
    ];

    /**
     * Check if a nav item is active.
     * For projects, all support sub-routes (ai-projects, ai-tasks, etc.) should highlight this item.
     * @param {Object} item - Navigation item
     * @returns {boolean}
     */
    isActive(item) {
        const current = this.props.router.current;
        if (item.path === "projects") {
            return current === "ai-projects"
                || current === "ai-project-kanban"
                || current === "ai-tasks"
                || current === "ai-task-detail"
                || current === "ai-chat";
        }
        if (item.path === "billing") {
            return current === "billing"
                || current === "billing-subscriptions"
                || current === "billing-transactions";
        }
        if (item.path === "billing/invoices") {
            return current === "billing-invoices"
                || current === "billing-invoice-detail";
        }
        if (item.path === "billing/topup") {
            return current === "billing-topup";
        }
        if (item.path === "settings") {
            return current === "settings"
                || current === "settings-payment-methods"
                || current === "settings-payment-methods-add";
        }
        return current === item.path;
    }

    isSection(item) {
        return !!item.section;
    }

    logout() {
        window.location.href = "/web/session/logout";
    }

    openHelp() {
        window.open("https://docs.woowtech.io", "_blank");
    }
}

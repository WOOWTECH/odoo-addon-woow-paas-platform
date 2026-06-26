/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { profileService } from "../../services/profile_service";

export class Sidebar extends Component {
    static template = "woow_paas_platform.Sidebar";
    static props = { router: Object };

    setup() {
        // is_admin 來自 /api/me（profileService）→ 條件渲染 admin-only nav 項。
        // 注意：前端隱藏只是 cosmetic，真把關在 reconcile_report controller 的 has_group。
        this.profile = useState(profileService);
        onMounted(() => {
            profileService.fetch();
        });
    }

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
        { path: "settings/reconcile-report", name: "孤兒對帳", icon: "cleaning_services", adminOnly: true },
    ];

    get isAdmin() {
        return !!(this.profile.partner && this.profile.partner.is_admin);
    }

    /** adminOnly 項僅 admin 可見；非 adminOnly 一律顯示（用 JS helper 避模板運算子歧義）。 */
    showItem(item) {
        return !item.adminOnly || this.isAdmin;
    }

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
        if (item.path === "settings/reconcile-report") {
            return current === "reconcile-report";
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

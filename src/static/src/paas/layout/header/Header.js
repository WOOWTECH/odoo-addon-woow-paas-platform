/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { billingService } from "../../services/billing_service";
import { profileService } from "../../services/profile_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { formatPoints } from "../../utils/billing_helpers";

const WORKSPACE_ROUTES = [
    "workspace-detail", "workspace-team", "marketplace", "configure", "service-detail",
];
const AI_ROUTES = [
    "ai-projects", "ai-tasks", "ai-task-detail", "ai-chat", "ai-project-kanban",
];
// Support sub-routes that sit under the Projects list page and need a
// "Projects" parent crumb. `ai-projects` itself is the list page, so it gets
// no parent crumb (the generic currentLabel push already yields "Projects").
const SUPPORT_SUB_ROUTES = [
    "ai-tasks", "ai-task-detail", "ai-chat", "ai-project-kanban",
];

export class Header extends Component {
    static template = "woow_paas_platform.Header";
    static props = { router: Object };

    setup() {
        this.billingService = billingService;
        this.profileService = useState(profileService);
        // 訂閱 breadcrumb store，詳情頁 publish 軌跡時 Header 才會重新渲染。
        this.breadcrumbService = useState(breadcrumbService);
        onMounted(() => {
            billingService.fetchWallet();
            profileService.fetch();
        });
    }

    get walletBalance() {
        return billingService.wallet.balance;
    }

    get formattedBalance() {
        return formatPoints(this.walletBalance);
    }

    get partnerName() {
        return this.profileService.partner?.name || "";
    }

    /** Up to 2 uppercase initials from the partner name, or empty if no name yet. */
    get initials() {
        const name = this.partnerName.trim();
        if (!name) return "";
        const parts = name.split(/\s+/).filter(Boolean);
        return parts.slice(0, 2).map(p => p[0].toUpperCase()).join("");
    }

    get avatarUrl() {
        return this.profileService.partner?.avatar_url || "";
    }

    get breadcrumbItems() {
        const route = this.props.router.current;

        // 詳情頁若已 publish 帶記錄名的軌跡，優先採用（Home 一律自動補在最前）。
        const trail = this.breadcrumbService.trailFor(route);
        if (trail) {
            return [{ label: _t("Home"), path: "dashboard" }, ...trail];
        }

        // Fallback：未 publish 軌跡的頁面（list / billing / settings 等）沿用
        // 依 routeName 推算的泛用麵包屑。
        const items = [{ label: _t("Home"), path: "dashboard" }];
        if (WORKSPACE_ROUTES.includes(route)) {
            items.push({ label: _t("Workspaces"), path: "workspaces" });
        } else if (SUPPORT_SUB_ROUTES.includes(route)) {
            items.push({ label: _t("Projects"), path: "projects" });
        }
        const currentRoute = this.props.router.routes.find(r => r.routeName === route);
        const currentLabel = currentRoute ? currentRoute.name : _t("Dashboard");
        items.push({ label: currentLabel, path: null });
        return items;
    }

    onBreadcrumbClick(path) {
        if (path) this.props.router.navigate(path);
    }
}

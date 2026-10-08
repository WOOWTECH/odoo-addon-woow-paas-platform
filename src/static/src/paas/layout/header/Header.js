/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { billingService } from "../../services/billing_service";
import { profileService } from "../../services/profile_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { formatPoints } from "../../utils/billing_helpers";
import { notificationService } from "../../services/notification_service";
import { NotificationPanel } from "../../components/notification-center/NotificationPanel";
import { AccountMenu } from "../../components/account-menu/AccountMenu";

const WORKSPACE_ROUTES = [
    "workspace-detail", "workspace-team", "marketplace", "configure", "service-detail",
];
const AI_ROUTES = [
    "ai-projects", "ai-tasks", "ai-task-detail", "ai-chat", "ai-project-kanban",
];
// Bottom-nav destinations: the mobile back button is not shown on these.
const TOP_LEVEL_ROUTES = ["dashboard", "workspaces", "ai-projects", "billing", "settings"];
// #1310：財務／設定子頁補上層麵包屑，手機版「返回」才有目的地。
const BILLING_SUB_ROUTES = [
    "billing-topup", "billing-invoices", "billing-subscriptions", "billing-transactions",
];
const SETTINGS_SUB_ROUTES = [
    "settings-profile", "settings-payment-methods", "reconcile-report",
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
    static components = { NotificationPanel, AccountMenu };

    setup() {
        this.billingService = billingService;
        this.profileService = useState(profileService);
        // 訂閱 breadcrumb store，詳情頁 publish 軌跡時 Header 才會重新渲染。
        this.breadcrumbService = useState(breadcrumbService);
        this.notifications = useState(notificationService);
        this.state = useState({ accountOpen: false });
        onMounted(() => {
            billingService.fetchWallet();
            profileService.fetch();
            notificationService.start();
        });
    }

    get notificationLabel() {
        const count = this.notifications.unseenActionableCount;
        return count
            ? _t("Notifications (%(count)s new)", { count })
            : _t("Notifications");
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
        } else if (BILLING_SUB_ROUTES.includes(route)) {
            items.push({ label: _t("Billing"), path: "billing" });
        } else if (route === "billing-invoice-detail") {
            items.push({ label: _t("Billing"), path: "billing" });
            items.push({ label: _t("Invoices"), path: "billing/invoices" });
        } else if (SETTINGS_SUB_ROUTES.includes(route)) {
            items.push({ label: _t("Settings"), path: "settings" });
        } else if (route === "settings-payment-methods-add") {
            items.push({ label: _t("Settings"), path: "settings" });
            items.push({ label: _t("Payment Methods"), path: "settings/payment-methods" });
        }
        const currentRoute = this.props.router.routes.find(r => r.routeName === route);
        const currentLabel = currentRoute ? currentRoute.name : _t("Overview");
        items.push({ label: currentLabel, path: null });
        return items;
    }

    /** Nearest clickable ancestor crumb; the mobile header shows a back button to it. */
    get parentCrumb() {
        if (TOP_LEVEL_ROUTES.includes(this.props.router.current)) return null;
        const items = this.breadcrumbItems;
        for (let i = items.length - 2; i >= 0; i--) {
            if (items[i].path) return items[i];
        }
        return null;
    }

    get backLabel() {
        return _t("Back to %(page)s", { page: this.parentCrumb?.label || "" });
    }

    toggleAccount() {
        this.state.accountOpen = !this.state.accountOpen;
        if (this.state.accountOpen) notificationService.close();
    }

    toggleNotifications() {
        this.state.accountOpen = false;
        notificationService.toggle();
    }

    closeAccount() {
        this.state.accountOpen = false;
    }

    onBreadcrumbClick(path) {
        if (path) this.props.router.navigate(path);
    }
}

/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { billingService } from "../../services/billing_service";
import { profileService } from "../../services/profile_service";
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
        const items = [{ label: "Home", path: "dashboard" }];
        if (WORKSPACE_ROUTES.includes(route)) {
            items.push({ label: "Workspaces", path: "workspaces" });
        } else if (SUPPORT_SUB_ROUTES.includes(route)) {
            items.push({ label: "Projects", path: "projects" });
        }
        const currentRoute = this.props.router.routes.find(r => r.routeName === route);
        const currentLabel = currentRoute ? currentRoute.name : "Dashboard";
        items.push({ label: currentLabel, path: null });
        return items;
    }

    onBreadcrumbClick(path) {
        if (path) this.props.router.navigate(path);
    }
}

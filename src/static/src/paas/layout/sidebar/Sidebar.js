/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { profileService } from "../../services/profile_service";
import { helpUrl, showLlm, showProjects } from "../../core/platform_config";
import { sidebarSection } from "../../core/nav_sections";

const SIDEBAR_SECTION = {
    dashboard: "dashboard",
    workspaces: "workspaces",
    projects: "projects",
    // LLM-01c：漏了這行 isActive 永遠比不到（nav_sections 回 "llm"），項目就永遠不亮。
    llm: "llm",
    billing: "billing",
    "billing/invoices": "invoices",
    "billing/topup": "topup",
    settings: "settings",
    "settings/reconcile-report": "reconcile",
};

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
        { path: "dashboard", name: _t("Overview"), icon: "dashboard" },
        { path: "workspaces", name: _t("Workspaces"), icon: "workspaces" },
        { path: "projects", name: _t("Projects"), icon: "folder", requiresProjects: true },
        // LLM-01c（DESIGN §6.1）：「工作區」「專案」之後、「財務」分區之前。
        { path: "llm", name: _t("AI Models"), icon: "neurology", requiresLlm: true },
        { section: _t("Finance") },
        { path: "billing", name: _t("Billing"), icon: "payments" },
        { path: "billing/invoices", name: _t("Invoices"), icon: "receipt_long" },
        { path: "billing/topup", name: _t("Top-up Store"), icon: "storefront" },
        { section: _t("System") },
        { path: "settings", name: _t("Settings"), icon: "settings" },
        { path: "settings/reconcile-report", name: _t("Orphan Reconciliation"), icon: "cleaning_services", adminOnly: true },
    ];

    get isAdmin() {
        return !!(this.profile.partner && this.profile.partner.is_admin);
    }

    get helpHref() {
        return helpUrl();
    }

    /** adminOnly 項僅 admin 可見；非 adminOnly 一律顯示（用 JS helper 避模板運算子歧義）。 */
    showItem(item) {
        return (!item.adminOnly || this.isAdmin)
            && (!item.requiresProjects || showProjects())
            && (!item.requiresLlm || showLlm());
    }

    /** #1310：判準集中在 core/nav_sections.js，涵蓋所有子頁。 */
    isActive(item) {
        return sidebarSection(this.props.router.current) === SIDEBAR_SECTION[item.path];
    }

    isSection(item) {
        return !!item.section;
    }

    logout() {
        window.location.href = "/web/session/logout";
    }

    openHelp() {
        // #1453：網址由 core/platform_config 統一提供（原本寫死的網域不存在）。
        window.open(helpUrl(), "_blank", "noopener");
    }
}

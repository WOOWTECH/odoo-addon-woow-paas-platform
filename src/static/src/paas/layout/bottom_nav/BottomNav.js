/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { showProjects } from "../../core/platform_config";
import { bottomNavSection } from "../../core/nav_sections";

export class BottomNav extends Component {
    static template = "woow_paas_platform.BottomNav";
    static props = { router: Object };

    navItems = [
        { path: "dashboard", name: _t("Dashboard"), icon: "dashboard" },
        { path: "workspaces", name: _t("Workspaces"), icon: "workspaces" },
        { path: "projects", name: _t("Projects"), icon: "folder", requiresProjects: true },
        { path: "billing", name: _t("Billing"), icon: "payments" },
        { path: "settings", name: _t("Settings"), icon: "settings" },
    ];

    showItem(item) {
        return !item.requiresProjects || showProjects();
    }

    /** #1310：判準集中在 core/nav_sections.js，財務子頁一律亮「Billing」。 */
    isActive(item) {
        return bottomNavSection(this.props.router.current) === item.path;
    }
}

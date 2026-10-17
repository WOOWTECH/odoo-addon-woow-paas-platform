/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { showLlm, showProjects } from "../../core/platform_config";
import { bottomNavSection } from "../../core/nav_sections";

export class BottomNav extends Component {
    static template = "woow_paas_platform.BottomNav";
    static props = { router: Object };

    navItems = [
        { path: "dashboard", name: _t("Overview"), icon: "dashboard" },
        { path: "workspaces", name: _t("Workspaces"), icon: "workspaces" },
        { path: "projects", name: _t("Projects"), icon: "folder", requiresProjects: true },
        // LLM-01c（〔REV〕55）：「專案」保留——總覽頁沒有專案入口、手機版側欄又是隱藏的，
        // 拿掉就等於手機進不去專案。show_projects 開時 6 格、關時 5 格（400px 寬驗收）。
        { path: "llm", name: _t("AI Models"), icon: "neurology", requiresLlm: true },
        { path: "billing", name: _t("Billing"), icon: "payments" },
        { path: "settings", name: _t("Settings"), icon: "settings" },
    ];

    showItem(item) {
        return (!item.requiresProjects || showProjects())
            && (!item.requiresLlm || showLlm());
    }

    /** #1310：判準集中在 core/nav_sections.js，財務子頁一律亮「Billing」。 */
    isActive(item) {
        return bottomNavSection(this.props.router.current) === item.path;
    }
}

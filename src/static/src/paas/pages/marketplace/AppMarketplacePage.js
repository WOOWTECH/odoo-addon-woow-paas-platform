/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { CategoryFilter } from "../../components/marketplace/CategoryFilter";
import { AppCard } from "../../components/marketplace/AppCard";
import { cloudService } from "../../services/cloud_service";
import { workspaceService } from "../../services/workspace_service";
import { breadcrumbService } from "../../services/breadcrumb_service";
import { router } from "../../core/router";

/**
 * AppMarketplacePage Component
 * Displays the application marketplace with search and category filtering
 */
export class AppMarketplacePage extends Component {
    static template = "woow_paas_platform.AppMarketplacePage";
    static components = {
        WoowCard,
        WoowIcon,
        WoowButton,
        CategoryFilter,
        AppCard,
    };
    static props = {
        workspaceId: { type: Number },
    };

    setup() {
        this.state = useState({
            searchQuery: "",
            selectedCategory: "all",
        });
        this.cloudService = useState(cloudService);
        this.router = useState(router);

        onMounted(() => {
            this.loadTemplates();
            this._publishBreadcrumb();
        });
    }

    /**
     * 麵包屑軌跡：Workspaces → {所屬工作區}(連回工作區) → App Marketplace。
     * Marketplace 頁本身只抓模板、不載入工作區，工作區名從 workspaceService
     * 的清單快取取得（經工作區頁進來時通常已有）；deep-link 缺快取時退守
     * 泛用標籤「Workspace」，連結仍正確指向該工作區。
     */
    _publishBreadcrumb() {
        const ws = workspaceService.workspaces.find(w => w.id === this.props.workspaceId);
        breadcrumbService.set("marketplace", [
            { label: _t("Workspaces"), path: "workspaces" },
            { label: ws?.name || _t("Workspace"), path: `workspace/${this.props.workspaceId}` },
            { label: _t("App Marketplace"), path: null },
        ]);
    }

    /**
     * Load templates from API with current filters
     */
    async loadTemplates() {
        const category = this.state.selectedCategory === "all" ? null : this.state.selectedCategory;
        const search = this.state.searchQuery.trim() || null;
        await cloudService.fetchTemplates(category, search);
    }

    /**
     * Get filtered templates
     * @returns {Array}
     */
    get templates() {
        return this.cloudService.templates;
    }

    /**
     * Check if loading
     * @returns {boolean}
     */
    get loading() {
        return this.cloudService.loading;
    }

    /**
     * Check if there are templates
     * @returns {boolean}
     */
    get hasTemplates() {
        return this.templates.length > 0;
    }

    /**
     * Handle search input change
     * @param {Event} ev - Input event
     */
    onSearchInput(ev) {
        this.state.searchQuery = ev.target.value;
    }

    /**
     * Handle search form submit
     * @param {Event} ev - Submit event
     */
    onSearchSubmit(ev) {
        ev.preventDefault();
        this.loadTemplates();
    }

    /**
     * Handle category change
     * @param {string} category - Selected category
     */
    onCategoryChange(category) {
        this.state.selectedCategory = category;
        this.loadTemplates();
    }

    /**
     * Handle add template click - navigate to configure page
     * @param {Object} template - Template to add
     */
    onAddTemplate(template) {
        this.router.navigate(`workspace/${this.props.workspaceId}/services/configure/${template.id}`);
    }

    /**
     * Navigate back to workspace detail
     */
    goBack() {
        this.router.navigate(`workspace/${this.props.workspaceId}`);
    }
}

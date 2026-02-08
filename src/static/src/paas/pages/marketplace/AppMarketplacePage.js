/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { CategoryFilter } from "../../components/marketplace/CategoryFilter";
import { AppCard } from "../../components/marketplace/AppCard";
import { cloudService } from "../../services/cloud_service";
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
        });
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

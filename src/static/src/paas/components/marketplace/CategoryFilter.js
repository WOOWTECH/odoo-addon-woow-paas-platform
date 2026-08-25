/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * CategoryFilter Component
 * Displays category tabs for filtering marketplace templates
 */
export class CategoryFilter extends Component {
    static template = "woow_paas_platform.CategoryFilter";
    static props = {
        selectedCategory: { type: String },
        onCategoryChange: { type: Function },
    };

    /**
     * Category definitions matching CloudAppTemplate model
     */
    categories = [
        { key: "all", label: _t("All") },
        { key: "ai_llm", label: _t("AI & LLM") },
        { key: "automation", label: _t("Automation") },
        { key: "database", label: _t("Database") },
        { key: "analytics", label: _t("Analytics") },
        { key: "devops", label: _t("DevOps") },
        { key: "web", label: _t("Web") },
        { key: "container", label: _t("Container") },
        { key: "mcp", label: _t("MCP Server") },
    ];

    /**
     * Handle category tab click
     * @param {string} category - Selected category key
     */
    selectCategory(category) {
        if (this.props.onCategoryChange) {
            this.props.onCategoryChange(category);
        }
    }

    /**
     * Check if a category is currently selected
     * @param {string} category - Category key to check
     * @returns {boolean}
     */
    isSelected(category) {
        return this.props.selectedCategory === category;
    }
}

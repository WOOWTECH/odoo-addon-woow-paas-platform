/** @odoo-module **/
import { Component } from "@odoo/owl";

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
        { key: "all", label: "All" },
        { key: "ai_llm", label: "AI & LLM" },
        { key: "automation", label: "Automation" },
        { key: "database", label: "Database" },
        { key: "analytics", label: "Analytics" },
        { key: "devops", label: "DevOps" },
        { key: "web", label: "Web" },
        { key: "container", label: "Container" },
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

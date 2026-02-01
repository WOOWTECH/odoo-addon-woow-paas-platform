/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * AppCard Component
 * Displays a cloud application template card in the marketplace
 */
export class AppCard extends Component {
    static template = "woow_paas_platform.AppCard";
    static components = { WoowIcon };
    static props = {
        template: { type: Object },
        onAdd: { type: Function, optional: true },
    };

    /**
     * Category icon mapping
     * Maps template category to Material Symbols icon name
     */
    categoryIcons = {
        ai_llm: "psychology",
        automation: "autorenew",
        database: "database",
        analytics: "bar_chart",
        devops: "terminal",
        web: "language",
        container: "inventory_2",
    };

    /**
     * Category color mapping
     * Maps template category to color class
     */
    categoryColors = {
        ai_llm: "purple",
        automation: "orange",
        database: "blue",
        analytics: "green",
        devops: "gray",
        web: "emerald",
        container: "indigo",
    };

    /**
     * Get icon name for template category
     * @returns {string}
     */
    get categoryIcon() {
        return this.categoryIcons[this.props.template.category] || "apps";
    }

    /**
     * Get color class for template category
     * @returns {string}
     */
    get categoryColor() {
        return this.categoryColors[this.props.template.category] || "gray";
    }

    /**
     * Format price for display
     * @returns {string}
     */
    get formattedPrice() {
        const price = this.props.template.monthly_price;
        if (price === 0) {
            return "Free";
        }
        return `$${price.toFixed(2)}/mo`;
    }

    /**
     * Get template tags as array
     * @returns {string[]}
     */
    get tags() {
        const tags = this.props.template.tags;
        if (!tags) return [];
        if (Array.isArray(tags)) return tags.slice(0, 3);
        return [];
    }

    /**
     * Handle add button click
     * @param {Event} ev - Click event
     */
    handleAdd(ev) {
        ev.stopPropagation();
        if (this.props.onAdd) {
            this.props.onAdd(this.props.template);
        }
    }
}

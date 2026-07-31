/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowIcon } from "../icon/WoowIcon";
import { formatTemplatePrice } from "../../utils/pricing_label";

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
        mcp: "hub",
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
        mcp: "teal",
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
     * 卡片價格標籤（#478）。
     *
     * 這裡曾經寫死 `return "Free"`，於是每個付費商品都在 marketplace 謊報免費、
     * 點進配置頁才看到 4500。改讀後端 `_template_pricing` 算出的起價：
     * - 沒有價格資訊 → 回空字串，模板端 `t-if` 據此**不渲染標籤**（寧可沉默，
     *   也不要編一個價格出來）
     * - 起價 0 → Free（真 0 元才叫 Free）
     * - 多個價位 → 「750 pts/mo 起」；單一價位不加「起」
     *
     * 單位縮寫沿用 PlanSelector 的 `_unitLabel` 慣例（mo／yr），讓卡片與下一頁
     * 的 plan 選卡區、總計列字樣一致。
     * @returns {string}
     */
    get formattedPrice() {
        return formatTemplatePrice(this.props.template.pricing);
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

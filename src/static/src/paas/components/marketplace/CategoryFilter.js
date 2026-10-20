/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * 範本分類代碼（cloud_app_template.category）→ 顯示文字。與下方分頁用同一組 msgid；
 * 設定頁直接 t-esc 代碼會在中文介面露出 `ai_llm` 這類值。
 * @param {string} key
 * @returns {string}
 */
export function getCategoryLabel(key) {
    const labels = {
        ai_llm: _t("AI & LLM"),
        automation: _t("Automation"),
        database: _t("Database"),
        analytics: _t("Analytics"),
        devops: _t("DevOps"),
        web: _t("Web"),
        container: _t("Container"),
        mcp: _t("MCP Server"),
    };
    return labels[key] || key;
}

/**
 * CategoryFilter Component
 * Displays category tabs for filtering marketplace templates
 */
export class CategoryFilter extends Component {
    static template = "woow_paas_platform.CategoryFilter";
    static props = {
        selectedCategory: { type: String },
        onCategoryChange: { type: Function },
        // X-LC-001-5：有公開範本的分類代碼；未提供（舊後端）時全部頁籤照舊顯示。
        availableCategories: { type: Array, optional: true },
    };

    /**
     * Category definitions matching CloudAppTemplate model
     */
    allCategories = [
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
     * 要顯示的頁籤：「全部」＋有公開範本的分類（依上方定義的順序）。
     * 目前選中的分類即使已沒有範本也保留，避免選取狀態指向一個看不到的頁籤。
     * @returns {Array<{key: string, label: string}>}
     */
    get categories() {
        const available = this.props.availableCategories;
        if (!Array.isArray(available)) {
            return this.allCategories;
        }
        return this.allCategories.filter(
            (c) => c.key === "all" || available.includes(c.key) || c.key === this.props.selectedCategory
        );
    }

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

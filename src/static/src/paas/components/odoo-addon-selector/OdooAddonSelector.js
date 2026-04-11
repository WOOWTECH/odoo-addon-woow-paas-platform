/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { cloudService } from "../../services/cloud_service";

export class OdooAddonSelector extends Component {
    static template = "woow_paas_platform.OdooAddonSelector";
    static props = {
        templateId: { type: Number },
        selectedIds: { type: Array, optional: true },
        onSelectionChange: { type: Function },
    };

    setup() {
        this.state = useState({
            loading: true,
            search: '',
            categoryFilter: null,
        });

        onMounted(() => this.loadAddons());
    }

    async loadAddons() {
        this.state.loading = true;
        try {
            await cloudService.fetchOdooAddons(this.props.templateId);
        } finally {
            this.state.loading = false;
        }
    }

    get addons() {
        let list = cloudService.odooAddons || [];
        if (this.state.search) {
            const q = this.state.search.toLowerCase();
            list = list.filter(a =>
                a.name.toLowerCase().includes(q) ||
                a.technical_name.toLowerCase().includes(q)
            );
        }
        if (this.state.categoryFilter) {
            list = list.filter(a => a.category === this.state.categoryFilter);
        }
        return list;
    }

    get categories() {
        const cats = new Set();
        for (const a of cloudService.odooAddons || []) {
            if (a.category) cats.add(a.category);
        }
        return [...cats].sort();
    }

    get selectedSet() {
        return new Set(this.props.selectedIds || []);
    }

    isSelected(addonId) {
        return this.selectedSet.has(addonId);
    }

    isDependency(addonId) {
        // Check if this addon is auto-selected as a dependency
        const selected = this.props.selectedIds || [];
        const allAddons = cloudService.odooAddons || [];
        for (const id of selected) {
            const addon = allAddons.find(a => a.id === id);
            if (addon && addon.depends_on.some(d => d.id === addonId)) {
                return true;
            }
        }
        return false;
    }

    toggleAddon(addonId) {
        const current = new Set(this.props.selectedIds || []);
        if (current.has(addonId)) {
            // Check if other selected addons depend on this one
            const dependedBy = this._getDependentAddons(addonId);
            if (dependedBy.length > 0) {
                // Cannot deselect — other selected addons depend on it
                return;
            }
            current.delete(addonId);
        } else {
            current.add(addonId);
            // Auto-add dependencies
            const allAddons = cloudService.odooAddons || [];
            const addon = allAddons.find(a => a.id === addonId);
            if (addon) {
                for (const dep of addon.depends_on) {
                    current.add(dep.id);
                }
            }
        }
        this.props.onSelectionChange([...current]);
    }

    /**
     * Get names of selected addons that depend on the given addon.
     */
    _getDependentAddons(addonId) {
        const selected = this.props.selectedIds || [];
        const allAddons = cloudService.odooAddons || [];
        const dependents = [];
        for (const id of selected) {
            if (id === addonId) continue;
            const addon = allAddons.find(a => a.id === id);
            if (addon && addon.depends_on.some(d => d.id === addonId)) {
                dependents.push(addon.name);
            }
        }
        return dependents;
    }

    /**
     * Check if this addon cannot be deselected due to dependents.
     */
    isRequiredByOthers(addonId) {
        if (!this.isSelected(addonId)) return false;
        return this._getDependentAddons(addonId).length > 0;
    }

    /**
     * Get tooltip text for why an addon cannot be deselected.
     */
    getRequiredByTooltip(addonId) {
        const dependents = this._getDependentAddons(addonId);
        if (dependents.length === 0) return "";
        return `Required by: ${dependents.join(", ")}`;
    }

    onSearchInput(ev) {
        this.state.search = ev.target.value;
    }

    onCategoryChange(ev) {
        this.state.categoryFilter = ev.target.value || null;
    }
}

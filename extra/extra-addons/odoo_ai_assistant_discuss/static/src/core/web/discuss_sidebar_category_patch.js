import { DiscussSidebarCategory } from "@mail/discuss/core/public_web/discuss_sidebar_categories";
import { patch } from "@web/core/utils/patch";

const DiscussSidebarCategoryPatch = {
    setup() {
        super.setup();
        this.state.quickAdd = false;
    },
    searchCategory() {
        this.addToCategory();
        if (this.state.quickAdd) {
            this.state.quickAdd = false;
            this.category.onQuickAdd = false;
        }
    },
    quickAddToCategory() {
        this.addToCategory();
        this.state.quickAdd = this.category.id;
        this.category.onQuickAdd = true;
    },
    get actions() {
        const actions = super.actions;
        if (this.category.canQuickAdd) {
            actions.push({
                onSelect: () => this.quickAddToCategory(),
                label: this.category.quickAddTitle,
                icon: "fa fa-plus",
                hotkey: this.category.quickAddHotkey,
                class: "o-mail-DiscussSidebarCategory-add",
            });
        }
        if (this.category.canSearch) {
            actions.push({
                onSelect: () => this.searchCategory(),
                label: this.category.searchTitle,
                icon: "fa fa-search",
                hotkey: this.category.searchHotkey,
                class: "o-mail-DiscussSidebarCategory-add",
            });
        }
        return actions;
    },
};

patch(DiscussSidebarCategory.prototype, DiscussSidebarCategoryPatch);

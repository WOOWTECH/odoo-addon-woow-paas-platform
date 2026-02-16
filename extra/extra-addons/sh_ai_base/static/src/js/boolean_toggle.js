/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { BooleanToggleField } from "@web/views/fields/boolean_toggle/boolean_toggle_field";
import { _t } from "@web/core/l10n/translation";

patch(BooleanToggleField.prototype, {
    async onChange(newValue) {
        const fieldName = this.props.name;
        const model = this.props.record.resModel;

        // Only apply exclusivity logic for is_default field on sh.ai.llm model in list view
        if (model === 'sh.ai.llm' && fieldName === 'is_default' && newValue === true) {
            // Setting to True - need to unset all others first
            const otherDefaults = await this.env.services.orm.call(
                'sh.ai.llm',
                'search',
                [[['is_default', '=', true], ['id', '!=', this.props.record.resId]]]
            );

            if (otherDefaults.length > 0) {
                // Unset all other defaults
                await this.env.services.orm.call(
                    'sh.ai.llm',
                    'write',
                    [otherDefaults, {'is_default': false}]
                );
            }
        }

        if (model === 'sh.ai.llm' && fieldName === 'is_default' && newValue === false) {
            // Trying to unset default - check if there are others
            const otherDefaults = await this.env.services.orm.call(
                'sh.ai.llm',
                'search',
                [[['is_default', '=', true], ['id', '!=', this.props.record.resId]]]
            );

            if (otherDefaults.length === 0) {
                // This is the only default, don't allow unsetting
                this.env.services.notification.add(
                    _t("Cannot unset the only default LLM. Please set another LLM as default first."),
                    {
                        type: "warning",
                    }
                );
                this.softReload();
                return;
            }
        }

        // Use standard toggle behavior for all cases
        this.state.value = newValue;
        const changes = { [this.props.name]: newValue };
        await this.props.record.update(changes, { save: this.props.autosave });

        this.softReload();
    },

    async softReload() {
        const fieldName = this.props.name;
        const model = this.props.record.resModel;

        // Soft reload the list view to show updated state without blinking
        if (model === 'sh.ai.llm' && fieldName === 'is_default') {
            try {
                // Method 1: Try direct model reload first
                const listModel = this.props.record.model.root;
                if (listModel) {
                    await listModel.load();
                    return;
                }

                // Method 2: Fallback to view service reload
                const viewService = this.env.services.view;
                const currentController = viewService.currentController;
                if (currentController && currentController.view.type === 'list') {
                    await currentController.model.load();
                    return;
                }

                // Method 3: Final fallback using action service
                const actionService = this.env.services.action;
                const currentAction = actionService.currentController?.action;
                if (currentAction) {
                    await actionService.doAction(currentAction, {
                        reload: true,
                        clearBreadcrumbs: false
                    });
                }
            } catch (error) {
                console.warn('Could not soft reload list view:', error);
                // If soft reload fails, the toggle will still work
                // Users just won't see the immediate visual update
            }
        }
    }
});
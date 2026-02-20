/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

/**
 * AiMentionDropdown
 *
 * Dropdown component that appears when the user types '@' in the chat
 * input area. Displays a filterable list of available AI assistants and
 * supports keyboard navigation (ArrowUp/ArrowDown/Enter/Escape).
 */
export class AiMentionDropdown extends Component {
    static template = "woow_paas_platform.AiMentionDropdown";
    static props = {
        assistants: { type: Array },
        visible: { type: Boolean },
        query: { type: String, optional: true },
        onSelect: { type: Function },
        onClose: { type: Function, optional: true },
    };

    setup() {
        this.state = useState({
            selectedIndex: 0,
        });
    }

    /**
     * Filter assistants by the current query string.
     * @returns {Array} Filtered list of assistants
     */
    get filteredAssistants() {
        const query = (this.props.query || "").toLowerCase().trim();
        if (!query) {
            return this.props.assistants;
        }
        return this.props.assistants.filter((assistant) => {
            const name = (assistant.name || "").toLowerCase();
            return name.includes(query);
        });
    }

    /**
     * Handle keyboard navigation within the dropdown.
     * @param {KeyboardEvent} ev
     */
    handleKeydown(ev) {
        const assistants = this.filteredAssistants;
        if (!assistants.length) {
            return;
        }

        switch (ev.key) {
            case "ArrowDown":
                ev.preventDefault();
                this.state.selectedIndex = (this.state.selectedIndex + 1) % assistants.length;
                break;
            case "ArrowUp":
                ev.preventDefault();
                this.state.selectedIndex =
                    (this.state.selectedIndex - 1 + assistants.length) % assistants.length;
                break;
            case "Enter":
                ev.preventDefault();
                this.selectAssistant(assistants[this.state.selectedIndex]);
                break;
            case "Escape":
                ev.preventDefault();
                if (this.props.onClose) {
                    this.props.onClose();
                }
                break;
        }
    }

    /**
     * Select an assistant and notify the parent component.
     * @param {Object} assistant - The selected assistant record
     */
    selectAssistant(assistant) {
        this.state.selectedIndex = 0;
        this.props.onSelect(assistant);
    }

    /**
     * Get the initial letter for the assistant avatar.
     * @param {Object} assistant
     * @returns {string} Single uppercase character
     */
    getInitial(assistant) {
        const name = assistant.name || "?";
        return name.charAt(0).toUpperCase();
    }
}

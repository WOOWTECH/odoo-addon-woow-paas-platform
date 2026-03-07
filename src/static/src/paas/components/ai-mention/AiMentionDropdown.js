/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

/**
 * AiMentionDropdown
 *
 * Dropdown component that appears when the user types '@' in the chat
 * input area. Displays a filterable list of available AI agents and
 * supports keyboard navigation (ArrowUp/ArrowDown/Enter/Escape).
 */
export class AiMentionDropdown extends Component {
    static template = "woow_paas_platform.AiMentionDropdown";
    static props = {
        agents: { type: Array },
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
     * Filter agents by the current query string.
     * @returns {Array} Filtered list of agents
     */
    get filteredAgents() {
        const query = (this.props.query || "").toLowerCase().trim();
        if (!query) {
            return this.props.agents;
        }
        return this.props.agents.filter((agent) => {
            const displayName = (agent.agent_display_name || agent.name || "").toLowerCase();
            const name = (agent.name || "").toLowerCase();
            return displayName.includes(query) || name.includes(query);
        });
    }

    /**
     * Handle keyboard navigation within the dropdown.
     * @param {KeyboardEvent} ev
     */
    handleKeydown(ev) {
        const agents = this.filteredAgents;
        if (!agents.length) {
            return;
        }

        switch (ev.key) {
            case "ArrowDown":
                ev.preventDefault();
                this.state.selectedIndex = (this.state.selectedIndex + 1) % agents.length;
                break;
            case "ArrowUp":
                ev.preventDefault();
                this.state.selectedIndex =
                    (this.state.selectedIndex - 1 + agents.length) % agents.length;
                break;
            case "Enter":
                ev.preventDefault();
                this.selectAgent(agents[this.state.selectedIndex]);
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
     * Select an agent and notify the parent component.
     * @param {Object} agent - The selected agent record
     */
    selectAgent(agent) {
        this.state.selectedIndex = 0;
        this.props.onSelect(agent);
    }

    /**
     * Get the initial letter for the agent avatar.
     * @param {Object} agent
     * @returns {string} Single uppercase character
     */
    getInitial(agent) {
        const name = agent.agent_display_name || agent.name || "?";
        return name.charAt(0).toUpperCase();
    }
}

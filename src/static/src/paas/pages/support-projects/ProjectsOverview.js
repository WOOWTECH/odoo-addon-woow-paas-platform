/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { supportService } from "../../services/support_service";
import { aiService } from "../../services/ai_service";

export class ProjectsOverview extends Component {
    static template = "woow_paas_platform.ProjectsOverview";
    static components = { WoowCard, WoowIcon };
    static props = {};

    setup() {
        this.state = useState({
            statsLoading: true,
            connectionLoading: true,
            agentLoading: true,
        });
        this.supportService = useState(supportService);
        this.aiService = useState(aiService);

        onMounted(async () => {
            await Promise.all([
                this._loadStats(),
                this._loadConnectionStatus(),
                this._loadAgentStatus(),
            ]);
        });
    }

    async _loadStats() {
        this.state.statsLoading = true;
        try {
            await supportService.fetchStats();
        } catch (err) {
            console.warn("Failed to load support stats:", err);
        } finally {
            this.state.statsLoading = false;
        }
    }

    async _loadConnectionStatus() {
        this.state.connectionLoading = true;
        try {
            await aiService.fetchConnectionStatus();
        } catch (err) {
            console.warn("Failed to load AI connection status:", err);
        } finally {
            this.state.connectionLoading = false;
        }
    }

    async _loadAgentStatus() {
        this.state.agentLoading = true;
        try {
            await aiService.fetchAgentStatus();
        } catch (err) {
            console.warn("Failed to load agent status:", err);
        } finally {
            this.state.agentLoading = false;
        }
    }

    get stats() {
        return this.supportService.stats;
    }

    get connection() {
        return this.aiService.connectionStatus || {
            connected: false,
            provider_name: "",
            model_name: "",
        };
    }

    get agent() {
        return this.aiService.agentStatus || {
            ready: false,
            enabled: false,
            operator_configured: false,
            template_name: "",
            model_name: "",
            reason: "",
        };
    }

    get isStatsLoading() {
        return this.state.statsLoading;
    }

    get isConnectionLoading() {
        return this.state.connectionLoading;
    }

    get isAgentLoading() {
        return this.state.agentLoading;
    }
}

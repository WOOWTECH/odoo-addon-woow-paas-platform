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
        });
        this.supportService = useState(supportService);
        this.aiService = useState(aiService);

        onMounted(async () => {
            await Promise.all([
                this._loadStats(),
                this._loadConnectionStatus(),
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

    get isStatsLoading() {
        return this.state.statsLoading;
    }

    get isConnectionLoading() {
        return this.state.connectionLoading;
    }
}

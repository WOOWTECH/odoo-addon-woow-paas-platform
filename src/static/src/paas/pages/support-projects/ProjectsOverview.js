/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { supportService } from "../../services/support_service";

export class ProjectsOverview extends Component {
    static template = "woow_paas_platform.ProjectsOverview";
    static components = { WoowCard, WoowIcon };
    static props = {};

    setup() {
        this.state = useState({
            statsLoading: true,
        });
        this.supportService = useState(supportService);

        onMounted(async () => {
            await this._loadStats();
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

    get stats() {
        return this.supportService.stats;
    }

    get isStatsLoading() {
        return this.state.statsLoading;
    }
}

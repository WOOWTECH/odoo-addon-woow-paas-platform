/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { supportService } from "../../../services/support_service";
import { router } from "../../../core/router";

/**
 * D2 (Plan 2026-05-19): the legacy service-centric MCP tab is now a redirect
 * placeholder. MCP servers are managed under Project Detail
 * (Plan AC #22 / #24). Routing surface stays at
 * `/workspace/:id/services/:serviceId/mcp-servers` to preserve bookmarks.
 */
export class McpServersTab extends Component {
    static template = "woow_paas_platform.McpServersTab";
    static components = { WoowCard, WoowIcon, WoowButton };
    static props = {
        service: { type: Object },
        workspaceId: { type: Number },
    };

    setup() {
        this.state = useState({
            boundProjectId: null,
            loading: true,
        });

        onMounted(() => this.resolveBoundProject());
    }

    async resolveBoundProject() {
        const result = await supportService.fetchProjectForService(this.props.service.id);
        if (result.success && result.data) {
            this.state.boundProjectId = result.data.id;
        }
        this.state.loading = false;
    }

    goToProjectSettings() {
        if (!this.state.boundProjectId) {
            return;
        }
        router.navigate("projects/" + this.state.boundProjectId);
    }
}

/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { StatusBadge } from "../../../components/common/StatusBadge";
import { supportService } from "../../../services/support_service";

export class McpServersSection extends Component {
    static template = "woow_paas_platform.ProjectDetailMcpServersSection";
    static components = { WoowCard, WoowIcon, WoowButton, StatusBadge };
    static props = {
        project: { type: Object },
        onListChanged: { type: Function },
    };

    setup() {
        this.state = useState({
            servers: [],
            loading: true,
            error: null,
            showForm: false,
            editingServer: null,
            form: this._emptyForm(),
            formError: null,
            saving: false,
            // Tracks which shortcut opened the form (Add Sidecar vs Add
            // External) so the commit payload carries the right
            // sidecar_backed flag.
            isSidecarFlow: false,
        });

        onMounted(() => this.loadServers());
    }

    get projectId() {
        return this.props.project.id;
    }

    get isUnbound() {
        return !this.props.project.cloud_service_id;
    }

    get canAddSidecarMcp() {
        // Button only visible when the backing service exposes a sidecar
        // template AND is actually deployed (has an in-cluster identity).
        // #271: an mcp_enabled-but-undeployed service would prefill a public
        // sidecar-hint URL + sidecar_backed=True → rejected by the new
        // in-cluster invariant. Gate on deployment so the sidecar flow is
        // always in-cluster. Endpoint still re-validates ACL on click.
        return (
            !!this.props.project.cloud_service_mcp_enabled &&
            !!this.props.project.cloud_service_deployed
        );
    }

    _emptyForm() {
        return {
            name: "",
            url: "",
            transport: "sse",
            api_key: "",
            description: "",
        };
    }

    async loadServers() {
        this.state.loading = true;
        this.state.error = null;
        const result = await supportService.fetchProjectMcpServers(this.projectId);
        if (result.success) {
            this.state.servers = result.data;
            this.props.onListChanged(result.data);
        } else {
            this.state.error = result.error || "Failed to load MCP servers";
        }
        this.state.loading = false;
    }

    async showAddSidecarForm() {
        if (!this.canAddSidecarMcp) {
            return;
        }
        this.state.editingServer = null;
        this.state.form = this._emptyForm();
        this.state.formError = null;
        this.state.isSidecarFlow = true;
        this.state.showForm = true;
        const hint = await supportService.fetchSidecarHint(this.projectId);
        if (hint.success && hint.data) {
            this.state.form = {
                name: hint.data.suggested_name || "",
                url: hint.data.url || "",
                transport: hint.data.transport || "streamable_http",
                api_key: hint.data.api_key || "",
                description: "",
            };
        } else if (hint.error) {
            this.state.formError = hint.error;
        }
    }

    showAddExternalForm() {
        this.state.editingServer = null;
        this.state.form = this._emptyForm();
        this.state.formError = null;
        this.state.isSidecarFlow = false;
        this.state.showForm = true;
    }

    showEditForm(server) {
        this.state.editingServer = server;
        this.state.isSidecarFlow = !!server.sidecar_backed;
        this.state.form = {
            name: server.name,
            url: server.url,
            transport: server.transport,
            api_key: "",
            description: server.description || "",
        };
        this.state.formError = null;
        this.state.showForm = true;
    }

    hideForm() {
        this.state.showForm = false;
        this.state.editingServer = null;
        this.state.formError = null;
        this.state.isSidecarFlow = false;
    }

    onFormInput(field, ev) {
        this.state.form[field] = ev.target.value;
    }

    async saveServer() {
        const { name, url, transport, api_key, description } = this.state.form;
        if (!name.trim() || !url.trim()) {
            this.state.formError = "Name and URL are required";
            return;
        }

        this.state.saving = true;
        this.state.formError = null;

        const payload = {
            name: name.trim(),
            url: url.trim(),
            transport,
            description,
        };
        if (api_key) {
            payload.api_key = api_key;
        }

        let result;
        if (this.state.editingServer) {
            result = await supportService.updateProjectMcpServer(
                this.projectId,
                this.state.editingServer.id,
                payload,
            );
        } else {
            payload.sidecar_backed = !!this.state.isSidecarFlow;
            result = await supportService.createProjectMcpServer(this.projectId, payload);
        }

        if (result.success) {
            this.hideForm();
            await this.loadServers();
        } else {
            this.state.formError = result.error || "Failed to save";
        }
        this.state.saving = false;
    }

    async deleteServer(server) {
        if (!confirm(`Are you sure you want to remove MCP server "${server.name}"?`)) {
            return;
        }
        const result = await supportService.deleteProjectMcpServer(this.projectId, server.id);
        if (result.success) {
            this.state.servers = this.state.servers.filter((s) => s.id !== server.id);
            this.props.onListChanged(this.state.servers);
        }
    }

    async syncServer(server) {
        server._syncing = true;
        const result = await supportService.syncProjectMcpServer(this.projectId, server.id);
        if (result.success) {
            Object.assign(server, result.data);
        }
        server._syncing = false;
    }

    async testServer(server) {
        server._testing = true;
        const result = await supportService.testProjectMcpServer(this.projectId, server.id);
        if (result.success) {
            server.state = result.data.state;
            server.state_message = result.data.state_message;
        }
        server._testing = false;
    }

    getStateBadge(state) {
        const map = { draft: "pending", connected: "running", error: "error" };
        return map[state] || "pending";
    }

    getTestHandler(server) {
        return () => this.testServer(server);
    }
    getSyncHandler(server) {
        return () => this.syncServer(server);
    }
    getEditHandler(server) {
        return () => this.showEditForm(server);
    }
    getDeleteHandler(server) {
        return () => this.deleteServer(server);
    }
}

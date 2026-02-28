/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { StatusBadge } from "../../../components/common/StatusBadge";
import { cloudService } from "../../../services/cloud_service";

export class McpServersTab extends Component {
    static template = "woow_paas_platform.McpServersTab";
    static components = { WoowCard, WoowIcon, WoowButton, StatusBadge };
    static props = {
        service: { type: Object },
        workspaceId: { type: Number },
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
        });

        onMounted(() => this.loadServers());
    }

    _emptyForm() {
        return {
            name: '',
            url: '',
            transport: 'sse',
            api_key: '',
            description: '',
        };
    }

    async loadServers() {
        this.state.loading = true;
        this.state.error = null;
        const result = await cloudService.fetchMcpServers(
            this.props.workspaceId,
            this.props.service.id,
        );
        if (result.success) {
            this.state.servers = result.data;
        } else {
            this.state.error = result.error || 'Failed to load MCP servers';
        }
        this.state.loading = false;
    }

    showAddForm() {
        this.state.editingServer = null;
        this.state.form = this._emptyForm();
        this.state.formError = null;
        this.state.showForm = true;
    }

    showEditForm(server) {
        this.state.editingServer = server;
        this.state.form = {
            name: server.name,
            url: server.url,
            transport: server.transport,
            api_key: '',
            description: server.description || '',
        };
        this.state.formError = null;
        this.state.showForm = true;
    }

    hideForm() {
        this.state.showForm = false;
        this.state.editingServer = null;
        this.state.formError = null;
    }

    onFormInput(field, ev) {
        this.state.form[field] = ev.target.value;
    }

    async saveServer() {
        const { name, url, transport, api_key, description } = this.state.form;
        if (!name.trim() || !url.trim()) {
            this.state.formError = 'Name and URL are required';
            return;
        }

        this.state.saving = true;
        this.state.formError = null;

        const payload = { name: name.trim(), url: url.trim(), transport, description };
        if (api_key) {
            payload.api_key = api_key;
        }

        let result;
        if (this.state.editingServer) {
            result = await cloudService.updateMcpServer(
                this.props.workspaceId,
                this.props.service.id,
                this.state.editingServer.id,
                payload,
            );
        } else {
            result = await cloudService.createMcpServer(
                this.props.workspaceId,
                this.props.service.id,
                payload,
            );
        }

        if (result.success) {
            this.hideForm();
            await this.loadServers();
        } else {
            this.state.formError = result.error || 'Failed to save';
        }
        this.state.saving = false;
    }

    async deleteServer(server) {
        const result = await cloudService.deleteMcpServer(
            this.props.workspaceId,
            this.props.service.id,
            server.id,
        );
        if (result.success) {
            this.state.servers = this.state.servers.filter(s => s.id !== server.id);
        }
    }

    async syncServer(server) {
        server._syncing = true;
        const result = await cloudService.syncMcpServer(
            this.props.workspaceId,
            this.props.service.id,
            server.id,
        );
        if (result.success) {
            // Update server in-place
            Object.assign(server, result.data);
        }
        server._syncing = false;
    }

    async testServer(server) {
        server._testing = true;
        const result = await cloudService.testMcpServer(
            this.props.workspaceId,
            this.props.service.id,
            server.id,
        );
        if (result.success) {
            server.state = result.data.state;
            server.state_message = result.data.state_message;
        }
        server._testing = false;
    }

    getStateBadge(state) {
        const map = {
            draft: 'pending',
            connected: 'running',
            error: 'error',
        };
        return map[state] || 'pending';
    }

    // Handler factories for template onClick props (OWL props can't use `this`)
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

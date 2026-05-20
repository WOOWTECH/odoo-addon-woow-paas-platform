/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { cloudService } from "../../../services/cloud_service";
import { supportService } from "../../../services/support_service";

const UNHEALTHY_STATES = new Set(["error", "deleted", "deleting"]);

export class CloudServiceBindingSection extends Component {
    static template = "woow_paas_platform.ProjectDetailCloudServiceBindingSection";
    static components = { WoowCard, WoowIcon, WoowButton };
    static props = {
        project: { type: Object },
        onUpdated: { type: Function },
    };

    setup() {
        this.state = useState({
            availableServices: [],
            loadingServices: false,
            pendingSelection: null,
            showConfirmModal: false,
            confirmAction: null,
            confirmMessage: "",
            saving: false,
            toast: null,
            error: null,
        });

        onMounted(() => this.loadAvailableServices());
    }

    get currentServiceId() {
        return this.props.project.cloud_service_id || null;
    }

    get currentServiceName() {
        return this.props.project.cloud_service_name || "";
    }

    get isBound() {
        return !!this.currentServiceId;
    }

    async loadAvailableServices() {
        const workspaceId = this.props.project.workspace_id;
        if (!workspaceId) {
            this.state.availableServices = [];
            return;
        }
        this.state.loadingServices = true;
        await cloudService.fetchServices(workspaceId);
        // D8: filter unhealthy services. Always keep the currently bound
        // service in the list so the dropdown selection remains coherent.
        const filtered = (cloudService.services || []).filter((svc) => {
            if (svc.id === this.currentServiceId) {
                return true;
            }
            return !UNHEALTHY_STATES.has(svc.state);
        });
        this.state.availableServices = filtered;
        this.state.loadingServices = false;
    }

    onSelectionChange(ev) {
        const rawValue = ev.target.value;
        const selectedId = rawValue === "" ? null : parseInt(rawValue, 10);

        if (selectedId === this.currentServiceId) {
            return;
        }

        const targetService = this.state.availableServices.find((s) => s.id === selectedId);
        let message;
        if (!selectedId) {
            // Unbind
            message = `Unbind this project from ${this.currentServiceName}? This will block if user-scope MCP servers exist.`;
        } else if (this.isBound) {
            // Rebind
            message = `Rebind this project from ${this.currentServiceName} to ${targetService?.name || "the selected service"}?`;
        } else {
            // Bind from unbound
            message = `Bind this project to ${targetService?.name || "the selected service"}?`;
        }

        this.state.pendingSelection = selectedId;
        this.state.confirmMessage = message;
        this.state.showConfirmModal = true;
        this.state.error = null;
        this.state.toast = null;

        // Revert the dropdown visually until the user confirms.
        ev.target.value = this.currentServiceId == null ? "" : String(this.currentServiceId);
    }

    async confirmChange() {
        const targetId = this.state.pendingSelection;
        this.state.saving = true;
        const payload = { cloud_service_id: targetId === null ? false : targetId };
        const result = await supportService.updateProject(this.props.project.id, payload);
        this.state.saving = false;
        this.state.showConfirmModal = false;
        if (result.success) {
            this.props.onUpdated(result.data);
            await this.loadAvailableServices();
        } else {
            const msg = result.error || "Failed to update";
            if (/cloud service/i.test(msg)) {
                this.state.toast = "This project has user-scope MCP servers. Remove them first.";
            } else {
                this.state.error = msg;
            }
        }
        this.state.pendingSelection = null;
    }

    cancelChange() {
        this.state.showConfirmModal = false;
        this.state.pendingSelection = null;
    }

    dismissToast() {
        this.state.toast = null;
    }
}

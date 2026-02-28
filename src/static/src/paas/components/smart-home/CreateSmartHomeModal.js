/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { workspaceService } from "../../services/workspace_service";

/**
 * CreateSmartHomeModal Component
 *
 * A modal dialog for creating a new smart home instance.
 * After creation, automatically triggers provisioning.
 *
 * Props:
 *   - workspaceId (number): Parent workspace ID
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onCreated (Function): Called after successful creation with smart home data
 */
export class CreateSmartHomeModal extends Component {
    static template = "woow_paas_platform.CreateSmartHomeModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        workspaceId: { type: Number },
        onClose: { type: Function },
        onCreated: { type: Function },
    };

    setup() {
        this.state = useState({
            name: "",
            haPort: "8123",
            loading: false,
            provisioning: false,
            error: null,
            step: "form", // "form" | "provisioning" | "done"
            provisionProgress: 0,
        });
    }

    onNameInput(ev) {
        this.state.name = ev.target.value;
        this.state.error = null;
    }

    onPortInput(ev) {
        this.state.haPort = ev.target.value;
        this.state.error = null;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.loading && !this.state.provisioning) {
            this.props.onClose();
        }
    }

    async onSubmit() {
        const name = this.state.name.trim();
        if (!name) {
            this.state.error = "Smart home name is required";
            return;
        }

        const port = parseInt(this.state.haPort, 10);
        if (isNaN(port) || port < 1 || port > 65535) {
            this.state.error = "Please enter a valid port number (1-65535)";
            return;
        }

        this.state.loading = true;
        this.state.error = null;

        // Step 1: Create smart home
        const createResult = await workspaceService.createSmartHome(this.props.workspaceId, {
            name,
            ha_port: port,
        });

        if (!createResult.success) {
            this.state.error = createResult.error || "Failed to create smart home";
            this.state.loading = false;
            return;
        }

        // Step 2: Start provisioning
        this.state.step = "provisioning";
        this.state.loading = false;
        this.state.provisioning = true;
        this.state.provisionProgress = 30;

        const homeId = createResult.data.id;
        const provisionResult = await workspaceService.provisionSmartHome(
            this.props.workspaceId,
            homeId
        );

        this.state.provisionProgress = 100;

        if (provisionResult.success) {
            this.state.step = "done";
            this.state.provisioning = false;
            // Notify parent after short delay so user sees completion
            setTimeout(() => {
                this.props.onCreated(provisionResult.data || createResult.data);
            }, 800);
        } else {
            this.state.error = provisionResult.error || "Provisioning failed";
            this.state.provisioning = false;
            // Still notify parent with the created data, even though provisioning failed
            this.props.onCreated(createResult.data);
        }
    }
}

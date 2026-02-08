/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";

/**
 * DeleteServiceModal Component
 *
 * A confirmation modal for deleting a cloud service.
 * Requires user to type the service name to confirm deletion.
 *
 * Props:
 *   - service (Object): Service data { id, name, workspace_id }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onDeleted (Function): Called after successful deletion
 */
export class DeleteServiceModal extends Component {
    static template = "woow_paas_platform.DeleteServiceModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        service: { type: Object },
        onClose: { type: Function },
        onDeleted: { type: Function },
    };

    setup() {
        this.state = useState({
            confirmName: "",
            deleting: false,
            error: null,
        });
    }

    get canDelete() {
        return this.state.confirmName.trim() === this.props.service.name;
    }

    onConfirmNameInput(ev) {
        this.state.confirmName = ev.target.value;
        this.state.error = null;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.deleting) {
            this.props.onClose();
        }
    }

    async onDelete() {
        if (!this.canDelete || this.state.deleting) {
            return;
        }

        this.state.deleting = true;
        this.state.error = null;

        const result = await cloudService.deleteService(
            this.props.service.workspace_id,
            this.props.service.id
        );

        if (result.success) {
            this.props.onDeleted();
        } else {
            this.state.error = result.error || "Failed to delete service";
            this.state.deleting = false;
        }
    }
}

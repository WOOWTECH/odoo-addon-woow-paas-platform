/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";

/**
 * RollbackModal Component
 *
 * Displays revision history and allows rollback to a previous version.
 *
 * Props:
 *   - service (Object): Service data { id, workspace_id, helm_revision, name }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onRollback (Function): Called after successful rollback
 */
export class RollbackModal extends Component {
    static template = "woow_paas_platform.RollbackModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        service: { type: Object },
        onClose: { type: Function },
        onRollback: { type: Function },
    };

    setup() {
        this.state = useState({
            revisions: [],
            selectedRevision: null,
            loading: true,
            rolling: false,
            error: null,
        });

        onMounted(() => {
            this.loadRevisions();
        });
    }

    get currentRevision() {
        return this.props.service.helm_revision || 1;
    }

    get canRollback() {
        return (
            this.state.selectedRevision !== null &&
            this.state.selectedRevision !== this.currentRevision &&
            !this.state.rolling
        );
    }

    async loadRevisions() {
        this.state.loading = true;
        this.state.error = null;

        const result = await cloudService.getRevisions(
            this.props.service.workspace_id,
            this.props.service.id
        );

        if (result.success) {
            this.state.revisions = result.data || [];
        } else {
            this.state.error = result.error || "Failed to load revision history";
        }

        this.state.loading = false;
    }

    selectRevision(revision) {
        if (revision.revision !== this.currentRevision && !this.state.rolling) {
            this.state.selectedRevision = revision.revision;
        }
    }

    isSelected(revision) {
        return this.state.selectedRevision === revision.revision;
    }

    isCurrent(revision) {
        return revision.revision === this.currentRevision;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.rolling) {
            this.props.onClose();
        }
    }

    formatDate(dateStr) {
        if (!dateStr) return "-";
        const date = new Date(dateStr);
        return date.toLocaleString("en-US", {
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
        });
    }

    getStatusClass(status) {
        const statusMap = {
            deployed: "o_woow_revision_success",
            superseded: "o_woow_revision_superseded",
            failed: "o_woow_revision_failed",
            pending: "o_woow_revision_pending",
        };
        return statusMap[status] || "";
    }

    async onRollback() {
        if (!this.canRollback) {
            return;
        }

        this.state.rolling = true;
        this.state.error = null;

        const result = await cloudService.rollbackService(
            this.props.service.workspace_id,
            this.props.service.id,
            this.state.selectedRevision
        );

        if (result.success) {
            this.props.onRollback();
        } else {
            this.state.error = result.error || "Failed to rollback service";
            this.state.rolling = false;
        }
    }
}

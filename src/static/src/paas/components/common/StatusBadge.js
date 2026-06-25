/** @odoo-module **/
import { Component } from "@odoo/owl";

/**
 * StatusBadge Component
 *
 * Displays a status indicator badge with appropriate colors and optional spinner
 * for in-progress states.
 *
 * Usage:
 *   <StatusBadge status="'running'" />
 *   <StatusBadge status="service.state" size="'sm'" />
 *
 * Props:
 *   - status (string): Service state - running, deploying, upgrading, pending, error, deleting, active, suspended
 *   - size (string, optional): Badge size - 'sm', 'md' (default), 'lg'
 */
export class StatusBadge extends Component {
    static template = "woow_paas_platform.StatusBadge";
    static props = {
        status: { type: String },
        size: { type: String, optional: true },
    };

    get statusConfig() {
        const configs = {
            running: {
                label: "Running",
                colorClass: "o_woow_status_running",
                showSpinner: false,
            },
            deploying: {
                label: "Deploying",
                colorClass: "o_woow_status_deploying",
                showSpinner: true,
            },
            initializing: {
                label: "Initializing",
                colorClass: "o_woow_status_deploying",
                showSpinner: true,
            },
            upgrading: {
                label: "Upgrading",
                colorClass: "o_woow_status_upgrading",
                showSpinner: true,
            },
            pending: {
                label: "Pending",
                colorClass: "o_woow_status_pending",
                showSpinner: false,
            },
            error: {
                label: "Error",
                colorClass: "o_woow_status_error",
                showSpinner: false,
            },
            deleting: {
                label: "Deleting",
                colorClass: "o_woow_status_deleting",
                showSpinner: true,
            },
            connected: {
                label: "Connected",
                colorClass: "o_woow_status_running",
                showSpinner: false,
            },
            disconnected: {
                label: "Disconnected",
                colorClass: "o_woow_status_pending",
                showSpinner: false,
            },
            provisioning: {
                label: "Provisioning",
                colorClass: "o_woow_status_deploying",
                showSpinner: true,
            },
            // Security Access / SmartHome service-body states (spec §4):
            // a provisioned service is "active" (green) and a billing-frozen
            // one is "suspended" (warning). Without these they fall to the
            // yellow "pending" fallback and a normally running service looks
            // unfinished.
            active: {
                label: "Active",
                colorClass: "o_woow_status_running",
                showSpinner: false,
            },
            // Security Access 軸2: tunnel provisioned but has zero application
            // routes — reachable at nothing yet. Amber (pending colour) to
            // signal "up but not serving", distinct from green Active.
            active_no_route: {
                label: "Active · No Route",
                colorClass: "o_woow_status_pending",
                showSpinner: false,
            },
            suspended: {
                label: "Suspended",
                colorClass: "o_woow_status_pending",
                showSpinner: false,
            },
        };

        return configs[this.props.status] || {
            label: this.props.status || "Unknown",
            colorClass: "o_woow_status_pending",
            showSpinner: false,
        };
    }

    get badgeClass() {
        const config = this.statusConfig;
        const size = this.props.size || "md";
        return `o_woow_status_badge o_woow_status_badge_${size} ${config.colorClass}`;
    }

    get label() {
        return this.statusConfig.label;
    }

    get showSpinner() {
        return this.statusConfig.showSpinner;
    }
}

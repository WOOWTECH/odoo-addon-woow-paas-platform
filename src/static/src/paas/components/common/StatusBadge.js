/** @odoo-module **/
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

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
 *   - phase (string, optional): #1159 Q40 — 不新增 state 值，只細分 `initializing`：
 *     queued / running / waiting_ready / retrying（取自服務 payload 的 `init_phase`）。
 *     其他 status 一律忽略。
 */
export class StatusBadge extends Component {
    static template = "woow_paas_platform.StatusBadge";
    static props = {
        status: { type: String },
        size: { type: String, optional: true },
        phase: { type: String, optional: true },
    };

    get statusConfig() {
        const configs = {
            running: {
                label: _t("Running"),
                colorClass: "o_woow_status_running",
                showSpinner: false,
            },
            deploying: {
                label: _t("Deploying"),
                colorClass: "o_woow_status_deploying",
                showSpinner: true,
            },
            initializing: {
                label: _t("Initializing"),
                colorClass: "o_woow_status_deploying",
                showSpinner: true,
            },
            upgrading: {
                label: _t("Upgrading"),
                colorClass: "o_woow_status_upgrading",
                showSpinner: true,
            },
            pending: {
                label: _t("Pending"),
                colorClass: "o_woow_status_pending",
                showSpinner: false,
            },
            error: {
                label: _t("Error"),
                colorClass: "o_woow_status_error",
                showSpinner: false,
            },
            deleting: {
                label: _t("Deleting"),
                colorClass: "o_woow_status_deleting",
                showSpinner: true,
            },
            connected: {
                label: _t("Connected"),
                colorClass: "o_woow_status_running",
                showSpinner: false,
            },
            disconnected: {
                label: _t("Disconnected"),
                colorClass: "o_woow_status_pending",
                showSpinner: false,
            },
            // #833: the tunnel is gone on Cloudflare, not merely offline.
            // ⚠ This entry is load-bearing, not decoration. Without it the
            // value falls through to the fallback below, which paints the raw
            // string with `o_woow_status_pending` — the SAME amber as
            // `disconnected`. The whole point of #833 is that "deleted" and
            // "not connected" must not look identical; a missing entry here
            // recreates the bug one layer up. Red, because the two states
            // differ in whether anyone has to act: deleted needs a rebuild,
            // disconnected just needs the connector to come back.
            deleted: {
                label: _t("Deleted"),
                colorClass: "o_woow_status_error",
                showSpinner: false,
            },
            provisioning: {
                label: _t("Provisioning"),
                colorClass: "o_woow_status_deploying",
                showSpinner: true,
            },
            // Security Access / SmartHome service-body states (spec §4):
            // a provisioned service is "active" (green) and a billing-frozen
            // one is "suspended" (warning). Without these they fall to the
            // yellow "pending" fallback and a normally running service looks
            // unfinished.
            active: {
                label: _t("Active"),
                colorClass: "o_woow_status_running",
                showSpinner: false,
            },
            // Security Access 軸2: tunnel provisioned but has zero application
            // routes — reachable at nothing yet. Amber (pending colour) to
            // signal "up but not serving", distinct from green Active.
            active_no_route: {
                label: _t("Active · No Route"),
                colorClass: "o_woow_status_pending",
                showSpinner: false,
            },
            suspended: {
                label: _t("Suspended"),
                colorClass: "o_woow_status_pending",
                showSpinner: false,
            },
        };

        return configs[this.props.status] || {
            label: this.props.status || _t("Unknown"),
            colorClass: "o_woow_status_pending",
            showSpinner: false,
        };
    }

    get badgeClass() {
        const config = this.statusConfig;
        const size = this.props.size || "md";
        return `o_woow_status_badge o_woow_status_badge_${size} ${config.colorClass}`;
    }

    get phaseLabel() {
        if (this.props.status !== "initializing") {
            return null;
        }
        const labels = {
            queued: _t("Initializing · Queued"),
            running: _t("Initializing"),
            waiting_ready: _t("Initializing · Waiting for app"),
            retrying: _t("Initializing · Retrying"),
        };
        return labels[this.props.phase] || null;
    }

    get label() {
        return this.phaseLabel || this.statusConfig.label;
    }

    get showSpinner() {
        return this.statusConfig.showSpinner;
    }
}

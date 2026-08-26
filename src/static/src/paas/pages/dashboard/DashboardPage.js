/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { dashboardService } from "../../services/dashboard_service";
import { profileService } from "../../services/profile_service";
import { formatPoints, formatDate, formatDateTimeShort } from "../../utils/billing_helpers";

export class DashboardPage extends Component {
    static template = "woow_paas_platform.DashboardPage";
    static components = { WoowCard, WoowIcon };

    setup() {
        this.dashboardService = useState(dashboardService);
        this.profileService = useState(profileService);
        onMounted(() => {
            // Profile fetch is shared with the Header; dashboardService loads
            // the rest of the page in one round-trip.
            profileService.fetch();
            dashboardService.fetchSummary();
        });
    }

    // ------------------------------------------------------------------
    // Welcome heading
    // ------------------------------------------------------------------

    get welcomeName() {
        const name = this.profileService.partner?.name?.trim();
        return name || _t("there");
    }

    // ------------------------------------------------------------------
    // State convenience getters
    // ------------------------------------------------------------------

    get loading() {
        return this.dashboardService.loading && !this.dashboardService.data;
    }

    get error() {
        return this.dashboardService.error;
    }

    get data() {
        return this.dashboardService.data;
    }

    // ------------------------------------------------------------------
    // Stat-card content
    // ------------------------------------------------------------------

    get membersCard() {
        const m = this.data?.members || { total: 0, active: 0, pending: 0 };
        return {
            label: _t("Members"),
            subtitle: _t("Workspace access"),
            icon: "group",
            color: "blue",
            items: [
                { label: _t("Total"), value: m.total },
                { label: _t("Active"), value: m.active, highlight: true },
            ],
        };
    }

    get billingCard() {
        const b = this.data?.billing || {};
        return {
            label: _t("Billing"),
            subtitle: _t("Credits & next bill"),
            icon: "account_balance_wallet",
            color: "green",
            items: [
                { label: _t("Credits Left"), value: formatPoints(b.credits_left), highlight: true },
                { label: _t("Cycle Usage"), value: formatPoints(b.current_cycle_usage) },
                {
                    label: _t("Next Bill"),
                    value: b.next_bill_date
                        ? `${formatDate(b.next_bill_date)} · ${formatPoints(b.next_bill_estimate)}`
                        : "—",
                },
            ],
        };
    }

    get workspacesCard() {
        const w = this.data?.workspaces || { total: 0, services: { running: 0, total: 0 } };
        return {
            label: _t("Workspaces"),
            subtitle: _t("Active spaces & services"),
            icon: "dashboard",
            color: "purple",
            items: [
                { label: _t("Total"), value: w.total, highlight: true },
                {
                    label: _t("Services"),
                    value: _t("%s running / %s total", w.services.running, w.services.total),
                },
            ],
        };
    }

    /** Three stat cards rendered together in the grid. */
    get statCards() {
        if (!this.data) return [];
        return [this.membersCard, this.billingCard, this.workspacesCard];
    }

    // ------------------------------------------------------------------
    // Recent activity helpers
    // ------------------------------------------------------------------

    get activities() {
        return this.data?.recent_activity || [];
    }

    /** Render `Mitchell Admin` → `MA` for the avatar circle. */
    initialsFor(actor) {
        if (!actor) return "?";
        return actor
            .trim()
            .split(/\s+/)
            .filter(Boolean)
            .slice(0, 2)
            .map(p => p[0].toUpperCase())
            .join("");
    }

    formatActivityTime(ts) {
        return formatDateTimeShort(ts);
    }
}

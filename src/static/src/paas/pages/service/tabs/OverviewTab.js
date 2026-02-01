/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { formatDate } from "../../../services/utils";

/**
 * OverviewTab Component
 *
 * Displays connection information and resource details for a cloud service.
 * Shows:
 * - Connection Info: Status, URLs, ports
 * - Resources: Helm release details, chart version, namespace
 *
 * Props:
 *   - service (Object): Service data object with all details
 */
export class OverviewTab extends Component {
    static template = "woow_paas_platform.OverviewTab";
    static components = { WoowCard, WoowIcon };
    static props = {
        service: { type: Object },
    };

    get isOnline() {
        return this.props.service.state === "running";
    }

    get publicUrl() {
        if (!this.props.service.subdomain) return null;
        return `https://${this.props.service.subdomain}.woowtech.com`;
    }

    get helmNamespace() {
        return this.props.service.helm_namespace || `paas-ws-${this.props.service.workspace_id}`;
    }

    formatDate(dateString) {
        return formatDate(dateString, { long: true });
    }

    copyToClipboard(text) {
        if (navigator.clipboard) {
            navigator.clipboard.writeText(text);
        }
    }
}

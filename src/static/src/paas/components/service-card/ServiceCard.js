/** @odoo-module **/
import { Component } from "@odoo/owl";
import { StatusBadge } from "../common/StatusBadge";
import { WoowIcon } from "../icon/WoowIcon";
import { WoowButton } from "../button/WoowButton";

/**
 * ServiceCard Component
 * Displays a cloud service instance with status, URL, and quick actions
 *
 * Usage:
 *   <ServiceCard
 *       service="serviceData"
 *       onOpen="() => openWebUI(serviceData)"
 *       onSettings="() => goToSettings(serviceData.id)"
 *   />
 */
export class ServiceCard extends Component {
    static template = "woow_paas_platform.ServiceCard";
    static components = { StatusBadge, WoowIcon, WoowButton };
    static props = {
        service: { type: Object },
        onOpen: { type: Function, optional: true },
        onSettings: { type: Function, optional: true },
    };

    /**
     * Category to icon mapping
     */
    static CATEGORY_ICONS = {
        database: "database",
        automation: "smart_toy",
        monitoring: "monitoring",
        storage: "cloud_queue",
        networking: "hub",
        security: "security",
        communication: "chat",
        development: "code",
        default: "cloud",
    };

    get templateIcon() {
        const category = this.props.service.template?.category || "default";
        return ServiceCard.CATEGORY_ICONS[category] || ServiceCard.CATEGORY_ICONS.default;
    }

    get serviceUrl() {
        const subdomain = this.props.service.subdomain;
        if (subdomain) {
            return `https://${subdomain}.paas.woow.tw`;
        }
        return null;
    }

    get displayUrl() {
        const subdomain = this.props.service.subdomain;
        if (subdomain) {
            return `${subdomain}.paas.woow.tw`;
        }
        return null;
    }

    get isClickable() {
        return this.props.service.state === "running" && this.serviceUrl;
    }

    get cardClass() {
        let cls = "o_woow_cloud_service_card";
        if (this.props.service.state === "error") {
            cls += " o_woow_cloud_service_card_error";
        }
        return cls;
    }

    onClick(ev) {
        // Navigate to service detail/settings
        if (this.props.onSettings) {
            this.props.onSettings();
        }
    }

    openWebUI(ev) {
        ev.stopPropagation();
        if (this.serviceUrl && this.props.service.state === "running") {
            window.open(this.serviceUrl, "_blank");
        }
        if (this.props.onOpen) {
            this.props.onOpen();
        }
    }

    openSettings(ev) {
        ev.stopPropagation();
        if (this.props.onSettings) {
            this.props.onSettings();
        }
    }

    onUrlClick(ev) {
        // Stop propagation to allow link click without triggering card click
        ev.stopPropagation();
    }
}

/** @odoo-module **/
import { Component } from "@odoo/owl";
import { StatusBadge } from "../common/StatusBadge";
import { WoowIcon } from "../icon/WoowIcon";
import { WoowButton } from "../button/WoowButton";

/**
 * SmartHomeCard Component
 *
 * Displays a smart home instance with status indicator, subdomain link,
 * and state badge.
 *
 * Usage:
 *   <SmartHomeCard
 *       home="homeData"
 *       domain="'woowtech.io'"
 *       onSelect="() => goToSmartHome(homeData.id)"
 *   />
 *
 * Props:
 *   - home (Object): Smart home data { id, name, state, subdomain, tunnel_id, ... }
 *   - domain (string): Platform domain for subdomain URLs
 *   - onSelect (Function, optional): Called when user clicks on the card
 */
export class SmartHomeCard extends Component {
    static template = "woow_paas_platform.SmartHomeCard";
    static components = { StatusBadge, WoowIcon, WoowButton };
    static props = {
        home: { type: Object },
        domain: { type: String, optional: true },
        onSelect: { type: Function, optional: true },
    };

    get statusDotClass() {
        const state = this.props.home.state;
        if (state === "connected" || state === "running") {
            return "o_woow_smart_home_dot o_woow_smart_home_dot_green";
        } else if (state === "error") {
            return "o_woow_smart_home_dot o_woow_smart_home_dot_red";
        }
        return "o_woow_smart_home_dot o_woow_smart_home_dot_gray";
    }

    get subdomainUrl() {
        const subdomain = this.props.home.subdomain;
        const domain = this.props.domain || "woowtech.io";
        if (subdomain) {
            return `https://${subdomain}.${domain}`;
        }
        return null;
    }

    get displayUrl() {
        const subdomain = this.props.home.subdomain;
        const domain = this.props.domain || "woowtech.io";
        if (subdomain) {
            return `${subdomain}.${domain}`;
        }
        return null;
    }

    get cardClass() {
        let cls = "o_woow_smart_home_card";
        if (this.props.home.state === "error") {
            cls += " o_woow_smart_home_card_error";
        }
        return cls;
    }

    onClick() {
        if (this.props.onSelect) {
            this.props.onSelect();
        }
    }

    onUrlClick(ev) {
        ev.stopPropagation();
    }
}

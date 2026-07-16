/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

export class AdminPasswordModal extends Component {
    static template = "woow_paas_platform.AdminPasswordModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        password: { type: String },
        onClose: { type: Function },
    };

    setup() {
        this.state = useState({ copied: false });
    }

    async copyPassword() {
        await navigator.clipboard.writeText(this.props.password);
        this.state.copied = true;
    }

    onBackdropClick(event) {
        if (event.target === event.currentTarget) {
            this.props.onClose();
        }
    }
}

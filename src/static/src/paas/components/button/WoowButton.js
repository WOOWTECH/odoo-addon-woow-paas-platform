/** @odoo-module **/
import { Component } from "@odoo/owl";

export class WoowButton extends Component {
    static template = "woow_paas_platform.WoowButton";
    static props = {
        variant: { type: String, optional: true }, // primary, secondary, ghost
        size: { type: String, optional: true }, // sm, md, lg
        onClick: { type: Function, optional: true },
        disabled: { type: Boolean, optional: true },
        slots: { type: Object, optional: true },
    };

    handleClick() {
        if (this.props.disabled) return;
        if (this.props.onClick) {
            this.props.onClick();
        }
    }
}

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
        // #1310：TopupStorePage／InvoiceDetailPage 一直有傳 class，但從未宣告也從未套用。
        class: { type: String, optional: true },
    };

    get buttonClass() {
        const base = 'o_woow_btn o_woow_btn_' + (this.props.variant || 'primary') + ' o_woow_btn_' + (this.props.size || 'md');
        return this.props.class ? base + ' ' + this.props.class : base;
    }

    handleClick(ev) {
        if (this.props.disabled) return;
        if (this.props.onClick) {
            this.props.onClick(ev);
        }
    }
}

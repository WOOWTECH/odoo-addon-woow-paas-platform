/** @odoo-module **/
import { Component } from "@odoo/owl";

export class WoowCard extends Component {
    static template = "woow_paas_platform.WoowCard";
    static props = {
        shadow: { type: Boolean, optional: true },
        class: { type: String, optional: true },
        slots: { type: Object, optional: true },
    };
}

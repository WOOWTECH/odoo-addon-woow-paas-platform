/** @odoo-module **/
import { Component } from "@odoo/owl";

export class WoowIcon extends Component {
    static template = "woow_paas_platform.WoowIcon";
    static props = {
        name: String,
        size: { type: Number, optional: true },
        filled: { type: Boolean, optional: true },
        class: { type: String, optional: true },
    };
}

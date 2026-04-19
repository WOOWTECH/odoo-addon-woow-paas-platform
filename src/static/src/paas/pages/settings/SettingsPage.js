/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { router } from "../../core/router";

export class SettingsPage extends Component {
    static template = "woow_paas_platform.SettingsPage";
    static components = { WoowCard, WoowIcon };
    static props = {};

    setup() {
        this.router = router;
    }

    goToPaymentMethods() {
        this.router.navigate("settings/payment-methods");
    }
}

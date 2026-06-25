/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { router } from "../../core/router";

export class NotFoundPage extends Component {
    static template = "woow_paas_platform.NotFoundPage";
    static components = { WoowButton, WoowIcon };

    goHome() {
        router.navigate("dashboard");
    }
}

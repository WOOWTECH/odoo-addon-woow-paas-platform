/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { router } from "../../core/router";
import { WoowIcon } from "../../components/icon/WoowIcon";

export class EmptyState extends Component {
    static template = "woow_paas_platform.EmptyState";
    static components = { WoowIcon };
    static props = {
        pageName: { type: String, optional: true },
    };

    setup() {
        this.router = useState(router);
    }

    get displayName() {
        if (this.props.pageName) {
            return this.props.pageName;
        }
        const route = this.router.routes.find(r => r.path === this.router.current);
        return route ? route.name : "Page";
    }
}

/** @odoo-module **/
import { Component } from "@odoo/owl";

export class Header extends Component {
    static template = "woow_paas_platform.Header";
    static props = { router: Object };

    get currentPageName() {
        const route = this.props.router.routes.find(r => r.path === this.props.router.current);
        return route ? route.name : "Dashboard";
    }
}

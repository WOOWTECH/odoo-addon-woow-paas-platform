/** @odoo-module **/
import { Component } from "@odoo/owl";

export class Sidebar extends Component {
    static template = "woow_paas_platform.Sidebar";
    static props = { router: Object };

    navItems = [
        { path: "dashboard", name: "Dashboard", icon: "dashboard" },
        { path: "workspaces", name: "Workspaces", icon: "workspaces" },
        { path: "billing", name: "Billing", icon: "payments" },
        { path: "settings", name: "Settings", icon: "settings" },
    ];
}

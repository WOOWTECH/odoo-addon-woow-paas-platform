/** @odoo-module **/
import { Component } from "@odoo/owl";
import { Sidebar } from "../sidebar/Sidebar";
import { Header } from "../header/Header";

export class AppShell extends Component {
    static template = "woow_paas_platform.AppShell";
    static components = { Sidebar, Header };
    static props = { router: Object, slots: { type: Object, optional: true } };
}

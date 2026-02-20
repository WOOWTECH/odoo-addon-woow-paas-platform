/** @odoo-module **/
import { Component } from "@odoo/owl";
import { Sidebar } from "../sidebar/Sidebar";
import { Header } from "../header/Header";
import { BottomNav } from "../bottom_nav/BottomNav";

export class AppShell extends Component {
    static template = "woow_paas_platform.AppShell";
    static components = { Sidebar, Header, BottomNav };
    static props = { router: Object, slots: { type: Object, optional: true } };
}

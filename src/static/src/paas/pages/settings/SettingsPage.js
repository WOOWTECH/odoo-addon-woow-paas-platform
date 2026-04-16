/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";

/**
 * SettingsPage
 * Entry page for user settings. Currently a placeholder — future settings
 * categories (Payment Methods, Notifications, API Keys, etc.) will be added
 * here as individual sub-pages.
 */
export class SettingsPage extends Component {
    static template = "woow_paas_platform.SettingsPage";
    static components = { WoowCard, WoowIcon };
    static props = {};
}

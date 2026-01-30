/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";

export class DashboardPage extends Component {
    static template = "woow_paas_platform.DashboardPage";
    static components = { WoowCard, WoowIcon, WoowButton };

    stats = [
        {
            label: "Members Overview",
            subtitle: "Team Management",
            icon: "group",
            color: "blue",
            items: [
                { label: "Total Members", value: "12" },
                { label: "Active Users", value: "8" },
                { label: "Pending Invites", value: "4" },
            ]
        },
        {
            label: "Billing Overview",
            subtitle: "Current Cycle",
            icon: "account_balance_wallet",
            color: "green",
            items: [
                { label: "Month Usage", value: "$245.00" },
                { label: "Credits Left", value: "$1,255.00" },
                { label: "Next Bill", value: "Nov 1st", highlight: true },
            ]
        },
        {
            label: "Workspace Overview",
            subtitle: "Pro Plan Limits",
            icon: "deployed_code",
            color: "purple",
            items: [
                { label: "Cloud Services", value: "5", max: "10" },
                { label: "Secure Tunnels", value: "3", max: "5" },
                { label: "Workspaces", value: "2", max: "3" },
            ]
        },
    ];

    activities = [
        { time: "10:48 AM", user: "Admin", action: "deployed", target: "Odoo Cloud" },
        { time: "10:41 AM", user: "Admin", action: "deployed", target: "Odoo Cloud" },
        { time: "10:27 AM", user: "Admin", action: "deployed", target: "Home Assistant" },
        { time: "10:21 AM", user: "Admin", action: "deployed", target: "Odoo Cloud" },
        { time: "10:01 AM", user: "Admin", action: "deployed", target: "Odoo Cloud" },
    ];

    quickAccess = [
        { name: "Odoo Cloud", icon: "cloud_circle", color: "purple", action: "Deploy" },
        { name: "Local Home Assistant", icon: "home_iot_device", color: "blue", tag: "Local", action: "Connect" },
        { name: "n8n Cloud", icon: "webhook", color: "orange", action: "Deploy" },
        { name: "Custom Local Server", icon: "dns", color: "slate", tag: "On-Prem", action: "Setup Tunnel" },
    ];
}

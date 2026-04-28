/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { profileService } from "../../services/profile_service";

export class DashboardPage extends Component {
    static template = "woow_paas_platform.DashboardPage";
    static components = { WoowCard, WoowIcon, WoowButton };

    setup() {
        this.profileService = useState(profileService);
        onMounted(() => profileService.fetch());
    }

    get welcomeName() {
        const name = this.profileService.partner?.name?.trim();
        return name || "there";
    }

    // Hotfix: mock data removed. Real wiring scheduled for sprint 3
    // (see docs/plans/2026-04-28-billing-issue-fix-design.md G4-P2).
    stats = [];
    activities = [];
    quickAccess = [];
}

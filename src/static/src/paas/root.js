/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { router } from "./core/router";
import { AppShell } from "./layout/app_shell/AppShell";
import { DashboardPage } from "./pages/dashboard/DashboardPage";
import { WorkspaceListPage } from "./pages/workspace/WorkspaceListPage";
import { WorkspaceDetailPage } from "./pages/workspace/WorkspaceDetailPage";
import { WorkspaceTeamPage } from "./pages/workspace/WorkspaceTeamPage";
import { EmptyState } from "./pages/empty/EmptyState";

export class Root extends Component {
    static template = "woow_paas_platform.Root";
    static components = {
        AppShell,
        DashboardPage,
        WorkspaceListPage,
        WorkspaceDetailPage,
        WorkspaceTeamPage,
        EmptyState,
    };
    static props = {};

    setup() {
        this.router = useState(router);
        onMounted(() => {
            this.router.init();
        });
    }

    get workspaceId() {
        return this.router.params.id ? parseInt(this.router.params.id, 10) : null;
    }
}

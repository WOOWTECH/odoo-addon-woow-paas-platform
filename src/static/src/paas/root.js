/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { router } from "./core/router";
import { cloudService } from "./services/cloud_service";
import { AppShell } from "./layout/app_shell/AppShell";
import { DashboardPage } from "./pages/dashboard/DashboardPage";
import { WorkspaceListPage } from "./pages/workspace/WorkspaceListPage";
import { WorkspaceDetailPage } from "./pages/workspace/WorkspaceDetailPage";
import { WorkspaceTeamPage } from "./pages/workspace/WorkspaceTeamPage";
import { AppMarketplacePage } from "./pages/marketplace/AppMarketplacePage";
import { AppConfigurationPage } from "./pages/configure/AppConfigurationPage";
import { ServiceDetailPage } from "./pages/service/ServiceDetailPage";
import { AiAssistantPage } from "./pages/ai-assistant/AiAssistantPage";
import { SupportProjectsPage } from "./pages/support-projects/SupportProjectsPage";
import { SupportTasksPage } from "./pages/support-tasks/SupportTasksPage";
import { EmptyState } from "./pages/empty/EmptyState";

export class Root extends Component {
    static template = "woow_paas_platform.Root";
    static components = {
        AppShell,
        DashboardPage,
        WorkspaceListPage,
        WorkspaceDetailPage,
        WorkspaceTeamPage,
        AppMarketplacePage,
        AppConfigurationPage,
        ServiceDetailPage,
        AiAssistantPage,
        SupportProjectsPage,
        SupportTasksPage,
        EmptyState,
    };
    static props = {};

    setup() {
        this.router = useState(router);
        onMounted(() => {
            this.router.init();
            // Initialize platform config (domain, etc.)
            cloudService.initConfig();
        });
    }

    get workspaceId() {
        return this.router.params.id ? parseInt(this.router.params.id, 10) : null;
    }

    get serviceId() {
        return this.router.params.serviceId ? parseInt(this.router.params.serviceId, 10) : null;
    }

    get templateId() {
        return this.router.params.templateId ? parseInt(this.router.params.templateId, 10) : null;
    }

    get serviceTab() {
        return this.router.params.tab || 'overview';
    }

    get taskId() {
        return this.router.params.taskId ? parseInt(this.router.params.taskId, 10) : null;
    }

    get conversationId() {
        return this.router.params.conversationId ? parseInt(this.router.params.conversationId, 10) : null;
    }
}

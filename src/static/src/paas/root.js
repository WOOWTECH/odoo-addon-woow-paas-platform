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
import { SupportProjectsPage } from "./pages/support-projects/SupportProjectsPage";
import { SupportTasksPage } from "./pages/support-tasks/SupportTasksPage";
import { TaskDetailPage } from "./pages/task-detail/TaskDetailPage";
import { AiChatPage } from "./pages/ai-chat/AiChatPage";
import { ProjectKanbanPage } from "./pages/project-kanban/ProjectKanbanPage";
import { ProjectDetailPage } from "./pages/project-detail/ProjectDetailPage";
import { SmartHomeDetailPage } from "./pages/smart-home/SmartHomeDetailPage";
import { SecurityAccessDetailPage } from "./pages/security-access/SecurityAccessDetailPage";
import { BillingOverviewPage } from "./pages/billing/BillingOverviewPage";
import { InvoiceListPage } from "./pages/billing/InvoiceListPage";
import { InvoiceDetailPage } from "./pages/billing/InvoiceDetailPage";
import { TopupStorePage } from "./pages/billing/TopupStorePage";
import { SubscriptionListPage } from "./pages/billing/SubscriptionListPage";
import { TransactionListPage } from "./pages/billing/TransactionListPage";
import { SettingsPage } from "./pages/settings/SettingsPage";
import { ProfilePage } from "./pages/settings/ProfilePage";
import { PaymentMethodsListPage } from "./pages/settings/PaymentMethodsListPage";
import { AddPaymentMethodPage } from "./pages/settings/AddPaymentMethodPage";
import { ReconcileReportPage } from "./pages/reconcile-report/ReconcileReportPage";
import { EmptyState } from "./pages/empty/EmptyState";
import { NotFoundPage } from "./pages/not-found/NotFoundPage";

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
        SupportProjectsPage,
        SupportTasksPage,
        TaskDetailPage,
        AiChatPage,
        ProjectKanbanPage,
        ProjectDetailPage,
        SmartHomeDetailPage,
        SecurityAccessDetailPage,
        BillingOverviewPage,
        InvoiceListPage,
        InvoiceDetailPage,
        TopupStorePage,
        SubscriptionListPage,
        TransactionListPage,
        SettingsPage,
        ProfilePage,
        PaymentMethodsListPage,
        AddPaymentMethodPage,
        ReconcileReportPage,
        EmptyState,
        NotFoundPage,
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

    get projectId() {
        return this.router.params.id ? parseInt(this.router.params.id, 10) : null;
    }

    get taskId() {
        return this.router.params.taskId ? parseInt(this.router.params.taskId, 10) : null;
    }

    get conversationId() {
        return this.router.params.conversationId ? parseInt(this.router.params.conversationId, 10) : null;
    }

    get homeId() {
        return this.router.params.homeId ? parseInt(this.router.params.homeId, 10) : null;
    }

    get securityAccessId() {
        return this.router.params.saId ? parseInt(this.router.params.saId, 10) : null;
    }

    get securityAccessTab() {
        return this.router.params.tab || 'overview';
    }

    get invoiceId() {
        return parseInt(this.router.params?.id) || 0;
    }
}

/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { AiChat } from "../../components/ai-chat/AiChat";
import { router } from "../../core/router";
import { supportService } from "../../services/support_service";
import { formatDate, getInitials } from "../../services/utils";

export class TaskDetailPage extends Component {
    static template = "woow_paas_platform.TaskDetailPage";
    static components = { WoowCard, WoowIcon, WoowButton, AiChat };
    static props = {
        taskId: { type: Number },
    };

    setup() {
        this.router = router;
        this.state = useState({
            task: null,
            loading: true,
            error: null,
            activeTab: "description",
        });

        onMounted(() => {
            this.loadTask();
        });
    }

    async loadTask() {
        this.state.loading = true;
        this.state.error = null;
        try {
            const result = await supportService.fetchTask(this.props.taskId);
            if (result.success) {
                this.state.task = result.data;
            } else {
                this.state.error = result.error || "Failed to load task";
            }
        } catch (err) {
            this.state.error = err.message || "Failed to load task";
        } finally {
            this.state.loading = false;
        }
    }

    setTab(tabName) {
        this.state.activeTab = tabName;
    }

    goBack() {
        this.router.navigate("ai-assistant/tasks");
    }

    async enableChat() {
        try {
            const result = await supportService.updateTask(this.props.taskId, {
                chat_enabled: true,
            });
            if (result.success) {
                await this.loadTask();
            } else {
                this.state.error = result.error || "Failed to enable chat";
            }
        } catch (err) {
            this.state.error = err.message || "Failed to enable chat";
        }
    }

    async toggleAutoReply() {
        const task = this.state.task;
        if (!task) return;
        try {
            const result = await supportService.updateTask(this.props.taskId, {
                ai_auto_reply: !task.ai_auto_reply,
            });
            if (result.success) {
                await this.loadTask();
            } else {
                this.state.error = result.error || "Failed to toggle auto-reply";
            }
        } catch (err) {
            this.state.error = err.message || "Failed to toggle auto-reply";
        }
    }

    getStatusClass(stageName) {
        const map = {
            "New": "o_woow_td_status--info",
            "In Progress": "o_woow_td_status--warning",
            "Review": "o_woow_td_status--warning",
            "Done": "o_woow_td_status--success",
            "Cancelled": "o_woow_td_status--muted",
        };
        return map[stageName] || "o_woow_td_status--info";
    }

    getPriorityStars(priority) {
        const level = parseInt(priority, 10) || 0;
        return [level >= 1, level >= 2, level >= 3];
    }

    formatDate(dateString) {
        return formatDate(dateString);
    }

    getInitials(name) {
        return getInitials(name);
    }
}

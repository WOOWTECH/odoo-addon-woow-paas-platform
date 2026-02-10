/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { AiChat } from "../../components/ai-chat/AiChat";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { aiService } from "../../services/ai_service";
import { router } from "../../core/router";

export class AiChatPage extends Component {
    static template = "woow_paas_platform.AiChatPage";
    static components = { AiChat, WoowIcon, WoowButton };
    static props = {
        conversationId: { type: Number },
    };

    setup() {
        this.state = useState({
            channelName: "",
            loading: true,
            error: null,
        });
        this.router = useState(router);

        onMounted(() => {
            this.loadChannel();
        });
    }

    async loadChannel() {
        this.state.loading = true;
        try {
            // Try to get channel info from chat history
            const result = await aiService.fetchChatHistory(this.props.conversationId);
            if (result.success) {
                this.state.channelName = result.data?.channel_name || `Chat #${this.props.conversationId}`;
            } else {
                this.state.channelName = `Chat #${this.props.conversationId}`;
            }
        } catch {
            this.state.channelName = `Chat #${this.props.conversationId}`;
        } finally {
            this.state.loading = false;
        }
    }

    goBack() {
        this.router.navigate("ai-assistant");
    }
}

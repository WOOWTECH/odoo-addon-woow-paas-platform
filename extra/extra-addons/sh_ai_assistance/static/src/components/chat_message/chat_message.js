import { Component, markup, useState } from "@odoo/owl";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";

export class ChatMessageComponent extends Component {
    static template = "sh_ai_assistance.ChatMessageTemplate";
    static props = {
        message: { type: Object },
        messageIndex: { type: Number, optional: true },
        currentUser: { type: Object, optional: true },
        currentLlm: { type: [Object, { value: null }], optional: true },
        availableModels: { type: Array, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            showDebugInfo: false,
        });
    }

    get isDebugMode() {
        // Check if Odoo debug mode is enabled (from environment)
        return !!this.env.debug;
    }

    get isAdmin() {
        // Check if current user has administrator privileges
        // isAdmin = true means user belongs to 'base.group_erp_manager' (Administrator)
        return user.isAdmin || false;
    }

    get hasDebugInfo() {
        // Show debug icon for assistant messages that have either query_details or final_query
        return (
            this.props.message.message_type === 'assistant' &&
            (this.props.message.query_details || 
             (this.props.message.debug_info && this.props.message.debug_info.final_query))
        );
    }

    get _activeQuery() {
        if (this.props.message.query_details) {
            return this.props.message.query_details;
        }
        if (this.props.message.debug_info && this.props.message.debug_info.final_query) {
            return this.props.message.debug_info.final_query;
        }
        return null;
    }

    get debugDomain() {
        return this._activeQuery?.domain || [];
    }

    get debugDomainFormatted() {
        return JSON.stringify(this.debugDomain, null, 2);
    }

    get debugModel() {
        return this._activeQuery?.model || '';
    }

    get debugFields() {
        return this._activeQuery?.fields || [];
    }

    get usagePromptTokens() {
        return this.props.message.prompt_tokens || 0;
    }

    get usageCompletionTokens() {
        return this.props.message.completion_tokens || 0;
    }

    get usageTotalTokens() {
        return this.props.message.total_tokens || 0;
    }

    get toolCallCount() {
        return this.props.message.tool_call_count || 0;
    }

    get executionTime() {
        const time = this.props.message.execution_time || 0;
        return time.toFixed(2);
    }
    
    get debugGroupBy(){
        return this._activeQuery?.group_by || "NO GROUPBY APPLIED";
    }

    get debugOperation(){
        return this._activeQuery?.operation || "NO OPERATION PERFORMED";
    }

    
    get debugFieldsFormatted() {
        if (!this.hasDebugInfo) return '';
        return JSON.stringify(this.debugFields, null, 2);
    }

    toggleDebugInfo() {
        this.state.showDebugInfo = !this.state.showDebugInfo;
    }

    // Removed bubbleClass - now using scoped CSS classes in template

    get authorName() {
        switch (this.props.message.message_type) {
            case 'user': return this.props.currentUser?.name || 'You';
            case 'assistant': {
                // Use the LLM that generated this specific message
                if (this.props.message.llm_details) {
                    return this.props.message.llm_details.name;
                }
                // Fallback to current LLM or default
                return this.props.currentLlm?.name || 'AI';
            }
            case 'system': return 'System';
            case 'error': return 'Error';
            default: return 'Message';
        }
    }

    get avatarUrl() {
        if (this.props.message.message_type === 'user' && this.props.currentUser?.id) {
            return `/web/image/res.users/${this.props.currentUser.id}/avatar_128`;
        }

        if (this.props.message.message_type === 'assistant' || (this.props.message.message_type === 'error' && this.props.message.llm_id)) {
            // Use the LLM that generated this specific message
            if (this.props.message.llm_details && this.props.message.llm_details.image) {
                return `/web/image/sh.ai.llm/${this.props.message.llm_details.id}/image`;
            }

            // Fallback to current LLM image if available
            if (this.props.currentLlm?.id && this.props.currentLlm?.image) {
                return `/web/image/sh.ai.llm/${this.props.currentLlm.id}/image`;
            }
        }

        // Default AI avatar
        return 'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAzNiAzNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHJlY3Qgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiByeD0iMTgiIGZpbGw9IiM2MzY2RjEiLz4KPHN2ZyB4PSI2IiB5PSI2IiB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIGZpbGw9IndoaXRlIj4KPHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTEyIDJMMTMuMDkgOC4yNkwyMCA5TDEzLjA5IDE1Ljc0TDEyIDIyTDEwLjkxIDE1Ljc0TDQgOUwxMC45MSA4LjI2TDEyIDJaIiBmaWxsPSJjdXJyZW50Q29sb3IiLz4KPC9zdmc+Cjwvc3ZnPgo8L3N2Zz4K';
    }

    get richContent() {
        if (this.props.message.message_type === 'assistant' && this.props.message.content) {
            // Convert markdown to HTML using marked.js
            try {
                const html = window.marked.parse(this.props.message.content);
                return markup(html);
            } catch (error) {
                console.error('Failed to parse markdown:', error);
                return markup(this.props.message.content);
            }
        }
        return this.props.message.content || "";
    }

    get timestamp() {
        // Convert create_date string to Date object with proper timezone handling
        if (this.props.message.create_date) {
            // Use Odoo's deserializeDateTime to handle timezone conversion
            try {
                return deserializeDateTime(this.props.message.create_date);
            } catch (error) {
                // Fallback to basic Date parsing if deserializeDateTime fails
                console.warn('Failed to deserialize datetime:', error);
                return new Date(this.props.message.create_date);
            }
        }
        return new Date(); // Fallback to current date
    }

    get formattedTime() {
        const date = this.timestamp;
        const now = new Date();
        const diffMinutes = Math.floor((now - date) / (1000 * 60));

        if (!this.props.message.create_date) {
            return 'Now';
        }

        if (diffMinutes < 0) {
            // Message is in the future (shouldn't happen, but handle gracefully)
            return 'Just now';
        } else if (diffMinutes < 1) {
            return 'Just now';
        } else if (diffMinutes < 60) {
            return `${diffMinutes}m ago`;
        } else if (diffMinutes < 1440) { // Less than 24 hours
            const hours = Math.floor(diffMinutes / 60);
            return `${hours}h ago`;
        } else {
            // More than 24 hours, show date and time
            return date.toLocaleString(undefined, {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
        }
    }

    get fullTimestamp() {
        return this.timestamp.toLocaleString(undefined, {
            year: 'numeric',
            month: 'long',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            timeZoneName: 'short'
        });
    }

    onChartTypeChange(chartType) {
        if (this.props.onChartTypeChange) {
            this.props.onChartTypeChange(chartType, this.props.messageIndex);
        }
    }

    async onOpenView(viewType) {
        try {
            // Call the backend method to get the action with specified view type
            const action = await this.orm.call(
                "sh.ai.chat.message",
                "open_records_action",
                [this.props.message.id, viewType]
            );

            console.log(`Opening ${viewType} view:`, action);

            // Ensure action has required fields
            if (!action || !action.res_model) {
                console.error("Invalid action received:", action);
                return;
            }

            // Execute the action to open records in specified view
            this.action.doAction(action);
        } catch (error) {
            console.error(`Error opening ${viewType} view:`, error);
        }
    }
}
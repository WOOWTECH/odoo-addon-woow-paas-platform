import { Component, useState, useRef } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

export class AiChatSidebarComponent extends Component {
    static template = "sh_ai_assistance.AiChatSidebarTemplate";
    static props = {
        sessions: { type: Array },
        currentSessionToken: { type: [String, { value: null }], optional: true },
        isCollapsed: { type: Boolean },
        isLoading: { type: Boolean },
        onNewChat: { type: Function },
        onSelectSession: { type: Function },
        onToggleSidebar: { type: Function },
        onDeleteSession: { type: Function },
        onImportSuccess: { type: Function, optional: true },
    };

    setup() {
        this.dialog = useService("dialog");
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.importFileInput = useRef("importFileInput");
        this.state = useState({
            hoveredSession: null,
        });
    }


    onNewChatClick() {
        this.props.onNewChat();
    }

    onSessionClick(sessionToken) {
        this.props.onSelectSession(sessionToken);
    }

    onDeleteClick(event, sessionToken) {
        event.stopPropagation(); // Prevent session selection

        this.dialog.add(ConfirmationDialog, {
            title: "Delete Chat",
            body: "This will permanently delete this chat session and all its messages. This action cannot be undone.",
            confirm: () => {
                this.props.onDeleteSession(sessionToken);
            },
            cancel: () => { },
        });
    }

    onSessionMouseEnter(sessionToken) {
        this.state.hoveredSession = sessionToken;
    }

    onSessionMouseLeave() {
        this.state.hoveredSession = null;
    }

    isSessionHovered(sessionToken) {
        return this.state.hoveredSession === sessionToken;
    }


    onToggleSidebarClick() {
        this.props.onToggleSidebar();
    }

    formatDate(dateStr) {
        const date = new Date(dateStr);
        const now = new Date();
        const diffTime = Math.abs(now - date);
        const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));

        if (diffDays === 1) {
            return 'Today';
        } else if (diffDays === 2) {
            return 'Yesterday';
        } else if (diffDays <= 7) {
            return `${diffDays - 1} days ago`;
        } else {
            return date.toLocaleDateString();
        }
    }

    isCurrentSession(sessionToken) {
        return this.props.currentSessionToken === sessionToken;
    }

    get shouldDisableNewChat() {
        // Disable if loading
        if (this.props.isLoading) {
            return true;
        }

        // Disable if ANY session is empty (has no messages)
        const emptySession = this.props.sessions.find(s => s.message_count === 0);
        if (emptySession) {
            return true;
        }

        return false;
    }

    get newChatButtonTooltip() {
        if (this.props.isLoading) {
            return "Creating new chat...";
        }

        const emptySession = this.props.sessions.find(s => s.message_count === 0);
        if (emptySession) {
            return "You already have an empty chat. Use it before creating a new one.";
        }

        return "Create a new chat session";
    }

    onImportChatClick() {
        if (this.importFileInput.el) {
            this.importFileInput.el.click();
        }
    }

    async onFileSelected(ev) {
        const file = ev.target.files[0];
        if (!file) return;

        try {
            const text = await file.text();
            const messages = JSON.parse(text);

            if (!Array.isArray(messages)) {
                this.notification.add("Invalid file format. Expected a JSON array.", { type: "danger" });
                return;
            }

            // Send to backend
            const result = await this.orm.call("sh.ai.chat.session", "import_session_from_json", [messages]);

            if (result && result.access_token) {
                this.notification.add("Chat imported successfully!", { type: "success" });

                // Notify parent to reload sessions and select the new one
                if (this.props.onImportSuccess) {
                    this.props.onImportSuccess(result.access_token);
                }
            } else {
                this.notification.add("Failed to import chat.", { type: "danger" });
            }

        } catch (error) {
            console.error("Failed to read file:", error);
            this.notification.add("Failed to read file. Please ensure it is a valid JSON.", { type: "danger" });
        } finally {
            // Reset input so same file can be selected again if needed
            ev.target.value = '';
        }
    }
}
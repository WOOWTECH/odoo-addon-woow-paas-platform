import { Component, useState, onWillUpdateProps } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { AiChatContentComponent } from "../ai_chat_content/ai_chat_content";
import { ShareSessionDialog } from "../share_session_dialog/share_session_dialog";

export class AiChatMainComponent extends Component {
    static template = "sh_ai_assistance.AiChatMainTemplate";
    static components = {
        AiChatContent: AiChatContentComponent,
        ShareSessionDialog,
    };
    static props = {
        sessionToken: { type: [String, { value: null }], optional: true },
        currentLlmCompany: { type: [String, { value: null }], optional: true },
        processingSessions: { type: Object },
        onSessionRenamed: { type: Function },
        onMessageSent: { type: Function },
        onModelChanged: { type: Function, optional: true },
        onSetSessionProcessing: { type: Function },
    };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.state = useState({
            currentSession: null,
            isLoading: false,
            error: null,
            isEditingName: false,
            editingName: '',
            availableModels: [],
            selectedModel: null,
        });

        // Watch for session token changes using proper OWL lifecycle
        onWillUpdateProps((nextProps) => {
            if (nextProps.sessionToken !== this.props.sessionToken) {
                console.log('Session token changed:', nextProps.sessionToken);
                this.loadSessionWithToken(nextProps.sessionToken);
            }
        });

        // Initial load
        this.loadSessionWithToken(this.props.sessionToken);
        this.loadAvailableModels();
    }

    async loadSessionWithToken(sessionToken) {
        if (!sessionToken) {
            this.state.currentSession = null;
            this.state.isLoading = false;
            this.state.error = null;
            return;
        }

        try {
            this.state.isLoading = true;
            this.state.error = null;

            console.log('Loading session with token:', sessionToken);

            // Find session by token
            const session = await this.orm.call("sh.ai.chat.session", "find_by_token", [sessionToken]);

            if (session) {
                this.state.currentSession = session;
                console.log('Session loaded successfully:', session.name, 'session.id:', session.id, 'llm_id:', session.llm_id);

                // Update selected model based on session
                this.updateSelectedModelFromSession();
            } else {
                this.state.error = "Session not found or access denied";
                this.state.currentSession = null;
                console.error('Session not found for token:', sessionToken);
            }

        } catch (error) {
            console.error("Failed to load session:", error);
            this.state.error = "Failed to load session";
            this.state.currentSession = null;
        } finally {
            this.state.isLoading = false;
        }
    }


    get hasSession() {
        return !!this.props.sessionToken && !!this.state.currentSession;
    }

    get isEmptyState() {
        return !this.props.sessionToken;
    }

    get hasError() {
        return !!this.state.error;
    }

    get isProcessing() {
        // Check if current session is processing
        return this.state.currentSession && !!this.props.processingSessions[this.state.currentSession.id];
    }

    startEditingName() {
        if (!this.state.currentSession) return;
        this.state.isEditingName = true;
        this.state.editingName = this.state.currentSession.name;
    }

    cancelEditingName() {
        this.state.isEditingName = false;
        this.state.editingName = '';
    }

    async saveSessionName() {
        if (!this.state.currentSession || !this.state.editingName.trim()) {
            this.cancelEditingName();
            return;
        }

        const newName = this.state.editingName.trim();
        if (newName === this.state.currentSession.name) {
            this.cancelEditingName();
            return;
        }

        try {
            await this.orm.write("sh.ai.chat.session", [this.state.currentSession.id], {
                name: newName
            });

            // Update local state
            this.state.currentSession.name = newName;
            this.cancelEditingName();

            // Notify parent to reload sessions
            this.props.onSessionRenamed();

        } catch (error) {
            console.error("Failed to rename session:", error);
            this.cancelEditingName();
        }
    }

    onNameInputKeydown(event) {
        if (event.key === 'Enter') {
            this.saveSessionName();
        } else if (event.key === 'Escape') {
            this.cancelEditingName();
        }
    }

    async onShareSession() {
        if (!this.state.currentSession) return;

        try {
            const snapshot_uuid = await this.orm.call(
                "sh.ai.chat.session",
                "create_session_snapshot",
                [this.state.currentSession.id]
            );

            const shareLink = `${window.location.origin}/web/snapshot/${snapshot_uuid}`;

            this.dialog.add(ShareSessionDialog, {
                shareLink: shareLink,
            });

        } catch (error) {
            console.error("Failed to create session snapshot:", error);
            this.notification.add("Failed to generate share link.", { type: "danger" });
        }
    }

    async onExportChat() {
        if (!this.state.currentSession) return;

        try {
            const url = `/ai/chat/export/${this.state.currentSession.id}`;
            window.location.href = url;
            this.notification.add("Downloading chat export...", { type: "info" });
        } catch (error) {
            console.error("Failed to export chat:", error);
            this.notification.add("Failed to export chat.", { type: "danger" });
        }
    }

    async loadAvailableModels() {
        try {
            const models = await this.orm.searchRead(
                "sh.ai.llm",
                [['active', '=', true]],
                ['id', 'name', 'sh_company', 'sh_model_code', 'active', 'image']
            );

            this.state.availableModels = models;

            // Set selected model based on current session or default
            this.updateSelectedModelFromSession();

            console.log('Loaded available models:', models);
        } catch (error) {
            console.error("Failed to load available models:", error);
        }
    }

    updateSelectedModelFromSession() {
        if (!this.state.currentSession) {
            // No session loaded, use first available model
            if (this.state.availableModels.length > 0) {
                this.state.selectedModel = this.state.availableModels[0];
            }
            return;
        }

        // Check if session has a saved model
        if (this.state.currentSession.llm_id && this.state.currentSession.llm_id !== false) {
            const modelId = Array.isArray(this.state.currentSession.llm_id)
                ? this.state.currentSession.llm_id[0]
                : this.state.currentSession.llm_id;

            const sessionModel = this.state.availableModels.find(m => m.id === modelId);
            if (sessionModel) {
                this.state.selectedModel = sessionModel;
                return;
            }
        }

        // Fallback to first available model if session model not found
        if (this.state.availableModels.length > 0) {
            this.state.selectedModel = this.state.availableModels[0];
        }
    }

    async selectModel(model) {
        if (model.id === this.state.selectedModel?.id || this.state.isProcessing) return;

        const previousModel = this.state.selectedModel;
        this.state.selectedModel = model;

        // Save model selection to current session
        if (this.state.currentSession) {
            try {
                console.log('Saving model to session:', this.state.currentSession.id, 'llm_id:', model.id);

                await this.orm.write("sh.ai.chat.session", [this.state.currentSession.id], {
                    llm_id: model.id
                });

                // Update local session data
                this.state.currentSession.llm_id = [model.id, model.name];

                console.log('Saved model selection to session:', model.name, 'Updated session data:', this.state.currentSession);

                // Notify parent to reload sessions (to update sidebar)
                if (this.props.onModelChanged) {
                    this.props.onModelChanged();
                }
            } catch (error) {
                console.error("Failed to save model selection:", error);
            }
        }

        console.log('Selected model:', model.name);

        // Show visual feedback
        this.notification.add(
            `Switched to ${model.name} (${model.sh_company})`,
            { type: "success", title: "AI Model Changed" }
        );
    }

    async refreshSessionData() {
        // Refresh current session data to update message count
        if (this.state.currentSession && this.props.sessionToken) {
            try {
                const session = await this.orm.call("sh.ai.chat.session", "find_by_token", [this.props.sessionToken]);
                if (session) {
                    this.state.currentSession = session;
                }
            } catch (error) {
                console.error("Failed to refresh session data:", error);
            }
        }
    }

    onMessageSent() {
        // Refresh session data when a message is sent to update message count
        this.refreshSessionData();

        // Call parent's onMessageSent if it exists
        if (this.props.onMessageSent) {
            this.props.onMessageSent();
        }
    }
}
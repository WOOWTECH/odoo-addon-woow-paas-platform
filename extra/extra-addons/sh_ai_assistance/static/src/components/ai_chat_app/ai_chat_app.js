import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
import { AiChatSidebarComponent } from "../ai_chat_sidebar/ai_chat_sidebar";
import { AiChatMainComponent } from "../ai_chat_main/ai_chat_main";
import { user } from "@web/core/user";

export class AiChatAppComponent extends Component {
    static template = "sh_ai_assistance.AiChatAppTemplate";
    static components = {
        AiChatSidebar: AiChatSidebarComponent,
        AiChatMain: AiChatMainComponent,
    };
    static props = {
        "*": true, // Accept any additional props from Odoo action system
    };

    setup() {
        this.orm = useService("orm");
        this.store = useService("mail.store");
        this.notification = useService("notification");
        this.urlMonitorInterval = null;

        this.state = useState({
            sidebarCollapsed: false,
            currentSessionToken: null,
            sessions: [],
            isLoading: false,
            processingSessions: {}, // Track which sessions are processing: { sessionId: true/false }
        });

        // Load sessions and check current URL for session token
        this.initializeApp();

        // Monitor URL integrity to ensure session parameter stays
        onMounted(() => {
            this.startUrlMonitoring();
        });

        onWillUnmount(() => {
            // Ensure interval is cleared to prevent memory leaks
            this.stopUrlMonitoring();
        });
    }

    get currentUser() {
        return {
            id: session.user_id || user.userId,
            name: session.partner_display_name || user.name || 'User',
        };
    }

    get currentLlmCompany() {
        // Get LLM company from the current session
        if (this.state.currentSessionToken) {
            const currentSession = this.state.sessions.find(s => s.access_token === this.state.currentSessionToken);
            if (currentSession && currentSession.llm_details) {
                return currentSession.llm_details.sh_company;
            }
        }
        return null;
    }

    async initializeApp() {
        this.state.isLoading = true;

        try {
            // Check URL for session token FIRST
            const urlParams = new URLSearchParams(window.location.search);

            const sessionToken = urlParams.get('session');

            // Load user's sessions
            await this.loadSessions();

            // If we have a session token from URL, validate it exists in our sessions
            if (sessionToken) {

                const sessionExists = this.state.sessions.find(s => s.access_token === sessionToken);

                if (sessionExists) {
                    this.state.currentSessionToken = sessionToken;
                    console.log('Restored session from URL:', sessionToken);
                    // Ensure URL is properly set (in case it got modified)
                    this.updateUrlWithSession(sessionToken);
                } else {
                    // Session token in URL doesn't exist, clear it
                    console.log('Session from URL not found, clearing:', sessionToken);
                    this.clearUrlSession();
                }
            }
        } catch (error) {
            console.error("Failed to initialize app:", error);
        } finally {
            this.state.isLoading = false;
        }
    }

    updateUrlWithSession(sessionToken) {
        if (!sessionToken) return;

        const url = new URL(window.location);
        const currentSessionParam = url.searchParams.get('session');

        // Only update URL if it's different to avoid unnecessary history changes
        if (currentSessionParam !== sessionToken) {
            url.searchParams.set('session', sessionToken);
            window.history.replaceState({}, '', url);
            console.log('Updated URL with session:', sessionToken);
        }
    }

    clearUrlSession() {
        const url = new URL(window.location);
        url.searchParams.delete('session');
        window.history.replaceState({}, '', url);
        this.state.currentSessionToken = null;
    }

    async loadSessions() {

        try {
            const sessions = await this.orm.searchRead(
                "sh.ai.chat.session",
                [['user_id', '=', this.currentUser.id]],
                ['id', 'name', 'access_token', 'last_message_date', 'message_count', 'llm_id'],
                { order: 'last_message_date desc' }
            );

            // Load LLM details for sessions that have an LLM assigned
            const llmIds = sessions.filter(s => s.llm_id).map(s => s.llm_id[0]);
            let llmDetails = {};

            if (llmIds.length > 0) {
                const uniqueLlmIds = [...new Set(llmIds)];
                const llms = await this.orm.searchRead(
                    "sh.ai.llm",
                    [['id', 'in', uniqueLlmIds]],
                    ['id', 'name', 'sh_company', 'image']
                );

                // Create lookup map
                llmDetails = llms.reduce((acc, llm) => {
                    acc[llm.id] = llm;
                    return acc;
                }, {});
            }

            // Enhance sessions with LLM details
            this.state.sessions = sessions.map(session => ({
                ...session,
                llm_details: session.llm_id ? llmDetails[session.llm_id[0]] : null
            }));

        } catch (error) {
            console.error("Failed to load sessions:", error);
        }
    }

    toggleSidebar() {
        this.state.sidebarCollapsed = !this.state.sidebarCollapsed;
    }

    async createNewSession() {
        try {
            // Prevent multiple rapid clicks during loading
            if (this.state.isLoading) {
                return;
            }

            // Check if ANY session is empty (has no messages)
            const emptySession = this.state.sessions.find(s => s.message_count === 0);
            if (emptySession) {
                // Found an empty session, don't create new one
                this.notification.add("Some Blank Chat already opened", {
                    type: 'info',
                });
                return;
            }

            this.state.isLoading = true;

            // Create new session (user_id is automatically set by server to current user)
            const sessionIds = await this.orm.create("sh.ai.chat.session", [{
                'name': 'New Chat',
            }]);

            // Get the created session with access token
            const sessions = await this.orm.read("sh.ai.chat.session", sessionIds,
                ['access_token', 'name']);

            const newSession = sessions[0];

            // Update URL and current session
            const url = new URL(window.location);
            url.searchParams.set('session', newSession.access_token);
            window.history.pushState({}, '', url);

            this.state.currentSessionToken = newSession.access_token;
            console.log('Created new session with token:', newSession.access_token);

            // Reload sessions list
            await this.loadSessions();

        } catch (error) {
            console.error("Failed to create new session:", error);
        } finally {
            this.state.isLoading = false;
        }
    }

    async selectSession(sessionToken) {
        if (sessionToken === this.state.currentSessionToken) return;

        this.state.isLoading = true;

        // Update URL using pushState for navigation history
        const url = new URL(window.location);
        url.searchParams.set('session', sessionToken);
        window.history.pushState({}, '', url);

        this.state.currentSessionToken = sessionToken;
        console.log('Selected session:', sessionToken);
        this.state.isLoading = false;
    }

    async deleteSession(sessionToken) {
        try {
            this.state.isLoading = true;

            // Find the session to delete
            const sessionToDelete = this.state.sessions.find(s => s.access_token === sessionToken);
            if (!sessionToDelete) {
                console.error('Session not found for deletion:', sessionToken);
                return;
            }

            // Delete the session from backend
            await this.orm.unlink("sh.ai.chat.session", [sessionToDelete.id]);

            // If we're deleting the current session, clear it
            if (this.state.currentSessionToken === sessionToken) {
                this.clearUrlSession();
            }

            // Reload sessions list
            await this.loadSessions();

            this.notification.add("Chat deleted successfully", {
                type: 'success',
            });

            console.log('Deleted session:', sessionToken);

        } catch (error) {
            console.error("Failed to delete session:", error);
            this.notification.add("Failed to delete chat", {
                type: 'danger',
            });
        } finally {
            this.state.isLoading = false;
        }
    }


    onSessionRenamed() {
        // Reload sessions when a session is renamed
        this.loadSessions();
    }

    onMessageSent() {
        // Reload sessions when a message is sent to update message_count
        this.loadSessions();
    }

    onModelChanged() {
        // Reload sessions when model is changed to update LLM details in sidebar
        this.loadSessions();
    }

    // Track processing state per session
    setSessionProcessing(sessionId, isProcessing) {
        console.log(`📊 Setting session ${sessionId} processing: ${isProcessing}`);
        this.state.processingSessions = {
            ...this.state.processingSessions,
            [sessionId]: isProcessing
        };
    }

    async onImportSuccess(newToken) {
        await this.loadSessions();
        if (newToken) {
            await this.selectSession(newToken);
        }
    }

    isSessionProcessing(sessionId) {
        return !!this.state.processingSessions[sessionId];
    }

    startUrlMonitoring() {
        // Check URL integrity every 2 seconds
        this.urlMonitorInterval = setInterval(() => {
            this.checkUrlIntegrity();
        }, 2000);
    }

    stopUrlMonitoring() {
        // Defensive cleanup to prevent memory leaks
        if (this.urlMonitorInterval) {
            clearInterval(this.urlMonitorInterval);
            this.urlMonitorInterval = null;
        }
    }

    checkUrlIntegrity() {

        const urlParams = new URLSearchParams(window.location.search);

        const urlSessionToken = urlParams.get('session');

        // If we have an active session but URL doesn't have the parameter
        if (this.state.currentSessionToken && !urlSessionToken) {
            console.log('URL lost session parameter, restoring:', this.state.currentSessionToken);
            this.updateUrlWithSession(this.state.currentSessionToken);
        }
        // If URL has a different session token than our state
        else if (urlSessionToken && urlSessionToken !== this.state.currentSessionToken) {
            console.log('URL session differs from state, syncing:', urlSessionToken);
            // Validate the URL token exists in our sessions
            const sessionExists = this.state.sessions.find(s => s.access_token === urlSessionToken);
            if (sessionExists) {
                this.state.currentSessionToken = urlSessionToken;
            } else {
                // Invalid session in URL, clear it
                this.clearUrlSession();
            }
        }
    }
}
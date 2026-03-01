import { Component, useState, useRef, onPatched, onWillUpdateProps, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
import { ChatMessageComponent } from "../chat_message/chat_message";
import { AiTypingIndicatorComponent } from "../ai_typing_indicator/ai_typing_indicator";

export class AiChatContentComponent extends Component {
    static template = "sh_ai_assistance.AiChatContentTemplate";
    static components = {
        ChatMessage: ChatMessageComponent,
        AiTypingIndicator: AiTypingIndicatorComponent,
    };
    static props = {
        sessionId: { type: Number },
        sessionToken: { type: String },
        selectedModel: { type: Object, optional: true },
        availableModels: { type: Array, optional: true },
        currentLlmCompany: { type: [String, { value: null }], optional: true },
        processingSessions: { type: Object },
        onSessionRenamed: { type: Function, optional: true },
        onMessageSent: { type: Function, optional: true },
        onSetSessionProcessing: { type: Function },
    };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.store = useService("mail.store");
        this.messagesContainerRef = useRef("messagesContainer");
        this.textInputRef = useRef("textInput");
        this.shouldAutoScroll = false;
        this.lastSessionId = null;
        this.question_list = [] ;
        this.pollingInterval = null; // For polling new messages during processing
        this.messageCountBeforeSend = 0; // Track message count to detect new AI response

        this.state = useState({
            messages: [],
            currentQuery: "",
        });

        // Auto-scroll to bottom when new message is added or session changes
        onPatched(() => {
            // Check if session has changed
            if (this.props.sessionId !== this.lastSessionId) {
                this.lastSessionId = this.props.sessionId;
                this.loadSessionMessages();
                return; // loadSessionMessages will set shouldAutoScroll
            }

            // Normal auto-scroll for new messages
            if (this.shouldAutoScroll) {
                this.scrollToBottom();
                this.shouldAutoScroll = false;
            }
        });

        // Watch for session changes and prepare for auto-scroll
        onWillUpdateProps((nextProps) => {
            if (nextProps.sessionId !== this.props.sessionId) {
                // Session is changing, we'll handle reload in onPatched
                console.log('Session changing from', this.props.sessionId, 'to', nextProps.sessionId);
            }
        });

        // Cleanup polling when component is destroyed
        onWillUnmount(() => {
            console.log('🧹 Component unmounting, cleaning up polling');
            this.stopPolling();
        });

        // Load session messages initially
        this.loadSessionMessages();
        this.lastSessionId = this.props.sessionId;
    }

    get currentUser() {
        return {
            id: this.store.self?.userId || session.uid || 1,
            name: this.store.self?.name || session.partner_display_name || session.name || 'User',
        };
    }

    get providerWarningMessage() {
        const company = this.props.currentLlmCompany;
        if (!company) return null;

        const companyLower = company.toLowerCase();

        if (companyLower.includes('google') || companyLower.includes('gemini')) {
            return "Google AI models may make mistakes, so double-check outputs.";
        } else if (companyLower.includes('openai') || companyLower.includes('open ai')) {
            return "ChatGPT models may make mistakes, so double-check outputs.";
        }

        return null;
    }

    get isProcessing() {
        // Check if current session is processing from parent state
        return !!this.props.processingSessions[this.props.sessionId];
    }

    async get_demo_questions(){
        let result = [] ; 
        if(this.props.sessionId){
            result = await this.orm.call(
                        "sh.ai.chat.session",
                        "get_demo_questions",
                        [this.props.sessionId]
                    );
            console.log("Shity RANDOM QUE : " , result.questions);
                
            return result.questions
        }
        else {
            result = [
                'How can I use this AI assistant?',
                'Show me my profile information',
                'What companies am I associated with?',
            ]
            console.log("RANDOM QUE : " , result);
            return result
        }
    }

    async loadSessionMessages() {
        if (!this.props.sessionId) {
            this.state.messages = [];
            this.question_list = await this.get_demo_questions();
            return;
        }

        try {
            const messages = await this.orm.searchRead("sh.ai.chat.message",
                [["session_id", "=", this.props.sessionId]],
                [
                    "message_type", "content", "create_date", "llm_id", "action_data", 
                    "debug_info", "query_details", "prompt_tokens", "completion_tokens", 
                    "total_tokens", "tool_call_count", "execution_time"
                ],
                { order: "create_date asc" }
            );

            // Only fetch demo questions if the chat is empty
            if (messages.length === 0) {
                this.question_list = await this.get_demo_questions();
            } else {
                this.question_list = [];
            }

            // Load LLM details for assistant messages that have an LLM assigned
            const llmIds = messages.filter(m => m.llm_id && m.message_type === 'assistant').map(m => m.llm_id[0]);
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

            // Enhance messages with LLM details
            this.state.messages = messages.map(message => ({
                ...message,
                llm_details: message.llm_id ? llmDetails[message.llm_id[0]] : null
            }));

            // Auto-scroll to bottom when messages are loaded (for chat opening)
            if (messages.length > 0) {
                this.shouldAutoScroll = true;
            }

            // Check if session is still processing (last message is user message waiting for AI response)
            if (messages.length > 0) {
                const lastMessage = messages[messages.length - 1];
                if (lastMessage.message_type === 'user') {
                    console.log('🔄 Detected pending user message on load, session is processing');
                    // Mark session as processing
                    this.props.onSetSessionProcessing(this.props.sessionId, true);

                    // Start polling for AI response (use current message count - 1 as initial since we already have user message)
                    const initialMessageCount = messages.length - 1;
                    this.startPolling(this.props.sessionId, initialMessageCount);
                }
            }

        } catch (error) {
            console.error("Failed to load messages:", error);
        }
    }

    startPolling(sessionId, initialMessageCount) {
        // Poll for new messages every 2 seconds while processing
        if (this.pollingInterval) {
            clearInterval(this.pollingInterval);
        }

        console.log(`🔄 Started polling for session ${sessionId}, initial messages: ${initialMessageCount}`);
        this.pollingInterval = setInterval(async () => {
            try {
                // Only poll if we're still on the same session and it's still processing
                if (this.props.sessionId === sessionId && this.isProcessing) {
                    console.log(`📡 Polling messages for session ${sessionId}`);
                    await this.loadSessionMessages();

                    // Check if we received a new AI response
                    const currentMessageCount = this.state.messages.length;
                    console.log(`📊 Messages: ${currentMessageCount} (initial: ${initialMessageCount})`);

                    if (currentMessageCount > initialMessageCount) {
                        const lastMessage = this.state.messages[currentMessageCount - 1];
                        console.log(`🔍 Last message type: ${lastMessage?.message_type}`);

                        if (lastMessage && lastMessage.message_type === 'assistant') {
                            console.log('✅ AI response detected, stopping processing');
                            this.props.onSetSessionProcessing(sessionId, false);
                            this.stopPolling();

                            // Notify parent about message sent
                            if (this.props.onMessageSent) {
                                this.props.onMessageSent();
                            }
                        }
                    }
                } else {
                    // Session changed or processing stopped, stop polling
                    console.log(`⏹️ Stopping poll: session=${this.props.sessionId}, target=${sessionId}, processing=${this.isProcessing}`);
                    this.stopPolling();
                }
            } catch (error) {
                // Component might be destroyed, stop polling
                console.log('⚠️ Polling error (component likely destroyed), stopping:', error.message);
                this.stopPolling();
            }
        }, 2000);
    }

    stopPolling() {
        if (this.pollingInterval) {
            console.log('🛑 Stopped polling');
            clearInterval(this.pollingInterval);
            this.pollingInterval = null;
        }
    }

    async sendMessage() {
        if (!this.state.currentQuery.trim() || this.isProcessing) {
            return;
        }

        if (!this.props.sessionId) {
            console.error("No session ID available for sending message");
            this.notification.add("No active session. Please create a new conversation.", { type: "warning" });
            return;
        }

        const userMessage = this.state.currentQuery.trim();
        const sessionId = this.props.sessionId; // Capture sessionId at the time of sending
        const initialMessageCount = this.state.messages.length; // Track count before sending
        this.state.currentQuery = "";
        this.shouldAutoScroll = true;

        // Set this session as processing
        this.props.onSetSessionProcessing(sessionId, true);

        // Scroll to bottom when typing indicator appears
        setTimeout(() => {
            this.scrollToBottom();
        }, 100);

        try {
            console.log('📤 Sending message with sessionId:', sessionId, 'current messages:', initialMessageCount);

            // Create user message
            await this.orm.create("sh.ai.chat.message", [{
                session_id: sessionId,
                message_type: "user",
                content: userMessage,
            }]);

            // Reload messages immediately to show user message
            await this.loadSessionMessages();

            // Start polling for new messages (AI response), pass initial count
            this.startPolling(sessionId, initialMessageCount);

            // Call backend to process message with LLM API (runs in background)
            try {
                const result = await this.orm.call(
                    "sh.ai.chat.message",
                    "process_user_message",
                    [sessionId, userMessage]
                );

                if (result.error) {
                    this.notification.add(result.error, { type: "danger" });
                }

                // Reload messages to show AI response
                await this.loadSessionMessages();

                if (this.props.onMessageSent) {
                    this.props.onMessageSent();
                }
            } catch (error) {
                console.error("Failed to get AI response:", error);
                this.notification.add("Failed to get AI response: " + error.message, { type: "danger" });

                // On error, stop processing immediately
                this.props.onSetSessionProcessing(sessionId, false);
                this.stopPolling();
            } finally {
                // Only stop processing if we're still on the same session
                // (polling might have already stopped it if AI response was detected)
                if (this.props.sessionId === sessionId && this.isProcessing) {
                    console.log('⚠️ Finally block: processing still active, stopping it');
                    this.props.onSetSessionProcessing(sessionId, false);
                    this.stopPolling();
                }
                this.shouldAutoScroll = true;
            }

        } catch (error) {
            console.error("Failed to send message:", error);

            let errorMessage = "Failed to send message";
            if (error.message) {
                errorMessage += ": " + error.message;
            }

            this.notification.add(errorMessage, { type: "danger" });

            // Mark session as no longer processing
            this.props.onSetSessionProcessing(sessionId, false);
            this.stopPolling();
        }
    }

    onKeyDown(event) {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            this.sendMessage();
        }
    }

    scrollToBottom() {
        if (this.messagesContainerRef.el) {
            this.messagesContainerRef.el.scrollTop = this.messagesContainerRef.el.scrollHeight;
        }
    }

    onInput(event) {
        this.state.currentQuery = event.target.value;
    }

    onInputChange(event) {
        this.state.currentQuery = event.target.value;
    }

    onKeyPress(event) {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            this.sendMessage();
        }
    }

    selectDemoQuestion(question) {
        this.state.currentQuery = question;
    }
}
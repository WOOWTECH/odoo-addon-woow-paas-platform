/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillUnmount } from "@odoo/owl";
import { AiMentionDropdown } from "../ai-mention/AiMentionDropdown";
import { aiService } from "../../services/ai_service";

/**
 * AiChat
 *
 * Core chat component with message list, input box, SSE streaming,
 * file upload, and @-mention agent selection.
 *
 * @prop {number} channelId - The discuss.channel ID
 * @prop {boolean} [autoReply=false] - Whether to auto-trigger AI streaming after sending
 */
export class AiChat extends Component {
    static template = "woow_paas_platform.AiChat";
    static components = { AiMentionDropdown };
    static props = {
        channelId: { type: Number },
        autoReply: { type: Boolean, optional: true },
    };

    setup() {
        this.state = useState({
            messages: [],
            inputText: "",
            loading: true,
            sending: false,
            streaming: false,
            streamingText: "",
            agents: [],
            mentionVisible: false,
            mentionQuery: "",
            selectedAgentId: null,
            uploadingFile: false,
            error: null,
        });

        this.messageListRef = useRef("messageList");
        this.textareaRef = useRef("textarea");
        this.fileInputRef = useRef("fileInput");
        this.eventSource = null;
        this._mentionSelectedIndex = 0;

        onMounted(async () => {
            await this.loadAgents();
            await this.loadHistory();
            this.scrollToBottom();
        });

        onWillUnmount(() => {
            this.closeStream();
        });
    }

    // ==================== Data Loading ====================

    /**
     * Load available AI agents from the service.
     */
    async loadAgents() {
        await aiService.fetchAgents();
        this.state.agents = [...aiService.agents];
    }

    /**
     * Load chat message history from the API.
     */
    async loadHistory() {
        this.state.loading = true;
        this.state.error = null;
        try {
            const result = await aiService.fetchChatHistory(this.props.channelId);
            if (result.success) {
                this.state.messages = result.data || [];
            } else {
                this.state.error = result.error || "Failed to load chat history";
            }
        } catch (err) {
            this.state.error = err.message || "Failed to load chat history";
        } finally {
            this.state.loading = false;
            this.scrollToBottom();
        }
    }

    // ==================== Messaging ====================

    /**
     * Send the current input text as a message.
     * If autoReply is enabled, triggers SSE streaming after sending.
     */
    async sendMessage() {
        const text = this.state.inputText.trim();
        if (!text || this.state.sending) {
            return;
        }

        this.state.sending = true;
        this.state.error = null;

        try {
            const result = await aiService.postMessage(
                this.props.channelId,
                text,
                this.state.selectedAgentId,
            );
            if (result.success && result.data) {
                this.state.messages.push({
                    id: result.data.id,
                    body: result.data.body,
                    author_name: result.data.author_name,
                    author_id: result.data.author_id,
                    date: result.data.date,
                    is_ai: false,
                    message_type: "comment",
                    attachments: [],
                });
                this.state.inputText = "";
                this.state.selectedAgentId = null;
                this.scrollToBottom();

                // Auto-trigger streaming AI reply
                if (this.props.autoReply) {
                    this.startStream();
                }
            } else {
                this.state.error = result.error || "Failed to send message";
            }
        } catch (err) {
            this.state.error = err.message || "Failed to send message";
        } finally {
            this.state.sending = false;
        }
    }

    // ==================== SSE Streaming ====================

    /**
     * Start an SSE connection to stream the AI response.
     */
    startStream() {
        this.closeStream();
        this.state.streaming = true;
        this.state.streamingText = "";

        const url = aiService.getStreamUrl(this.props.channelId);
        this.eventSource = new EventSource(url);

        this.eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);

                if (data.error) {
                    this.state.error = data.error;
                    this.closeStream();
                    return;
                }

                if (data.chunk) {
                    this.state.streamingText += data.chunk;
                    this.scrollToBottom();
                }

                if (data.warning) {
                    this.state.error = data.warning;
                }

                if (data.done) {
                    // Add the complete AI message to the list
                    if (this.state.streamingText) {
                        this.state.messages.push({
                            id: Date.now(),
                            body: data.full_response || this.state.streamingText,
                            author_name: "AI Assistant",
                            author_id: null,
                            date: new Date().toISOString(),
                            is_ai: true,
                            message_type: "comment",
                            attachments: [],
                        });
                    }
                    this.closeStream();
                    this.scrollToBottom();
                }
            } catch (parseErr) {
                console.warn("Failed to parse SSE chunk:", event.data, parseErr);
            }
        };

        this.eventSource.onerror = () => {
            const hadContent = !!this.state.streamingText;
            const partialText = this.state.streamingText;
            this.closeStream();
            if (hadContent) {
                this.state.messages.push({
                    id: Date.now(),
                    body: partialText,
                    author_name: "AI Assistant",
                    author_id: null,
                    date: new Date().toISOString(),
                    is_ai: true,
                    message_type: "comment",
                    attachments: [],
                });
            }
            this.state.error = "Connection to AI was lost. Please try sending your message again.";
        };
    }

    /**
     * Close the active EventSource connection and reset streaming state.
     */
    closeStream() {
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
        this.state.streaming = false;
        this.state.streamingText = "";
    }

    // ==================== Input Handling ====================

    /**
     * Handle keydown events in the textarea.
     * Enter sends the message; Shift+Enter inserts a newline.
     * Also delegates to mention dropdown keyboard navigation.
     * @param {KeyboardEvent} ev
     */
    handleKeydown(ev) {
        // Delegate to mention dropdown if visible
        if (this.state.mentionVisible) {
            if (["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(ev.key)) {
                // The AiMentionDropdown handles these via its own handleKeydown
                // We call it through a ref or direct method
                this._mentionKeydown(ev);
                return;
            }
        }

        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.sendMessage();
        }
    }

    /**
     * Handle input events on the textarea.
     * Detects '@' for mention triggering and auto-resizes.
     * @param {InputEvent} ev
     */
    handleInput(ev) {
        this.state.inputText = ev.target.value;
        this.detectMention(ev.target);
        this.autoResize(ev.target);
    }

    /**
     * Detect '@' character in the textarea to trigger mention dropdown.
     * @param {HTMLTextAreaElement} textarea
     */
    detectMention(textarea) {
        const value = textarea.value;
        const cursorPos = textarea.selectionStart;
        const textBeforeCursor = value.substring(0, cursorPos);

        // Look for @ followed by optional text (no space after @)
        const mentionMatch = textBeforeCursor.match(/@(\w*)$/);
        if (mentionMatch) {
            this.state.mentionVisible = true;
            this.state.mentionQuery = mentionMatch[1];
        } else {
            this.state.mentionVisible = false;
            this.state.mentionQuery = "";
        }
    }

    /**
     * Auto-resize the textarea based on content.
     * @param {HTMLTextAreaElement} textarea
     */
    autoResize(textarea) {
        textarea.style.height = "auto";
        const maxHeight = 150;
        textarea.style.height = Math.min(textarea.scrollHeight, maxHeight) + "px";
    }

    /**
     * Forward keydown events to the mention dropdown component.
     * This is used internally when the mention dropdown is visible.
     * @param {KeyboardEvent} ev
     */
    _mentionKeydown(ev) {
        const agents = this._getFilteredAgents();
        if (!agents.length) {
            return;
        }

        switch (ev.key) {
            case "ArrowDown":
                ev.preventDefault();
                this._mentionSelectedIndex =
                    ((this._mentionSelectedIndex || 0) + 1) % agents.length;
                break;
            case "ArrowUp":
                ev.preventDefault();
                this._mentionSelectedIndex =
                    ((this._mentionSelectedIndex || 0) - 1 + agents.length) % agents.length;
                break;
            case "Enter":
                ev.preventDefault();
                this.onAgentSelect(agents[this._mentionSelectedIndex || 0]);
                break;
            case "Escape":
                ev.preventDefault();
                this.closeMention();
                break;
        }
    }

    /**
     * Get filtered agents based on current mention query.
     * @returns {Array}
     */
    _getFilteredAgents() {
        const query = (this.state.mentionQuery || "").toLowerCase().trim();
        if (!query) {
            return this.state.agents;
        }
        return this.state.agents.filter((agent) => {
            const displayName = (agent.agent_display_name || agent.name || "").toLowerCase();
            const name = (agent.name || "").toLowerCase();
            return displayName.includes(query) || name.includes(query);
        });
    }

    // ==================== Mention Selection ====================

    /**
     * Handle agent selection from the mention dropdown.
     * Inserts the agent @mention into the textarea at the cursor position.
     * @param {Object} agent - The selected agent record
     */
    onAgentSelect(agent) {
        const textarea = this.textareaRef.el;
        if (!textarea) {
            return;
        }

        const value = textarea.value;
        const cursorPos = textarea.selectionStart;
        const textBeforeCursor = value.substring(0, cursorPos);

        // Find the @ position
        const atIndex = textBeforeCursor.lastIndexOf("@");
        if (atIndex === -1) {
            return;
        }

        const displayName = agent.agent_display_name || agent.name;
        const before = value.substring(0, atIndex);
        const after = value.substring(cursorPos);
        const newText = before + "@" + displayName + " " + after;

        this.state.inputText = newText;
        this.state.selectedAgentId = agent.id;
        this.closeMention();

        // Set cursor position after the inserted mention
        requestAnimationFrame(() => {
            if (textarea) {
                const newCursorPos = atIndex + displayName.length + 2; // +2 for @ and space
                textarea.selectionStart = newCursorPos;
                textarea.selectionEnd = newCursorPos;
                textarea.focus();
            }
        });
    }

    /**
     * Close the mention dropdown.
     */
    closeMention() {
        this.state.mentionVisible = false;
        this.state.mentionQuery = "";
        this._mentionSelectedIndex = 0;
    }

    // ==================== File Upload ====================

    /**
     * Trigger the hidden file input click.
     */
    triggerFileUpload() {
        const fileInput = this.fileInputRef.el;
        if (fileInput) {
            fileInput.click();
        }
    }

    /**
     * Handle file selection and upload via multipart form data.
     * @param {Event} ev - The change event from the file input
     */
    async handleFileUpload(ev) {
        const file = ev.target.files[0];
        if (!file) {
            return;
        }

        this.state.uploadingFile = true;
        this.state.error = null;

        try {
            const formData = new FormData();
            formData.append("channel_id", this.props.channelId);
            formData.append("file", file);
            formData.append("csrf_token", odoo.csrf_token || "");

            const response = await fetch("/api/ai/chat/upload", {
                method: "POST",
                body: formData,
            });

            const result = await response.json();

            if (result.success && result.data) {
                // Add the uploaded message to the list
                this.state.messages.push({
                    id: result.data.message_id,
                    body: "Uploaded: " + file.name,
                    author_name: "You",
                    author_id: null,
                    date: new Date().toISOString(),
                    is_ai: false,
                    message_type: "comment",
                    attachments: [result.data.attachment],
                });
                this.scrollToBottom();
            } else {
                this.state.error = result.error || "Upload failed";
            }
        } catch (err) {
            this.state.error = err.message || "Upload failed";
        } finally {
            this.state.uploadingFile = false;
            // Reset file input
            ev.target.value = "";
        }
    }

    // ==================== UI Helpers ====================

    /**
     * Scroll the message list to the bottom.
     */
    scrollToBottom() {
        requestAnimationFrame(() => {
            const el = this.messageListRef.el;
            if (el) {
                el.scrollTop = el.scrollHeight;
            }
        });
    }

    /**
     * Format a datetime string for display.
     * @param {string} dateStr - ISO datetime string
     * @returns {string} Formatted time string
     */
    formatTime(dateStr) {
        if (!dateStr) {
            return "";
        }
        try {
            const date = new Date(dateStr);
            return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        } catch {
            return "";
        }
    }

    /**
     * Get the initial letter for the message author avatar.
     * @param {Object} msg - The message object
     * @returns {string} Single uppercase character
     */
    getAuthorInitial(msg) {
        const name = msg.author_name || "?";
        return name.charAt(0).toUpperCase();
    }

    /**
     * Check if the message list has any messages.
     * @returns {boolean}
     */
    get hasMessages() {
        return this.state.messages.length > 0;
    }

    /**
     * Determine if the send button should be disabled.
     * @returns {boolean}
     */
    get sendDisabled() {
        return !this.state.inputText.trim() || this.state.sending || this.state.streaming;
    }
}

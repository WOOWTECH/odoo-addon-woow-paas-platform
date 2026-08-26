/** @odoo-module **/

import {
  Component,
  useState,
  useRef,
  onMounted,
  onWillUnmount,
  onPatched,
} from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { AiMentionDropdown } from "../ai-mention/AiMentionDropdown";
import { aiService } from "../../services/ai_service";
import { openAiStream } from "../../services/ai_stream";
import { safeHtml } from "../../services/html_sanitize";
import { parseMarkdown } from "../../services/markdown_parser";
import { renderMermaidBlocks } from "../../services/mermaid_loader";
import { formatUserTime } from "../../utils/datetime_helpers";

const ERROR_MESSAGES = {
  channel_not_found: _t("This chat channel no longer exists. Please refresh the page."),
  no_agent: _t(
    "No AI assistant is available. Please ask your administrator to set one up.",
  ),
  provider_not_configured: _t(
    "The AI provider has not been configured yet. Please contact your administrator.",
  ),
  no_message: _t("Message not found. Please send it again."),
  access_denied: _t("You do not have permission to access this chat channel."),
  csrf_error: _t("Request validation failed. Please refresh the page."),
  // #129: agent-runtime 空回應（MiniMax 等串流異常）→ 可安全重試。
  llm_empty_response: _t(
    "The AI assistant did not reply this time. Please tap Retry to try again.",
  ),
};

/**
 * AiChat
 *
 * Core chat component with message list, input box, SSE streaming,
 * file upload, and @-mention assistant selection.
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
      streamingToolCalls: [],
      assistants: [],
      mentionVisible: false,
      mentionQuery: "",
      selectedAssistantId: null,
      uploadingFile: false,
      error: null,
      // #129: 是否顯示「重試」鈕。預設 true（保留既有行為，零回歸）；agent-runtime
      // error frame 帶 retryable:false 時才隱藏（如不可重試的終態失敗）。
      errorRetryable: true,
      connectionState: "idle", // idle | connecting | spawning | connected | streaming | error | reconnecting
    });

    this.messageListRef = useRef("messageList");
    this.textareaRef = useRef("textarea");
    this.fileInputRef = useRef("fileInput");
    this.eventSource = null;
    this._mentionSelectedIndex = 0;
    this._reconnectAttempts = 0;
    this._reconnectTimer = null;
    this._maxReconnectAttempts = 3;
    this._connectedTimer = null;
    this._consecutiveParseErrors = 0;
    this._toolCallCounter = 0;

    onMounted(async () => {
      await this.loadAssistants();
      await this.loadHistory();
      this.scrollToBottom();
    });

    onWillUnmount(() => {
      this.closeStream();
      this._clearReconnectTimer();
      if (this._connectedTimer) {
        clearTimeout(this._connectedTimer);
      }
    });

    onPatched(() => {
      this._renderMermaidInMessages();
    });
  }

  // ==================== Data Loading ====================

  /**
   * Load available AI assistants from the service.
   */
  async loadAssistants() {
    try {
      await aiService.fetchAssistants();
      this.state.assistants = [...aiService.assistants];
    } catch (err) {
      console.error("Failed to load AI assistants:", err);
      this.state.error = _t(
        "Could not load the AI assistant list. Please refresh the page.",
      );
    }
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
        this.state.messages = (result.data || []).map((msg) => {
          // AI messages are in Markdown, user messages may contain HTML
          const body = msg.body
            ? msg.is_ai
              ? parseMarkdown(msg.body)
              : safeHtml(msg.body)
            : "";
          return {
            ...msg,
            body,
          };
        });
      } else {
        this.state.error = result.error || _t("Failed to load chat history");
      }
    } catch (err) {
      this.state.error = err.message || _t("Failed to load chat history");
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
      const result = await aiService.postMessage(this.props.channelId, text);
      if (result.success && result.data) {
        this.state.messages.push({
          ...result.data,
          body: result.data.body ? safeHtml(result.data.body) : "",
          is_ai: false,
          message_type: "comment",
          attachments: result.data.attachments || [],
        });
        this.state.inputText = "";
        this.state.selectedAssistantId = null;
        this.scrollToBottom();

        // Auto-trigger streaming AI reply
        if (this.props.autoReply) {
          this.startStream();
        }
      } else {
        this.state.error = result.error || _t("Failed to send message");
      }
    } catch (err) {
      this.state.error = err.message || _t("Failed to send message");
    } finally {
      this.state.sending = false;
    }
  }

  // ==================== SSE Streaming ====================

  /**
   * Start an SSE connection to stream the AI response.
   */
  startStream() {
    // Pre-flight: validate channelId
    const channelId = this.props.channelId;
    if (!channelId || typeof channelId !== "number" || channelId <= 0) {
      this.state.error = _t(
        'Chat is not enabled for this task yet. Please tap "Enable chat" first.',
      );
      this.state.connectionState = "error";
      return;
    }

    this.state.connectionState = "connecting";
    this.closeStream();
    this.state.streaming = true;
    this.state.streamingText = "";
    this.state.errorRetryable = true; // #129: 每次串流重置，避免上一輪 retryable:false 殘留

    this.eventSource = openAiStream(this.props.channelId, {
      onError: (data) => {
        this.state.error = ERROR_MESSAGES[data.error_code] || data.error;
        this.state.errorRetryable = data.retryable !== false;
        if (this.state.errorRetryable) {
          const toolCalls = this.state.streamingToolCalls.length
            ? [...this.state.streamingToolCalls]
            : null;
          if (this.state.streamingText || toolCalls) {
            const aiMsg = this._createAiMessage(
              this.state.streamingText,
              data.message_id,
            );
            if (toolCalls) {
              aiMsg.toolCalls = toolCalls;
            }
            this.state.messages.push(aiMsg);
          }
        }
        this.state.connectionState = "error";
        this.closeStream();
      },
      onSpawning: () => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        this.state.connectionState = "spawning";
        this.state.error = null;
      },
      onToolCall: (data) => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        if (this.state.connectionState !== "streaming") {
          this.state.connectionState = "streaming";
        }
        this.state.streamingToolCalls.push({
          id: ++this._toolCallCounter,
          tool: data.tool,
          args: data.args || {},
          result: null,
          expanded: false,
        });
        this.scrollToBottom();
      },
      onToolResult: (data) => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        const tc = [...this.state.streamingToolCalls]
          .reverse()
          .find((t) => t.tool === data.tool && t.result === null);
        if (tc) {
          tc.result = data.result || "";
        }
        this.scrollToBottom();
      },
      onToolError: (data) => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        const tc = [...this.state.streamingToolCalls]
          .reverse()
          .find((t) => t.tool === data.tool && t.result === null);
        if (tc) {
          tc.result = null;
          tc.error = data.error || _t("Tool execution failed");
        }
        this.scrollToBottom();
      },
      onChunk: (data) => {
        this._reconnectAttempts = 0;
        this._consecutiveParseErrors = 0;
        if (this.state.connectionState !== "streaming") {
          this.state.connectionState = "streaming";
        }
        this.state.streamingText += data.chunk;
        this.scrollToBottom();
      },
      onWarning: (data) => {
        this.state.error = data.warning;
      },
      onDone: (data) => {
        const toolCalls = this.state.streamingToolCalls.length
          ? [...this.state.streamingToolCalls]
          : null;
        const hadContent = this.state.streamingText || toolCalls;
        // #129 INVARIANT: 最終訊息以 done frame 的 full_response 為準
        // （streamingText 僅作 fallback）— 不依賴逐 delta。
        if (hadContent) {
          const aiMsg = this._createAiMessage(
            data.full_response || this.state.streamingText,
            data.message_id,
          );
          if (toolCalls) {
            aiMsg.toolCalls = toolCalls;
          }
          this.state.messages.push(aiMsg);
        }
        this.closeStream();
        this.state.connectionState = "connected";
        this._connectedTimer = setTimeout(() => {
          if (this.state.connectionState === "connected") {
            this.state.connectionState = "idle";
          }
        }, 2000);
        this.scrollToBottom();
      },
      onParseError: (raw, err) => {
        console.error("Failed to parse SSE data:", raw, err);
        this._consecutiveParseErrors++;
        if (this._consecutiveParseErrors >= 3) {
          this.state.error = _t(
            "Something went wrong while receiving the AI reply. Please refresh the page.",
          );
          this.state.connectionState = "error";
          this.closeStream();
        }
      },
      onConnectionError: (event) => {
        console.error("SSE connection error:", event);
        const hadContent = !!this.state.streamingText;
        const partialText = this.state.streamingText;
        this.closeStream();
        if (hadContent) {
          this.state.messages.push(
            this._createAiMessage(
              partialText +
                "\n\n⚠️ " +
                _t("(This reply is incomplete because the connection was interrupted.)"),
            ),
          );
          this.state.error = _t(
            "The AI reply was cut off by a connection drop. Send your message again to get the full reply.",
          );
          this.state.connectionState = "error";
        } else {
          this.state.connectionState = "reconnecting";
          this._scheduleReconnect();
        }
      },
    });
  }

  /**
   * Close the active EventSource connection and reset streaming state.
   */
  closeStream() {
    this._clearReconnectTimer();
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    this.state.streaming = false;
    this.state.streamingText = "";
    this.state.streamingToolCalls = [];
  }

  _scheduleReconnect() {
    if (this._reconnectAttempts >= this._maxReconnectAttempts) {
      this.state.error = _t(
        "Could not connect to the AI service. Please try again later or refresh the page.",
      );
      this.state.connectionState = "error";
      return;
    }
    const delay = Math.pow(2, this._reconnectAttempts) * 1000; // 1s, 2s, 4s
    this._reconnectAttempts++;
    this.state.error = _t("Reconnecting... (attempt %s)", this._reconnectAttempts);
    this.state.connectionState = "reconnecting";

    this._reconnectTimer = setTimeout(() => {
      this.startStream();
    }, delay);
  }

  _clearReconnectTimer() {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
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
   * @param {KeyboardEvent} ev
   */
  _mentionKeydown(ev) {
    const assistants = this._getFilteredAssistants();
    if (!assistants.length) {
      return;
    }

    switch (ev.key) {
      case "ArrowDown":
        ev.preventDefault();
        this._mentionSelectedIndex =
          ((this._mentionSelectedIndex || 0) + 1) % assistants.length;
        break;
      case "ArrowUp":
        ev.preventDefault();
        this._mentionSelectedIndex =
          ((this._mentionSelectedIndex || 0) - 1 + assistants.length) %
          assistants.length;
        break;
      case "Enter":
        ev.preventDefault();
        this.onAssistantSelect(assistants[this._mentionSelectedIndex || 0]);
        break;
      case "Escape":
        ev.preventDefault();
        this.closeMention();
        break;
    }
  }

  /**
   * Get filtered assistants based on current mention query.
   * @returns {Array}
   */
  _getFilteredAssistants() {
    const query = (this.state.mentionQuery || "").toLowerCase().trim();
    if (!query) {
      return this.state.assistants;
    }
    return this.state.assistants.filter((assistant) => {
      const name = (assistant.name || "").toLowerCase();
      return name.includes(query);
    });
  }

  // ==================== Mention Selection ====================

  /**
   * Handle assistant selection from the mention dropdown.
   * Inserts the assistant @mention into the textarea at the cursor position.
   * @param {Object} assistant - The selected assistant record
   */
  onAssistantSelect(assistant) {
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

    const displayName = assistant.name;
    const before = value.substring(0, atIndex);
    const after = value.substring(cursorPos);
    const newText = before + "@" + displayName + " " + after;

    this.state.inputText = newText;
    this.state.selectedAssistantId = assistant.id;
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
          body: _t("Uploaded: %s", file.name),
          author_name: _t("You"),
          author_id: null,
          date: new Date().toISOString(),
          is_ai: false,
          message_type: "comment",
          attachments: [result.data.attachment],
        });
        this.scrollToBottom();
      } else {
        this.state.error = result.error || _t("Upload failed");
      }
    } catch (err) {
      this.state.error = err.message || _t("Upload failed");
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
    // ⚠ 這個函式的輸入有兩種來源：後端歷史訊息的 `.isoformat()`（不帶 offset）
    // 與前端串流完成時自產的 `toISOString()`（帶 `Z`）。
    // `formatUserTime` 兩種都吃，且解析失敗回 fallback 而非 `Invalid DateTime`。
    return formatUserTime(dateStr, "");
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
    return (
      !this.state.inputText.trim() || this.state.sending || this.state.streaming
    );
  }

  _createAiMessage(body, messageId) {
    return {
      id: messageId || Date.now(),
      body: body ? parseMarkdown(body) : "", // AI responses are in Markdown
      author_name: _t("AI Assistant"),
      author_id: null,
      date: new Date().toISOString(),
      is_ai: true,
      message_type: "comment",
      attachments: [],
    };
  }

  // ==================== Mermaid Rendering ====================

  /**
   * Scan the message list for unprocessed mermaid blocks and trigger rendering.
   * Uses requestAnimationFrame to avoid blocking the UI thread.
   * The data-processed attribute prevents re-rendering on subsequent patch cycles.
   */
  _renderMermaidInMessages() {
    // Defer mermaid rendering until streaming completes to prevent jitter
    if (this.state.streaming) return;

    const messageList = this.messageListRef.el;
    if (!messageList) return;

    const pending = messageList.querySelectorAll(
      ".o_woow_mermaid:not([data-processed])",
    );
    if (pending.length === 0) return;

    requestAnimationFrame(() => {
      renderMermaidBlocks(messageList);
    });
  }

  /**
   * Replace all mermaid blocks in streaming text with placeholders.
   * Both complete and incomplete blocks are replaced to prevent mermaid SVG
   * rendering during streaming, which causes jitter from render-destroy cycles.
   * @param {string} text - The raw streaming text
   * @returns {string} Processed text with placeholders for all mermaid blocks
   */
  _handleStreamingMermaidBlocks(text) {
    const parts = text.split(/(```mermaid\n)/);
    let result = "";
    let inMermaidBlock = false;

    for (const part of parts) {
      if (part === "```mermaid\n") {
        inMermaidBlock = true;
        continue;
      }

      if (inMermaidBlock) {
        const closeIdx = part.indexOf("\n```");
        if (closeIdx !== -1) {
          // Complete block - replace with placeholder, keep text after closing fence
          const afterClose = part.substring(closeIdx + 4);
          result +=
            "\n> 📊 *" +
            _t("The diagram will be shown once the reply is complete") +
            "*\n" +
            afterClose;
        } else {
          // Incomplete block - replace with loading placeholder
          result += "\n> 📊 *" + _t("Loading diagram...") + "*\n";
        }
        inMermaidBlock = false;
      } else {
        result += part;
      }
    }

    return result;
  }

  get streamingHtml() {
    let text = this.state.streamingText;
    if (!text) return "";

    // Handle incomplete mermaid blocks during streaming
    text = this._handleStreamingMermaidBlocks(text);

    return parseMarkdown(text);
  }

  get connectionIndicator() {
    const map = {
      idle: { color: "gray", text: _t("AI assistant idle"), visible: false },
      connecting: {
        color: "yellow",
        text: _t("Connecting to AI assistant..."),
        visible: true,
        animate: "blink",
      },
      spawning: {
        color: "yellow",
        text: _t(
          "Starting the AI assistant... (the first start can take up to a minute)",
        ),
        visible: true,
        animate: "blink",
      },
      connected: { color: "green", text: _t("AI assistant connected"), visible: true },
      streaming: {
        color: "green",
        text: _t("AI assistant is replying..."),
        visible: true,
        animate: "pulse",
      },
      error: {
        color: "red",
        text: this.state.error || _t("AI connection error"),
        visible: true,
      },
      reconnecting: {
        color: "yellow",
        text: this.state.error || _t("Reconnecting to AI assistant..."),
        visible: true,
        animate: "blink",
      },
    };
    return map[this.state.connectionState] || map.idle;
  }

  onRetryClick() {
    this._reconnectAttempts = 0;
    this.state.connectionState = "connecting";
    this.state.error = null;
    this.startStream();
  }

  toggleToolCall(tc) {
    tc.expanded = !tc.expanded;
  }
}

/** @odoo-module **/
import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { aiService } from "../paas/services/ai_service";
import { openAiStream } from "../paas/services/ai_stream";
import { parseMarkdown } from "../paas/services/markdown_parser";
import { safeHtml } from "../paas/services/html_sanitize";

export class PortalTaskChat extends Component {
  static template = "woow_paas_platform.PortalTaskChat";
  static props = { channelId: Number, projectId: Number };

  setup() {
    this.state = useState({
      messages: [],          // {id, isAi, authorName, html, date}
      input: "",
      sending: false,
      streaming: false,
      streamingText: "",
      spawning: false,
      ready: false,
      notReadyReason: "",
      error: null,
    });
    this.eventSource = null;
    this.listRef = useRef("messageList");

    onWillStart(async () => {
      const [status, history] = await Promise.all([
        aiService.fetchAgentStatus(this.props.projectId),
        aiService.fetchChatHistory(this.props.channelId),
      ]);
      if (status.success) {
        this.state.ready = !!status.data.ready;
        this.state.notReadyReason = status.data.reason || "";
      } else {
        this.state.notReadyReason = status.error || _t("Agent status unavailable");
      }
      if (history.success) {
        this.state.messages = history.data.map((m) => this._toMessage(m));
      } else {
        this.state.error = history.error || _t("Failed to load chat history");
      }
    });
    onMounted(() => this._scrollToBottom());
    onWillUnmount(() => this._closeStream());
  }

  get streamingHtml() {
    return parseMarkdown(this.state.streamingText);
  }

  _toMessage(m) {
    return {
      id: m.id,
      isAi: m.is_ai,
      authorName: m.author_name,
      // AI 訊息走 parseMarkdown（內含 DOMPurify），user 訊息走 safeHtml
      // 縱深防禦（與 /woow AiChat 同源），勿重複包 AI 訊息。
      html: m.is_ai ? parseMarkdown(m.body) : safeHtml(m.body),
      date: m.date,
    };
  }

  async send() {
    const body = this.state.input.trim();
    if (!body || this.state.sending || this.state.streaming || !this.state.ready) {
      return;
    }
    this.state.sending = true;
    this.state.error = null;
    const res = await aiService.postMessage(this.props.channelId, body);
    this.state.sending = false;
    if (!res.success) {
      this.state.error = res.error || _t("Failed to send message");
      return;
    }
    this.state.messages.push({
      id: res.data.id,
      isAi: false,
      authorName: res.data.author_name,
      html: safeHtml(res.data.body),
      date: res.data.date,
    });
    this.state.input = "";
    this._scrollToBottom();
    this._startStream();
  }

  onKeydown(ev) {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      this.send();
    }
  }

  _startStream() {
    this._closeStream();
    this.state.streaming = true;
    this.state.streamingText = "";
    this.state.spawning = false;
    this.eventSource = openAiStream(this.props.channelId, {
      onSpawning: () => {
        this.state.spawning = true;
      },
      onChunk: (data) => {
        this.state.spawning = false;
        this.state.streamingText += data.chunk;
        this._scrollToBottom();
      },
      onDone: (data) => {
        const text = data.full_response || this.state.streamingText;
        if (text) {
          this.state.messages.push({
            id: data.message_id,
            isAi: true,
            authorName: "AI",
            html: parseMarkdown(text),
            date: new Date().toISOString(),
          });
        }
        this._closeStream();
        this._scrollToBottom();
      },
      onError: (data) => {
        this.state.error = data.error || _t("Failed to get the AI reply");
        this._closeStream();
      },
      onConnectionError: () => {
        if (this.state.streamingText) {
          this.state.messages.push({
            id: null,
            isAi: true,
            authorName: "AI",
            html: parseMarkdown(
              this.state.streamingText +
                "\n\n⚠️ " +
                _t("(This reply is incomplete because the connection was interrupted.)"),
            ),
            date: new Date().toISOString(),
          });
        } else {
          this.state.error = _t("Connection lost. Please send your message again.");
        }
        this._closeStream();
      },
    });
  }

  _closeStream() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    this.state.streaming = false;
    this.state.streamingText = "";
    this.state.spawning = false;
  }

  _scrollToBottom() {
    requestAnimationFrame(() => {
      const el = this.listRef.el;
      if (el) {
        el.scrollTop = el.scrollHeight;
      }
    });
  }
}

registry
  .category("public_components")
  .add("woow_paas_platform.PortalTaskChat", PortalTaskChat);

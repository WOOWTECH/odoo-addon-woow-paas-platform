/** @odoo-module **/
import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { aiService } from "../paas/services/ai_service";
import { openAiStream } from "../paas/services/ai_stream";
import { parseMarkdown } from "../paas/services/markdown_parser";

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
        this.state.notReadyReason = status.error || "Agent 狀態取得失敗";
      }
      if (history.success) {
        this.state.messages = history.data.map((m) => this._toMessage(m));
      } else {
        this.state.error = history.error || "無法載入對話歷史";
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
      html: m.is_ai ? parseMarkdown(m.body) : m.body,
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
      this.state.error = res.error || "訊息傳送失敗";
      return;
    }
    this.state.messages.push({
      id: res.data.id,
      isAi: false,
      authorName: res.data.author_name,
      html: res.data.body,
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
        this.state.error = data.error || "AI 回覆失敗";
        this._closeStream();
      },
      onConnectionError: () => {
        if (this.state.streamingText) {
          this.state.messages.push({
            id: null,
            isAi: true,
            authorName: "AI",
            html: parseMarkdown(
              this.state.streamingText + "\n\n⚠️ (回覆因連線中斷而不完整)"),
            date: new Date().toISOString(),
          });
        } else {
          this.state.error = "連線中斷，請重新傳送訊息。";
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

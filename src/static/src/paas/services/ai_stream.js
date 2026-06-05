/** @odoo-module **/
import { aiService } from "./ai_service";

/**
 * Open the AI SSE stream for a channel and dispatch typed events.
 *
 * Shared protocol consumer for /woow AiChat AND the portal task chat —
 * single source of truth for the SSE frame contract:
 *   error / status(spawning) / tool_call / tool_result / tool_error /
 *   chunk / warning / done.  (`tool_unavailable` is intentionally ignored,
 *   matching the historical AiChat behaviour.)
 *
 * @param {number} channelId
 * @param {Object} handlers - optional callbacks:
 *   onError(data), onSpawning(data), onToolCall(data), onToolResult(data),
 *   onToolError(data), onChunk(data), onWarning(data), onDone(data),
 *   onParseError(raw, err), onConnectionError(event)
 * @returns {EventSource} caller is responsible for closing it
 */
export function openAiStream(channelId, handlers) {
  const url = aiService.getStreamUrl(channelId);
  const es = new EventSource(url);
  es.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      handlers.onParseError?.(event.data, err);
      return;
    }
    if (data.error) {
      handlers.onError?.(data);
      return;
    }
    if (data.type === "status" && data.payload?.stage === "spawning") {
      handlers.onSpawning?.(data);
      return;
    }
    if (data.type === "tool_call") {
      handlers.onToolCall?.(data);
      return;
    }
    if (data.type === "tool_result") {
      handlers.onToolResult?.(data);
      return;
    }
    if (data.type === "tool_error") {
      handlers.onToolError?.(data);
      return;
    }
    if (data.chunk) {
      handlers.onChunk?.(data);
    }
    if (data.warning) {
      handlers.onWarning?.(data);
    }
    if (data.done) {
      handlers.onDone?.(data);
    }
  };
  es.onerror = (event) => handlers.onConnectionError?.(event);
  return es;
}

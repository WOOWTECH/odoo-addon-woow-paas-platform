/** @odoo-module **/
import { useState, onMounted, onWillUnmount } from "@odoo/owl";
import { jsonRpc } from "./rpc";
import { createResourcePoller } from "../utils/resource_polling";
import {
  mcpSetupCondition,
  normalizeMcpBackendState,
  normalizeMcpSetupState,
  safeAdminUrl,
} from "../utils/mcp_setup_status";

/** 視窗重新取得焦點時，距離上一次請求不到這麼久就不再問（visibilitychange 與 focus 常常一起來）。 */
export const MCP_FOCUS_REFRESH_GAP_MS = 1500;

/** 畫面正在提示時多久再問一次（平常 30 秒）：存檔、子行程重啟的空窗過後，提示最多再掛這麼久。 */
export const MCP_ACTIONABLE_REFRESH_MS = 10000;

/**
 * 2026-10-27：MCP 服務「還沒設定連線」的即時輪詢（與 `useVpnNodeStatus` 同款：一頁一個 poller、頁面不可見
 * 就停、不重疊、預設每 30 秒；切回分頁時立刻再問一次——租戶多半是去管理頁存完連線再切回來）。
 * 純讀——後端 `/mcp-status` 不寫 DB、不碰 state。
 *
 * 第五輪：
 * - 由服務頁（ServiceDetailPage）呼叫，一頁一個；頁首提示與 Overview 的 Connection Info 共用同一份結果，
 *   不各自輪詢。
 * - 回應多了 `backend`（configured／missing／unknown）。
 * - 畫面正在提示（`mcpSetupCondition` 有值）時，**視窗重新取得焦點**也立刻重問：管理頁開在並排的另一個
 *   視窗時不會觸發 visibilitychange，原本要等下一輪 30 秒。後端對「要租戶動手」的結果只快取 2 秒
 *   （`services/mcp_setup_status.py` 的 `_ACTIONABLE_CACHE_TTL_SECONDS`），所以這一問拿得到存檔後的狀態。
 * - 畫面正在提示時改成每 `MCP_ACTIONABLE_REFRESH_MS`（10 秒）再問一次：剛按下 Save 就切回來，可能剛好
 *   落在子行程重啟的空窗（或別的分頁 2 秒內才問過），這一輪拿到的還是舊狀態；不必再等 30 秒。健康時
 *   （沒有提示）維持 30 秒。頁面不可見時 poller 不發請求，這個計時也就停了。
 *
 * `getEnabled()` 為 false（沒有這個能力、服務不在 running、服務還沒載入）時不打任何請求，並清掉舊結果；
 * 呼叫端在它變動時呼叫 `refresh()`，不必等下一輪。後端回 `state: "unknown"`（讀不到、逾時、舊映像）是
 * 正常降級，不顯示任何東西；請求本身失敗時保留上一筆結果（一次失敗不該讓提示閃掉或閃出來）。
 *
 * @param {() => number|string} getWorkspaceId
 * @param {() => number|string} getServiceId
 * @param {() => boolean} getEnabled
 * @param {{windowRef?: EventTarget|null, now?: () => number, schedule?: Function, cancel?: Function}} [options]
 *   測試注入用
 */
export function useMcpSetupStatus(getWorkspaceId, getServiceId, getEnabled, options = {}) {
  const state = useState({ status: null, backend: "unknown", adminUrl: "", loading: true });
  const now = options.now || (() => Date.now());
  const windowRef = "windowRef" in options
    ? options.windowRef
    : (typeof window === "undefined" ? null : window);
  const schedule = options.schedule || ((fn, ms) => setTimeout(fn, ms));
  const cancel = options.cancel || ((handle) => clearTimeout(handle));
  let disposed = false;
  let lastRequestAt = -Infinity;
  let followUp = null;
  const clearFollowUp = () => {
    if (followUp !== null) {
      cancel(followUp);
      followUp = null;
    }
  };
  // 提示顯示中：10 秒後再問一次（refresh 會順便把 poller 自己的下一輪往後排）。
  const armFollowUp = () => {
    clearFollowUp();
    if (!disposed && mcpSetupCondition(state.status, state.backend)) {
      followUp = schedule(() => {
        followUp = null;
        poller.refresh();
      }, MCP_ACTIONABLE_REFRESH_MS);
    }
  };
  const poller = createResourcePoller({
    async load() {
      if (disposed || !getEnabled()) return { skipped: true };
      lastRequestAt = now();
      const result = await jsonRpc(
        `/api/workspaces/${getWorkspaceId()}/services/${getServiceId()}/mcp-status`,
        {},
        15000,
      );
      if (!result?.success) throw new Error("MCP setup status unavailable");
      return { skipped: false, data: result.data };
    },
    onData({ skipped, data }) {
      state.loading = false;
      if (skipped) {
        state.status = null;
        state.backend = "unknown";
        state.adminUrl = "";
      } else {
        state.status = normalizeMcpSetupState(data?.state);
        state.backend = normalizeMcpBackendState(data?.backend);
        state.adminUrl = safeAdminUrl(data?.admin_url);
      }
      armFollowUp();
    },
    onError() {
      state.loading = false;
      armFollowUp();
    },
  });
  const onWindowFocus = () => {
    if (disposed || !mcpSetupCondition(state.status, state.backend)) return;
    if (now() - lastRequestAt < MCP_FOCUS_REFRESH_GAP_MS) return;
    poller.refresh();
  };
  onMounted(() => {
    poller.start();
    windowRef?.addEventListener("focus", onWindowFocus);
  });
  onWillUnmount(() => {
    disposed = true;
    clearFollowUp();
    poller.stop();
    windowRef?.removeEventListener("focus", onWindowFocus);
  });
  return { state, refresh: poller.refresh };
}

/** @odoo-module **/
import { useState, onMounted, onWillUnmount } from "@odoo/owl";
import { jsonRpc } from "./rpc";
import { createResourcePoller } from "../utils/resource_polling";
import { normalizeMcpSetupState, safeAdminUrl } from "../utils/mcp_setup_status";

/**
 * 2026-10-27：MCP 服務「還沒設定連線」的即時輪詢（與 `useVpnNodeStatus` 同款：一頁一個 poller、頁面不可見
 * 就停、不重疊、預設每 30 秒；切回分頁時立刻再問一次——租戶多半是去管理頁存完連線再切回來）。
 * 純讀——後端 `/mcp-status` 不寫 DB、不碰 state。
 *
 * `getEnabled()` 為 false（沒有這個能力、服務不在 running）時不打任何請求，並清掉舊結果。
 * 後端回 `state: "unknown"`（讀不到、逾時、舊映像）是正常降級，不顯示任何東西；請求本身失敗時保留
 * 上一筆結果（一次失敗不該讓提示閃掉或閃出來）。
 */
export function useMcpSetupStatus(getWorkspaceId, getServiceId, getEnabled) {
  const state = useState({ status: null, adminUrl: "", loading: true });
  let disposed = false;
  const poller = createResourcePoller({
    async load() {
      if (disposed || !getEnabled()) return { skipped: true };
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
        state.adminUrl = "";
        return;
      }
      state.status = normalizeMcpSetupState(data?.state);
      state.adminUrl = safeAdminUrl(data?.admin_url);
    },
    onError() {
      state.loading = false;
    },
  });
  onMounted(() => poller.start());
  onWillUnmount(() => {
    disposed = true;
    poller.stop();
  });
  return { state, refresh: poller.refresh };
}

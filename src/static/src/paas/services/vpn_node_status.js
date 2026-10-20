/** @odoo-module **/
import { useState, onMounted, onWillUnmount } from "@odoo/owl";
import { jsonRpc } from "./rpc";
import { createResourcePoller } from "../utils/resource_polling";

/**
 * INFRA-FIX-005：VPN 節點狀態的即時輪詢（與 `useServiceResources` 同款：一頁一個 poller、
 * 頁面不可見就停、不重疊）。純讀——後端 `/node-status` 不寫 DB、不碰 state。
 *
 * `getEnabled()` 為 false（沒有這個能力、服務不在 running）時不打任何請求，並清掉舊結果。
 * 後端回 `available:false`（pod 還沒起來、operator 太舊／沒回應）是正常降級，不顯示錯誤。
 */
export function useVpnNodeStatus(getWorkspaceId, getServiceId, getEnabled) {
  const state = useState({ report: null, loading: true, failed: false });
  let disposed = false;
  const poller = createResourcePoller({
    async load() {
      if (disposed || !getEnabled()) return { skipped: true };
      const result = await jsonRpc(
        `/api/workspaces/${getWorkspaceId()}/services/${getServiceId()}/node-status`,
        {},
        20000,
      );
      if (!result?.success) throw new Error("VPN node status unavailable");
      return { skipped: false, data: result.data };
    },
    onData({ skipped, data }) {
      state.loading = false;
      state.failed = false;
      state.report = skipped || !data?.available ? null : data.report;
    },
    onError() {
      // 保留上一筆結果：一次讀取失敗不該讓卡片閃掉。
      state.loading = false;
      state.failed = true;
    },
  });
  onMounted(() => poller.start());
  onWillUnmount(() => {
    disposed = true;
    poller.stop();
  });
  return { state, refresh: poller.refresh };
}

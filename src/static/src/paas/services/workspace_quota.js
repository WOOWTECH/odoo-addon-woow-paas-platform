/** @odoo-module **/
import { jsonRpc } from "./rpc";

/**
 * #1647：工作區 namespace 配額（CPU／記憶體／磁碟）的上限與已佔用量。
 *
 * 後端端點同步向 operator 讀一次叢集上的 ResourceQuota。逾時預算是一條階梯，外層
 * 嚴格大於內層：
 *
 *   這裡 QUOTA_REQUEST_TIMEOUT_MS（15s）
 *     > 平台 NAMESPACE_QUOTA_TIMEOUT（connect 3s＋read 8s，src/services/paas_operator.py）
 *       > operator NS_QUOTA_READ_TIMEOUT_SECONDS（5s，一次 kubectl get）
 *
 * 比 jsonRpc 預設的 30s 短是刻意的：這塊面板只是輔助資訊，拿不到就顯示「暫時無法取得」，
 * 不值得讓人等半分鐘。
 */
const QUOTA_REQUEST_TIMEOUT_MS = 15000;

/**
 * 面板讀到資料之後，隔這麼久再補讀**一次**（一次性，不是輪詢的間隔）。
 *
 * 只為了一個情境：刪除服務後回到工作區頁，面板掛載的當下 pod 還在結束中，而 k8s 的
 * ResourceQuota 對還沒結束的 pod 照算——這時讀到的是「已經變小的上限＋還沒回補的用量」，
 * 比例不降反升；之後清單裡沒有進行中的服務，頁面不會再有任何東西觸發重讀。
 *
 * 30 秒沒有在叢集上實測過回補要多久：取這個數字是因為 k8s 預設的
 * terminationGracePeriodSeconds 就是 30 秒，pod 通常在那之前結束、用量隨即回補。撐滿
 * 寬限期（或設了更長寬限期）才結束的 pod 仍可能趕不上，那時要靠面板上的重新整理鈕。
 */
export const QUOTA_FOLLOW_UP_DELAY_MS = 30000;

/**
 * @param {number} workspaceId
 * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
 *   `data` 為 `{available, reason, level, hint, resources}`（形狀見後端
 *   `services/workspace_quota.build_view`）。永不 throw——網路錯誤與逾時都收成
 *   `{success: false}`，呼叫端不必包 try/catch。
 */
export async function fetchWorkspaceQuota(workspaceId) {
  try {
    return await jsonRpc(`/api/workspaces/${workspaceId}/quota`, {}, QUOTA_REQUEST_TIMEOUT_MS);
  } catch (err) {
    return { success: false, error: err.message };
  }
}

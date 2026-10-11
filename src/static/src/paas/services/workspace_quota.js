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

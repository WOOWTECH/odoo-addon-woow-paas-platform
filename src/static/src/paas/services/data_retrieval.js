/** @odoo-module **/
import { jsonRpc } from "./rpc";

/**
 * #1540：停權／刪除後的限時唯讀資料取回入口。
 *
 * 兩個入口共用同一組動作（status／open／close）：
 *   - 停權中的服務：/api/workspaces/<ws>/services/<id>/data-retrieval
 *   - 已刪除服務的保留資料：/api/workspaces/<ws>/retained-data/<id>/data-retrieval
 *
 * 開／關要等 operator 建或刪 pod 與 tunnel route，平台端預算 75s
 * （DATA_RETRIEVAL_TIMEOUT）；這裡給 90s（同 reset 的階梯：再長會撞 Cloudflare 的 524）。
 */
const RETRIEVAL_ACTION_TIMEOUT_MS = 90000;

export function serviceRetrievalEndpoint(workspaceId, serviceId) {
  return `/api/workspaces/${workspaceId}/services/${serviceId}/data-retrieval`;
}

export function retainedRetrievalEndpoint(workspaceId, retainedId) {
  return `/api/workspaces/${workspaceId}/retained-data/${retainedId}/data-retrieval`;
}

/**
 * @param {string} endpoint
 * @param {"status"|"open"|"close"} action
 * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
 *   open 成功時 `data.credentials` 帶一次性密碼——只顯示、不存。
 */
export async function dataRetrievalAction(endpoint, action) {
  try {
    const timeout = action === "status" ? undefined : RETRIEVAL_ACTION_TIMEOUT_MS;
    return await jsonRpc(endpoint, { action }, timeout);
  } catch (err) {
    return { success: false, error: err.message };
  }
}

/**
 * 工作區裡已刪除、磁碟仍在保留期的服務（只有 owner／admin 拿得到）。
 * @param {number} workspaceId
 * @returns {Promise<{success: boolean, data?: Object[], error?: string}>}
 */
export async function listRetainedData(workspaceId) {
  try {
    return await jsonRpc(`/api/workspaces/${workspaceId}/retained-data`, {});
  } catch (err) {
    return { success: false, error: err.message };
  }
}

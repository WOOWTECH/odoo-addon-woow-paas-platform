/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

/**
 * 商品可購買性的單一判準與文案來源（#501）。
 *
 * 卡片（marketplace）與 configure 頁 header 顯示同一件事（這個商品現在買不買得
 * 到），兩邊各寫一份就會漂移——#478 本身就是「卡片一個判準、plan 區另一個判準」
 * 的產物，同型前例還有 #460（Reset 按鈕前端 slug 白名單 vs 後端 has_admin_gui）。
 * 要改判準或措辭就改這裡一處。
 *
 * ⚠ **前端不是安全邊界**：租戶知道 template id 仍可直接構造 JSON-RPC。權威閘門在
 * `CloudService.create()`（model 層，所有入口共用）。這裡只是不讓正常使用者走進
 * 一條必然失敗的流程。
 *
 * ⚠ **不得改用 slug 名單推導可購買性**：能力／狀態一律由後端欄位表達，前端讀
 * payload（`docs/decisions/patterns/capability-gate-single-source-field.md`，
 * repo-lint 的 slug capability gate 會機械攔下）。缺欄位就補後端 payload。
 *
 * @param {{purchasable: boolean, reason: string}|null|undefined} availability
 *        後端 `_format_template` 的頂層 `availability` 欄位。
 */

/**
 * @returns {boolean} 是否可購買。**fail-closed**：欄位缺失（舊快取、尚未更新的
 *          payload、其他 endpoint 漏帶）一律視同不可購買——寧可少顯示一顆按鈕，
 *          也不要長出一顆按了必然失敗的。
 */
export function isPurchasable(availability) {
  return availability?.purchasable === true;
}

/**
 * reason → 標籤。停售（營運明示）、未釘版（平台端設定缺陷，#552 層 4）與售罄
 * （達上限後推導）是三件不同的事，前台分別顯示：使用者看到「暫停販售」知道之後
 * 可能會回來，看到「已售罄」知道是名額用完了。
 *
 * ⚠ 這裡刻意寫成**對照表 + 中性 fallback**，而不是原本的
 * `reason === "paused" ? "暫停販售" : "已售罄"`。那個三元式把**任何**新 reason
 * 都渲染成「已售罄」——新增後端判準時前端會靜默對租戶說謊（名額根本沒滿），
 * 而且不會有任何錯誤。新 reason 只該讓標籤變成不精確（中性），不該變成假的。
 */
const REASON_LABELS = {
  paused: _t("Sales paused"),
  sold_out: _t("No slots available"),
  unpinned_chart: _t("Not available for purchase"),
};

/**
 * @returns {string} 狀態標籤；**空字串代表可購買、不該渲染任何標籤**。
 */
export function availabilityLabel(availability) {
  if (isPurchasable(availability)) {
    return "";
  }
  // fallback「暫不開放」＝「不可購買，但我們不確定是哪一種」。缺 reason 的
  // payload（舊快取／漏帶欄位）走到這裡也一樣——中性描述才與 isPurchasable 的
  // fail-closed 語意一致。
  return REASON_LABELS[availability?.reason] ?? _t("Not available for purchase");
}

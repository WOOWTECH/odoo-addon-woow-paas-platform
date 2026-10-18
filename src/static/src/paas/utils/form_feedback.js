/** @odoo-module **/

import { useEffect, useRef } from "@odoo/owl";

/**
 * X-LC-002-3／X-LC-006-3：表單送出失敗時，把使用者帶到錯誤所在。
 *
 * 開通表單（AppConfigurationPage）很長，「啟動應用程式」在頁尾：
 * - 前端驗證失敗：欄位紅框與訊息在畫面外（390px 實測在視窗上方 -1550px），頁面不捲、
 *   焦點不動，看起來像「沒反應」。
 * - 後端拒絕（例：「管理員 Email 格式不正確」）：訊息只在頁面最底的橫幅
 *   （1440px 實測 top=1762px、視窗高 1000px），按下按鈕後畫面沒有任何變化。
 *
 * 做法：錯誤出現的那一次 render 之後，把第一個錯誤捲到畫面中間，焦點放到該欄位的輸入框
 * （沒有輸入框就放到錯誤本身，並補 tabindex=-1 讓它可聚焦）。只在「這次送出」觸發——
 * 使用者邊打字邊清錯誤時不會被拉來拉去。
 */

export const FIELD_ERROR_SELECTOR = ".o_woow_has_error, .o_woow_field_error";
export const LAUNCH_ERROR_SELECTOR = ".o_woow_launch_error";
const FOCUSABLE_SELECTOR =
  "input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled])";
const GROUP_SELECTOR = ".o_woow_form_group";

/**
 * 捲到 `root` 裡第一個符合 `selector` 的錯誤並聚焦。
 *
 * @param {Element|null|undefined} root
 * @param {string} selector
 * @param {{behavior?: ScrollBehavior}} [options]
 * @returns {Element|null} 被聚焦的元素；找不到錯誤時回 null
 */
export function revealFirstError(root, selector, options = {}) {
  if (!root || typeof root.querySelector !== "function") {
    return null;
  }
  const target = root.querySelector(selector);
  if (!target) {
    return null;
  }
  const { behavior = "smooth" } = options;
  if (typeof target.scrollIntoView === "function") {
    target.scrollIntoView({ block: "center", behavior });
  }
  const group = (typeof target.closest === "function" && target.closest(GROUP_SELECTOR)) || target;
  const field = typeof group.querySelector === "function" ? group.querySelector(FOCUSABLE_SELECTOR) : null;
  const focusTarget = field || target;
  if (focusTarget === target && !target.hasAttribute("tabindex")) {
    target.setAttribute("tabindex", "-1");
  }
  if (typeof focusTarget.focus === "function") {
    // 已經捲過了；preventScroll 避免瀏覽器再把它捲到邊緣。
    focusTarget.focus({ preventScroll: true });
  }
  return focusTarget;
}

/**
 * 在 component setup 裡呼叫。`refName` 是表單根元素的 `t-ref`。
 *
 * - `getValidationToken()`：前端驗證失敗時遞增的數字（0＝還沒失敗過）。
 * - `getLaunchError()`：後端錯誤訊息；由空變成有值時捲到 `.o_woow_launch_error` 橫幅。
 *
 * @param {string} refName
 * @param {() => number} getValidationToken
 * @param {() => (string|null)} getLaunchError
 * @returns {{el: Element|null}} 根元素的 ref
 */
export function useFormErrorReveal(refName, getValidationToken, getLaunchError) {
  const root = useRef(refName);
  useEffect(
    (token) => {
      if (token) {
        revealFirstError(root.el, FIELD_ERROR_SELECTOR);
      }
    },
    () => [getValidationToken()],
  );
  useEffect(
    (launchError) => {
      if (launchError) {
        revealFirstError(root.el, LAUNCH_ERROR_SELECTOR);
      }
    },
    () => [getLaunchError()],
  );
  return root;
}

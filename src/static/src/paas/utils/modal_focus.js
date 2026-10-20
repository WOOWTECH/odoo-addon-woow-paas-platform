/** @odoo-module **/

/**
 * 對話框的鍵盤與讀屏守線（LLM-01c 一次性金鑰對話框；與元件無關的純 DOM helper，
 * tests/ui/test_llm_overview.mjs 用假 DOM 驗）。
 *
 * - `trapTab`：Tab／Shift+Tab 只在對話框裡循環（backdrop 只擋得住滑鼠，鍵盤會 Tab 到背景的
 *   側欄與 Header 連結，按下去就換頁、整串元件卸載）。
 * - `inertOutside`：對話框打開時把它所有祖先的兄弟節點設成 `inert`（背景點不到、Tab 不到、
 *   讀屏讀不到）；回傳還原函式，只還原自己設的那些。
 */

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(", ");

/** root 裡可以 Tab 到、而且有畫出來的元素（依 DOM 順序）。 */
export function focusableWithin(root) {
  if (!root || typeof root.querySelectorAll !== "function") return [];
  return Array.from(root.querySelectorAll(FOCUSABLE)).filter(
    (el) => !el.hidden && typeof el.getClientRects === "function" && el.getClientRects().length > 0
  );
}

/** keydown（Tab）時呼叫：焦點跑到頭尾或對話框外時，繞回另一端。 */
export function trapTab(ev, root) {
  if (!root || ev.key !== "Tab") return;
  const items = focusableWithin(root);
  const active = root.ownerDocument ? root.ownerDocument.activeElement : null;
  if (!items.length) {
    ev.preventDefault();
    root.focus();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const inside = !!active && active !== root && root.contains(active);
  if (ev.shiftKey) {
    if (!inside || active === first) {
      ev.preventDefault();
      last.focus();
    }
  } else if (!inside || active === last) {
    ev.preventDefault();
    first.focus();
  }
}

/** 把 element 以外的整頁設成 inert；回傳還原函式（冪等）。 */
export function inertOutside(element) {
  const changed = [];
  const body = element && element.ownerDocument ? element.ownerDocument.body : null;
  let node = element;
  while (node && node.parentElement && node !== body) {
    const parent = node.parentElement;
    for (const sibling of Array.from(parent.children)) {
      if (sibling !== node && !sibling.hasAttribute("inert")) {
        sibling.setAttribute("inert", "");
        changed.push(sibling);
      }
    }
    node = parent;
  }
  return () => {
    while (changed.length) {
      changed.pop().removeAttribute("inert");
    }
  };
}

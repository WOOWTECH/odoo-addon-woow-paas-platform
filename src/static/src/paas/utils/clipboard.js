/** @odoo-module **/

/**
 * Copy text, falling back to a hidden textarea when the async Clipboard API
 * is missing or refused (older browsers, non-secure contexts). Resolves to
 * true on success so callers can show feedback (#1310: copy buttons used to
 * fail silently).
 *
 * The fallback textarea may hold a secret (LLM-01c: the one-time API key), so
 * it is always emptied and removed — even when select() or execCommand()
 * throws — and focus goes back to whatever had it before (select() moves focus
 * into the textarea, which would otherwise pull it out of an open dialog).
 */
export async function copyText(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  let area = null;
  const previousFocus = document.activeElement;
  try {
    area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    return document.execCommand("copy") === true;
  } catch {
    return false;
  } finally {
    if (area) {
      area.value = "";
      area.remove();
    }
    if (previousFocus && previousFocus !== document.activeElement
        && typeof previousFocus.focus === "function") {
      try {
        previousFocus.focus();
      } catch {
        // the element may be gone; nothing else to restore
      }
    }
  }
}

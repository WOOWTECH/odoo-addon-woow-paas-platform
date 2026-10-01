/** @odoo-module **/

/**
 * Copy text, falling back to a hidden textarea when the async Clipboard API
 * is missing or refused (older browsers, non-secure contexts). Resolves to
 * true on success so callers can show feedback (#1310: copy buttons used to
 * fail silently).
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
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

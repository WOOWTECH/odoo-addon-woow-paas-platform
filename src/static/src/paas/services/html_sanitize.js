/** @odoo-module **/

import { markup } from "@odoo/owl";

const ALLOWED_TAGS = [
    "p", "br", "strong", "em", "b", "i",
    "code", "pre", "blockquote",
    "ul", "ol", "li",
    "a",
    "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
    "hr", "del", "span", "div",
];

const ALLOWED_ATTR = ["href", "target", "rel", "class"];

let _hooksRegistered = false;

/**
 * Register DOMPurify hooks once. ``addHook`` stacks on repeat calls, so
 * we guard with a module-level flag — safeHtml() is the only entry point,
 * so first-call init is enough.
 *
 * Hook: tabnabbing defence — any ``<a target="_blank">`` left after sanitize
 * gets ``rel="noopener noreferrer"`` set, even if the source HTML omitted it
 * (CAQA C-3, re-loop 2026-05-20). DOMPurify's ALLOWED_ATTR already permits
 * ``target`` + ``rel``, but does not enforce the security pairing; this hook
 * closes the gap centrally instead of relying on every author to remember.
 */
function _ensureHooks() {
    if (_hooksRegistered || !window.DOMPurify) {
        return;
    }
    window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
        if (node.tagName === "A" && node.getAttribute("target") === "_blank") {
            node.setAttribute("rel", "noopener noreferrer");
        }
    });
    _hooksRegistered = true;
}

/**
 * Sanitize an HTML string using DOMPurify and return an OWL markup
 * object that can be rendered with ``t-out``.
 *
 * @param {string} html - Raw HTML string (e.g. from AI response or mail.message body)
 * @returns {import("@odoo/owl").Markup|string} Sanitized HTML wrapped in markup(), or ""
 */
export function safeHtml(html) {
    if (!html) {
        return "";
    }
    if (!window.DOMPurify) {
        console.warn("DOMPurify not loaded, falling back to text content");
        const escaped = String(html)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
        return markup(escaped);
    }
    _ensureHooks();
    const clean = window.DOMPurify.sanitize(html, {
        ALLOWED_TAGS,
        ALLOWED_ATTR,
    });
    return markup(clean);
}

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
    const clean = window.DOMPurify.sanitize(html, {
        ALLOWED_TAGS,
        ALLOWED_ATTR,
    });
    return markup(clean);
}

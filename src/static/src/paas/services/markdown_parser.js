/** @odoo-module **/

import { markup } from "@odoo/owl";

/**
 * Configure marked.js with secure defaults
 */
if (window.marked) {
    const renderer = {
        code(code, language) {
            if (language === "mermaid") {
                const encoded = btoa(unescape(encodeURIComponent(code)));
                const escaped = code
                    .replace(/&/g, "&amp;")
                    .replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;");
                return `<div class="o_woow_mermaid" data-mermaid="${encoded}"><pre><code>${escaped}</code></pre></div>`;
            }
            // Non-mermaid: return false to use marked's default renderer
            return false;
        },
    };

    window.marked.use({
        gfm: true,              // GitHub Flavored Markdown
        breaks: true,           // Single line breaks become <br>
        headerIds: false,       // Don't generate header IDs
        mangle: false,          // Don't mangle email addresses
        renderer,
    });
}

/**
 * Convert Markdown to safe HTML markup for OWL rendering.
 *
 * This function:
 * 1. Parses Markdown to HTML using marked.js
 * 2. Sanitizes the HTML using DOMPurify with a whitelist
 * 3. Returns an OWL markup() object for safe rendering with t-out
 *
 * @param {string} markdown - Markdown text to convert
 * @returns {Markup|string} OWL markup object or empty string
 *
 * @example
 * // In OWL component
 * import { parseMarkdown } from "../../services/markdown_parser";
 *
 * get formattedBody() {
 *     return parseMarkdown(this.state.text);
 * }
 *
 * // In template
 * <div t-out="formattedBody"/>
 */
export function parseMarkdown(markdown) {
    if (!markdown) {
        return "";
    }

    // Fallback if marked.js is not loaded
    if (!window.marked) {
        console.warn("marked.js not loaded, falling back to plain text with line breaks");
        const escaped = markdown
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/\n/g, "<br>");
        return markup(escaped);
    }

    // Markdown → HTML
    const rawHtml = window.marked.parse(markdown);

    // DOMPurify whitelist sanitization
    const cleanHtml = window.DOMPurify.sanitize(rawHtml, {
        ALLOWED_TAGS: [
            "p", "br", "strong", "em", "b", "i",
            "code", "pre", "blockquote",
            "ul", "ol", "li",
            "a", "h1", "h2", "h3", "h4", "h5", "h6",
            "table", "thead", "tbody", "tr", "th", "td",
            "hr", "del", "span", "div",
        ],
        ALLOWED_ATTR: ["href", "target", "rel", "class", "data-mermaid", "data-processed"],
    });

    return markup(cleanHtml);
}

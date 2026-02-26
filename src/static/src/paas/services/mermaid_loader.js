/** @odoo-module **/

/**
 * Mermaid.js Lazy Loader Service
 *
 * Provides on-demand loading of mermaid.js for rendering diagrams in chat messages.
 * The library (~2.8MB) is only fetched when the first mermaid block is encountered,
 * keeping the initial bundle size small.
 *
 * @example
 * import { ensureLoaded, renderMermaidBlocks } from "../services/mermaid_loader";
 *
 * // In a component's mounted/patched hook:
 * const container = this.rootRef.el;
 * if (container.querySelector(".o_woow_mermaid:not([data-processed])")) {
 *     await renderMermaidBlocks(container);
 * }
 */

const SCRIPT_URL = "/woow_paas_platform/static/lib/mermaid/mermaid.min.js";

/** @type {Promise<void>|null} */
let _loadPromise = null;

/** Counter for generating unique render IDs */
let _renderCounter = 0;

/**
 * Ensure mermaid.js is loaded and initialized.
 *
 * On the first call, a <script> tag is dynamically inserted into the document.
 * Subsequent calls return the cached Promise so the library is never loaded twice.
 *
 * After the script loads, mermaid is initialized with secure defaults:
 * - startOnLoad: false (we control rendering manually)
 * - securityLevel: 'strict' (prevent XSS in diagram content)
 * - theme: 'default'
 *
 * @returns {Promise<void>} Resolves when mermaid is ready to use
 * @throws {Error} If the script fails to load
 */
export function ensureLoaded() {
    if (_loadPromise) {
        return _loadPromise;
    }

    _loadPromise = new Promise((resolve, reject) => {
        // Check if mermaid is already available (e.g. loaded by another mechanism)
        if (window.mermaid) {
            window.mermaid.initialize({
                startOnLoad: false,
                securityLevel: "strict",
                theme: "default",
            });
            resolve();
            return;
        }

        const script = document.createElement("script");
        script.src = SCRIPT_URL;
        script.type = "text/javascript";

        script.onload = () => {
            if (!window.mermaid) {
                reject(new Error("mermaid.js loaded but window.mermaid is not available"));
                return;
            }
            window.mermaid.initialize({
                startOnLoad: false,
                securityLevel: "strict",
                theme: "default",
            });
            resolve();
        };

        script.onerror = () => {
            // Reset so a retry is possible on next call
            _loadPromise = null;
            reject(new Error(`Failed to load mermaid.js from ${SCRIPT_URL}`));
        };

        document.head.appendChild(script);
    });

    return _loadPromise;
}

/**
 * Scan a container for unprocessed mermaid blocks and render them as SVG.
 *
 * Looks for elements matching `.o_woow_mermaid:not([data-processed])`.
 * Each element must have a `data-mermaid` attribute containing base64-encoded
 * mermaid diagram source code.
 *
 * Blocks are rendered sequentially to avoid mermaid.render() concurrency issues.
 *
 * On success, the element's inner HTML is replaced with the rendered SVG.
 * On failure, an error message is prepended while keeping the original code visible.
 * In both cases, `data-processed="true"` is set to prevent re-processing.
 *
 * @param {HTMLElement} containerEl - The DOM element to scan for mermaid blocks
 * @returns {Promise<void>}
 */
export async function renderMermaidBlocks(containerEl) {
    if (!containerEl) {
        return;
    }

    const blocks = containerEl.querySelectorAll(".o_woow_mermaid:not([data-processed])");
    if (blocks.length === 0) {
        return;
    }

    // Only load the library when we actually have blocks to render
    await ensureLoaded();

    // Render sequentially to avoid mermaid.render() concurrency issues
    for (const block of blocks) {
        const encodedContent = block.getAttribute("data-mermaid");
        if (!encodedContent) {
            block.setAttribute("data-processed", "true");
            continue;
        }

        let diagramSource;
        try {
            diagramSource = atob(encodedContent);
        } catch (e) {
            console.error("[mermaid_loader] Failed to decode base64 content:", e);
            _showError(block, "Failed to decode diagram content");
            block.setAttribute("data-processed", "true");
            continue;
        }

        const uniqueId = `o_woow_mermaid_${++_renderCounter}`;

        try {
            const { svg } = await window.mermaid.render(uniqueId, diagramSource);
            block.innerHTML = svg;
        } catch (e) {
            console.error("[mermaid_loader] Failed to render mermaid diagram:", e);
            _showError(block, e.message || "Failed to render diagram");
        }

        block.setAttribute("data-processed", "true");
    }
}

/**
 * Display an error message inside a mermaid block while preserving the
 * original code content so users can still read the diagram source.
 *
 * @param {HTMLElement} block - The mermaid container element
 * @param {string} message - Human-readable error description
 * @private
 */
function _showError(block, message) {
    const errorDiv = document.createElement("div");
    errorDiv.className = "o_woow_mermaid_error";
    errorDiv.style.cssText =
        "color: #dc3545; font-size: 0.85em; padding: 4px 8px; " +
        "margin-bottom: 4px; border-left: 3px solid #dc3545; background: #fff5f5;";
    errorDiv.textContent = `Diagram render error: ${message}`;
    block.insertBefore(errorDiv, block.firstChild);
}

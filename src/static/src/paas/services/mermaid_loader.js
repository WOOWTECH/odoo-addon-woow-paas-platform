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

    // Render sequentially to avoid mermaid.render() concurrency issues
    for (const block of blocks) {
        const encodedContent = block.getAttribute("data-mermaid");
        if (!encodedContent) {
            _showError(block, "空的圖表定義");
            block.setAttribute("data-processed", "true");
            continue;
        }

        let diagramSource;
        try {
            diagramSource = atob(encodedContent);
        } catch (e) {
            console.error("[mermaid_loader] Failed to decode base64 content:", e);
            _showError(block, "圖表資料解碼失敗");
            block.setAttribute("data-processed", "true");
            continue;
        }

        if (!diagramSource || !diagramSource.trim()) {
            _showError(block, "空的圖表定義");
            block.setAttribute("data-processed", "true");
            continue;
        }

        _showLoading(block);

        try {
            await ensureLoaded();
        } catch (loadErr) {
            console.error("[mermaid_loader] Failed to load library:", loadErr);
            _removeLoading(block);
            _showError(block, "無法載入圖表渲染引擎，請重新整理頁面");
            block.setAttribute("data-processed", "true");
            continue;
        }

        const uniqueId = `o_woow_mermaid_${++_renderCounter}`;

        try {
            const { svg } = await window.mermaid.render(uniqueId, diagramSource);
            _removeLoading(block);
            block.innerHTML = `<div class="o_woow_mermaid__svg_wrapper">${svg}</div>`;
            _addToolbar(block);
            _attachInteraction(block);
        } catch (e) {
            console.error("[mermaid_loader] Render error:", e);
            _removeLoading(block);
            _showError(block, "圖表語法有誤，請檢查 Mermaid 語法");
            // Original <pre><code> remains visible below the error
        }

        block.setAttribute("data-processed", "true");
    }
}

/**
 * Inject the zoom/pan/reset toolbar into a rendered mermaid container.
 *
 * @param {HTMLElement} block - The mermaid container element
 * @private
 */
function _addToolbar(block) {
    const toolbar = document.createElement("div");
    toolbar.className = "o_woow_mermaid__toolbar";
    toolbar.innerHTML = `
        <button class="o_woow_mermaid__btn" data-action="zoom-in" title="放大">
            <span class="material-symbols-outlined">zoom_in</span>
        </button>
        <button class="o_woow_mermaid__btn" data-action="zoom-out" title="縮小">
            <span class="material-symbols-outlined">zoom_out</span>
        </button>
        <button class="o_woow_mermaid__btn" data-action="reset" title="重置">
            <span class="material-symbols-outlined">fit_screen</span>
        </button>
    `;
    block.prepend(toolbar);
}

/**
 * Apply CSS transform for zoom and pan to an element.
 *
 * @param {HTMLElement} el - Target element
 * @param {number} scale - Zoom scale factor
 * @param {number} x - Horizontal translation in pixels
 * @param {number} y - Vertical translation in pixels
 * @private
 */
function _applyTransform(el, scale, x, y) {
    el.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
}

/**
 * Attach interactive zoom, pan, and reset handlers to a mermaid container.
 *
 * Supports:
 * - Mouse wheel zoom (0.5x to 3x)
 * - Left-click drag to pan
 * - Double-click to reset
 * - Toolbar buttons for zoom-in, zoom-out, and reset
 *
 * @param {HTMLElement} container - The mermaid container element
 * @private
 */
function _attachInteraction(container) {
    let scale = 1;
    let translateX = 0;
    let translateY = 0;
    let isDragging = false;
    let startX = 0;
    let startY = 0;

    const svgWrapper = container.querySelector(".o_woow_mermaid__svg_wrapper");
    if (!svgWrapper) {
        return;
    }

    // --- Wheel zoom ---
    const onWheel = (e) => {
        e.preventDefault();
        const delta = e.deltaY > 0 ? -0.1 : 0.1;
        scale = Math.max(0.5, Math.min(3, scale + delta));
        _applyTransform(svgWrapper, scale, translateX, translateY);
    };
    container.addEventListener("wheel", onWheel, { passive: false });

    // --- Drag pan ---
    const onMouseDown = (e) => {
        // Only respond to left button; ignore toolbar button clicks
        if (e.button !== 0 || e.target.closest(".o_woow_mermaid__toolbar")) {
            return;
        }
        isDragging = true;
        startX = e.clientX - translateX;
        startY = e.clientY - translateY;
        container.style.cursor = "grabbing";
    };

    const onMouseMove = (e) => {
        if (!isDragging) {
            return;
        }
        translateX = e.clientX - startX;
        translateY = e.clientY - startY;
        _applyTransform(svgWrapper, scale, translateX, translateY);
    };

    const onMouseUp = () => {
        if (!isDragging) {
            return;
        }
        isDragging = false;
        container.style.cursor = "";
    };

    container.addEventListener("mousedown", onMouseDown);
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);

    // --- Double-click reset ---
    const onDblClick = () => {
        scale = 1;
        translateX = 0;
        translateY = 0;
        _applyTransform(svgWrapper, scale, translateX, translateY);
    };
    container.addEventListener("dblclick", onDblClick);

    // --- Toolbar buttons ---
    const onToolbarClick = (e) => {
        const btn = e.target.closest("[data-action]");
        if (!btn) {
            return;
        }
        const action = btn.getAttribute("data-action");
        if (action === "zoom-in") {
            scale = Math.min(3, scale + 0.2);
        } else if (action === "zoom-out") {
            scale = Math.max(0.5, scale - 0.2);
        } else if (action === "reset") {
            scale = 1;
            translateX = 0;
            translateY = 0;
        }
        _applyTransform(svgWrapper, scale, translateX, translateY);
    };

    const toolbar = container.querySelector(".o_woow_mermaid__toolbar");
    if (toolbar) {
        toolbar.addEventListener("click", onToolbarClick);
    }

    // Store cleanup function for potential future use
    container._mermaidCleanup = () => {
        container.removeEventListener("wheel", onWheel);
        container.removeEventListener("mousedown", onMouseDown);
        document.removeEventListener("mousemove", onMouseMove);
        document.removeEventListener("mouseup", onMouseUp);
        container.removeEventListener("dblclick", onDblClick);
        if (toolbar) {
            toolbar.removeEventListener("click", onToolbarClick);
        }
    };
}

/**
 * Display a loading indicator inside a mermaid block.
 *
 * @param {HTMLElement} block - The mermaid container element
 * @private
 */
function _showLoading(block) {
    const loading = document.createElement("div");
    loading.className = "o_woow_mermaid--loading";
    loading.innerHTML =
        '<span class="material-symbols-outlined o_woow_mermaid__spinner">progress_activity</span> 圖表載入中...';
    block.prepend(loading);
}

/**
 * Remove the loading indicator from a mermaid block.
 *
 * @param {HTMLElement} block - The mermaid container element
 * @private
 */
function _removeLoading(block) {
    const loading = block.querySelector(".o_woow_mermaid--loading");
    if (loading) {
        loading.remove();
    }
}

/**
 * Display an error message inside a mermaid block while preserving the
 * original code content so users can still read the diagram source.
 *
 * Uses the CSS class `o_woow_mermaid__error` (defined in _mermaid.scss)
 * instead of inline styles.
 *
 * @param {HTMLElement} block - The mermaid container element
 * @param {string} message - Human-readable error description (Chinese)
 * @private
 */
function _showError(block, message) {
    const errorDiv = document.createElement("div");
    errorDiv.className = "o_woow_mermaid__error";
    errorDiv.innerHTML =
        '<span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle;margin-right:4px">warning</span>' +
        message;
    block.insertBefore(errorDiv, block.firstChild);
}

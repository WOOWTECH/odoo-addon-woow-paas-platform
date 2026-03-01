/**
 * Detect system color scheme preference and set cookie for backend.
 *
 * This script:
 * 1. Detects browser/OS dark mode preference
 * 2. Sets a cookie that backend reads when user preference is "system"
 * 3. Listens for OS theme changes and reloads page
 *
 * NOTE: This script does NOT manipulate DOM directly.
 * The backend (ir_http.py) reads the cookie and applies the correct class.
 *
 * Flow:
 * - JS detects OS preference → sets cookie
 * - Backend reads cookie (if user chose "system" mode) → applies o_dark class
 * - When OS preference changes → JS reloads page → backend re-evaluates
 */

(function() {
    // Detect system preference using matchMedia
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");

    function setColorSchemeCookie() {
        const isDark = prefersDark.matches;
        const scheme = isDark ? "dark" : "light";

        // Set cookie for backend to read
        // Backend checks this cookie when user preference is "system"
        document.cookie = `color_scheme_detected=${scheme}; path=/; max-age=31536000`; // 1 year
    }

    // Set cookie immediately
    setColorSchemeCookie();

    // Listen for OS theme changes and reload page
    // Backend will re-evaluate color scheme on reload
    prefersDark.addEventListener("change", function() {
        setColorSchemeCookie();
        // Reload to let backend apply new theme
        window.location.reload();
    });
})();

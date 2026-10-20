/** @odoo-module **/

/** Plaintext stays in this component-owned closure and is consumed at most once. */
export function createLlmDocsKeyHandoff() {
  let key = "";
  let mode = "paid";
  let allowedModels = null;
  return {
    put(value, keyMode, modelAllowlist = null) {
      key = typeof value === "string" ? value : "";
      mode = keyMode === "free" ? "free" : "paid";
      allowedModels = Array.isArray(modelAllowlist) ? [...modelAllowlist] : null;
    },
    peekKey() { return key; },
    peekMode() { return mode; },
    peekAllowedModels() { return allowedModels ? [...allowedModels] : null; },
    consume() {
      const value = { key, mode, allowedModels: this.peekAllowedModels() };
      this.clear();
      return value;
    },
    clear() { key = ""; mode = "paid"; allowedModels = null; },
  };
}

/** Only the two configured public endpoints from `/api/llm/state` may be shown as service URLs. */
export function validatedLlmPublicBaseUrl(state) {
  const value = state && state.public_base_url;
  return value === "https://newapi.woowtech.io/v1" || value === "https://newapi-stg.woowtech.io/v1"
    ? value : null;
}

/** Choose only a published paid/default model returned by the model catalog. */
export function publishedPaidModel(catalog, allowedModels = null) {
  if (!catalog || catalog.state !== "ok" || !Array.isArray(catalog.items)) return null;
  const allowlist = Array.isArray(allowedModels) ? new Set(allowedModels) : null;
  const item = catalog.items.find((entry) => entry && entry.group === "default"
    && typeof entry.model === "string" && entry.model && (!allowlist || allowlist.has(entry.model)));
  return item ? item.model : null;
}

/** Quote a value as one POSIX-shell argument, including apostrophes in JSON strings. */
export function shellQuote(value) {
  return "'" + value.replace(/'/g, "'\\''") + "'";
}

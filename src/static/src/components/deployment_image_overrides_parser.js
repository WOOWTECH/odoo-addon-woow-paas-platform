/** @odoo-module **/

export function parseImageOverrides(text) {
  const value = JSON.parse(text.trim() || "{}");
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Image overrides must be a JSON object.");
  }
  return value;
}

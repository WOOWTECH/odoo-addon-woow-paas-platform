/** @odoo-module **/

/**
 * 服務品牌圖示（2026-10-09 使用者決定）：每個服務改用原生品牌圖示；MCP 伺服器用所連軟體
 * 的圖示；智慧家庭連結用 WoowTech 商標、安全存取用 Cloudflare、「雲端服務」類型用 Kubernetes。
 *
 * 圖檔在 static/src/img/brands/，來源與雜湊見同目錄 README.md。
 * 查不到的模板代碼回 null，呼叫端退回原本的 Material 圖示（新模板沒登記也不會破圖）。
 */

const BASE = "/woow_paas_platform/static/src/img/brands/";

const FILES = {
  "code-server": "code-server.png",
  cloudflare: "cloudflare.png",
  emqx: "emqx.png",
  headscale: "headscale.png",
  hermes: "hermes.png",
  "home-assistant": "home-assistant.png",
  kubernetes: "kubernetes.svg",
  litellm: "litellm.png",
  n8n: "n8n.svg",
  nextcloud: "nextcloud.png",
  odoo: "odoo.png",
  omnigent: "omnigent.png",
  opendesign: "opendesign.png",
  pi: "pi.png",
  tailscale: "tailscale.png",
  "vibe-kanban": "vibe-kanban.png",
  woowtech: "woowtech.png",
};

// cloud_app_template.slug → 品牌。MCP 伺服器對到它所連的軟體。
const TEMPLATE_BRANDS = {
  odoo: "odoo",
  "mcp-odoo": "odoo",
  "mcp-odoo-manage": "odoo",
  n8n: "n8n",
  "mcp-n8n": "n8n",
  nextcloud: "nextcloud",
  "mcp-nextcloud": "nextcloud",
  litellm: "litellm",
  "mcp-litellm": "litellm",
  "hermes-agent": "hermes",
  "mcp-hermes": "hermes",
  "open-design": "opendesign",
  "mcp-opendesign": "opendesign",
  "mcp-k8s": "kubernetes",
  "mcp-vk": "vibe-kanban",
  "ha-full": "home-assistant",
  "ha-full-apporo": "home-assistant",
  haos: "home-assistant",
  emqx: "emqx",
  omnigent: "omnigent",
  "pi-agent": "pi",
  "code-server": "code-server",
  headscale: "headscale",
  tailscale: "tailscale",
};

// 雲端服務是平台用 k3s（Kubernetes）替使用者架設的，服務類型卡片用 Kubernetes 圖示（使用者 2026-10-09 指定）。
export const CLOUD_SERVICE_BRAND = "kubernetes";
export const SMART_HOME_BRAND = "woowtech";
export const SECURITY_ACCESS_BRAND = "cloudflare";

/**
 * @param {string} brand
 * @returns {string|null} 圖檔網址；未知品牌回 null
 */
export function brandIconUrl(brand) {
  const file = brand && FILES[brand];
  return file ? BASE + file : null;
}

/**
 * @param {string} slug cloud_app_template.slug
 * @returns {string|null}
 */
export function templateBrand(slug) {
  return (slug && TEMPLATE_BRANDS[slug]) || null;
}

/**
 * 訂閱對應的品牌：有連到雲端服務就看該服務的模板；否則依方案的服務類型。
 * @param {{service_template_slug?: string, service_type?: string}} sub
 * @returns {string|null}
 */
export function subscriptionBrand(sub) {
  if (!sub) return null;
  const fromTemplate = templateBrand(sub.service_template_slug);
  if (fromTemplate) return fromTemplate;
  if (sub.service_type === "smart_home") return SMART_HOME_BRAND;
  if (sub.service_type === "security_access") return SECURITY_ACCESS_BRAND;
  return null;
}

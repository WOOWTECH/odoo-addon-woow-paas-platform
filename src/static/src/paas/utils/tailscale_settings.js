/** @odoo-module **/
import { _t } from "@web/core/l10n/translation";

/**
 * VPN 節點（charts/tailscale）設定的存檔前檢查——INFRA-TS-005-5、INFRA-TS-007-1（前端即時提示）。
 *
 * 後端 `services/tailscale_settings.py` 是權威（存檔時一定再驗一次）；這裡是同一套規則的鏡像，讓租戶在
 * 按「儲存」前就看到哪一筆不行，而不是儲存成功、節點默默略過。規則逐條對照映像 entrypoint 的
 * `parse_serve`：格式 `<tailnet 埠>=<服務>:<埠>`、埠 1–65535、只接受同一工作區的 Service、
 * Service 名是 DNS label、tailnet 埠不重複、最多 32 筆。
 *
 * 由 spec 的 `validate` 驅動（`tailscale_serve`／`tailscale_login`），表單元件不認 slug。
 */

const SERVE_MAX = 32;
const ENTRY_RE = /^([0-9]+)=([A-Za-z0-9.-]+):([0-9]+)$/;
const NAME_RE = /^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$/;
const HOST_RE = /^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$/;
const LOGIN_KEYS = ["tailscale.controlPlane", "tailscale.loginServer", "tailscale.authKey"];
const PLANES = ["official", "workspace", "custom"];

const show = (entry) => String(entry).replace(/[^A-Za-z0-9=:.,_/@-]/g, "?").slice(0, 80) || "?";
const validPort = (text) => /^[0-9]{1,5}$/.test(text) && Number(text) >= 1 && Number(text) <= 65535;

/**
 * 轉發清單 → 第一個不合格項目 `{ code, entry?, port?, max? }`；全部合格回 `null`。
 * `namespace` 為 `null`／`undefined`＝還不知道（開通頁），不做同工作區檢查（後端會做）；
 * 空字串＝namespace 不明，只接受裸的 Service 名（同後端）。
 */
export function checkServe(raw, namespace, domain = "cluster.local") {
  if (raw === undefined || raw === null || raw === false) return null;
  if (typeof raw !== "string") return { code: "serve_bad_format", entry: show(raw) };
  const checkNamespace = namespace !== null && namespace !== undefined;
  const ns = checkNamespace ? String(namespace).trim().toLowerCase() : "";
  const allowed = new Set([""]);
  if (ns) {
    allowed.add(ns);
    allowed.add(`${ns}.svc`);
    allowed.add(`${ns}.svc.${domain}`);
  }
  const seen = new Set();
  for (const piece of raw.split(",")) {
    const entry = piece.replace(/\s+/g, "");
    if (!entry) continue;
    const match = ENTRY_RE.exec(entry);
    if (!match) return { code: "serve_bad_format", entry: show(entry) };
    const [, lport, rawTarget, tport] = match;
    if (!validPort(lport) || !validPort(tport)) return { code: "serve_port_range", entry: show(entry) };
    const target = rawTarget.toLowerCase().replace(/\.$/, "");
    const dot = target.indexOf(".");
    const name = dot === -1 ? target : target.slice(0, dot);
    const rest = dot === -1 ? "" : target.slice(dot + 1);
    if (checkNamespace && !allowed.has(rest)) return { code: "serve_other_namespace", entry: show(entry) };
    if (!NAME_RE.test(name) || name === "localhost") {
      return { code: "serve_not_service_name", entry: show(entry) };
    }
    const port = Number(lport);
    if (seen.has(port)) return { code: "serve_duplicate_port", entry: show(entry), port };
    if (seen.size >= SERVE_MAX) return { code: "serve_too_many", max: SERVE_MAX };
    seen.add(port);
  }
  return null;
}

export function effectiveControlPlane(plane, loginServer) {
  if (PLANES.includes(plane)) return plane;
  if (typeof loginServer === "string" && loginServer.trim()) return "custom";
  return "official";
}

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

/** 控制平面／控制伺服器／auth key → 第一個問題 `{ code, field, entry? }` 或 `null`（同後端 check_login）。 */
export function checkLogin(plane, loginServer, authKey) {
  const effective = effectiveControlPlane(plane, loginServer);
  if (effective === "custom") {
    const server = typeof loginServer === "string" ? loginServer.trim() : "";
    if (!server) return { code: "login_server_required", field: "tailscale.loginServer" };
    const scheme = /^([a-z][a-z0-9+.-]*):\/\//i.exec(server)?.[1]?.toLowerCase();
    const host = hostOf(server);
    if (!["http", "https"].includes(scheme) || !HOST_RE.test(host) || /\s/.test(server)) {
      return { code: "login_server_invalid", field: "tailscale.loginServer", entry: show(server) };
    }
  }
  if (typeof authKey !== "string" || !authKey.trim()) return null;
  const key = authKey.trim();
  if (/\s/.test(key)) return { code: "auth_key_whitespace", field: "tailscale.authKey" };
  const isTailscaleKey = key.startsWith("tskey-");
  if (effective === "official" && !isTailscaleKey) {
    return { code: "auth_key_not_tailscale", field: "tailscale.authKey" };
  }
  const host = effective === "custom" ? hostOf(String(loginServer).trim()).toLowerCase() : "";
  const officialHost = host === "tailscale.com" || host.endsWith(".tailscale.com");
  if ((effective === "workspace" || (effective === "custom" && !officialHost)) && isTailscaleKey) {
    return { code: "auth_key_is_tailscale", field: "tailscale.authKey" };
  }
  return null;
}

/** 錯誤碼 → 租戶語言（與後端 `_spec_format_message` 同義；不回顯 key）。 */
export function settingsMessage(error) {
  switch (error?.code) {
    case "serve_bad_format":
      return _t('"%s" is not in the format tailnet-port=service:port (example: 8069=svc-1a2b3c4d:80).', error.entry);
    case "serve_port_range":
      return _t('"%s" has a port outside 1-65535.', error.entry);
    case "serve_other_namespace":
      return _t('"%s" is not a service of this workspace (no IP addresses, external names or other workspaces).', error.entry);
    case "serve_not_service_name":
      return _t('"%s" does not name a service.', error.entry);
    case "serve_duplicate_port":
      return _t("Tailnet port %s is used more than once.", error.port);
    case "serve_too_many":
      return _t("At most %s forwards are allowed.", error.max);
    case "login_server_required":
      return _t('Required when the control plane is "Custom URL".');
    case "login_server_invalid":
      return _t("Not a valid URL; use https://headscale.example.com or http://host:port.");
    case "auth_key_whitespace":
      return _t("The key contains a space or line break; paste it again as one line.");
    case "auth_key_not_tailscale":
      return _t("Official Tailscale needs a key starting with tskey-. A Headscale pre-auth key only works with a Headscale control plane.");
    case "auth_key_is_tailscale":
      return _t("A tskey- key only works with official Tailscale. Create a pre-auth key in Headplane instead.");
    default:
      return "";
  }
}

/**
 * spec 驅動的檢查：回 `{ specKey: message }`。
 *
 * @param {Array} specs 表單上的 spec（required＋optional 攤平）
 * @param {Object} values 表單值（dot-path 扁平）
 * @param {Object} options
 *   - namespace：服務的 namespace（重新設定頁有；開通頁沒有＝`null`）
 *   - changed(key)：這一格有沒有被改過（重新設定頁用；預設全部視為改過）。登入三欄都沒動時不重驗
 *     已存的組合（同後端：只改別的欄位不被舊資料擋住）。
 */
export function settingsErrors(specs, values, { namespace = null, changed = () => true } = {}) {
  const errors = {};
  const validators = new Set((specs || []).map((spec) => spec?.validate).filter(Boolean));
  const get = (key) => values?.[key];
  if (validators.has("tailscale_serve") && changed("tailscale.serve")) {
    const error = checkServe(get("tailscale.serve"), namespace);
    if (error) errors["tailscale.serve"] = settingsMessage(error);
  }
  if (validators.has("tailscale_login") && LOGIN_KEYS.some((key) => changed(key))) {
    const error = checkLogin(get("tailscale.controlPlane"), get("tailscale.loginServer"), get("tailscale.authKey"));
    if (error) errors[error.field] = settingsMessage(error);
  }
  return errors;
}

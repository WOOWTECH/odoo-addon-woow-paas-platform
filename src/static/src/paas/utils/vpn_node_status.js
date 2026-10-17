/** @odoo-module **/
import { _t } from "@web/core/l10n/translation";

/**
 * INFRA-FIX-005：VPN 節點（charts/tailscale）「VPN 節點狀態」卡的呈現邏輯（純函式）。
 *
 * 輸入是 `/node-status` 端點回的 report（後端 `services/vpn_node_status.public_report` 已把每個欄位
 * 驗成封閉集合）。這裡只做「代碼 → 文案」，不自己判斷任何能力：卡片畫不畫看 payload 的
 * `has_vpn_node_status`（capability-gate-single-source-field）。
 *
 * 文案放 JS（`_t` 單行字串）而不是模板的多行文字節點：模板文字的 msgid 會連同換行與縮排一起
 * 抽出，排版一動翻譯就對不上。
 */

/** 節點「有問題、使用者該採取行動或至少知道」的狀態。Connection Info 的 ONLINE 也讓路給它們。 */
export const NODE_PROBLEM_STATES = ["needs_login", "needs_approval", "offline", "stopped"];

export function hasNodeProblem(report) {
  return !!report && NODE_PROBLEM_STATES.includes(report.state);
}

export function nodeStateLabel(state) {
  switch (state) {
    case "online":
      return _t("Connected");
    case "offline":
      return _t("Not connected");
    case "needs_login":
      return _t("Login required");
    case "needs_approval":
      return _t("Waiting for approval");
    case "starting":
      return _t("Starting");
    case "stopped":
      return _t("Stopped");
    default:
      return _t("Unavailable");
  }
}

/** 徽章色調：ok / warn / bad / muted（對應 scss 的 `.o_woow_vpn_state_<tone>`）。 */
export function nodeStateTone(state) {
  switch (state) {
    case "online":
      return "ok";
    case "needs_login":
    case "needs_approval":
    case "offline":
      return "bad";
    case "starting":
    case "stopped":
      return "warn";
    default:
      return "muted";
  }
}

/**
 * 狀態底下的說明（一句話）。`unknown` 依 probe 說「為什麼讀不到」，不是錯誤。
 */
export function nodeStateHint(report) {
  if (!report) return "";
  switch (report.state) {
    case "online":
      return "";
    case "offline":
      return _t("The node is running, but its control server shows it as offline. It reconnects on its own; if it stays offline for several minutes, restart the service.");
    case "needs_login":
      return _t("The service is running, but this node is no longer logged in to its control server (its key expired or the node was removed there), so nothing is reachable through the VPN until it logs in again.");
    case "needs_approval":
      return _t("The control server is waiting for an administrator to approve this machine before it joins the VPN.");
    case "starting":
      return _t("The node is starting or logging in. This normally takes less than a minute.");
    case "stopped":
      return _t("The node is stopped.");
    default:
      return _t("The node status cannot be read right now (the service is starting or restarting). It is re-checked automatically.");
  }
}

/**
 * INFRA-TS-005-5：節點上一次登入失敗的原因（operator 從 tailscaled 的 Health 對應的封閉代碼）。
 * 原本只在容器日誌（`Received error: authkey expired`），租戶分不出是 key 錯、網址錯還是連不到。
 * 節點已登入（online／offline）時一律不顯示。
 */
export function loginErrorHint(report) {
  if (!report || !report.login_error) return "";
  if (report.state === "online" || report.state === "offline") return "";
  switch (report.login_error) {
    case "authkey_expired":
      return _t("Last login attempt failed: the auth key has expired. Create a new key and save it in Configuration > Auth key.");
    case "authkey_used":
      return _t("Last login attempt failed: the auth key was already used (single-use key). Create a new key, or a reusable one.");
    case "authkey_invalid":
      return _t("Last login attempt failed: the control server rejected the auth key (unknown key, or a key for another control server or user).");
    case "control_unreachable":
      return _t("Last login attempt failed: the node could not reach its control server. Check the control plane setting, and that the server is running.");
    default:
      return _t("Last login attempt failed. Check the control plane setting and the auth key.");
  }
}

/**
 * INFRA-TS-005-5：已登入的節點會靜默忽略設定裡的 auth key（entrypoint 只在日誌寫 "not using it"）。
 */
export function authKeyNotice(report) {
  if (!report || report.authkey_ignored !== true) return "";
  return _t("This node is already logged in, so the auth key in Configuration is not used. It is only used when the node needs to log in again; you can clear it.");
}

/**
 * 登入說明（只有 needs_login；needs_approval 的處理在控制台，狀態說明已夠）。回 `[{ kind, text, url?, code? }]`：
 * kind = "text" | "link" | "code"。外部登入網址與註冊碼只有 owner／admin 的 report 才有值。
 */
export function loginGuidance(report, canManage) {
  if (!report || report.state !== "needs_login") return [];
  const lines = [];
  if (!canManage) {
    lines.push({ kind: "text", text: _t("Ask a workspace owner or admin to log this node in again.") });
    return lines;
  }
  const plane = report.control_plane;
  if (plane === "workspace") {
    lines.push({ kind: "text", text: _t("To log it in again: create a new pre-auth key in Headplane of this workspace's Headscale, paste it into Configuration > Auth key, and save.") });
  } else if (plane === "custom") {
    lines.push({ kind: "text", text: _t("To log it in again: create a new pre-auth key on your control server, paste it into Configuration > Auth key, and save.") });
  } else {
    lines.push({ kind: "text", text: _t("To log it in again: create a new auth key in your Tailscale admin console, paste it into Configuration > Auth key, and save.") });
  }
  const login = report.login || {};
  if (login.kind === "external_url" && login.url) {
    lines.push({ kind: "link", text: _t("Or open the login page"), url: login.url });
  } else if (login.kind === "cluster_internal") {
    lines.push({ kind: "text", text: _t("The login page of a workspace Headscale uses an internal address that a browser cannot open, so use a pre-auth key.") });
    if (login.register_key) {
      lines.push({ kind: "code", text: _t("Registration code waiting for approval on the control server"), code: login.register_key });
    }
  }
  return lines;
}

export function forwardStatusLabel(status) {
  switch (status) {
    case "applied":
      return _t("Active");
    case "not_applied":
      return _t("Not active yet");
    case "service_missing":
      return _t("Service not found in this workspace");
    case "mismatch":
      return _t("A different target is active");
    case "unverified":
      return _t("Active (target not verified)");
    default:
      return _t("Unknown");
  }
}

export function forwardStatusTone(status) {
  if (status === "applied") return "ok";
  if (status === "not_applied" || status === "unverified") return "warn";
  return "bad";
}

export function skipReasonLabel(reason) {
  switch (reason) {
    case "bad_format":
      return _t("Wrong format. Use tailnet-port=service:port");
    case "port_range":
      return _t("Ports must be between 1 and 65535");
    case "other_namespace":
      return _t("Only Services of this workspace are allowed (no IP addresses, external names or other workspaces)");
    case "not_service_name":
      return _t("Not a valid Service name");
    case "duplicate_port":
      return _t("This tailnet port is already used by an earlier entry");
    case "too_many":
      return _t("At most 32 forwards are allowed");
    case "namespace_unknown":
      return _t("The whole list is ignored: the node cannot tell which workspace it belongs to");
    default:
      return _t("Skipped by the node");
  }
}

/**
 * 轉發清單的列：生效／待生效／目標不存在的轉發，加上被略過的項目（附原因）。
 * `wanted_known=false`（舊映像沒有 entrypoint 的解析器）時只列得出實際生效的。
 */
export function forwardRows(report) {
  const serve = report?.serve;
  if (!serve) return [];
  const rows = [];
  for (const forward of serve.forwards || []) {
    const target = forward.service
      ? `${forward.service}${forward.target_port ? `:${forward.target_port}` : ""}`
      : "";
    rows.push({
      key: `f${forward.port}`,
      skipped: false,
      label: `${forward.port} → ${target}`,
      status: forwardStatusLabel(forward.status),
      tone: forwardStatusTone(forward.status),
    });
  }
  (serve.skipped || []).forEach((item, index) => {
    rows.push({
      key: `s${index}`,
      skipped: true,
      label: item.entry,
      status: skipReasonLabel(item.reason),
      tone: "bad",
    });
  });
  return rows;
}

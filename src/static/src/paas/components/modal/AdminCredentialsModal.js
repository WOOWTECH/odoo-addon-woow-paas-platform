/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";

// 與後端 TENANT_ADMIN_USERNAME_RE / TENANT_ADMIN_PASSWORD_RE（models/cloud_service.py）
// 同一條規則。前端先擋只是為了即時提示；權威判準在後端，chart 端 fail render 是第三道。
const USERNAME_RE = /^[A-Za-z0-9._@-]{1,64}$/;
const PASSWORD_RE = /^[\x21-\x7e]{12,128}$/;

/**
 * 租戶自設 admin 帳號／密碼（template.admin_credentials_settable 為 true 的服務）。
 * usernameEditable=false（template.admin_username_editable 為 false，例 code-server
 * 只有密碼、沒有帳號）時不顯示帳號欄、也不送 username。
 *
 * 密碼兩種模式：平台隨機產生（預設，成功後由 OneTimeCredentialsModal 一次性顯示）
 * 或自行設定（後端不回傳明文）。儲存走 helm upgrade；重啟範圍由 template 旗標
 * credential_change_restart_scope（restartScope prop）描述，不在前端自建 slug 判準。
 *
 * #1557：有 gateway 的服務（LiteLLM 的 master key）多一個「一併換掉 Gateway API Key」
 * 勾選，預設不勾＝保留目前的 key（腳本與整合不受影響）；勾了才換發，並在送出前把後果
 * 寫清楚。判準讀 `service.has_gateway`（與後端交付判準同一欄位，repo-lint
 * check_gateway_copy_gate_anchor 鎖這段文案的 gate）。
 */
export class AdminCredentialsModal extends Component {
  static template = "woow_paas_platform.AdminCredentialsModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    workspaceId: { type: Number },
    serviceId: { type: Number },
    currentUsername: { type: [String, { value: null }], optional: true },
    usernameEditable: { type: Boolean, optional: true },
    restartScope: { type: String, optional: true },
    // #1557：detail payload（讀 has_gateway）。沒傳＝當作沒有 gateway。
    service: { type: Object, optional: true },
    onClose: { type: Function },
    onSaved: { type: Function },
  };

  setup() {
    this.state = useState({
      username: this.props.currentUsername || "",
      passwordMode: "generate",
      password: "",
      passwordConfirm: "",
      // #1557：預設保留目前的 Gateway API Key。
      rotateGatewayKey: false,
      error: null,
    });
  }

  /** #1557：這台服務有沒有 Gateway API Key（fail-closed：欄位缺席＝沒有）。 */
  get hasGateway() {
    return this.props.service?.has_gateway === true;
  }

  onRotateGatewayKeyChange(ev) {
    this.state.rotateGatewayKey = !!ev.target.checked;
    this.state.error = null;
  }

  get loading() {
    return !!cloudService.operationLoading.resetAdminPassword;
  }

  onUsernameInput(ev) {
    this.state.username = ev.target.value;
    this.state.error = null;
  }

  onPasswordModeChange(ev) {
    this.state.passwordMode = ev.target.value;
    this.state.error = null;
  }

  onPasswordInput(ev) {
    this.state.password = ev.target.value;
    this.state.error = null;
  }

  onPasswordConfirmInput(ev) {
    this.state.passwordConfirm = ev.target.value;
    this.state.error = null;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.loading) {
      this.props.onClose();
    }
  }

  get showUsername() {
    // 預設 true：沿用既有呼叫端（沒傳這個 prop）的行為。
    return this.props.usernameEditable !== false;
  }

  async onSubmit() {
    const username = this.state.username.trim();
    if (this.showUsername && !USERNAME_RE.test(username)) {
      this.state.error = _t("Username must be 1-64 characters: letters, digits, and . _ @ -");
      return;
    }
    let password = null;
    if (this.state.passwordMode === "custom") {
      password = this.state.password;
      if (!PASSWORD_RE.test(password)) {
        this.state.error = _t(
          "Password must be 12-128 characters, using letters, digits and symbols only (no spaces)."
        );
        return;
      }
      if (password !== this.state.passwordConfirm) {
        this.state.error = _t("The two passwords do not match.");
        return;
      }
    }

    this.state.error = null;
    // 帳號沒變就不送，避免一次無意義的 values 寫入。
    // #1557：一律明確送出要不要換 Gateway API Key（後端預設不換）。
    const payload = {
      password,
      rotate_gateway_key: this.hasGateway && this.state.rotateGatewayKey,
    };
    if (this.showUsername && username !== (this.props.currentUsername || "")) {
      payload.username = username;
    }
    const result = await cloudService.setAdminCredentials(
      this.props.workspaceId,
      this.props.serviceId,
      payload
    );
    if (result.success) {
      this.props.onSaved(result.data);
    } else {
      this.state.error = result.error || _t("Failed to update admin credentials");
    }
  }
}

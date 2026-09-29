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
 * 租戶自設 admin 帳號／密碼（template.admin_username_editable 為 true 的服務）。
 *
 * 密碼兩種模式：平台隨機產生（預設，成功後由 OneTimeCredentialsModal 一次性顯示）
 * 或自行設定（後端不回傳明文）。儲存走 helm upgrade ＋ rollout restart，服務會重啟。
 */
export class AdminCredentialsModal extends Component {
  static template = "woow_paas_platform.AdminCredentialsModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    workspaceId: { type: Number },
    serviceId: { type: Number },
    currentUsername: { type: [String, { value: null }], optional: true },
    onClose: { type: Function },
    onSaved: { type: Function },
  };

  setup() {
    this.state = useState({
      username: this.props.currentUsername || "",
      passwordMode: "generate",
      password: "",
      passwordConfirm: "",
      error: null,
    });
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

  async onSubmit() {
    const username = this.state.username.trim();
    if (!USERNAME_RE.test(username)) {
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
    const payload = { password };
    if (username !== (this.props.currentUsername || "")) {
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

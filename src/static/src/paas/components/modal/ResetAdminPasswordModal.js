/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * ResetAdminPasswordModal（#1560）
 *
 * 「重設管理員密碼」的確認框。原本一按就直接重設：目前的密碼立刻失效，MCP 類
 * 服務還會重啟、連線中斷；誤按就把正在用的管理員鎖在外面（N10）。
 *
 * 後果依重設方式而不同，判準全部由父層傳入（都是父層已有的 payload 鏡射 getter，
 * 本元件不自建判準）：
 *   - appReset：operator 直接改 app 資料庫（Odoo／n8n／Omnigent）——不重啟、立即生效
 *   - keepsSessions：app 重設不登出既有 session（Omnigent）
 *   - noSecondFactor：app 重設會登出、但沒有 2FA 可關（EMQX 開源版，#1774）
 * Gateway API Key 會不會一併換發（#530，hermes-agent）讀 service payload 的
 * `has_gateway`（getter `replacesGatewayKey`）——與後端交付判準同一個欄位，
 * repo-lint check_gateway_copy_gate_anchor 鎖這句文案的 gate。
 *
 * Props:
 *   - service (Object): detail payload（含 has_gateway）
 *   - appReset (Boolean)
 *   - keepsSessions (Boolean)
 *   - noSecondFactor (Boolean)
 *   - onClose (Function)
 *   - onConfirm (Function): 回傳 Promise；完成後由父層關閉本視窗
 */
export class ResetAdminPasswordModal extends Component {
  static template = "woow_paas_platform.ResetAdminPasswordModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    service: { type: Object },
    appReset: { type: Boolean, optional: true },
    keepsSessions: { type: Boolean, optional: true },
    noSecondFactor: { type: Boolean, optional: true },
    onClose: { type: Function },
    onConfirm: { type: Function },
  };

  setup() {
    this.state = useState({ resetting: false });
  }

  /** fail-closed：欄位缺席一律不提 gateway key。 */
  get replacesGatewayKey() {
    return this.props.service?.has_gateway === true;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.resetting) {
      this.props.onClose();
    }
  }

  async onReset() {
    if (this.state.resetting) {
      return;
    }
    this.state.resetting = true;
    try {
      await this.props.onConfirm();
    } finally {
      this.state.resetting = false;
    }
  }
}

/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";

/**
* RestartServiceModal（#1432）
*
* 租戶的「重新啟動」確認框：說明會發生什麼（所有元件依序重建、約一分鐘、進行中的
* 工作會中斷、資料與設定不受影響），確認後呼叫 `action=restart`。後端只下達不等待，
* 回來時服務已轉 `upgrading`，詳情頁的輪詢會在就緒後轉回 running。
*
* Props:
*   - service (Object): { id, name, workspace_id }
*   - onClose (Function)
*   - onRestarted (Function): 成功送出後呼叫
*/
export class RestartServiceModal extends Component {
  static template = "woow_paas_platform.RestartServiceModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    service: { type: Object },
    onClose: { type: Function },
    onRestarted: { type: Function },
  };

  setup() {
    this.state = useState({ restarting: false, error: null });
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.restarting) {
      this.props.onClose();
    }
  }

  async onRestart() {
    if (this.state.restarting) {
      return;
    }
    this.state.restarting = true;
    this.state.error = null;
    const result = await cloudService.restartService(
      this.props.service.workspace_id,
      this.props.service.id
    );
    if (result.success) {
      this.props.onRestarted(result.data);
    } else {
      this.state.error = result.error || _t("Failed to restart the service");
      this.state.restarting = false;
    }
  }
}

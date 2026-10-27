/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowIcon } from "../icon/WoowIcon";
import { adminPageLabel, mcpSetupNotice } from "../../utils/mcp_setup_status";

/**
 * 2026-10-27：服務頁的「MCP 尚未設定連線」提示（純呈現）。
 *
 * 第五輪起輪詢不在這裡：服務頁（ServiceDetailPage）一頁一個 `useMcpSetupStatus`，頁首這則提示與
 * Overview 的 Connection Info 讀同一份結果。父層只在 `mcpSetupCondition` 有值時掛載（該 getter 的 gate＝
 * payload 的 `has_mcp_setup_status` 且服務 running，前端不用 slug 判）。
 *
 * 能管理這台服務的人（`canManage`＝payload 的 `viewer_can_manage === true`）看到「到管理頁設定」與管理頁
 * 連結；其他成員看到「請工作區擁有者或管理員設定」，不給連結（他們拿不到管理密碼，連過去也登不進去）。
 * 不改服務狀態：徽章照樣 Running（服務是正常的，只是要設定）。
 *
 * Props:
 *   - condition (String): `mcpSetupCondition` 的結果（not_configured／not_running／backend_missing）
 *   - adminUrl (String, optional): 後端給的管理頁網址（已過 safeAdminUrl）
 *   - canManage (Boolean, optional): payload 的 viewer_can_manage
 */
export class McpSetupStatusNotice extends Component {
  static template = "woow_paas_platform.McpSetupStatusNotice";
  static components = { WoowIcon };
  static props = {
    condition: { type: String },
    adminUrl: { type: String, optional: true },
    canManage: { type: Boolean, optional: true },
  };

  get notice() {
    return mcpSetupNotice(this.props.condition, this.props.canManage === true);
  }

  /** 管理頁連結只給能管理的人；其他成員的文案是「請擁有者或管理員設定」。 */
  get adminUrl() {
    return this.props.canManage === true ? this.props.adminUrl || "" : "";
  }

  get adminPageLabel() {
    return adminPageLabel();
  }
}

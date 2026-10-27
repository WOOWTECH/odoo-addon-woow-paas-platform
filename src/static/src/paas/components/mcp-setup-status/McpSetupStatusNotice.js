/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowIcon } from "../icon/WoowIcon";
import { useMcpSetupStatus } from "../../services/mcp_setup_status";
import { adminPageLabel, mcpSetupNotice } from "../../utils/mcp_setup_status";

/**
 * 2026-10-27：服務頁的「MCP 尚未設定連線」提示。
 *
 * 父層只在 payload 的 `has_mcp_setup_status`（後端 `_chart_has_mcp_setup_status`，mcp-server 家族）為 true
 * 時掛載本元件；本元件自己再讀一次同一個旗標決定要不要輪詢（fail-closed：欄位缺席就不問）。
 * 只有服務在 running 時才問 `/mcp-status`；not_configured／not_running 才畫提示，running／unknown 不畫。
 * 不改服務狀態：徽章照樣 Running（服務是正常的，只是要設定）。
 *
 * Props:
 *   - service (Object): service detail payload
 *   - workspaceId (Number|String): 所屬工作區
 */
export class McpSetupStatusNotice extends Component {
  static template = "woow_paas_platform.McpSetupStatusNotice";
  static components = { WoowIcon };
  static props = {
    service: { type: Object },
    workspaceId: { type: [Number, String] },
  };

  setup() {
    this.mcp = useMcpSetupStatus(
      () => this.props.workspaceId,
      () => this.props.service.id,
      () => this.props.service.has_mcp_setup_status === true && this.props.service.state === "running",
    );
  }

  get notice() {
    return mcpSetupNotice(this.mcp.state.status);
  }

  get adminUrl() {
    return this.mcp.state.adminUrl;
  }

  get adminPageLabel() {
    return adminPageLabel();
  }
}

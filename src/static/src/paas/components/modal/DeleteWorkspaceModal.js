/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { workspaceService } from "../../services/workspace_service";
import { router } from "../../core/router";

/** #1295：後端拒絕封存（底下還有未刪除的服務）時回的結構化錯誤碼。 */
export const ERR_WORKSPACE_HAS_SERVICES = "workspace_has_services";

/**
 * DeleteWorkspaceModal Component
 *
 * 刪除（封存）工作區的確認視窗：輸入工作區名稱才能送出。
 *
 * #1295：工作區底下還有未刪除的服務時，後端回 `code: "workspace_has_services"`
 * 與服務清單；本元件把清單列出來（可點擊前往該服務頁面刪除），不再送出。
 *
 * Props:
 *   - workspace (Object): Workspace data { id, name }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onDeleted (Function): Called after successful deletion
 */
export class DeleteWorkspaceModal extends Component {
  static template = "woow_paas_platform.DeleteWorkspaceModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    workspace: { type: Object },
    onClose: { type: Function },
    onDeleted: { type: Function },
  };

  setup() {
    this.state = useState({
      confirmName: "",
      deleting: false,
      error: null,
      blockingServices: [],
    });
  }

  get canDelete() {
    return this.state.confirmName.trim() === this.props.workspace.name;
  }

  get hasBlockingServices() {
    return this.state.blockingServices.length > 0;
  }

  serviceTypeLabel(type) {
    if (type === "security_access") {
      return _t("Security Access");
    }
    if (type === "smart_home") {
      return _t("Smart Home");
    }
    return _t("Cloud Service");
  }

  servicePath(service) {
    const wsId = this.props.workspace.id;
    if (service.type === "security_access") {
      return `workspace/${wsId}/security-access/${service.id}/overview`;
    }
    if (service.type === "smart_home") {
      return `workspace/${wsId}/smarthome/${service.id}`;
    }
    return `workspace/${wsId}/services/${service.id}`;
  }

  goToService(service) {
    this.props.onClose();
    router.navigate(this.servicePath(service));
  }

  onConfirmNameInput(ev) {
    this.state.confirmName = ev.target.value;
    this.state.error = null;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.deleting) {
      this.props.onClose();
    }
  }

  async onDelete() {
    if (!this.canDelete || this.state.deleting) {
      return;
    }

    this.state.deleting = true;
    this.state.error = null;
    this.state.blockingServices = [];

    const result = await workspaceService.deleteWorkspace(this.props.workspace.id);

    if (result.success) {
      this.props.onDeleted();
      return;
    }
    if (result.code === ERR_WORKSPACE_HAS_SERVICES && result.services && result.services.length) {
      this.state.blockingServices = result.services;
    } else {
      this.state.error = result.error || _t("Failed to delete workspace");
    }
    this.state.deleting = false;
  }
}

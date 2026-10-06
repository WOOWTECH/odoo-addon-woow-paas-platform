/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";
import { formatUserDateTime } from "../../utils/datetime_helpers";

/**
 * RollbackModal Component
 *
 * Displays revision history and allows rollback to a previous version.
 *
 * Props:
 *   - service (Object): Service data { id, workspace_id, helm_revision, name }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onRollback (Function): Called after successful rollback
 */
export class RollbackModal extends Component {
    static template = "woow_paas_platform.RollbackModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        service: { type: Object },
        onClose: { type: Function },
        onRollback: { type: Function },
    };

    setup() {
        this.state = useState({
            revisions: [],
            selectedRevision: null,
            loading: true,
            rolling: false,
            error: null,
            // #1433：選好版本後先進確認步驟（說明會回復什麼、不會回復什麼），
            // 再按一次才真的送出。原本單擊即執行、對話框也沒提帳密。
            confirming: false,
        });

        onMounted(() => {
            this.loadRevisions();
        });
    }

    get currentRevision() {
        return this.props.service.helm_revision || 1;
    }

    get canRollback() {
        return (
            this.state.selectedRevision !== null &&
            this.state.selectedRevision !== this.currentRevision &&
            !this.state.rolling
        );
    }

    async loadRevisions() {
        this.state.loading = true;
        this.state.error = null;

        const result = await cloudService.fetchRevisions(
            this.props.service.workspace_id,
            this.props.service.id
        );

        if (result.success) {
            this.state.revisions = result.data || [];
        } else {
            this.state.error = result.error || _t("Failed to load revision history");
        }

        this.state.loading = false;
    }

    selectRevision(revision) {
        if (revision.revision !== this.currentRevision && !this.state.rolling) {
            this.state.selectedRevision = revision.revision;
        }
    }

    /**
     * #1433：平台管的管理員帳密在回復時保留目前的值（後端改走保留憑證的回復）。
     * 判準鏡射 `template.has_admin_gui`——與後端 `_rollback_preserved_value_paths`
     * 同一個欄位，前端不自建 slug 名單。
     */
    get keepsAdminCredentials() {
        return !!this.props.service.template?.has_admin_gui;
    }

    _revisionByNumber(number) {
        return this.state.revisions.find((r) => r.revision === number) || null;
    }

    /** 確認步驟的 chart 版本差異；兩邊相同或缺資料時回 null（不顯示這一行）。 */
    get chartChange() {
        const current = this._revisionByNumber(this.currentRevision);
        const target = this._revisionByNumber(this.state.selectedRevision);
        if (!current || !target || !current.chart || !target.chart) {
            return null;
        }
        if (current.chart === target.chart) {
            return null;
        }
        return { from: current.chart, to: target.chart };
    }

    onBack() {
        if (!this.state.rolling) {
            this.state.confirming = false;
        }
    }

    isSelected(revision) {
        return this.state.selectedRevision === revision.revision;
    }

    isCurrent(revision) {
        return revision.revision === this.currentRevision;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.rolling) {
            this.props.onClose();
        }
    }

    /**
     * 依 Odoo 使用者語系格式化 helm revision 的時間戳。
     *
     * 輸入來自 paas-operator 的 `helm history --output json`，
     * 實測長相為 `2026-08-26T10:44:22.962052+08:00`（帶 offset）。
     */
    formatDate(dateStr) {
        return formatUserDateTime(dateStr, { showSeconds: false }, "-");
    }

    getStatusClass(status) {
        const statusMap = {
            deployed: "o_woow_revision_success",
            superseded: "o_woow_revision_superseded",
            failed: "o_woow_revision_failed",
            pending: "o_woow_revision_pending",
        };
        return statusMap[status] || "";
    }

    async onRollback() {
        if (!this.canRollback) {
            return;
        }
        if (!this.state.confirming) {
            this.state.confirming = true;
            this.state.error = null;
            return;
        }

        this.state.rolling = true;
        this.state.error = null;

        const result = await cloudService.rollbackService(
            this.props.service.workspace_id,
            this.props.service.id,
            this.state.selectedRevision
        );

        if (result.success) {
            this.props.onRollback();
        } else {
            this.state.error = result.error || _t("Failed to rollback service");
            this.state.rolling = false;
        }
    }
}

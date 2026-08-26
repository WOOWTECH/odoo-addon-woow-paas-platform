/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { reconcileReportService } from "../../services/reconcile_report_service";
import { router } from "../../core/router";
import { formatUserDateTime } from "../../utils/datetime_helpers";

/**
 * ReconcileReportPage — /woow 後台「L4 反向孤兒對帳報表」頁（admin-only）。
 *
 * dry-run 唯讀清單：列出 CF 帳號級 inventory 對 Odoo 左集取差集 + 分類後的孤兒候選。
 * **刪除權留人手**：本頁不刪任何資源；每筆提供「重新驗證是否仍為孤兒」（唯讀重算，防 TOCTOU）。
 * **no-silent-drop**：含 reconcilable=false（cross-env / protected / unknown）全列呈現。
 */
export class ReconcileReportPage extends Component {
    static template = "woow_paas_platform.ReconcileReportPage";
    static components = { WoowCard, WoowIcon, WoowButton };

    setup() {
        this.service = useState(reconcileReportService);
        this.router = router;
        // 逐筆 reverify 的進行中旗標 / 結果（key = Odoo row id）。
        this.state = useState({
            reverifying: {},
            reverifyResult: {},
        });
        onMounted(() => {
            reconcileReportService.fetch();
        });
    }

    get loading() {
        return this.service.loading && this.service.candidates.length === 0;
    }
    get error() {
        return this.service.error;
    }
    get candidates() {
        return this.service.candidates;
    }
    get scannedAt() {
        return this.service.scannedAt;
    }
    get isPartial() {
        return this.service.partial;
    }
    get isEmpty() {
        return !this.loading && !this.error && this.candidates.length === 0;
    }

    /** 依 Odoo 使用者語系格式化掃描時間。 */
    formatDateTime(iso) {
        return formatUserDateTime(iso, { showSeconds: false }, "—");
    }

    envBadgeClass(envHint) {
        return envHint === "unknown"
            ? "o_woow_recon_env_unknown"
            : "o_woow_recon_env_known";
    }

    reconcilableLabel(candidate) {
        return candidate.reconcilable ? _t("Safe to reclaim") : _t("Manual review needed");
    }
    reconcilableClass(candidate) {
        return candidate.reconcilable
            ? "o_woow_recon_badge_actionable"
            : "o_woow_recon_badge_manual";
    }

    isReverifying(candidate) {
        return !!this.state.reverifying[candidate.id];
    }
    reverifyVerdict(candidate) {
        return this.state.reverifyResult[candidate.id] || null;
    }

    async refresh() {
        await reconcileReportService.fetch();
    }

    async reverify(candidate) {
        if (this.state.reverifying[candidate.id]) return;
        this.state.reverifying[candidate.id] = true;
        try {
            const res = await reconcileReportService.reverify(
                candidate.kind,
                candidate.resource_uid,
            );
            this.state.reverifyResult[candidate.id] = res.success
                ? res.data
                : { error: res.error || _t("Re-verify failed") };
        } finally {
            this.state.reverifying[candidate.id] = false;
        }
    }

    goBack() {
        this.router.navigate("settings");
    }
}

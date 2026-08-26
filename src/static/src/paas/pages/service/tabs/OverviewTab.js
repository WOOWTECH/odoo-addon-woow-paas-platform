/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { formatDate } from "../../../services/utils";
import { formatUserDateTime } from "../../../utils/datetime_helpers";
import { cloudService } from "../../../services/cloud_service";

/**
 * OverviewTab Component
 *
 * Displays connection information and resource details for a cloud service.
 * Shows:
 * - Connection Info: Status, URLs, ports
 * - Resources: Helm release details, chart version, namespace
 *
 * Props:
 *   - service (Object): Service data object with all details
 */
export class OverviewTab extends Component {
    static template = "woow_paas_platform.OverviewTab";
    static components = { WoowCard, WoowIcon };
    static props = {
        service: { type: Object },
    };

    get isOnline() {
        return this.props.service.state === "running";
    }

    get publicUrl() {
        if (!this.props.service.subdomain) return null;
        return `https://${this.props.service.subdomain}.${cloudService.domain}`;
    }

    get helmNamespace() {
        return this.props.service.helm_namespace || `paas-ws-${this.props.service.workspace_id}`;
    }

    formatDate(dateString) {
        return formatDate(dateString);
    }

    /**
     * #917：這一格原本是裸 `t-esc`——後端原始字串直接印給租戶看，
     * 而且因為後端漏了 `.isoformat()`，那個字串還是 SQL 空格格式
     * （`2026-08-26 12:34:56`），與同一張卡片其他日期的格式不同。
     *
     * 本批兩端一起修：後端補 `.isoformat()`（`controllers/paas.py`），
     * 前端走 shared helper ⇒ 這條格式線被消除，不是被前端吸收。
     *
     * ⚠ 這一格與同檔的 `formatDate`（走 `services/utils.js`）必須一起改：
     * 本批改了 `services/utils.js` 的日期格式 ⇒ 不修這格的話，
     * 同一張卡片會出現兩種日期格式。
     */
    formatSyncedAt(dateString) {
        return formatUserDateTime(dateString, { showSeconds: false }, "");
    }

    copyToClipboard(text) {
        if (navigator.clipboard) {
            navigator.clipboard.writeText(text);
        }
    }
}

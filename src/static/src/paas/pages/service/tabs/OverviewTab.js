/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { formatDate } from "../../../services/utils";
import { formatUserDateTime } from "../../../utils/datetime_helpers";
import { cloudService } from "../../../services/cloud_service";
import { copyText } from "../../../utils/clipboard";
import { formatPoints } from "../../../utils/billing_helpers";
import { hasConfirmedWorkloadFault } from "../../../utils/service_presentation";
import { ResourceUsage } from "../../../components/resource-usage/ResourceUsage";
import { useVpnNodeStatus } from "../../../services/vpn_node_status";
import {
    authKeyNotice,
    forwardRows,
    hasNodeProblem,
    loginErrorHint,
    loginGuidance,
    nodeStateHint,
    nodeStateLabel,
    nodeStateTone,
} from "../../../utils/vpn_node_status";

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
    static components = { WoowCard, WoowIcon, ResourceUsage };
    static props = {
        service: { type: Object },
        resourceObservation: { type: Object, optional: true },
        resourcesLoading: { type: Boolean, optional: true },
        resourcesRefreshFailed: { type: Boolean, optional: true },
        resourcesNow: { type: Number, optional: true },
        workspaceId: { type: [Number, String], optional: true },
    };

    setup() {
        // #1310：複製按鈕原本無論成敗都沒有回饋，不支援 Clipboard API 時直接沒反應。
        this.copyState = useState({ copied: null, failed: null });
        // INFRA-FIX-005：VPN 節點狀態（登入狀態／登入入口／轉發結果）。gate 讀後端的
        // `has_vpn_node_status`（capability-gate-single-source-field）；沒有這個能力或服務不在
        // running／upgrading 時 poller 不打任何請求。
        this.vpn = useVpnNodeStatus(
            () => this.props.workspaceId ?? this.props.service.workspace_id,
            () => this.props.service.id,
            () => this.props.service.has_vpn_node_status === true
                && ["running", "upgrading"].includes(this.props.service.state),
        );
    }

    get isOnline() {
        return this.connectionStatus === "online";
    }

    /**
     * #1456：連線狀態三態。
     *
     * - running → online
     * - running，但平台已確認它沒有在執行（payload 的 workload_fault 有值，#1647）→ offline：
     *   同一頁的 banner 正在說「目前沒有在執行」，這一格不能同時寫 ONLINE（瀏覽器實走 F1）。
     *   不新增 state；狀態徽章維持 Running 是 #1647 明定的設計，不在這裡改。
     * - upgrading（改設定／改 addon 的 helm upgrade）→ updating：升級期間舊版本照常服務
     *   （R8 實測 HA／Omnigent／OD 全程 200），原本卻顯示 OFFLINE。
     * - deploying／initializing 且曾經升級過（last_upgraded_at 有值＝重新部署、addon 變更
     *   的 init）→ updating，理由同上；首次開通的 deploying／initializing 服務本來就還
     *   不能用，維持 offline。
     * - 其他（error、suspended、deleting…）→ offline。
     *
     * ⚠ 這仍是平台「最後一次觀測」的狀態，不是即時探測：running 服務平台不會持續
     * 探測 HTTP，應用程式自己重啟（例 HA 內按重新啟動）時這裡仍顯示 online（#1456 的
     * 未修部分，需 chart httpGet probe＋平台對 running 服務的低頻觀測）。#1647 的低頻觀測
     * 只涵蓋「Pod 一顆都沒有」，Pod 在但應用程式不通仍然看不出來。
     */
    get connectionStatus() {
        const { state, last_upgraded_at: lastUpgradedAt } = this.props.service;
        if (state === "running") {
            // INFRA-FIX-005：pod Ready ≠ 節點在線（NeedsLogin 時 pod 仍 Ready）。節點狀態卡讀到
            // 「需要登入／等待核可／離線／停止」時，這一格不能同時寫 ONLINE。
            if (hasConfirmedWorkloadFault(this.props.service) || hasNodeProblem(this.vpn.state.report)) {
                return "offline";
            }
            return "online";
        }
        if (state === "upgrading") return "updating";
        if (["deploying", "initializing"].includes(state) && lastUpgradedAt) return "updating";
        return "offline";
    }

    // ---- INFRA-FIX-005：VPN 節點狀態卡 ------------------------------------------------
    get vpnReport() {
        return this.props.service.has_vpn_node_status ? this.vpn.state.report : null;
    }

    get vpnCanManage() {
        return this.props.service.viewer_can_manage === true;
    }

    get vpnStateLabel() {
        return nodeStateLabel(this.vpnReport?.state);
    }

    get vpnStateTone() {
        return nodeStateTone(this.vpnReport?.state);
    }

    get vpnStateHint() {
        return nodeStateHint(this.vpnReport);
    }

    /** INFRA-TS-005-5：上一次登入失敗的原因（key 過期／已用過／不對／連不到控制伺服器）。 */
    get vpnLoginError() {
        return loginErrorHint(this.vpnReport);
    }

    /** INFRA-TS-005-5：已登入的節點不會使用設定裡的 auth key。 */
    get vpnAuthKeyNotice() {
        return authKeyNotice(this.vpnReport);
    }

    get vpnLoginGuidance() {
        return loginGuidance(this.vpnReport, this.vpnCanManage);
    }

    get vpnForwardRows() {
        return forwardRows(this.vpnReport);
    }

    get vpnTailnetAddress() {
        const report = this.vpnReport;
        return report && report.tailnet_ips && report.tailnet_ips.length ? report.tailnet_ips.join(", ") : "";
    }

    get vpnCardTitle() {
        return _t("VPN node status");
    }

    get vpnStateRowLabel() {
        return _t("Node");
    }

    get vpnAddressRowLabel() {
        return _t("Tailnet address");
    }

    get vpnForwardsRowLabel() {
        return _t("Forwards");
    }

    get vpnNoForwardsText() {
        return _t("No forwards are configured.");
    }

    get vpnLoadingText() {
        return _t("Reading the node status...");
    }

    get subscription() {
        return this.props.service.subscription || null;
    }

    /**
     * #1454：試用中的首個計費日。訂閱的 start_date 在試用時被設成 trial_end＋1
     * （subscription_lifecycle_mixin），就是第一次扣款的日子；沒有時退回 next_payment。
     */
    get firstBillingDate() {
        const sub = this.subscription;
        if (!sub) return null;
        return sub.start_date || sub.next_payment || null;
    }

    get formattedPlanPrice() {
        return formatPoints(this.subscription?.plan_price || 0);
    }

    get publicUrl() {
        if (!this.props.service.subdomain) return null;
        return `https://${this.props.service.subdomain}.${cloudService.domain}`;
    }

    /**
     * 對外位址那一格的標題：範本的標籤（資料值、seed 原文，例 EMQX「對外 MQTT 位址」、
     * Headscale「對外控制網址」）；範本沒設時用通用的程式碼字串（_t，走 po）。只是顯示字
     * ——這一格畫不畫只看 `has_public_tunnel_address`。
     */
    get publicTunnelLabel() {
        return this.props.service.public_tunnel_label || _t("Public address");
    }

    // 文案放 JS（_t 單行字串）而不是模板的多行文字節點：模板文字的 msgid 會連同換行
    // 與縮排一起抽出，排版一動翻譯就對不上（舊的 "Public MQTT" 說明就是這樣一直沒有中文）。
    get publicTunnelPendingHint() {
        return _t("Not available yet. This address needs your own ngrok authtoken in Configuration; once the agent connects it appears here (re-checked in the background). With a reserved ngrok address or your own domain it stays the same; otherwise it may change whenever the service restarts.");
    }

    get publicTunnelCopyTitle() {
        return _t("Copy address");
    }

    /**
     * 商用驗收 HS-013-2：對外位址那一格最後一次「探過、有結論」的時間（背景回讀；剛進入 running 的
     * 10 分鐘內每分鐘一次，之後每日對帳）。沒有時回 ""（不畫）。
     */
    get publicTunnelCheckedText() {
        const checkedAt = this.props.service.public_tunnel_checked_at;
        if (!checkedAt) return "";
        return _t("Last checked: %s", formatUserDateTime(checkedAt, { showSeconds: false }, ""));
    }

    // #1647：Resource Allocation 卡顯示的是「這台服務的方案規格」，容易被讀成工作區的上限。
    // 補一句說明兩者的差別。刻意**不寫**「目前用量請看工作區頁」：那塊配額面板在 operator
    // 還沒升級、平台沒接 operator、namespace 沒有 quota、服務住在舊 namespace 時整塊不畫，
    // 這一句不能承諾那裡一定看得到用量。
    get workspaceQuotaHint() {
        return _t("This is the plan of this service only. All services in the workspace share one overall CPU, memory and disk quota.");
    }

    // ── DATA-ODOO-005-2／X-BK-006-1：唯讀備份卡 ───────────────────────────────
    // payload.backup 由後端 `_backup_status_public` 組（只讀已存值）；supported＝畫不畫。
    get backup() {
        return this.props.service.backup || { supported: false };
    }

    get backupTitle() {
        return _t("Backups");
    }

    get backupStatusLabel() {
        if (this.backup.paused) return _t("Paused");
        switch (this.backup.health) {
            case "ok":
                return _t("Healthy");
            case "failed":
                return _t("Failed");
            case "stale":
                return _t("Overdue");
            default:
                return _t("Not checked yet");
        }
    }

    get backupBadgeClass() {
        if (this.backup.paused) return "o_woow_badge_gray";
        return {
            ok: "o_woow_badge_green",
            failed: "o_woow_badge_red",
            stale: "o_woow_badge_amber",
        }[this.backup.health] || "o_woow_badge_gray";
    }

    get backupLastSuccessLabel() {
        return _t("Last successful backup");
    }

    get backupLastFailureLabel() {
        return _t("Last failed backup");
    }

    get backupLastSuccessText() {
        return this.backup.last_success_at
            ? this.formatSyncedAt(this.backup.last_success_at)
            : _t("None yet");
    }

    /** 原因代碼 → 租戶看得懂的一句話（代碼定義：chart backup-common.sh／db-agent.sh、operator backup_status.py）。 */
    get backupReasonText() {
        const reason = this.backup.failure_reason;
        const texts = {
            no_space: _t("The backup storage is full."),
            postgres_unavailable: _t("The database could not be reached during the backup."),
            lock_busy: _t("Another backup or restore was still running."),
            oom_killed: _t("The backup ran out of memory."),
            deadline_exceeded: _t("The backup took too long and was stopped."),
            terminated: _t("The backup took too long and was stopped."),
        };
        return texts[reason] || _t("The backup could not be completed.");
    }

    get backupHint() {
        if (this.backup.paused) {
            return _t("No backups are taken while the service is suspended.");
        }
        if (this.backup.health === "failed") {
            return _t("Your service keeps running and earlier backups are kept. Our team has been notified and will fix it; contact support if you need help.");
        }
        if (this.backup.health === "stale") {
            return _t("No successful backup in the last day. Our team has been notified.");
        }
        return _t("A backup is taken automatically every day. Contact support to restore data from a backup.");
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

    async copyToClipboard(text, key) {
        const ok = await copyText(text);
        this.copyState.copied = ok ? key : null;
        this.copyState.failed = ok ? null : key;
        clearTimeout(this._copyTimer);
        this._copyTimer = setTimeout(() => {
            this.copyState.copied = null;
            this.copyState.failed = null;
        }, 1500);
    }

    copyIcon(key) {
        if (this.copyState.copied === key) return "check";
        if (this.copyState.failed === key) return "error";
        return "content_copy";
    }
}

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

    setup() {
        // #1310：複製按鈕原本無論成敗都沒有回饋，不支援 Clipboard API 時直接沒反應。
        this.copyState = useState({ copied: null, failed: null });
    }

    get isOnline() {
        return this.connectionStatus === "online";
    }

    /**
     * #1456：連線狀態三態。
     *
     * - running → online
     * - upgrading（改設定／改 addon 的 helm upgrade）→ updating：升級期間舊版本照常服務
     *   （R8 實測 HA／Omnigent／OD 全程 200），原本卻顯示 OFFLINE。
     * - deploying／initializing 且曾經升級過（last_upgraded_at 有值＝重新部署、addon 變更
     *   的 init）→ updating，理由同上；首次開通的 deploying／initializing 服務本來就還
     *   不能用，維持 offline。
     * - 其他（error、suspended、deleting…）→ offline。
     *
     * ⚠ 這仍是平台「最後一次觀測」的狀態，不是即時探測：running 服務平台不會持續
     * 探測 HTTP，應用程式自己重啟（例 HA 內按重新啟動）時這裡仍顯示 online（#1456 的
     * 未修部分，需 chart httpGet probe＋平台對 running 服務的低頻觀測）。
     */
    get connectionStatus() {
        const { state, last_upgraded_at: lastUpgradedAt } = this.props.service;
        if (state === "running") return "online";
        if (state === "upgrading") return "updating";
        if (["deploying", "initializing"].includes(state) && lastUpgradedAt) return "updating";
        return "offline";
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

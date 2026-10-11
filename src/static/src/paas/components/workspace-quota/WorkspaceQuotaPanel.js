/** @odoo-module **/
import { Component, useState, onMounted, onWillUpdateProps, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { formatFloat } from "@web/core/utils/numbers";
import { WoowIcon } from "../icon/WoowIcon";
import { fetchWorkspaceQuota } from "../../services/workspace_quota";

/**
 * WorkspaceQuotaPanel（#1647）
 *
 * 工作區頁上的配額面板：這個工作區 namespace 的 CPU／記憶體／磁碟配額各有多少、已被
 * 佔用多少、還剩多少。數字是「已佔用的配額」（各服務設定的資源上限與磁碟大小加總；
 * 磁碟另含已刪除服務尚在保留期內的資料），不是即時使用率——配額用完時，新的或重新
 * 啟動的服務會建不出來。
 *
 * 等級（ok／warning／full）與百分比都由後端算好放在 payload 裡，這裡只讀欄位、不自己
 * 拿門檻再算一次（兩份判準只會無聲漂移）。
 *
 * 整塊畫不畫：
 *   - 有資料 → 畫三格；
 *   - 讀取失敗（網路、逾時、operator 讀不到叢集）→ 畫，並顯示「暫時無法取得」；
 *   - 本來就沒有東西可顯示（工作區還沒有服務、平台沒接 operator、operator 太舊、
 *     namespace 沒有 quota）→ 整塊不畫。
 *
 * 不輪詢：掛載時讀一次，之後只在 reloadKey 變了（服務清單或狀態有變）或使用者按
 * 重新整理時重讀。
 *
 * Props:
 *   - workspaceId (Number)
 *   - reloadKey (String, optional): 值一變就重讀（父層用服務 id＋狀態組出來）
 */
export class WorkspaceQuotaPanel extends Component {
  static template = "woow_paas_platform.WorkspaceQuotaPanel";
  static components = { WoowIcon };
  static props = {
    workspaceId: { type: Number },
    reloadKey: { type: String, optional: true },
  };

  setup() {
    this.state = useState({ loading: false, quota: null, failed: false });
    // 每次 load 遞增；回應回來時序號對不上＝已有更新的請求（或元件已卸載），丟掉。
    this._seq = 0;

    onMounted(() => this.load());
    onWillUpdateProps((next) => {
      if (next.workspaceId !== this.props.workspaceId) {
        // 換了工作區：先清掉上一個工作區的數字，不讓它在新資料回來前留在畫面上。
        this.state.quota = null;
        this.state.failed = false;
      }
      if (next.reloadKey !== this.props.reloadKey || next.workspaceId !== this.props.workspaceId) {
        this.load(next.workspaceId);
      }
    });
    onWillUnmount(() => {
      this._seq++;
    });
  }

  async load(workspaceId = this.props.workspaceId) {
    const seq = ++this._seq;
    this.state.loading = true;
    const result = await fetchWorkspaceQuota(workspaceId);
    if (seq !== this._seq) return;
    this.state.quota = result.success ? result.data : null;
    this.state.failed = !result.success;
    this.state.loading = false;
  }

  onRefresh() {
    if (this.state.loading) return;
    this.load();
  }

  // ── 顯示判斷 ──

  get visible() {
    if (this.state.failed) return true;
    const quota = this.state.quota;
    // 還沒拿到第一次回應：先不畫，免得一塊空殼閃一下又消失。
    if (!quota) return false;
    if (quota.available) return true;
    return quota.reason === "unreadable" || quota.reason === "error";
  }

  get unavailable() {
    return this.state.failed || Boolean(this.state.quota && !this.state.quota.available);
  }

  // ── 文案（放 JS 的單行 _t：模板的多行文字節點會把換行與縮排一起抽成 msgid）──

  get title() {
    return _t("Workspace resource quota");
  }

  get intro() {
    return _t("All services in this workspace share one CPU, memory and disk quota. The figures show how much of it is reserved (the configured resource limits and disk sizes of the services, plus, for disk, data of deleted services that is still within its retention period), not live usage. When the quota runs out, new or restarting services cannot start.");
  }

  get unavailableText() {
    return _t("Usage is temporarily unavailable.");
  }

  get refreshLabel() {
    return _t("Refresh quota usage");
  }

  get refreshIconClass() {
    return this.state.loading ? "o_woow_spin" : "";
  }

  get alertText() {
    const level = this.state.quota?.level;
    // 磁碟那一格吃緊時後端會在 payload 帶 hint，告警句換成另一版：已刪除服務的資料在
    // 保留期內仍佔用磁碟配額，「刪掉不需要的服務」對磁碟沒有用（刪完上限還會變小）。
    // 哪一格吃緊由後端判，這裡只讀欄位。
    const diskIsTight = this.state.quota?.hint === "storage";
    if (level === "full") {
      if (diskIsTight) {
        return _t("This workspace has used up its quota. New or restarting services cannot start until some of it is freed. Data of deleted services still counts toward the disk quota until its retention period ends, so deleting a service does not free disk quota right away. Contact support if you need more quota.");
      }
      return _t("This workspace has used up its quota. New or restarting services cannot start until some of it is freed. Delete a service you no longer need, or contact support.");
    }
    if (level === "warning") {
      if (diskIsTight) {
        return _t("This workspace is close to its quota. Deploying, restarting or resizing a service may fail. Data of deleted services still counts toward the disk quota until its retention period ends, so deleting a service does not free disk quota right away. Contact support if you need more quota.");
      }
      return _t("This workspace is close to its quota. Deploying, restarting or resizing a service may fail. Delete a service you no longer need, or contact support.");
    }
    return "";
  }

  get alertClass() {
    const level = this.state.quota?.level;
    const modifier = level === "full" ? "full" : "warning";
    return "o_woow_quota_panel__alert o_woow_quota_panel__alert--" + modifier;
  }

  // ── 三格 ──

  /** 整數不帶小數、最多兩位；千分位與小數點跟隨使用者語系（formatFloat）。 */
  formatAmount(value) {
    const amount = Number(value);
    return formatFloat(Number.isFinite(amount) ? amount : 0, { digits: [16, 2], trailingZeros: false });
  }

  resourceLabel(key) {
    if (key === "cpu") return _t("CPU quota");
    if (key === "memory") return _t("Memory quota");
    if (key === "storage") return _t("Disk quota");
    return null;
  }

  usedText(unit, used, hard) {
    if (unit === "vcpu") return _t("%(used)s of %(hard)s vCPU", { used, hard });
    if (unit === "gb") return _t("%(used)s of %(hard)s GB", { used, hard });
    return null;
  }

  remainingText(unit, remaining) {
    if (unit === "vcpu") return _t("%s vCPU left", remaining);
    if (unit === "gb") return _t("%s GB left", remaining);
    return null;
  }

  levelLabel(level) {
    if (level === "full") return _t("Quota used up");
    if (level === "warning") return _t("Quota almost used up");
    return "";
  }

  get rows() {
    const rows = [];
    for (const resource of this.state.quota?.resources || []) {
      const label = this.resourceLabel(resource.key);
      const usedText = this.usedText(
        resource.unit,
        this.formatAmount(resource.used),
        this.formatAmount(resource.hard)
      );
      const remainingText = this.remainingText(resource.unit, this.formatAmount(resource.remaining));
      // 這一版前端不認得的 key／unit：略過該列，不畫一格沒有標題或單位的數字。
      if (!label || !usedText || !remainingText) continue;

      const percent = Number.isFinite(Number(resource.percent)) ? Number(resource.percent) : 0;
      let className = "o_woow_quota_panel__row";
      if (resource.level === "full") {
        className += " o_woow_quota_panel__row--full";
      } else if (resource.level === "warning") {
        className += " o_woow_quota_panel__row--warning";
      }
      rows.push({
        key: resource.key,
        label,
        usedText,
        remainingText,
        percent,
        percentText: `${percent}%`,
        // 超額時 percent 會大於 100；比例條只畫到滿。
        fillStyle: `width: ${Math.min(Math.max(percent, 0), 100)}%`,
        className,
        levelLabel: this.levelLabel(resource.level),
      });
    }
    return rows;
  }
}

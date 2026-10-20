/** @odoo-module **/
import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { notificationService } from "../../services/notification_service";
import { formatUserDate, formatUserDateTime } from "../../utils/datetime_helpers";

const TYPE_ICONS = {
  trial_ending: "hourglass_bottom",
  renewal_low_balance: "account_balance_wallet",
  service_error: "error",
  backup_failed: "backup",
  invoice_unpaid: "receipt_long",
  wallet_activity: "payments",
};

/**
 * Notification panel opened from the header bell (#1310).
 * Desktop: dropdown anchored under the bell. ≤768px: a sheet below the
 * mobile header with a backdrop. Closes on backdrop click, Escape, or when
 * an item is followed.
 */
export class NotificationPanel extends Component {
  static template = "woow_paas_platform.NotificationPanel";
  static props = { router: Object };

  setup() {
    this.service = useState(notificationService);
    this.panelRef = useRef("panel");
    this.onKeydown = (ev) => {
      if (ev.key === "Escape") this.service.close();
    };
    onMounted(() => {
      document.addEventListener("keydown", this.onKeydown);
      this.panelRef.el?.focus();
    });
    onWillUnmount(() => document.removeEventListener("keydown", this.onKeydown));
  }

  icon(item) {
    return TYPE_ICONS[item.type] || "notifications";
  }

  when(item) {
    if (!item.ts) return "";
    return /^\d{4}-\d{2}-\d{2}$/.test(item.ts) ? formatUserDate(item.ts) : formatUserDateTime(item.ts);
  }

  get emptyText() {
    return _t("You're all caught up.");
  }

  onItemClick(item) {
    this.service.close();
    if (item.route) this.props.router.navigate(item.route);
  }

  onRetry() {
    this.service.fetch();
  }
}

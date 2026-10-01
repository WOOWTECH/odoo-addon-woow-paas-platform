/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { formatPoints, planUnitLabel } from "../../utils/billing_helpers";
import { formatUserDate } from "../../utils/datetime_helpers";

const TEMPLATE_ICONS = {
  n8n: "cloud",
  postgresql: "storage",
  odoo: "cloud",
  anythingllm: "smart_toy",
  smarthome: "home_iot_device",
  default: "cloud",
};

/**
 * SubscribedServicesModal Component
 *
 * Shows the services associated with a subscription.
 *
 * Props:
 *   - subscription (Object): Subscription data { id, plan_name, plan_price, recurrency_label, unit, recurrency }
 *   - onClose (Function): Called when modal is closed
 */
export class SubscribedServicesModal extends Component {
  static template = "woow_paas_platform.SubscribedServicesModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    subscription: { type: Object },
    onClose: { type: Function },
  };

  setup() {
    this.state = useState({
      loading: true,
      detail: null,
    });

    onMounted(() => {
      this.loadDetail();
    });
  }

  async loadDetail() {
    this.state.loading = true;
    await billingService.fetchSubscription(this.props.subscription.id);
    this.state.detail = billingService.subscriptionDetail;
    this.state.loading = false;
  }

  get planName() {
    return this.state.detail?.plan_name || this.props.subscription.plan_name || "";
  }

  get planPrice() {
    const price = this.state.detail?.plan_price ?? this.props.subscription.plan_price ?? 0;
    return formatPoints(price);
  }

  /**
   * #1310：價格後面的週期（原本寫死 "/ month"，年繳方案也顯示月）。
   *
   * 每 N 個週期才收一次（recurrency > 1）時，左側的 recurrencyLabel 已經寫了
   * 「每 3 個月」，價格就是那一期的金額 ⇒ 不再加單位，避免讀成「每月」。
   * 後端沒帶 unit（舊 payload）時也不顯示，而不是猜一個「月」。
   */
  get pricePeriod() {
    const src = this.state.detail || this.props.subscription;
    const unit = src?.unit;
    if (!unit || (src.recurrency || 1) > 1) {
      return "";
    }
    return _t("/ %(unit)s", { unit: planUnitLabel(unit) });
  }

  get recurrencyLabel() {
    return this.state.detail?.recurrency_label || this.props.subscription.recurrency_label || _t("Monthly");
  }

  get services() {
    return this.state.detail?.services ?? [];
  }

  get serviceCount() {
    return this.services.length;
  }

  getServiceIcon(service) {
    const templateLower = (service.template_name || "").toLowerCase();
    for (const [key, icon] of Object.entries(TEMPLATE_ICONS)) {
      if (key !== "default" && templateLower.includes(key)) {
        return icon;
      }
    }
    return TEMPLATE_ICONS.default;
  }

  getServiceStateLabel(service) {
    const stateMap = {
      running: _t("Active"),
      stopped: _t("Stopped"),
      deploying: _t("Deploying"),
      error: _t("Error"),
      pending: _t("Pending"),
    };
    return stateMap[service.state] || service.state || _t("Unknown");
  }

  getServiceStateClass(service) {
    const classMap = {
      running: "o_woow_svc_status_green",
      stopped: "o_woow_svc_status_gray",
      deploying: "o_woow_svc_status_blue",
      error: "o_woow_svc_status_red",
      pending: "o_woow_svc_status_blue",
    };
    return classMap[service.state] || "o_woow_svc_status_gray";
  }

  /** 依 Odoo 使用者語系格式化日期。 */
  formatDate(dateStr) {
    return formatUserDate(dateStr, "-");
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget) {
      this.props.onClose();
    }
  }
}

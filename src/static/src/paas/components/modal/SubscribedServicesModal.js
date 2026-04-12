/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";

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
 *   - subscription (Object): Subscription data { id, plan_name, plan_price, recurrency_label }
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
    return price.toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
  }

  get recurrencyLabel() {
    return this.state.detail?.recurrency_label || this.props.subscription.recurrency_label || "Monthly";
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
      running: "Active",
      stopped: "Stopped",
      deploying: "Deploying",
      error: "Error",
      pending: "Pending",
    };
    return stateMap[service.state] || service.state || "Unknown";
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

  formatDate(dateStr) {
    if (!dateStr) return "-";
    try {
      const d = new Date(dateStr);
      return d.toLocaleDateString("en-US", {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
    } catch {
      return dateStr;
    }
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget) {
      this.props.onClose();
    }
  }
}

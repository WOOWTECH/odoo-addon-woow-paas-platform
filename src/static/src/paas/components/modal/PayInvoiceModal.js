/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { formatPoints } from "../../utils/billing_helpers";

/**
 * PayInvoiceModal Component
 *
 * Confirmation modal for paying an invoice using wallet balance.
 *
 * Props:
 *   - invoice (Object): Invoice data { id, name/number, amount_total }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onPaid (Function): Called after successful payment
 */
export class PayInvoiceModal extends Component {
  static template = "woow_paas_platform.PayInvoiceModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    invoice: { type: Object },
    onClose: { type: Function },
    onPaid: { type: Function },
  };

  setup() {
    this.billing = useState(billingService);
    this.state = useState({
      paying: false,
      error: null,
    });

    onMounted(() => {
      // Ensure wallet balance is fresh
      billingService.fetchWallet();
    });
  }

  get invoiceName() {
    return this.props.invoice.name || this.props.invoice.number || "-";
  }

  get amount() {
    return this.props.invoice.amount_total ?? 0;
  }

  get formattedAmount() {
    return formatPoints(this.amount);
  }

  get walletBalance() {
    return this.billing.wallet.balance ?? 0;
  }

  get formattedBalance() {
    return formatPoints(this.walletBalance);
  }

  get hasSufficientBalance() {
    return this.walletBalance >= this.amount;
  }

  onBackdropClick(ev) {
    if (ev.target === ev.currentTarget && !this.state.paying) {
      this.props.onClose();
    }
  }

  async onConfirm() {
    if (this.state.paying || !this.hasSufficientBalance) return;

    this.state.paying = true;
    this.state.error = null;

    const result = await billingService.payInvoice(this.props.invoice.id);

    if (result.success) {
      // Refresh wallet balance after payment
      await billingService.fetchWallet();
      this.props.onPaid();
    } else {
      this.state.error = result.error || _t("Payment failed");
      // #864 格1：付款失敗後餘額必須重抓。後端這一路的失敗會把已扣的點數
      // 回捲（`api_invoice_pay` 的 savepoint），而 `this.billing.wallet` 仍是
      // 打 API 之前的舊快取 —— 使用者會看著一個不存在的餘額決定要不要再按
      // 一次。刻意排在設 error 之後：錯誤訊息立刻渲染，餘額隨後自我修正；
      // 也刻意排在 `paying = false` 之前，重抓期間確認鈕維持 disabled。
      // `fetchWallet()` 自帶 try/catch/finally（billing_service.js），失敗只
      // 進 console，不會把錯誤訊息蓋掉。
      await billingService.fetchWallet();
      this.state.paying = false;
    }
  }
}

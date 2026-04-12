/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

export class BillingOverviewPage extends Component {
  static template = "woow_paas_platform.BillingOverviewPage";
  static components = { WoowCard, WoowIcon, WoowButton };

  setup() {
    this.billing = useState(billingService);
    this.router = useState(router);

    onMounted(() => {
      this.loadWallet();
    });
  }

  async loadWallet() {
    await billingService.fetchWallet();
  }

  get loading() {
    return this.billing.walletLoading;
  }

  get formattedBalance() {
    const balance = this.billing.wallet.balance ?? 0;
    return balance.toLocaleString();
  }

  get cardInfo() {
    const w = this.billing.wallet;
    if (!w.cardBound) {
      return null;
    }
    return {
      last4: w.cardLast4,
      expiry: w.cardExpiry,
    };
  }

  goToTopup() {
    this.router.navigate("billing/topup");
  }

  goToInvoices() {
    this.router.navigate("billing/invoices");
  }

  goToSubscriptions() {
    this.router.navigate("billing/subscriptions");
  }
}

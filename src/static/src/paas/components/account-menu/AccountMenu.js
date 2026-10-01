/** @odoo-module **/
import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { billingService } from "../../services/billing_service";
import { profileService } from "../../services/profile_service";
import { formatPoints } from "../../utils/billing_helpers";

export const HELP_URL = "https://docs.woowtech.io";

/**
 * Account menu opened from the header avatar (#1310).
 *
 * On ≤768px the sidebar (Help & Support, Log Out, admin reconciliation) and
 * the header credits badge are hidden, which left those functions with no
 * entry point on phones. This menu carries them on every screen size.
 */
export class AccountMenu extends Component {
  static template = "woow_paas_platform.AccountMenu";
  static props = { router: Object, onClose: Function };

  setup() {
    this.profile = useState(profileService);
    this.billing = useState(billingService);
    this.menuRef = useRef("menu");
    this.onKeydown = (ev) => {
      if (ev.key === "Escape") this.props.onClose();
    };
    onMounted(() => {
      document.addEventListener("keydown", this.onKeydown);
      this.menuRef.el?.focus();
    });
    onWillUnmount(() => document.removeEventListener("keydown", this.onKeydown));
  }

  get partner() {
    return this.profile.partner || {};
  }

  get isAdmin() {
    return !!this.partner.is_admin;
  }

  get balance() {
    return this.billing.walletLoading ? "…" : formatPoints(this.billing.wallet.balance);
  }

  go(path) {
    this.props.onClose();
    this.props.router.navigate(path);
  }

  openHelp() {
    this.props.onClose();
    window.open(HELP_URL, "_blank", "noopener");
  }

  logout() {
    window.location.href = "/web/session/logout";
  }
}

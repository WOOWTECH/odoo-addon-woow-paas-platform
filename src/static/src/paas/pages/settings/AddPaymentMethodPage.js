/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { loadJS } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

/**
 * SDK script URLs. Order matters: jquery → ecpay sdk.
 *
 * We deliberately do NOT pre-load node-forge from CDN, even though ECPay's
 * docs and payment_ecpg_templates.xml suggest it. When forge is already on
 * window, sdk-1.0.0.js skips loading its own forge-0.10.0.min.js and ends up
 * calling its closure variable `i` as a function before any code path has
 * assigned it — throwing "TypeError: i is not a function" inside `ECP.initialize`.
 * Letting the SDK self-load its bundled forge avoids the bug entirely.
 */
const SDK_SCRIPTS = [
    "https://code.jquery.com/jquery-3.7.1.min.js",
    "https://ecpg.ecpay.com.tw/Scripts/sdk-1.0.0.js",
];

async function loadEcpaySdk() {
    // Sequential await preserves required load order (ECPay SDK requirement).
    for (const url of SDK_SCRIPTS) {
        await loadJS(url);
    }
    if (typeof window.ECPay === "undefined") {
        throw new Error(_t("ECPay SDK did not expose window.ECPay after load"));
    }
}

export class AddPaymentMethodPage extends Component {
    static template = "woow_paas_platform.AddPaymentMethodPage";
    static components = { WoowCard, WoowButton, WoowIcon };
    static props = {};

    setup() {
        this.router = router;
        this.state = useState({
            phase: "loading",     // loading | ready | submitting | error
            error: null,
            bindContext: null,    // {token, member_id, trade_no, merchant_id, sandbox}
        });

        onMounted(async () => {
            try {
                const [, prep] = await Promise.all([
                    loadEcpaySdk(),
                    billingService.preparePaymentMethodBind(),
                ]);
                if (!prep.success) {
                    throw new Error(prep.error || _t("Prepare bind failed"));
                }
                this.state.bindContext = prep.data;

                const envi = prep.data.sandbox ? "Stage" : "Prod";
                await new Promise((resolve, reject) => {
                    window.ECPay.initialize(envi, 1, (errMsg) => {
                        if (errMsg) reject(new Error(_t("SDK init failed: %s", errMsg)));
                        else resolve();
                    });
                });
                await new Promise((resolve, reject) => {
                    window.ECPay.addBindingCard(prep.data.token, "zh-TW", (errMsg) => {
                        if (errMsg) reject(new Error(_t("Card form failed: %s", errMsg)));
                        else resolve();
                    });
                });
                this.state.phase = "ready";
            } catch (err) {
                console.error("AddPaymentMethodPage setup error:", err);
                this.state.phase = "error";
                this.state.error = err.message || String(err);
            }
        });

        onWillUnmount(() => {
            // Best-effort: clear the SDK's DOM container so re-entries start clean.
            const el = document.getElementById("ECPayPayment");
            if (el) el.innerHTML = "";
        });
    }

    goBack() {
        this.router.navigate("settings/payment-methods");
    }

    async onConfirm() {
        if (this.state.phase !== "ready") return;
        this.state.phase = "submitting";
        this.state.error = null;

        try {
            const payToken = await new Promise((resolve, reject) => {
                window.ECPay.getBindCardPayToken((result, errMsg) => {
                    if (errMsg) return reject(new Error(_t("PayToken failed: %s", errMsg)));
                    const t = result && result.BindCardPayToken;
                    if (!t) return reject(new Error(_t("Invalid BindCardPayToken")));
                    resolve(t);
                });
            });

            const ctx = this.state.bindContext;
            const result = await billingService.createPaymentMethodBind({
                member_id: ctx.member_id,
                trade_no: ctx.trade_no,
                bind_card_pay_token: payToken,
            });
            if (!result.success) {
                throw new Error(result.error || _t("Bind rejected"));
            }

            if (result.data.three_d_url) {
                window.location.href = result.data.three_d_url;
                return; // page unloads
            }
            // No 3DS: the server already persisted the card from the synchronous
            // CreateBindCard response and reports the outcome in bind_status.
            const bindStatus = ["ok", "failed", "error"].includes(result.data.bind_status)
                ? result.data.bind_status
                : "ok";
            this.router.navigate(`settings/payment-methods?bind_status=${bindStatus}`);
        } catch (err) {
            console.error("AddPaymentMethodPage confirm error:", err);
            this.state.phase = "ready";
            this.state.error = err.message || String(err);
        }
    }
}

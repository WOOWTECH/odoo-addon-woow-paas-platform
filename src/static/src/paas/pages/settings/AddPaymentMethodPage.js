/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { loadJS } from "@web/core/assets";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

/**
 * SDK script URLs. Order matters: jquery → node-forge → ecpay sdk.
 * These are ECPay's canonical CDN URLs (see payment_ecpay_ecpg/views/payment_ecpg_templates.xml).
 */
const SDK_SCRIPTS = [
    "https://code.jquery.com/jquery-3.7.1.min.js",
    "https://cdn.jsdelivr.net/npm/node-forge@0.7.0/dist/forge.min.js",
    "https://ecpg.ecpay.com.tw/Scripts/sdk-1.0.0.js",
];

async function loadEcpaySdk() {
    // Sequential await preserves required load order (ECPay SDK requirement).
    for (const url of SDK_SCRIPTS) {
        await loadJS(url);
    }
    if (typeof window.ECPay === "undefined") {
        throw new Error("ECPay SDK did not expose window.ECPay after load");
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
                    throw new Error(prep.error || "Prepare bind failed");
                }
                this.state.bindContext = prep.data;

                const envi = prep.data.sandbox ? "Stage" : "Prod";
                await new Promise((resolve, reject) => {
                    window.ECPay.initialize(envi, 1, (errMsg) => {
                        if (errMsg) reject(new Error("SDK init failed: " + errMsg));
                        else resolve();
                    });
                });
                await new Promise((resolve, reject) => {
                    window.ECPay.addBindingCard(prep.data.token, "zh-TW", (errMsg) => {
                        if (errMsg) reject(new Error("Card form failed: " + errMsg));
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
                    if (errMsg) return reject(new Error("PayToken failed: " + errMsg));
                    const t = result && result.BindCardPayToken;
                    if (!t) return reject(new Error("Invalid BindCardPayToken"));
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
                throw new Error(result.error || "Bind rejected");
            }

            if (result.data.three_d_url) {
                window.location.href = result.data.three_d_url;
                return; // page unloads
            }
            // No 3DS (unusual as of 2025-08 when 3DS 2.0 is mandatory)
            this.router.navigate("settings/payment-methods?bind_status=ok");
        } catch (err) {
            console.error("AddPaymentMethodPage confirm error:", err);
            this.state.phase = "ready";
            this.state.error = err.message || String(err);
        }
    }
}

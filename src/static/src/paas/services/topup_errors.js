/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

const CODE_MAP = {
    payment_not_configured: _t("Credit top-up is not available yet. Please contact support."),
    no_card: _t("Please add a payment method before topping up."),
    plan_not_found: _t("This plan is no longer available."),
    provider_disabled: _t("Payment service is temporarily unavailable."),
    duplicate_pending: _t("Your previous top-up is still processing, please wait."),
    sdk_error: _t("Payment service unavailable. Please try again in a moment."),
    card_declined: _t("The card was declined. Please verify the card or try another."),
    limit_exceeded: _t("The card's limit was exceeded."),
    duplicate_transaction: _t("Duplicate transaction detected. Please wait a moment and retry."),
    generic: _t("Payment failed. Please retry or contact support."),
    intent_not_recorded: _t("Payment service is temporarily unavailable. Your card was not charged; please try again later."),
    wallet_credit_failed: _t("Your payment went through but the credits could not be added yet. Our team has been alerted and will fix it — please do not pay again."),
    "3ds_required": _t("Your bank requires card verification. Please try again and complete the verification step."),
    topup_pending: _t("Still confirming payment — please refresh in a moment."),
    error: _t("Payment failed. Please retry or contact support."),
    cancel: _t("Payment failed. Please retry or contact support."),
};

export function translateTopupError(message, code) {
    if (code && CODE_MAP[code]) return CODE_MAP[code];
    if (message) return message;
    return CODE_MAP.generic;
}


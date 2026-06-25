/** @odoo-module **/

const CODE_MAP = {
    payment_not_configured: "Credit top-up is not available yet. Please contact support.",
    no_card: "Please add a payment method before topping up.",
    plan_not_found: "This plan is no longer available.",
    provider_disabled: "Payment service is temporarily unavailable.",
    duplicate_pending: "Your previous top-up is still processing, please wait.",
    sdk_error: "Payment service unavailable. Please try again in a moment.",
    card_declined: "The card was declined. Please verify the card or try another.",
    limit_exceeded: "The card's limit was exceeded.",
    duplicate_transaction: "Duplicate transaction detected. Please wait a moment and retry.",
    generic: "Payment failed. Please retry or contact support.",
};

export function translateTopupError(message, code) {
    if (code && CODE_MAP[code]) return CODE_MAP[code];
    if (message) return message;
    return CODE_MAP.generic;
}

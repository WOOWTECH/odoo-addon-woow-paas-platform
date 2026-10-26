/** @odoo-module **/
import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService } from "../../services/cloud_service";
import { createCustomDomainPoller } from "../../utils/custom_domains";

const STATUS_PRESENTATION = {
    pending_dns: { label: _t("Waiting for DNS"), tone: "pending" },
    verifying: { label: _t("Verifying DNS"), tone: "pending" },
    issuing: { label: _t("Issuing certificate"), tone: "pending" },
    configuring: { label: _t("Configuring"), tone: "pending" },
    waiting_cname: { label: _t("Ready to switch DNS"), tone: "warning" },
    active: { label: _t("Active"), tone: "success" },
    error: { label: _t("Action required"), tone: "error" },
    removing: { label: _t("Removing"), tone: "pending" },
    removed: { label: _t("Removed"), tone: "muted" },
    needs_review: { label: _t("Needs review"), tone: "warning" },
};

export function domainStatus(state) {
    return STATUS_PRESENTATION[state] || {
        label: _t("Unknown status: %s", state || _t("not reported")),
        tone: "warning",
    };
}

export class EditDomainModal extends Component {
    static template = "woow_paas_platform.EditDomainModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        service: { type: Object },
        onClose: { type: Function },
        onSaved: { type: Function },
    };

    setup() {
        const capability = this.props.service.custom_domain_capability || {};
        this.state = useState({
            domains: [],
            capability: {
                supported: capability.supported === true,
                reason: capability.reason || "",
                max_domains: capability.max_domains || 0,
            },
            hostname: "",
            loading: true,
            action: null,
            error: null,
            copied: null,
        });
        this.poller = createCustomDomainPoller({
            load: () => this.request({ action: "list" }),
            onData: (data) => this.acceptPayload(data),
            onError: (error) => {
                this.state.loading = false;
                this.state.error = error.message || _t("Failed to load custom domains");
            },
        });
        onMounted(() => this.poller.start());
        onWillUnmount(() => this.poller.stop());
    }

    get canCreate() {
        return this.state.capability.supported === true &&
            this.state.hostname.trim() !== "" && !this.state.action && !this.atLimit;
    }

    get atLimit() {
        const limit = Number(this.state.capability.max_domains);
        const retained = this.state.domains.filter((domain) => !["removed", "removing"].includes(domain.state));
        return limit > 0 && retained.length >= limit;
    }

    status(state) {
        return domainStatus(state);
    }

    async request(params) {
        const result = await cloudService.manageDomains(this.props.service.id, params);
        if (!result.success) throw new Error(result.error || _t("Custom domain request failed"));
        return result.data || {};
    }

    acceptPayload(data) {
        this.state.domains = Array.isArray(data.domains) ? data.domains : [];
        if (data.capability) {
            this.state.capability = {
                supported: data.capability.supported === true,
                reason: data.capability.reason || "",
                max_domains: data.capability.max_domains || 0,
            };
        }
        this.state.loading = false;
        this.state.error = null;
    }

    onHostnameInput(ev) {
        this.state.hostname = ev.target.value;
        this.state.error = null;
    }

    async perform(action, extra = {}) {
        if (this.state.action) return;
        // Invalidate any in-flight list before applying a mutation so a stale
        // poll response cannot overwrite the mutation response.
        this.poller.stop();
        this.state.action = `${action}:${extra.domain_id || "new"}`;
        this.state.error = null;
        try {
            const data = await this.request({ action, ...extra });
            this.acceptPayload(data);
            if (action === "create") this.state.hostname = "";
            this.props.onSaved();
        } catch (error) {
            this.state.error = error.message || _t("Custom domain request failed");
        } finally {
            this.state.action = null;
            this.poller.start();
        }
    }

    createDomain() {
        if (this.canCreate) return this.perform("create", { hostname: this.state.hostname.trim().toLowerCase() });
    }

    checkDomain(domain) {
        if (this.state.capability.supported === true) return this.perform("check", { domain_id: domain.id });
    }

    removeDomain(domain) {
        // Existing entries stay removable when new-domain support is disabled.
        if (!["removing", "removed"].includes(domain.state)) return this.perform("remove", { domain_id: domain.id });
    }

    async copyRecord(domain, record, index) {
        try {
            await navigator.clipboard.writeText(record.value);
            this.state.copied = `${domain.id}:${index}`;
        } catch (_error) {
            this.state.error = _t("Could not copy the DNS value");
        }
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.action) this.props.onClose();
    }
}

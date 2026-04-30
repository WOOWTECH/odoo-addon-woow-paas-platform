/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { profileService } from "../../services/profile_service";

const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;

export class ProfilePage extends Component {
    static template = "woow_paas_platform.ProfilePage";
    static components = { WoowCard, WoowButton, WoowIcon };
    static props = {};

    setup() {
        this.profile = useState(profileService);
        this.state = useState({
            form: { name: "", email: "", phone: "" },
            fieldErrors: {},
            saving: false,
            saveSuccess: false,
            serverError: null,
            invoiceForm: {
                invoice_carrier_type: "email",
                invoice_carrier: "",
                invoice_tax_id: "",
                invoice_title: "",
                invoice_lovecode: "",
            },
            invoiceSaving: false,
            invoiceSaveSuccess: false,
            invoiceServerError: null,
        });
        this._saveSuccessTimer = null;
        this._invoiceSaveSuccessTimer = null;

        onMounted(async () => {
            const tasks = [];
            if (!this.profile.partner) tasks.push(profileService.fetch());
            if (!this.profile.invoicePreference) tasks.push(profileService.fetchInvoicePreference());
            await Promise.all(tasks);
            this._resetFormFromService();
            this._resetInvoiceFormFromService();
        });

        onWillUnmount(() => {
            if (this._saveSuccessTimer) {
                clearTimeout(this._saveSuccessTimer);
                this._saveSuccessTimer = null;
            }
            if (this._invoiceSaveSuccessTimer) {
                clearTimeout(this._invoiceSaveSuccessTimer);
                this._invoiceSaveSuccessTimer = null;
            }
        });
    }

    _resetFormFromService() {
        const p = this.profile.partner;
        if (!p) return;
        this.state.form.name = p.name || "";
        this.state.form.email = p.email || "";
        this.state.form.phone = p.phone || "";
        this.state.fieldErrors = {};
        this.state.serverError = null;
    }

    _resetInvoiceFormFromService() {
        const ip = this.profile.invoicePreference;
        if (!ip) return;
        this.state.invoiceForm.invoice_carrier_type = ip.invoice_carrier_type || "email";
        this.state.invoiceForm.invoice_carrier = ip.invoice_carrier || "";
        this.state.invoiceForm.invoice_tax_id = ip.invoice_tax_id || "";
        this.state.invoiceForm.invoice_title = ip.invoice_title || "";
        this.state.invoiceForm.invoice_lovecode = ip.invoice_lovecode || "";
        this.state.invoiceServerError = null;
    }

    get isLoading() {
        return this.profile.loading && !this.profile.partner;
    }

    get avatarUrl() {
        return this.profile.partner?.avatar_url || "";
    }

    get displayName() {
        return this.profile.partner?.name || "—";
    }

    _validate() {
        const errors = {};
        if (!this.state.form.name.trim()) {
            errors.name = "Name is required";
        }
        if (this.state.form.email.trim() && !EMAIL_RE.test(this.state.form.email.trim())) {
            errors.email = "Invalid email format";
        }
        this.state.fieldErrors = errors;
        return Object.keys(errors).length === 0;
    }

    async onSave() {
        this.state.saveSuccess = false;
        this.state.serverError = null;
        if (!this._validate()) return;

        this.state.saving = true;
        const result = await profileService.update({
            name: this.state.form.name.trim(),
            email: this.state.form.email.trim(),
            phone: this.state.form.phone.trim(),
        });
        this.state.saving = false;

        if (result.success) {
            this._resetFormFromService();
            this.state.saveSuccess = true;
            if (this._saveSuccessTimer) clearTimeout(this._saveSuccessTimer);
            this._saveSuccessTimer = setTimeout(() => {
                this.state.saveSuccess = false;
                this._saveSuccessTimer = null;
            }, 3000);
        } else if (result.code === "name_required") {
            this.state.fieldErrors = { name: result.error };
        } else if (result.code === "invalid_email") {
            this.state.fieldErrors = { email: result.error };
        } else {
            this.state.serverError = result.error || "Update failed";
        }
    }

    onCancel() {
        this._resetFormFromService();
    }

    get invoiceCarrierType() {
        return this.state.invoiceForm.invoice_carrier_type;
    }

    get showCarrierNumber() {
        return ["mobile", "citizen"].includes(this.invoiceCarrierType);
    }

    get showBusinessFields() {
        return this.invoiceCarrierType === "triplicate";
    }

    get showLovecode() {
        return this.invoiceCarrierType === "donation";
    }

    async onSaveInvoice() {
        this.state.invoiceSaveSuccess = false;
        this.state.invoiceServerError = null;
        this.state.invoiceSaving = true;
        const result = await profileService.updateInvoicePreference({
            invoice_carrier_type: this.state.invoiceForm.invoice_carrier_type,
            invoice_carrier: this.state.invoiceForm.invoice_carrier.trim(),
            invoice_tax_id: this.state.invoiceForm.invoice_tax_id.trim(),
            invoice_title: this.state.invoiceForm.invoice_title.trim(),
            invoice_lovecode: this.state.invoiceForm.invoice_lovecode.trim(),
        });
        this.state.invoiceSaving = false;
        if (result.success) {
            this._resetInvoiceFormFromService();
            this.state.invoiceSaveSuccess = true;
            if (this._invoiceSaveSuccessTimer) clearTimeout(this._invoiceSaveSuccessTimer);
            this._invoiceSaveSuccessTimer = setTimeout(() => {
                this.state.invoiceSaveSuccess = false;
                this._invoiceSaveSuccessTimer = null;
            }, 3000);
        } else {
            this.state.invoiceServerError = result.error || "Update failed";
        }
    }

    onCancelInvoice() {
        this._resetInvoiceFormFromService();
    }
}

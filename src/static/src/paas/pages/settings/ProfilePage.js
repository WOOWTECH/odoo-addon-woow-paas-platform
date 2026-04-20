/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { profileService } from "../../services/profile_service";
import { router } from "../../core/router";

const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;

export class ProfilePage extends Component {
    static template = "woow_paas_platform.ProfilePage";
    static components = { WoowCard, WoowButton, WoowIcon };
    static props = {};

    setup() {
        this.router = router;
        this.profile = useState(profileService);
        this.state = useState({
            form: { name: "", email: "", phone: "" },
            fieldErrors: {},
            saving: false,
            saveSuccess: false,
            serverError: null,
        });

        onMounted(async () => {
            if (!this.profile.partner) {
                await profileService.fetch();
            }
            this._resetFormFromService();
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
            setTimeout(() => { this.state.saveSuccess = false; }, 3000);
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

    goBack() {
        this.router.navigate("settings");
    }
}

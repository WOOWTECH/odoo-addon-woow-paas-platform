/** @odoo-module **/
import { Component, useState, useRef, useEffect } from "@odoo/owl";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { WoowButton } from "../../../components/button/WoowButton";
import { supportService } from "../../../services/support_service";
import { safeHtml } from "../../../services/html_sanitize";

export class BasicInfoSection extends Component {
    static template = "woow_paas_platform.ProjectDetailBasicInfoSection";
    static components = { WoowCard, WoowIcon, WoowButton };
    static props = {
        project: { type: Object },
        onUpdated: { type: Function },
    };

    setup() {
        this.state = useState({
            editingName: false,
            editingDescription: false,
            nameDraft: this.props.project.name || "",
            saving: false,
            error: null,
        });

        this.descEditorRef = useRef("descEditor");

        // Initialise the contenteditable with the current description HTML
        // each time edit mode is entered. We can't bind innerHTML to state
        // (OWL re-renders would reset the cursor on every keystroke), so we
        // seed it once via useEffect on enter-edit. Seed is sanitized so the
        // edit-path mirrors the read-path render and stays safe even if a
        // future write-side regression lets raw HTML through.
        useEffect(
            (el) => {
                if (el) {
                    el.innerHTML = safeHtml(this.props.project.description || "");
                    el.focus();
                }
            },
            () => [this.descEditorRef.el],
        );
    }

    get descriptionMarkup() {
        return safeHtml(this.props.project.description);
    }

    startEditName() {
        this.state.nameDraft = this.props.project.name || "";
        this.state.editingName = true;
        this.state.error = null;
    }

    cancelEditName() {
        this.state.editingName = false;
        this.state.error = null;
    }

    onNameInput(ev) {
        this.state.nameDraft = ev.target.value;
    }

    async saveName() {
        const name = this.state.nameDraft.trim();
        if (!name) {
            this.state.error = "Name cannot be empty";
            return;
        }
        this.state.saving = true;
        const result = await supportService.updateProject(this.props.project.id, { name });
        this.state.saving = false;
        if (result.success) {
            this.props.onUpdated(result.data);
            this.state.editingName = false;
        } else {
            this.state.error = result.error || "Failed to save";
        }
    }

    startEditDescription() {
        this.state.editingDescription = true;
        this.state.error = null;
    }

    cancelEditDescription() {
        this.state.editingDescription = false;
        this.state.error = null;
    }

    /**
     * Toolbar handler — bold/italic via document.execCommand. The API is
     * legacy but still supported across browsers for contenteditable; no
     * heavy editor dependency required for the minimal rich-text needs of
     * a project description.
     */
    applyFormat(command, ev) {
        if (ev) {
            ev.preventDefault();
        }
        document.execCommand(command, false);
        if (this.descEditorRef.el) {
            this.descEditorRef.el.focus();
        }
    }

    onBold(ev) {
        this.applyFormat("bold", ev);
    }

    onItalic(ev) {
        this.applyFormat("italic", ev);
    }

    onBulletList(ev) {
        this.applyFormat("insertUnorderedList", ev);
    }

    async saveDescription() {
        const html = this.descEditorRef.el ? this.descEditorRef.el.innerHTML : "";
        this.state.saving = true;
        const result = await supportService.updateProject(this.props.project.id, {
            description: html,
        });
        this.state.saving = false;
        if (result.success) {
            this.props.onUpdated(result.data);
            this.state.editingDescription = false;
        } else {
            this.state.error = result.error || "Failed to save";
        }
    }
}

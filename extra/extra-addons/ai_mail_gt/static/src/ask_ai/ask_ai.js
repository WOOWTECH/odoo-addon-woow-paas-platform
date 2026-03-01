import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { onWillStart, onWillUpdateProps } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";

export class AskAI extends Component {
    static template = "ai_mail_gt.ask_ai";
    static components = {
        Dropdown,
        DropdownItem,
    };
    static props = {
        getActiveIds: { type: Function, optional: true },
        resModel: { type: String, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.store = useService("mail.store");
        this.dropdownState = useDropdownState();
        this._lastResModel = null;
        this._lastActiveId = null;

        this.state = useState({
            assistants: [],
            assistant: {},
            message: "",
        });
        onWillStart(async () => {
            await this._fetchAssistantsIfNeeded(this.props);
        });
        onWillUpdateProps(async (nextProps) => {
            await this._fetchAssistantsIfNeeded(nextProps);
        });
    }

    async _fetchAssistantsIfNeeded(props) {
        const currentActiveId = this.activeId;
        // Only fetch if model or activeId changed
        if (props.resModel !== this._lastResModel || currentActiveId !== this._lastActiveId) {
            this._lastResModel = props.resModel;
            this._lastActiveId = currentActiveId;
            this.state.assistants = await this.getAssistants(props);
            this.state.assistant = this.state.assistants.length > 0 ? this.state.assistants[0] : {};
        }
    }

    get activeId() {
        let activeIds = this.props.getActiveIds();
        return activeIds.length > 0 ? activeIds[0] : null;
    }

    get recordTag() {
        if (this.activeId) {
            return `$${this.props.resModel}/${this.activeId}`;
        } else {
            return `$${this.props.resModel}`;
        }
    }

    async getAssistants(props) {
        const assistants = await this.orm.call("ai.assistant", "get_assistants_for_record", [props.resModel, this.activeId]);
        return assistants;
    }

    onChangeAssistant(ev) {
        const assistant = this.state.assistants.find(assistant => assistant.id === parseInt(ev.target.value));
        this.state.assistant = assistant;
    }

    onInputMessage(ev) {
        this.state.message = ev.target.value;
    }

    onKeyDownMessage(ev) {
        // Send message on Ctrl + Enter
        if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
            ev.preventDefault();
            this.onSendMessage(ev);
        }
    }

    async onSendMessage(ev) {
        if (!this.state.message) {
            return;
        }
        const message = `${this.recordTag} ${this.state.message}`;
        // Open chat window and send message
        const chat = await this.store.getChat({ partnerId: this.state.assistant.partner_id });
        if (chat) {
            chat.open();
            chat.post(message);
        }
        // Reset message
        this.state.message = "";
        // Close dropdown
        this.dropdownState.close();
    }
}

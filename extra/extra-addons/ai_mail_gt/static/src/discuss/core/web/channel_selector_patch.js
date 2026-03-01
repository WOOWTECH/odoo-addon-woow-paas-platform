import { patch } from "@web/core/utils/patch";
import { ChannelSelector } from "@mail/discuss/core/web/channel_selector";
import { cleanTerm } from "@mail/utils/common/format";
import { _t } from "@web/core/l10n/translation";

patch(ChannelSelector.prototype, {
    get inputPlaceholder() {
        if (this.props.category.id === "ai_chats" && this.props.category.onQuickAdd) {
            return this.props.category.quickAddTitle;
        } else if (this.props.category.id === "ai_chats" && this.props.category.canSearch) {
            return this.props.category.searchTitle;
        }
        return super.inputPlaceholder;
    },
	async fetchSuggestions() {
        const cleanedTerm = cleanTerm(this.state.value);
        if (this.props.category.id === "ai_chats") {
            if (this.props.category.onQuickAdd) {
                return this.fetchSuggestionsQuickAdd(cleanedTerm);
            } else if (cleanedTerm) {
                return this.fetchSuggestionsEditing(cleanedTerm);
            } else {
                return super.fetchSuggestions(...arguments);
            }
        }
		return super.fetchSuggestions(...arguments);
	},
    async fetchSuggestionsQuickAdd(cleanedTerm) {
        const domain = cleanedTerm ? [["name", "ilike", cleanedTerm]] : [];
        const fields = ["name"];
        const results = await this.sequential(async () => {
            this.state.navigableListProps.isLoading = true;
            const res = await this.orm.searchRead("ai.assistant", domain, fields, {
                limit: 10,
            });
            this.state.navigableListProps.isLoading = false;
            return res;
        });
        if (!results) {
            this.state.navigableListProps.options = [];
            return;
        }
        const choices = results.map((assistant) => {
            return {
                assistantId: assistant.id,
                classList: "o-discuss-ChannelSelector-suggestion",
                label: assistant.name,
            };
        });
        this.state.navigableListProps.options = choices;
        return;
    },
    async fetchSuggestionsEditing(cleanedTerm) {
        const domain = [
            ["channel_type", "=", "ai_chat"],
            ["is_member", "=", true],
            ["name", "ilike", cleanedTerm]
        ];
        const fields = ["name"];
        const results = await this.sequential(async () => {
            this.state.navigableListProps.isLoading = true;
            const res = await this.orm.searchRead("discuss.channel", domain, fields, {
                limit: 10,
            });
            this.state.navigableListProps.isLoading = false;
            return res;
        });
        if (!results) {
            this.state.navigableListProps.options = [];
            return;
        }
        const choices = results.map((channel) => {
            return {
                channelId: channel.id,
                classList: "o-discuss-ChannelSelector-suggestion",
                label: channel.name,
            };
        });
        this.state.navigableListProps.options = choices;
        return;
    },
    onSelect(option) {
        if (this.props.multiple && this.props.category.id === "ai_chats") {
            if (option.assistantId) {
                this.state.selectedAssistantId = option.assistantId;
            } else {
                this.state.selectedAssistantId = false;
                const thread = this.store.Thread.insert({
                    channel_type: "ai_chat",
                    id: option.channelId,
                    model: "discuss.channel",
                    name: option.label,
                });
                thread.open();
            }
            this.onValidate();
        }
        super.onSelect(option);
    },
    async onValidate() {
        if (this.props.category.id === "ai_chats") {
            if (this.state.selectedAssistantId) {
                const res = await this.orm.searchRead(
                    "ai.assistant",
                    [["id", "=", this.state.selectedAssistantId]],
                    ["partner_id"],
                )
                if (res.length) {
                    const aiPartnerId = res[0].partner_id[0]
                    await this.discussCoreCommonService.startChat(
                        [aiPartnerId],
                        this.env.inChatWindow
                    );
                }
            }
        }
        await super.onValidate();
    }
});

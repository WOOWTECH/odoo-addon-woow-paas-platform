import { Composer } from "@mail/core/common/composer";
import { patch } from "@web/core/utils/patch";

patch(Composer.prototype, {
    get navigableListProps() {
        const props = super.navigableListProps;
        if (!this.hasSuggestions) {
            return props;
        }
        const suggestions = this.suggestion.state.items.suggestions;
        if (this.suggestion.state.items.type === "Model") {
            return {
                ...props,
                optionTemplate: "ai_mail_gt.Composer.suggestionModel",
                options: suggestions.map((suggestion) => ({
                    label: suggestion.label,
                    name: suggestion.name,
                    record: suggestion,
                    classList: "o-mail-Composer-suggestion",
                })),
            };
        }
        else if (this.suggestion.state.items.type === "Record") {
            return {
                ...props,
                optionTemplate: "ai_mail_gt.Composer.suggestionRecord",
                options: suggestions.map((suggestion) => ({
                    label: suggestion.label,
                    name: suggestion.name,
                    record: suggestion,
                    classList: "o-mail-Composer-suggestion",
                })),
            };
        }
        return props;
    },
});

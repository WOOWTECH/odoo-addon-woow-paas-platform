import { SuggestionService } from "@mail/core/common/suggestion_service";
import { patch } from "@web/core/utils/patch";
import { cleanTerm } from "@mail/utils/common/format";

patch(SuggestionService.prototype, {
    getSupportedDelimiters(thread) {
        let delimiters = super.getSupportedDelimiters(thread);
        // Add record tagging delimiter for AI chats
        if (thread?.model === "discuss.channel" && ['ai_chat', 'channel', 'chatter'].includes(thread?.channel_type)) {
            delimiters.push(['$']);
            if (thread?.channel_type === 'ai_chat') {
                // In private AI chats, don't allow mentioning users or threads
                delimiters = delimiters.filter(delimiter => !['@', '#'].includes(delimiter[0]));
            }
        }
        return delimiters;
    },

    async fetchSuggestions({ delimiter, term }, { thread } = {}) {
        if (delimiter === '$' && ['ai_chat', 'channel', 'chatter'].includes(thread?.channel_type)) {
            await this.fetchRecordSuggestions(cleanTerm(term), thread);
        } else {
            return super.fetchSuggestions(...arguments);
        }
    },

    async fetchRecordSuggestions(term, thread) {
        const parts = term.split('/');
        const kwargs = { thread_id: thread.id };

        let suggestions = [];
        if (parts.length === 1) {
            // Model suggestion phase: $sale or $res.part
            kwargs.term = term;
            suggestions = await this.orm.silent.call(
                "ai.thread",
                "get_model_suggestions",
                [],
                kwargs
            );
            this.store.ModelTagging.insert(suggestions);
        } else if (parts.length === 2) {
            // Record suggestion phase: $sale.order/ or $sale.order/SO
            const [model, recordTerm] = parts;
            kwargs.model = model;
            kwargs.record_term = recordTerm || '';
            suggestions = await this.orm.silent.call(
                "ai.thread",
                "get_record_suggestions",
                [],
                kwargs
            );
            this.store.RecordTagging.insert(suggestions);
        }
    },

    searchSuggestions({ delimiter, term }, { thread, sort = false } = {}) {
        if (delimiter === '$' && ['ai_chat', 'channel', 'chatter'].includes(thread?.channel_type)) {
            const parts = cleanTerm(term).split('/', 2);
            let suggestions = [];
            let cleanedTerm = '';
            if (parts.length === 2) {
                cleanedTerm = parts[1].toLowerCase();
                suggestions = Object.values(this.store.RecordTagging.records);
            } else {
                cleanedTerm = parts[0].toLowerCase();
                suggestions = Object.values(this.store.ModelTagging.records);
            }
            // Filter suggestions based on current search term
            const filteredSuggestions = suggestions.filter(suggestion => {
                const name = (suggestion.name || '').toLowerCase();
                const model = (suggestion.model || '').toLowerCase();
                return name.includes(cleanedTerm) || model.includes(cleanedTerm);
            });
    
            return {
                type: parts.length === 2 ? 'Record' : 'Model',
                suggestions: sort ? filteredSuggestions.slice(0, 8) : filteredSuggestions,
            };
        }
        return super.searchSuggestions(...arguments);
    },
});

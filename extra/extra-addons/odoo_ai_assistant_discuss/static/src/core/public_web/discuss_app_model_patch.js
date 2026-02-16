import { DiscussApp } from "@mail/core/public_web/discuss_app_model";
import { Record } from "@mail/core/common/record";

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

patch(DiscussApp, {
    new(data) {
        const res = super.new(data);
        res.ai_chats = {
            extraClass: "o-mail-DiscussSidebarCategory-ai-chat",
            id: "ai_chats",
            name: _t("AI Conversations"),
            sequence: 5,
            isOpen: false,
            canView: false,
            canSearch: true,
            searchTitle: _t("Search for conversation"),
            searchHotkey: "s",
            canQuickAdd: true,
            quickAddTitle: _t("Search for assistant"),
            quickAddHotkey: "n",
            onQuickAdd: false,
            serverStateKey: "is_discuss_sidebar_category_ai_chat_open",
        };
        return res;
    },
});

patch(DiscussApp.prototype, {
    setup(env) {
        super.setup(env);
        this.ai_chats = Record.one("DiscussAppCategory");
    },
});

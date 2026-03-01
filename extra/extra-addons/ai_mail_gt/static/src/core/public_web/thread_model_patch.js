import { Thread } from "@mail/core/common/thread_model";

import { patch } from "@web/core/utils/patch";

patch(Thread.prototype, {
    get canLeave() {
        return this.channel_type !== "ai_chat" && super.canLeave;
    },
    get canUnpin() {
        if (this.channel_type === "ai_chat") {
            return !this.selfMember || this.selfMember.message_unread_counter === 0;
        }
        return super.canUnpin;
    },
    get avatarUrl() {
        if (this.channel_type === "ai_chat" && this.correspondent) {
            return this.correspondent.persona.avatarUrl;
        }
        return super.avatarUrl;
    },
    setAsDiscussThread(pushState) {
        super.setAsDiscussThread(pushState);
        if (this.store.env.services.ui.isSmall && this.channel_type === "ai_chat") {
            this.store.discuss.activeTab = "ai_chat";
        }
    },
    _computeDiscussAppCategory() {
        if (this.channel_type === "ai_chat") {
            return this.store.discuss.ai_chats;
        }
        return super._computeDiscussAppCategory();
    },
});

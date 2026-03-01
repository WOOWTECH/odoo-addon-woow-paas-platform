import { threadActionsRegistry } from "@mail/core/common/thread_actions";
import { patch } from "@web/core/utils/patch";

const invitePeopleAction = threadActionsRegistry.get("invite-people");
const memberListAction = threadActionsRegistry.get("member-list");

patch(invitePeopleAction, {
    condition(component) {
        if (component.thread?.channel_type === "ai_chat") {
            return false;
        }
        return super.condition(...arguments);
    },
});

patch(memberListAction, {
    condition(component) {
        if (component.thread?.channel_type === "ai_chat") {
            return false;
        }
        return super.condition(...arguments);
    },
});

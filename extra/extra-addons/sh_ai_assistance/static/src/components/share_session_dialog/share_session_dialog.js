/** @odoo-module */

import { Component, useRef } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

export class ShareSessionDialog extends Component {
    static template = "sh_ai_assistance.ShareSessionDialog";
    static components = { Dialog };
    static props = {
        close: { type: Function },
        shareLink: { type: String },
    };

    setup() {
        this.shareLinkInput = useRef("shareLinkInput");
        this.notification = useService("notification");
    }

    async onCopyLink() {
        // Simple copy to clipboard
        try {
            await navigator.clipboard.writeText(this.props.shareLink);
            this.notification.add("Link copied to clipboard!", { type: "success" });
        } catch (err) {
            console.error('Failed to copy text: ', err);
            this.notification.add("Failed to copy link.", { type: "danger" });
        }
        this.props.close();
    }
}

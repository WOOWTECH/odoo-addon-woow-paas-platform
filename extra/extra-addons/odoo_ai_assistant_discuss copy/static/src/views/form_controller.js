import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
import { AskAI } from "@ai_mail_gt/ask_ai/ask_ai";

patch(FormController, {
    components: {
        ...FormController.components,
        AskAI,
    }
});

/** @odoo-module **/

import { registry } from "@web/core/registry";
import { AiChatAppComponent } from "../components/ai_chat_app/ai_chat_app";

registry.category("actions").add("sh_ai_assistance", AiChatAppComponent);
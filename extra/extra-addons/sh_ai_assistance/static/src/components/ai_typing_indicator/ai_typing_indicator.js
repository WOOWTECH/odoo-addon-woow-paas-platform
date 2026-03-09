import { Component } from "@odoo/owl";

export class AiTypingIndicatorComponent extends Component {
    static template = "sh_ai_assistance.AiTypingIndicatorTemplate";
    static props = {
        currentLlm: { type: [Object, { value: null }], optional: true },
        currentUser: { type: Object, optional: true },
    };

    get avatarUrl() {
        if (this.props.currentLlm?.id && this.props.currentLlm?.image) {
            return `/web/image/sh.ai.llm/${this.props.currentLlm.id}/image`;
        }
        // Default AI avatar if no model image
        return 'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAzNiAzNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHJlY3Qgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiByeD0iMTgiIGZpbGw9IiM2MzY2RjEiLz4KPHN2ZyB4PSI2IiB5PSI2IiB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIGZpbGw9IndoaXRlIj4KPHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTEyIDJMMTMuMDkgOC4yNkwyMCA5TDEzLjA5IDE1Ljc0TDEyIDIyTDEwLjkxIDE1Ljc0TDQgOUwxMC45MSA4LjI2TDEyIDJaIiBmaWxsPSJjdXJyZW50Q29sb3IiLz4KPC9zdmc+Cjwvc3ZnPgo8L3N2Zz4K';
    }

    get modelName() {
        return this.props.currentLlm?.name || 'AI';
    }

    get typingMessage() {
        // Creative, non-advanced messaging options
        const messages = [
            'is typing...',
            'is responding...',
            'is working on your request...',
            'is generating a response...'
        ];

        // For now, use the first one. Could randomize or make configurable later
        return messages[0];
    }

    get providerClass() {
        if (!this.props.currentLlm?.sh_company) return '';

        const company = this.props.currentLlm.sh_company.toLowerCase();
        if (company.includes('google') || company.includes('gemini')) {
            return 'google';
        } else if (company.includes('openai') || company.includes('gpt')) {
            return 'openai';
        } else if (company.includes('anthropic') || company.includes('claude')) {
            return 'anthropic';
        }
        return '';
    }
}
# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

"""
Dynamic Prompt Builder for AI Assistant

Assembles the complete system prompt using modular instruction blocks
stored in the LLM configuration. Follows Claude's prompt engineering
best practices:
- Long-form data (Model Catalog) at the top
- XML tags for clear section separation
- User query context at the bottom
"""


class PromptBuilder:
    """
    Builds dynamic prompts using instructions stored in database LLM records.
    Optimized for Claude/Gemini with XML structure and long-context best practices.
    """

    def __init__(self, env):
        self.env = env

    def build_user_context(self):
        """
        Build dynamic user and company context.
        Placed near the end of prompt (after instructions, before examples).
        """
        user = self.env.user
        company = user.company_id
        user_tz = user.tz or company.partner_id.tz or 'UTC'

        return f"""
<current_context>
<user>
- Name: {user.name}
- Timezone: {user_tz}
- Language: {user.lang or 'en_US'}
</user>
<company>
- Name: {company.name}
- Currency: {company.currency_id.symbol} ({company.currency_id.name})
- Country: {company.country_id.name if company.country_id else 'Not set'}
</company>
</current_context>
"""

    def build_tools_section(self, tool_declarations):
        """
        Build tool documentation from declarations.
        Uses concise format optimized for LLM understanding.
        """
        if not tool_declarations:
            return ""

        section = "<available_tools>\n"
        for tool in tool_declarations:
            section += f"\n## {tool['name']}\n"
            section += f"Purpose: {tool['description']}\n"
            if 'parameters' in tool and 'properties' in tool['parameters']:
                section += "Parameters:\n"
                for p_name, p_info in tool['parameters']['properties'].items():
                    req = "(required)" if p_name in tool['parameters'].get('required', []) else "(optional)"
                    section += f"  - {p_name} {req}: {p_info.get('description', '')}\n"
        section += "\n</available_tools>"
        return section

    def build_model_catalog(self, model_catalog):
        """
        Build the model catalog section.
        CRITICAL: Placed at the TOP of prompt for best long-context performance.
        """
        if not model_catalog:
            return "<model_catalog>\nNo models available.\n</model_catalog>"

        section = """<model_catalog>
CRITICAL: This is your complete reference of available Odoo models.
Use ONLY models from this list. If a model is missing, the user lacks access.

"""
        for model in model_catalog:
            display = model.get('display_name', '')
            name = model.get('model', '')
            section += f"- {display} ({name})\n"

        section += "\n</model_catalog>"
        return section

    def build_technical_memory(self, previous_messages):
        """
        Extract technical IDs and resolved names from history to give
        the AI 'Technical Memory' for faster follow-up queries.
        """
        if not previous_messages:
            return ""

        resolutions = []
        for msg in previous_messages:
            debug = msg.debug_info or {}
            for tc in debug.get('tool_calls', []):
                # Extract resolutions from fuzzy_lookup or successful search returns
                if tc.get('tool') == 'fuzzy_lookup' and tc.get('result', {}).get('success'):
                    term = tc.get('args', {}).get('search_term')
                    model = tc.get('args', {}).get('model')
                    # We store full results in raw_result now
                    raw = tc.get('raw_result') or {}
                    results = raw.get('results', [])
                    for r in results:
                        if 'id' in r and 'display_name' in r:
                            resolutions.append(f"- '{term}' resolved to ID {r['id']} ({r['display_name']}) in model '{model}'")
                
                # Also capture direct search findings if they look like specific record hits
                elif tc.get('tool') == 'search_records' and tc.get('result', {}).get('success'):
                    # If AI searched by name and found a result, it's worth remembering
                    domain = tc.get('args', {}).get('domain', [])
                    for cond in domain:
                        if isinstance(cond, (list, tuple)) and cond[0] == 'name' and cond[1] == '=':
                            raw = tc.get('raw_result') or {}
                            records = raw.get('records', [])
                            for rec in records:
                                if 'id' in rec:
                                    resolutions.append(f"- Explicit match: '{cond[2]}' is ID {rec['id']} in model '{tc['args']['model']}'")

        if not resolutions:
            return ""

        # Use set to unique-ify and joined with newline
        unique_resolutions = "\n".join(sorted(list(set(resolutions))))
        return f"""
<recent_technical_memory>
In this session, the following records were already identified. 
USE THESE IDs DIRECTLY for follow-up questions to avoid redundant lookups and ensure accuracy:
{unique_resolutions}
</recent_technical_memory>
"""

    def build_complete_prompt(self, system_prompt, tool_declarations, model_catalog=None, instructions=None, is_followup=False):
        """
        Assemble the complete prompt following Claude's best practices:
        1. Critical rules first (highest priority)
        2. Model catalog early (long-form data at top)
        3. Identity and workflow
        4. Tools documentation
        5. User context
        6. Examples last (near the query)
        
        Args:
            is_followup: If True, builds a condensed prompt for follow-up queries
                         (omits Model Catalog and verbose instructions to save tokens)
        """
        instructions = instructions or {}
        parts = []

        # =====================================================================
        # LAYER 1: CRITICAL GUARDRAILS (Always included - highest priority)
        # =====================================================================
        if instructions.get('critical'):
            parts.append(instructions['critical'].strip())

        # =====================================================================
        # LAYER 2: MODEL CATALOG (Only on first query, or condensed reference)
        # =====================================================================
        if is_followup:
            # Condensed reference for follow-up queries
            parts.append("""<model_catalog_reference>
The complete Model Catalog was provided at the start of this conversation.
Use the models you've already seen. If you need to discover a new model, call get_models_list().
</model_catalog_reference>""")
        elif model_catalog:
            parts.append(self.build_model_catalog(model_catalog))

        # =====================================================================
        # LAYER 3: IDENTITY & CORE PRINCIPLES
        # =====================================================================
        if system_prompt:
            parts.append(system_prompt.strip())

        # =====================================================================
        # LAYER 4: WORKFLOW (Step-by-step reasoning)
        # =====================================================================
        if instructions.get('workflow'):
            if is_followup:
                # Condensed workflow reminder
                parts.append("""<workflow_reminder>
Continue using the 6-step workflow: Analyze → Match Model → Discover Fields → Resolve IDs → Execute Tool → Present Results.
</workflow_reminder>""")
            else:
                parts.append(instructions['workflow'].strip())

        # =====================================================================
        # LAYER 5: TOOL SELECTION RULES
        # =====================================================================
        if instructions.get('tool'):
            if not is_followup:
                parts.append(instructions['tool'].strip())

        # =====================================================================
        # LAYER 6: TOOLS DOCUMENTATION (Auto-generated, always included)
        # =====================================================================
        tools_doc = self.build_tools_section(tool_declarations)
        if tools_doc:
            parts.append(tools_doc)

        # =====================================================================
        # LAYER 7: OUTPUT FORMATTING
        # =====================================================================
        if instructions.get('formatting'):
            if not is_followup:
                parts.append(instructions['formatting'].strip())

        # =====================================================================
        # LAYER 8: SECURITY & ACCESS CONTROL
        # =====================================================================
        if instructions.get('security'):
            if not is_followup:
                parts.append(instructions['security'].strip())

        # =====================================================================
        # LAYER 9: USER/COMPANY CONTEXT (Always included for personalization)
        # =====================================================================
        parts.append(self.build_user_context().strip())

        # =====================================================================
        # LAYER 10: EXAMPLES (Multishot - only on first query)
        # =====================================================================
        if instructions.get('examples'):
            if not is_followup:
                parts.append(instructions['examples'].strip())

        # =====================================================================
        # LAYER 11: TECHNICAL MEMORY (For follow-up speed)
        # =====================================================================
        if is_followup and 'previous_messages' in instructions:
            tech_memory = self.build_technical_memory(instructions['previous_messages'])
            if tech_memory:
                parts.append(tech_memory.strip())

        # =====================================================================
        # ASSEMBLE WITH CLEAR SEPARATORS
        # =====================================================================
        return "\n\n".join(parts)


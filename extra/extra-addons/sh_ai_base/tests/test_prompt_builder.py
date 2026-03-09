# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo.tests.common import TransactionCase
from odoo.addons.sh_ai_base.provider.prompt_builder import PromptBuilder

class TestPromptBuilder(TransactionCase):

    def setUp(self):
        super(TestPromptBuilder, self).setUp()
        self.builder = PromptBuilder(self.env)

    def test_build_complete_prompt_structure(self):
        """Test that the prompt builder correctly assembles instructions and catalog"""
        catalog = [{'model': 'sale.order', 'display_name': 'Sales Order'}]
        instructions = {
            'workflow': 'DO THIS FIRST',
            'tool': 'USE TOOLS',
            'critical': 'DANGER RULES',
        }
        
        prompt = self.builder.build_complete_prompt(
            system_prompt="YOU ARE AI",
            tool_declarations=[{'name': 'test_tool', 'description': 'testing tool'}],
            model_catalog=catalog,
            instructions=instructions
        )
        
        # Check for core sections
        self.assertIn("YOU ARE AI", prompt)
        self.assertIn("<model_catalog>", prompt)
        self.assertIn("sale.order", prompt)
        self.assertIn("DO THIS FIRST", prompt)
        self.assertIn("DANGER RULES", prompt)
        self.assertIn("<available_tools>", prompt)
        self.assertIn("test_tool", prompt)

    def test_prompt_xml_escaping(self):
        """Test that XML tags in instructions are handled (if applicable)"""
        instructions = {'workflow': '<test>content</test>'}
        prompt = self.builder.build_complete_prompt(
            system_prompt="AI",
            tool_declarations=[],
            model_catalog=[],
            instructions=instructions
        )
        self.assertIn("<test>content</test>", prompt)

# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo.tests.common import TransactionCase
from odoo.addons.sh_ai_base.provider.odoo_tools import fuzzy_lookup, _validate_and_fix_domain

class TestOdooTools(TransactionCase):

    def setUp(self):
        super(TestOdooTools, self).setUp()
        # Create a test partner (Contact) with UNIQUE name
        self.partner_azure = self.env['res.partner'].create({
            'name': 'UniqueAzureInterior',
        })
        # Create a test category
        self.tag_senior = self.env['res.partner.category'].create({
            'name': 'UniqueSeniorTag',
        })
        self.partner_azure.category_id = [(4, self.tag_senior.id)]

    def test_fuzzy_lookup_exact(self):
        """Test exact match with fuzzy_lookup"""
        result = fuzzy_lookup(self.env, 'res.partner', 'UniqueAzureInterior')
        self.assertTrue(result['success'])
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['id'], self.partner_azure.id)

    def test_fuzzy_lookup_with_suffix(self):
        """Test match after stripping 'Contact' suffix"""
        result = fuzzy_lookup(self.env, 'res.partner', 'UniqueAzureInterior Contact')
        self.assertTrue(result['success'])
        self.assertEqual(result['cleaned_term'], 'UniqueAzureInterior')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['id'], self.partner_azure.id)

    def test_validate_and_fix_domain_many2one(self):
        """Test that string values in Many2one domains are converted to IDs"""
        domain = [['partner_id', '=', 'UniqueAzureInterior Contact']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.users', domain)
        
        self.assertEqual(fixed_domain, [['partner_id', '=', self.partner_azure.id]])
        self.assertTrue(any("Fixed partner_id" in c for c in corrections))

    def test_validate_and_fix_domain_many2many(self):
        """Test Many2many resolution"""
        domain = [['category_id', 'in', 'UniqueSeniorTag']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.partner', domain)
        
        self.assertEqual(fixed_domain, [['category_id', 'in', [self.tag_senior.id]]])

    def test_validate_and_fix_domain_no_match(self):
        """Test that original domain is kept if no match is found"""
        # Use a very random string to avoid partial hits on "Partner"
        domain = [['partner_id', '=', 'XYZ_NONEXISTENT_PARTNER_ABC']]
        fixed_domain, corrections = _validate_and_fix_domain(self.env, 'res.users', domain)
        
        self.assertEqual(fixed_domain, domain)
        self.assertTrue(any("Could not resolve" in c for c in corrections))

#!/usr/bin/env python3
"""PR #986 comment 11112 global-finance-ledger policy regression."""

import csv
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MODEL = _REPO_ROOT / 'src/models/billing_incident.py'
_VIEW = _REPO_ROOT / 'src/views/billing_incident_views.xml'
_RUNBOOK = (
    _REPO_ROOT / 'docs/guides/maintenance/'
    'billing-incident-manual-resolution.md'
)
_ACL = _REPO_ROOT / 'src/security/ir.model.access.csv'
_RULES = _REPO_ROOT / 'src/security/ir_rules.xml'


class TestBillingIncidentPolicy986(unittest.TestCase):

    def test_comment_11112_wording_is_durable_on_every_policy_surface(self):
        exact_contract = (
            'Odoo role `account.group_account_manager` (not a Workspace role)',
            'all incidents across all companies',
            'all current managers in that Odoo group',
            'no company M2O/rule; base.group_system behavior is not narrowed',
        )
        for path in (_MODEL, _VIEW, _RUNBOOK):
            source = path.read_text(encoding='utf-8')
            normalized_source = ' '.join(source.split())
            with self.subTest(path=str(path.relative_to(_REPO_ROOT))):
                for wording in exact_contract:
                    self.assertIn(wording, normalized_source)

        runbook = _RUNBOOK.read_text(encoding='utf-8')
        for evidence in (
                'prod/stg each currently has 1 `res.company`',
                'prod=1/stg=3',
                'current-state evidence only',
                'policy does not assume single-company',
                '`base.group_system` alone does not grant',
                'unresolved incidents',
                'current account-manager'):
            self.assertIn(evidence, runbook)

    def test_acl_is_odoo_account_manager_and_has_no_company_rule(self):
        with _ACL.open(encoding='utf-8', newline='') as stream:
            rows = [
                row for row in csv.DictReader(stream)
                if row['model_id:id'] == 'model_woow_billing_incident'
            ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0],
            {
                'id': 'access_billing_incident_account_manager',
                'name': 'woow.billing.incident.account.manager',
                'model_id:id': 'model_woow_billing_incident',
                'group_id:id': 'account.group_account_manager',
                'perm_read': '1',
                'perm_write': '1',
                'perm_create': '0',
                'perm_unlink': '0',
            },
        )
        model_source = _MODEL.read_text(encoding='utf-8')
        self.assertNotIn("fields.Many2one('res.company'", model_source)
        self.assertNotIn('company_ref = fields.Many2one', model_source)
        self.assertNotIn(
            'model_woow_billing_incident',
            _RULES.read_text(encoding='utf-8'),
            'global ledger must not acquire an ir.rule, including a company rule',
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)

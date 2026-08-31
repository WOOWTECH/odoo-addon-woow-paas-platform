#!/usr/bin/env python3
"""PR #986 comment 11112 global-finance-ledger policy regression."""

import ast
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
                '現況佐證，不是 single-company policy',
                '`base.group_system` alone does not grant',
                'unresolved incidents',
                'current account-manager'):
            self.assertIn(evidence, runbook)

    def test_every_incident_activity_path_uses_roster_then_review_lock(self):
        source = _MODEL.read_text(encoding='utf-8')
        tree = ast.parse(source)
        classes = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }

        def method(class_name, method_name):
            matches = [
                node for node in classes[class_name].body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            ]
            self.assertEqual(
                len(matches), 1,
                '%s.%s must exist exactly once' % (class_name, method_name),
            )
            return matches[0]

        def call_lines(node, call_name):
            return sorted(
                call.lineno
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and (
                    (isinstance(call.func, ast.Attribute)
                     and call.func.attr == call_name)
                    or (isinstance(call.func, ast.Name)
                        and call.func.id == call_name)
                )
            )

        helper = method(
            'BillingIncident', '_acquire_review_activity_locks')
        roster_lines = call_lines(helper, '_acquire_billing_roster_lock')
        review_lines = call_lines(helper, '_acquire_review_activity_lock')
        self.assertEqual(len(roster_lines), 1)
        self.assertEqual(len(review_lines), 1)
        self.assertLess(
            roster_lines[0], review_lines[0],
            'global roster advisory lock must precede every incident review lock',
        )

        direct_review_callers = set()
        for class_node in classes.values():
            for function in class_node.body:
                if not isinstance(
                        function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if call_lines(function, '_acquire_review_activity_lock'):
                    direct_review_callers.add(
                        (class_node.name, function.name))
        self.assertEqual(
            direct_review_callers,
            {('BillingIncident', '_acquire_review_activity_locks')},
            'no activity path may acquire the review lock directly',
        )

        for class_name, method_name in (
                ('BillingIncident',
                 '_ensure_review_activities_current_cursor'),
                ('BillingIncident', '_clear_review_activities'),
                ('BillingIncident', '_transition_resolved_current_cursor'),
                ('BillingIncident', 'action_mark_manually_resolved'),
                ('BillingIncident', '_resolve'),
                ('MailActivity', '_action_done'),
                ('MailActivity', 'unlink')):
            with self.subTest(path='%s.%s' % (class_name, method_name)):
                self.assertTrue(
                    call_lines(
                        method(class_name, method_name),
                        '_acquire_review_activity_locks',
                    ),
                    'path must enter the roster→review lock helper',
                )

        schedule = method('BillingIncident', '_schedule_review_activities')
        convergence = method(
            'BillingIncident',
            '_converge_unresolved_review_activities_current_cursor',
        )
        self.assertTrue(call_lines(
            schedule, '_ensure_review_activities_current_cursor'))
        self.assertTrue(call_lines(
            convergence, '_acquire_billing_roster_lock'))
        self.assertTrue(call_lines(
            convergence, '_ensure_review_activities_current_cursor'))
        role_hooks = {
            'ResUsers': ('create', 'write', 'unlink'),
            'ResGroups': ('create', 'copy', 'write', 'unlink'),
        }
        for class_name, method_names in role_hooks.items():
            for method_name in method_names:
                with self.subTest(path='%s.%s' % (class_name, method_name)):
                    hook = method(class_name, method_name)
                    self.assertTrue(call_lines(
                        hook, '_acquire_billing_roster_lock'))
                    self.assertTrue(call_lines(
                        hook, '_converge_billing_roster_if_changed'))

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

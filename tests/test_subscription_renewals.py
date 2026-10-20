"""Local regression for the unchanged 3-day API and shared 7-day reserve.

Run: python3 tests/test_subscription_renewals.py -v
The real endpoint body is loaded with AST to avoid importing Odoo's HTTP stack.
ORM selection is exercised against records with inclusive boundary dates, owner,
state and concealment differences. Database/ACL behavior remains an Odoo check.
"""
import ast
import importlib.util
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'subscription_renewals', ROOT / 'src/services/subscription_renewals.py')
renewals = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renewals)
TODAY = date(2026, 10, 18)


class _Subscriptions:
    def __init__(self, rows):
        self.rows = rows
        self.domains = []

    def sudo(self):
        return self

    def search(self, domain):
        self.domains.append(domain)
        selected = self.rows
        for key, operator, value in domain:
            if operator == '=':
                selected = [row for row in selected if getattr(row, key) == value]
            elif operator == '>=':
                selected = [row for row in selected if getattr(row, key) >= value]
            elif operator == '<=':
                selected = [row for row in selected if getattr(row, key) <= value]
            elif operator == 'not in':
                selected = [row for row in selected if getattr(row, key) not in value]
            else:
                raise AssertionError(operator)
        return selected


class _Templates:
    def sudo(self):
        return self

    def search(self, domain):
        if domain != [('conceal_instances', '=', True)]:
            raise AssertionError(domain)
        return SimpleNamespace(ids=[900])


class _Services:
    def sudo(self):
        return self

    def search(self, domain):
        if domain != [('template_id', 'in', [900])]:
            raise AssertionError(domain)
        return self

    def mapped(self, relation):
        if relation != 'subscription_id':
            raise AssertionError(relation)
        return SimpleNamespace(ids=[90, 91])


def _subscription(identifier, offset, amount=105.0, owner=11, state='in_progress', plan=True):
    return SimpleNamespace(
        id=identifier, sh_partner_id=owner, state=state, sh_plan_price=amount,
        sh_date_of_next_payment=TODAY + timedelta(days=offset),
        sh_subscription_plan_id=SimpleNamespace(name='Monthly') if plan else False)


def _endpoint(env, partner):
    """Execute the checked-in controller method with its route decorator removed."""
    tree = ast.parse((ROOT / 'src/controllers/billing.py').read_text())
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == 'api_upcoming_renewals')
    method.decorator_list = []
    method.returns = None
    scope = {
        'request': SimpleNamespace(env=env),
        'fields': SimpleNamespace(Date=SimpleNamespace(today=lambda: TODAY)),
        'upcoming_renewal_requirements': renewals.upcoming_renewal_requirements,
    }
    env.user = SimpleNamespace(partner_id=partner)
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<billing endpoint>', 'exec'), scope)
    return scope['api_upcoming_renewals'](
        SimpleNamespace(_hidden_subscription_ids=lambda request_env: [90, 91]))


class _Env(dict):
    pass


class TestSubscriptionRenewals(unittest.TestCase):
    def setUp(self):
        self.partner = SimpleNamespace(id=11, wallet_balance=110.0)
        self.subscriptions = _Subscriptions([
            _subscription(1, 0), _subscription(2, 3, 50.5),
            _subscription(3, 4, 20.0), _subscription(4, 7, 30.0),
            _subscription(5, 8), _subscription(6, -1),
            _subscription(7, 2, owner=12), _subscription(8, 2, state='cancel'),
            _subscription(90, 2), _subscription(91, 5),
        ])
        self.env = _Env({
            'sh.subscription.subscription': self.subscriptions,
            'woow_paas_platform.cloud_service': _Services(),
            'woow_paas_platform.cloud_app_template': _Templates(),
        })

    def test_three_day_endpoint_keeps_exact_payload_and_filtering(self):
        self.assertEqual(_endpoint(self.env, self.partner), {
            'success': True,
            'data': {
                'has_low_balance': True,
                'upcoming': [
                    {'subscription_id': 1, 'plan_name': 'Monthly', 'amount': 105.0,
                     'renewal_date': '2026-10-18', 'sufficient': True},
                    {'subscription_id': 2, 'plan_name': 'Monthly', 'amount': 50.5,
                     'renewal_date': '2026-10-21', 'sufficient': True},
                ],
                'shortfall': 45.5,
            },
        })
        self.assertEqual(self.subscriptions.domains[-1], [
            ('sh_partner_id', '=', 11), ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '>=', TODAY),
            ('sh_date_of_next_payment', '<=', TODAY + timedelta(days=3)),
            ('id', 'not in', [90, 91]),
        ])

    def test_seven_day_reserve_uses_same_owner_state_and_hidden_filter(self):
        upcoming, total = renewals.upcoming_renewal_requirements(
            self.env, self.partner, today=TODAY, days=7)
        self.assertEqual([row['subscription_id'] for row in upcoming], [1, 2, 3, 4])
        self.assertEqual(total, 205.5)
        # sh_plan_price already includes tax; no extra 5% or rounding is applied.
        self.assertEqual(upcoming[0]['amount'], 105.0)
        self.assertEqual(self.subscriptions.domains[-1][-1], ('id', 'not in', [90, 91]))

    def test_model_hidden_chain_and_billing_exclusion_produce_identical_result(self):
        model_result = renewals.upcoming_renewal_requirements(
            self.env, self.partner, today=TODAY, days=7)
        billing_result = renewals.upcoming_renewal_requirements(
            self.env, self.partner, today=TODAY, days=7,
            hidden_subscription_ids=[90, 91])
        self.assertEqual(model_result, billing_result)

    def test_sufficient_flag_remains_per_subscription_and_empty_plan_is_blank(self):
        self.partner.wallet_balance = 105.0
        self.subscriptions.rows = [_subscription(1, 0), _subscription(2, 1, None, plan=False)]
        payload = _endpoint(self.env, self.partner)
        self.assertFalse(payload['data']['has_low_balance'])
        self.assertEqual(payload['data']['shortfall'], 0.0)
        self.assertTrue(payload['data']['upcoming'][0]['sufficient'])
        self.assertEqual(payload['data']['upcoming'][1], {
            'subscription_id': 2, 'plan_name': '', 'amount': 0.0,
            'renewal_date': '2026-10-19', 'sufficient': True,
        })

    def test_no_upcoming_renewal_keeps_existing_empty_response(self):
        self.subscriptions.rows = []
        self.assertEqual(_endpoint(self.env, self.partner), {
            'success': True,
            'data': {'has_low_balance': False, 'upcoming': [], 'shortfall': 0.0},
        })

    def test_zero_day_window_includes_today_only(self):
        upcoming, total = renewals.upcoming_renewal_requirements(
            self.env, self.partner, today=TODAY, days=0)
        self.assertEqual([row['subscription_id'] for row in upcoming], [1])
        self.assertEqual(total, 105.0)

    def test_invalid_window_is_rejected_before_reading_subscriptions(self):
        for days in (True, False, -1, 7.0, '7', None):
            with self.assertRaises(ValueError):
                renewals.upcoming_renewal_requirements(
                    self.env, self.partner, today=TODAY, days=days)
        self.assertFalse(self.subscriptions.domains)


if __name__ == '__main__':
    unittest.main()

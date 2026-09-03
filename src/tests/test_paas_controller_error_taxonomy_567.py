# -*- coding: utf-8 -*-
"""#567 Phase A：PaaS controller technical exception 不得原文外洩。"""
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import (
    AccessDenied, AccessError, MissingError, UserError, ValidationError,
)
from odoo.tests.common import HttpCase, TransactionCase, tagged
from psycopg2 import OperationalError

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.cloud_service import (
    _internal_provision,
)
from odoo.addons.woow_paas_platform.models.subscription_lifecycle_mixin import (
    SubscriptionLifecycleMixin,
)

_REQUEST = 'odoo.addons.woow_paas_platform.controllers.paas.request'
_CREATE_GENERIC = 'An error occurred while creating the service.'
_SKIP_GENERIC = 'An error occurred while skipping the trial period.'
_TECHNICAL_ERRORS = (AccessDenied, AccessError, MissingError, RuntimeError)


@tagged('post_install', '-at_install')
class TestPaaSCreateAndSkipErrorTaxonomy567(HttpCase):
    """HTTP route 層級釘住 create/skip-trial 的 ordered taxonomy。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.owner = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'PaaS Error Taxonomy WS 567',
            'owner_id': self.owner.id,
        })

        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 567', 'code': 'TS567', 'type': 'sale',
                'company_id': company.id,
            })
        company._ensure_subscription_journal()

        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Plan 567', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
        })
        self.env['product.template'].create({
            'name': 'Product 567', 'type': 'service', 'list_price': 100,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
        })
        self.template = self.env[
            'woow_paas_platform.cloud_app_template'
        ].create({
            'name': 'Template 567', 'slug': 'template-567',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/template-567',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False,
            'subscription_plan_ids': [(6, 0, [self.plan.id])],
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service']
        ).create({
            'name': 'Skip Trial Service 567',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

    def _json_route(self, path, params):
        response = self.url_open(
            path,
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
        ).json()
        self.assertIn('result', response, response)
        return response['result']

    def _create(self, name):
        return self._json_route(
            '/api/workspaces/%s/services' % self.workspace.id,
            {
                'action': 'create', 'template_id': self.template.id,
                'name': name, 'plan_id': self.plan.id,
            },
        )

    def _skip_trial(self):
        return self._json_route(
            '/api/cloud-services/services/%s/skip-trial' % self.service.id,
            {},
        )

    def test_create_technical_errors_are_generic_and_triggered(self):
        for index, error_type in enumerate(_TECHNICAL_ERRORS):
            sentinel = 'create-sensitive-567-%s' % error_type.__name__
            with self.subTest(error_type=error_type.__name__), patch.object(
                SubscriptionLifecycleMixin,
                '_create_and_confirm_subscription',
                side_effect=error_type(sentinel),
            ) as raised, patch.object(
                PaasController, '_format_service',
            ) as downstream:
                result = self._create('create-567-%s' % index)
                self.assertEqual(raised.call_count, 1)
                downstream.assert_not_called()
                self.assertEqual(
                    result, {'success': False, 'error': _CREATE_GENERIC})
                self.assertNotIn(sentinel, result['error'])

    def test_create_business_errors_keep_their_messages(self):
        for index, error_type in enumerate((UserError, ValidationError)):
            message = 'create-business-567-%s' % error_type.__name__
            with self.subTest(error_type=error_type.__name__), patch.object(
                SubscriptionLifecycleMixin,
                '_create_and_confirm_subscription',
                side_effect=error_type(message),
            ) as raised, patch.object(
                PaasController, '_format_service',
            ) as downstream:
                result = self._create('create-business-567-%s' % index)
                self.assertEqual(raised.call_count, 1)
                downstream.assert_not_called()
                self.assertEqual(result, {'success': False, 'error': message})

    def test_skip_trial_technical_errors_are_generic_and_triggered(self):
        for error_type in _TECHNICAL_ERRORS:
            sentinel = 'skip-sensitive-567-%s' % error_type.__name__
            with self.subTest(error_type=error_type.__name__), patch.object(
                SubscriptionLifecycleMixin, '_skip_trial',
                side_effect=error_type(sentinel),
            ) as raised, patch.object(
                type(self.service), '_get_subscription_info',
            ) as downstream:
                result = self._skip_trial()
                self.assertEqual(raised.call_count, 1)
                downstream.assert_not_called()
                self.assertEqual(
                    result, {'success': False, 'error': _SKIP_GENERIC})
                self.assertNotIn(sentinel, result['error'])

    def test_skip_trial_business_errors_keep_their_messages(self):
        for error_type in (UserError, ValidationError):
            message = 'skip-business-567-%s' % error_type.__name__
            with self.subTest(error_type=error_type.__name__), patch.object(
                SubscriptionLifecycleMixin, '_skip_trial',
                side_effect=error_type(message),
            ) as raised, patch.object(
                type(self.service), '_get_subscription_info',
            ) as downstream:
                result = self._skip_trial()
                self.assertEqual(raised.call_count, 1)
                downstream.assert_not_called()
                self.assertEqual(result, {'success': False, 'error': message})

    def test_allowed_create_and_skip_execute_their_downstreams(self):
        with patch.object(
            SubscriptionLifecycleMixin, '_create_and_confirm_subscription',
            return_value=None,
        ) as create_gate, patch.object(
            PaasController, '_format_service', return_value={'id': 567},
        ) as create_downstream:
            create_result = self._create('create-allowed-567')
        self.assertEqual(create_gate.call_count, 1)
        create_downstream.assert_called_once()
        self.assertTrue(create_result['success'], create_result)

        invoice = {'id': 567}
        subscription = {'state': 'in_progress'}
        with patch.object(
            SubscriptionLifecycleMixin, '_skip_trial', return_value=invoice,
        ) as skip_gate, patch.object(
            type(self.service), '_get_subscription_info',
            return_value=subscription,
        ) as skip_downstream:
            skip_result = self._skip_trial()
        self.assertEqual(skip_gate.call_count, 1)
        skip_downstream.assert_called_once()
        self.assertEqual(skip_result, {
            'success': True,
            'data': {'invoice': invoice, 'subscription': subscription},
        })

    def test_create_operational_error_is_reraised(self):
        controller = PaasController()
        downstream = MagicMock()
        with patch(_REQUEST, new=MagicMock(env=self.env)), patch.object(
            SubscriptionLifecycleMixin,
            '_create_and_confirm_subscription',
            side_effect=OperationalError('create-retry-567'),
        ) as raised, patch.object(
            PaasController, '_format_service', new=downstream,
        ):
            with self.assertRaises(OperationalError):
                controller._create_service(
                    self.workspace, self.template.id, 'create-retry-567', {},
                    plan_id=self.plan.id,
                )
        self.assertEqual(raised.call_count, 1)
        downstream.assert_not_called()

    def test_skip_trial_operational_error_is_reraised(self):
        """🔴 `request.env` 必須是 **workspace owner** 的 env，不能是 `self.env`。

        隔壁 `test_create_operational_error_is_reraised` 用 `self.env` 是對的——它
        呼叫的是**私有**的 `_create_service()`，前面沒有任何閘。本條呼叫的是**公開
        端點** `api_service_skip_trial()`，而它在 try 之前有兩道會 return 的閘
        （`src/controllers/paas.py:1357` 的 Service not found、`:1361` 的
        Admin access required）。

        `self.env` 是 `HttpCase` 的 SUPERUSER env，而
        `Workspace.check_user_access()`（`src/models/workspace.py:434-450`）
        **沒有 superuser 旁路**：它就是去 `workspace_access` 搜 `user_id`，搜不到
        回 `False`。`Workspace.create()`（`:207-225`）只替 `owner_id` 建那一列
        ⇒ 用 `self.env` 呼叫會停在閘 2，`_skip_trial()` 一次都不會被呼叫，
        `assertRaises` 收到的是「OperationalError not raised」。

        ⚠ `setUp` 的 `self.authenticate('admin', 'admin')` 只影響 **HTTP session**，
        影響不到 `self.env` ⇒ 直接呼叫端點時幫不上忙。同檔走真 HTTP 的
        `test_skip_trial_technical_errors_are_generic_and_triggered` 之所以能走到
        `_skip_trial()`，靠的就是那個 session，不是 `self.env`。

        Refs #956 #567 #947
        """
        controller = PaasController()
        downstream = MagicMock()
        owner_env = self.env(user=self.workspace.owner_id)
        with patch(_REQUEST, new=MagicMock(env=owner_env)), patch.object(
            SubscriptionLifecycleMixin, '_skip_trial',
            side_effect=OperationalError('skip-retry-567'),
        ) as raised, patch.object(
            type(self.service), '_get_subscription_info', new=downstream,
        ):
            with self.assertRaises(OperationalError):
                controller.api_service_skip_trial(self.service.id)
        self.assertEqual(raised.call_count, 1)
        downstream.assert_not_called()


@tagged('post_install', '-at_install')
class TestPaaSOperationGateErrorTaxonomy567(TransactionCase):
    """四個 operation-gate catch site 的 taxonomy、短路與陽性對照。"""

    _GENERICS = {
        'update': 'An error occurred while updating the service.',
        'delete': 'An error occurred while deleting the service.',
        'rollback': 'An error occurred while rolling back the service.',
        'access': 'An error occurred while accessing the service.',
    }

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.owner = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Operation Gate WS 567',
            'owner_id': self.owner.id,
        })
        template = self.env[
            'woow_paas_platform.cloud_app_template'
        ].create({
            'name': 'Operation Gate Template 567',
            'slug': 'operation-gate-template-567',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/operation-gate-567',
            'helm_chart_version': '0.0.0',
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service']
        ).create({
            'name': 'Operation Gate Service 567',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
        })

    def _invoke(self, site, downstream=None):
        downstream_name = {
            'update': '_update_service',
            'delete': '_delete_service',
            'rollback': '_rollback_service',
            'access': '_update_service_access_config',
        }[site]
        success = {'success': True, 'data': {'site': site}}
        if downstream is None:
            downstream = MagicMock(return_value=success)
        with patch.object(
            PaasController, downstream_name, new=downstream,
        ), patch(
            _REQUEST, new=MagicMock(env=self.env(user=self.owner)),
        ):
            if site in ('update', 'delete'):
                result = self.controller.api_workspace_service(
                    self.workspace.id, self.service.id, action=site,
                )
            elif site == 'rollback':
                result = self.controller.api_service_rollback(
                    self.workspace.id, self.service.id, revision=1,
                )
            else:
                result = self.controller.api_service_access_config(
                    self.workspace.id, self.service.id,
                )
        return result, downstream

    def test_every_gate_masks_all_technical_errors_and_short_circuits(self):
        for site in self._GENERICS:
            for error_type in _TECHNICAL_ERRORS:
                sentinel = 'gate-sensitive-567-%s-%s' % (
                    site, error_type.__name__)
                with self.subTest(site=site, error_type=error_type.__name__), \
                        patch.object(
                            type(self.service), '_check_operation_allowed',
                            side_effect=error_type(sentinel),
                        ) as gate:
                    result, downstream = self._invoke(site)
                self.assertEqual(gate.call_count, 1)
                downstream.assert_not_called()
                self.assertEqual(
                    result,
                    {'success': False, 'error': self._GENERICS[site]},
                )
                self.assertNotIn(sentinel, result['error'])

    def test_every_gate_preserves_business_errors_and_short_circuits(self):
        for site in self._GENERICS:
            for error_type in (UserError, ValidationError):
                message = 'gate-business-567-%s-%s' % (
                    site, error_type.__name__)
                with self.subTest(site=site, error_type=error_type.__name__), \
                        patch.object(
                            type(self.service), '_check_operation_allowed',
                            side_effect=error_type(message),
                        ) as gate:
                    result, downstream = self._invoke(site)
                self.assertEqual(gate.call_count, 1)
                downstream.assert_not_called()
                self.assertEqual(result, {'success': False, 'error': message})

    def test_every_allowed_gate_executes_its_downstream(self):
        for site in self._GENERICS:
            with self.subTest(site=site), patch.object(
                type(self.service), '_check_operation_allowed',
                return_value=None,
            ) as gate:
                result, downstream = self._invoke(site)
            self.assertEqual(gate.call_count, 1)
            downstream.assert_called_once()
            self.assertTrue(result['success'], result)
            self.assertEqual(result['data']['site'], site)

    def test_every_gate_reraises_operational_error(self):
        for site in self._GENERICS:
            downstream = MagicMock()
            with self.subTest(site=site), patch.object(
                type(self.service), '_check_operation_allowed',
                side_effect=OperationalError('gate-retry-567-%s' % site),
            ) as gate:
                with self.assertRaises(OperationalError):
                    self._invoke(site, downstream=downstream)
            self.assertEqual(gate.call_count, 1)
            downstream.assert_not_called()

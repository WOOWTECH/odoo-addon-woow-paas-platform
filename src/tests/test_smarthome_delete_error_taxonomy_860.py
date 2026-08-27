# -*- coding: utf-8 -*-
"""#860：Smart Home delete 不得把 technical exception 原文回給租戶。"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import (
    AccessDenied, AccessError, MissingError, UserError, ValidationError,
)
from odoo.tests.common import TransactionCase, tagged
from psycopg2 import OperationalError

from odoo.addons.woow_paas_platform.controllers.smart_home import (
    SmartHomeController,
)
from .smart_home_fixture import sh_model

_GENERIC_DELETE_ERROR = 'Failed to delete smart home. Please try again.'
_REQUEST = 'odoo.addons.woow_paas_platform.controllers.smart_home.request'
_TECHNICAL_ERRORS = (AccessDenied, AccessError, MissingError, RuntimeError)


@tagged('post_install', '-at_install')
class TestSmartHomeDeleteErrorTaxonomy860(TransactionCase):
    """以真 registry record 呼叫公開 route handler，釘住 gate 與 delete side effect。"""

    def setUp(self):
        super().setUp()
        self.controller = SmartHomeController()
        self.owner = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Smart Home Delete Error WS 860',
            'owner_id': self.owner.id,
        })
        self.smart_home = sh_model(self.env).create({
            'name': 'Smart Home Delete Error 860',
            'workspace_id': self.workspace.id,
        })

    def _delete(self, gate_error=None, action_error=None):
        operation_gate = MagicMock(side_effect=gate_error)
        action_delete = MagicMock(side_effect=action_error)
        with patch(_REQUEST, new=MagicMock(env=self.env(user=self.owner))), \
                patch.object(
                    type(self.smart_home), '_check_operation_allowed',
                    operation_gate,
                ), patch.object(
                    type(self.smart_home), 'action_delete', action_delete,
                ):
            result = self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='delete',
            )
        return result, operation_gate, action_delete

    def test_technical_errors_are_operation_specific_and_opaque(self):
        for error_type in _TECHNICAL_ERRORS:
            sentinel = 'sensitive-delete-860-%s' % error_type.__name__
            with self.subTest(error_type=error_type.__name__):
                result, gate, action_delete = self._delete(
                    action_error=error_type(sentinel),
                )
                self.assertEqual(gate.call_count, 1)
                self.assertEqual(action_delete.call_count, 1)
                self.assertEqual(
                    result,
                    {'success': False, 'error': _GENERIC_DELETE_ERROR},
                )
                self.assertNotIn(sentinel, result['error'])

    def test_business_user_errors_keep_their_messages(self):
        for error_type in (UserError, ValidationError):
            message = 'delete-business-860-%s' % error_type.__name__
            with self.subTest(error_type=error_type.__name__):
                result, gate, action_delete = self._delete(
                    action_error=error_type(message),
                )
                self.assertEqual(gate.call_count, 1)
                self.assertEqual(action_delete.call_count, 1)
                self.assertEqual(result, {'success': False, 'error': message})

    def test_allowed_delete_keeps_success_envelope(self):
        result, gate, action_delete = self._delete()
        self.assertEqual(gate.call_count, 1)
        self.assertEqual(action_delete.call_count, 1)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['message'], 'Smart home deleted')

    def test_route_gate_technical_errors_are_generic_and_short_circuit(self):
        for error_type in _TECHNICAL_ERRORS:
            sentinel = 'sensitive-delete-gate-860-%s' % error_type.__name__
            with self.subTest(error_type=error_type.__name__):
                result, gate, action_delete = self._delete(
                    gate_error=error_type(sentinel),
                )
                self.assertEqual(gate.call_count, 1)
                action_delete.assert_not_called()
                self.assertEqual(
                    result,
                    {'success': False, 'error': _GENERIC_DELETE_ERROR},
                )
                self.assertNotIn(sentinel, result['error'])

    def test_route_gate_business_errors_keep_messages_and_short_circuit(self):
        for error_type in (UserError, ValidationError):
            message = 'delete-gate-business-860-%s' % error_type.__name__
            with self.subTest(error_type=error_type.__name__):
                result, gate, action_delete = self._delete(
                    gate_error=error_type(message),
                )
                self.assertEqual(gate.call_count, 1)
                action_delete.assert_not_called()
                self.assertEqual(result, {'success': False, 'error': message})

    def test_route_gate_operational_error_is_reraised_and_short_circuits(self):
        operation_gate = MagicMock(
            side_effect=OperationalError('delete-gate-retry-860'),
        )
        action_delete = MagicMock()
        with patch(_REQUEST, new=MagicMock(env=self.env(user=self.owner))), \
                patch.object(
                    type(self.smart_home), '_check_operation_allowed',
                    operation_gate,
                ), patch.object(
                    type(self.smart_home), 'action_delete', action_delete,
                ), self.assertRaises(OperationalError):
            self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='delete',
            )
        self.assertEqual(operation_gate.call_count, 1)
        action_delete.assert_not_called()

    def test_action_operational_error_is_reraised(self):
        operation_gate = MagicMock(return_value=None)
        action_delete = MagicMock(
            side_effect=OperationalError('delete-action-retry-860'),
        )
        with patch(_REQUEST, new=MagicMock(env=self.env(user=self.owner))), \
                patch.object(
                    type(self.smart_home), '_check_operation_allowed',
                    operation_gate,
                ), patch.object(
                    type(self.smart_home), 'action_delete', action_delete,
                ), self.assertRaises(OperationalError):
            self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='delete',
            )
        self.assertEqual(operation_gate.call_count, 1)
        self.assertEqual(action_delete.call_count, 1)

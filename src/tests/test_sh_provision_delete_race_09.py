# -*- coding: utf-8 -*-
"""RACE-09 SH entry: Smart Home provision vs delete. Fake operator only."""
import threading
import uuid

import odoo
from odoo import api
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .race09_harness import TwoTxCase
from .smart_home_fixture import sh_model
from .test_cs_sh_fail_closed_ac1011 import FakeOperator

_SH = 'woow_paas_platform.smart_home'
_WS = 'woow_paas_platform.workspace'


class GatedOperator(FakeOperator):
    def __init__(self):
        super().__init__()
        self.created = threading.Event()
        self.release = threading.Event()
        self.tunnels = {}

    def create_tunnel(self, **kwargs):
        tid = 'race09sh-' + uuid.uuid4().hex
        self.tunnels[tid] = kwargs['name']
        self.created.set()
        if not self.release.wait(timeout=15):
            raise AssertionError('test did not release fake create')
        return {'tunnel_id': tid, 'tunnel_name': kwargs['name'],
                'tunnel_token': 'fake-token'}

    def delete_tunnel(self, tunnel_id, tunnel_name=None, **kwargs):
        self.tunnels.pop(tunnel_id, None)
        return {}


@tagged('post_install', '-at_install', '-standard', 'race09_sh_provision_delete')
class TestSmartHomeProvisionDeleteRace09(TwoTxCase):
    PATCH_TARGETS = (
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client',
        'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client',
    )

    def setUp(self):
        super().setUp()
        self.open_registry()
        self.fake = None
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            ws = env[_WS].create({'name': 'race09sh-' + uuid.uuid4().hex,
                                  'owner_id': odoo.SUPERUSER_ID})
            sh = sh_model(env).create({
                'name': 'RACE-09 SH', 'workspace_id': ws.id, 'state': 'pending'})
            self.ws_id, self.sh_id = ws.id, sh.id
            cr.commit()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        fake = self.fake or GatedOperator()
        fake.release.set()
        from unittest.mock import patch
        ps = self.patches(fake)
        for p in ps:
            p.start()
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                sh = env[_SH].browse(self.sh_id).exists()
                if sh:
                    sh.action_delete()
                ws = env[_WS].browse(self.ws_id).exists()
                if ws:
                    ws.unlink()
                cr.commit()
        finally:
            for p in ps:
                p.stop()

    def test_RACE09_SH_delete_waits_or_fails_closed(self):
        fake = self.fake = GatedOperator()
        ps = self.patches(fake)
        for p in ps:
            p.start()
        try:
            state, early, first_res, second_res, t1, t2 = self.drive(
                _SH, self.sh_id, 'action_provision', 'action_delete',
                fake, fake.created)
            self.assertFalse(
                state == 'completed' and early[0] == 'ok',
                'RACE-09 RED: SH delete completed while fake create was '
                'still blocked before returning its identity')
            fake.release.set()
            t1.join(timeout=20)
            t2.join(timeout=20)
            self.assertFalse(t1.is_alive() or t2.is_alive(), 'worker hung')
            d = early if state == 'completed' else second_res.get(timeout=2)
            gone = not self.exists(_SH, self.sh_id)
            if d[0] == 'ok':
                self.assertTrue(gone)
                self.assertFalse(fake.tunnels,
                                 'RACE-09: SH delete committed with live tunnel')
            else:
                self.assertIsInstance(d[1], (UserError, Exception))
                if not gone:
                    with self.registry.cursor() as cr:
                        env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                        tid = env[_SH].browse(self.sh_id).tunnel_id
                    self.assertTrue(set(fake.tunnels) <= {tid},
                                    'unanchored remote tunnel survived')
        finally:
            fake.release.set()
            for p in ps:
                p.stop()

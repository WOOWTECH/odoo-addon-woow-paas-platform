# -*- coding: utf-8 -*-
"""RACE-10: lifecycle locks must be per-row; other rows stay fully concurrent.

Row A is held inside a fake create (lock held across the external call). While
it is held, a different SA and a different SH in OTHER workspaces must provision
and delete normally, with no waiting. Fake operator only.
"""
import queue
import threading
import time
import uuid
from unittest.mock import patch

import odoo
from odoo import api
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorClient
from .publication_receipt_fixture import routes_receipt
from .race09_harness import deny_real_http
from .smart_home_fixture import sh_model
from .test_cs_sh_fail_closed_ac1011 import FakeOperator

_SA, _SH, _WS = ('woow_paas_platform.security_access',
                 'woow_paas_platform.smart_home', 'woow_paas_platform.workspace')
_T = ('odoo.addons.woow_paas_platform.models.security_access.get_paas_operator_client',
      'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client',
      'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')


class Op(FakeOperator):
    """create_tunnel blocks only for the 'hold' name; others return at once."""

    def __init__(self):
        super().__init__()
        self.hold_started = threading.Event()
        self.release = threading.Event()
        self.live = {}
        self._first = threading.Lock()
        self._taken = False

    def create_tunnel(self, name, service_url=None, **kw):
        tid = 'race10-' + uuid.uuid4().hex
        self.live[tid] = name
        with self._first:
            hold = not self._taken
            self._taken = True
        if hold:
            self.hold_started.set()
            if not self.release.wait(timeout=20):
                raise AssertionError('not released')
        return {'tunnel_id': tid, 'tunnel_name': name, 'tunnel_token': 't'}

    def sync_tunnel_routes(self, tunnel_id, routes, **kw):
        return routes_receipt(tunnel_id, routes, **kw)

    def delete_tunnel(self, tunnel_id, tunnel_name=None, **kw):
        self.live.pop(tunnel_id, None)
        return {}


@tagged('post_install', '-at_install', '-standard', 'race10_independent_rows')
class TestRace10IndependentRows(BaseCase):

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.ids = {'ws': [], 'sa': [], 'sh': []}
        self.op = Op()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            for tag in ('hold', 'other'):
                ws = env[_WS].create({'name': 'race10-%s-%s' % (tag, uuid.uuid4().hex[:8]),
                                      'owner_id': odoo.SUPERUSER_ID})
                sa = env[_SA].create({'name': 'race10 %s sa' % tag, 'workspace_id': ws.id})
                sh = sh_model(env).create({'name': 'race10 %s sh' % tag,
                                           'workspace_id': ws.id, 'state': 'pending'})
                self.ids['ws'].append(ws.id)
                self.ids.setdefault(tag, {}).update(sa=sa.id, sh=sh.id)
                self.ids['sa'].append(sa.id)
                self.ids['sh'].append(sh.id)
            cr.commit()
        self.addCleanup(self._cleanup)

    def _patches(self):
        ps = [patch(t, return_value=self.op) for t in _T]
        ps.append(patch.object(PaaSOperatorClient, '_request', side_effect=deny_real_http))
        return ps

    def _cleanup(self):
        self.op.release.set()
        ps = self._patches()
        [p.start() for p in ps]
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                for model, key in ((_SA, 'sa'), (_SH, 'sh')):
                    for rid in self.ids[key]:
                        rec = env[model].browse(rid).exists()
                        if rec:
                            rec.sudo().write({'provision_outcome_unknown': False}) \
                                if 'provision_outcome_unknown' in rec._fields else None
                            rec.action_delete()
                for rid in self.ids['ws']:
                    ws = env[_WS].browse(rid).exists()
                    if ws:
                        ws.unlink()
                cr.commit()
        finally:
            [p.stop() for p in ps]

    def _tx(self, model, rid, method, out):
        t0 = time.monotonic()
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                getattr(env[model].browse(rid), method)()
                cr.commit()
            out.put(('ok', time.monotonic() - t0))
        except BaseException as exc:  # noqa: BLE001
            out.put(('error', exc, time.monotonic() - t0))

    def test_RACE10_other_rows_not_blocked_while_one_row_held(self):
        ps = self._patches()
        [p.start() for p in ps]
        hold_out = queue.Queue()
        try:
            if True:
                t = threading.Thread(target=self._tx, args=(
                    _SA, self.ids['hold']['sa'], 'action_provision', hold_out))
                t.start()
                self.assertTrue(self.op.hold_started.wait(timeout=15),
                                'held row did not reach the external create')
                results = {}
                for label, model, method, key in (
                        ('other_sa_provision', _SA, 'action_provision', ('other', 'sa')),
                        ('other_sh_provision', _SH, 'action_provision', ('other', 'sh')),
                        ('other_sa_delete', _SA, 'action_delete', ('other', 'sa')),
                        ('other_sh_delete', _SH, 'action_delete', ('other', 'sh')),
                        ('hold_sh_provision', _SH, 'action_provision', ('hold', 'sh'))):
                    q = queue.Queue()
                    th = threading.Thread(target=self._tx, args=(
                        model, self.ids[key[0]][key[1]], method, q))
                    th.start()
                    th.join(timeout=10)
                    self.assertFalse(th.is_alive(), '%s blocked behind unrelated held row' % label)
                    results[label] = q.get(timeout=2)
                self.assertTrue(t.is_alive(), 'held row should still be in flight')
                for label in ('other_sa_provision', 'other_sh_provision',
                              'other_sa_delete', 'other_sh_delete'):
                    r = results[label]
                    self.assertEqual(r[0], 'ok', '%s failed: %r' % (label, r[1]))
                    self.assertLess(r[-1], 8, '%s was slow (%.1fs): serialised?' % (label, r[-1]))
                # SH of the SAME workspace is a different table/row: also independent.
                self.assertEqual(results['hold_sh_provision'][0], 'ok',
                                 'same-workspace different-row SH blocked: %r'
                                 % (results['hold_sh_provision'][1:],))
        finally:
            self.op.release.set()
            try:
                t.join(timeout=30)
            except Exception:
                pass
            [p.stop() for p in ps]
        self.assertEqual(hold_out.get(timeout=5)[0], 'ok')

# -*- coding: utf-8 -*-
"""RACE-09 shared harness (SH / CS). Fake operator only; real PG transactions."""
import queue
import threading
import time
from unittest.mock import patch

import odoo
from odoo import api
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name

from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorClient


def deny_real_http(*args, **kwargs):
    raise AssertionError('RACE-09 fake-only guard blocked real operator HTTP')


class TwoTxCase(BaseCase):
    """Run provision-like and delete-like operations in separate committed txs."""

    PATCH_TARGETS = ()

    def open_registry(self):
        self.registry = Registry(get_db_name())

    def patches(self, fake):
        ps = [patch(t, return_value=fake) for t in self.PATCH_TARGETS]
        ps.append(patch.object(PaaSOperatorClient, '_request',
                               side_effect=deny_real_http))
        return ps

    def run_tx(self, model, rec_id, method, fake, ready=None, result=None):
        try:
            with self.registry.cursor() as cr:
                cr.execute('SELECT pg_backend_pid()')
                pid = cr.fetchone()[0]
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                rec = env[model].browse(rec_id)
                _ = rec.state  # take snapshot before release
                if ready is not None:
                    ready.put(pid)
                (method(rec) if callable(method) else getattr(rec, method)())
                cr.commit()
            if result is not None:
                result.put(('ok', None))
        except BaseException as exc:  # noqa: BLE001
            if result is not None:
                result.put(('error', exc))

    def wait_blocked_or_done(self, delete_pid, other_pid, delete_result, concurrent=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                return ('completed', delete_result.get_nowait())
            except queue.Empty:
                pass
            if concurrent is not None and concurrent.is_set():
                return ('concurrent', None)
            with self.registry.cursor() as cr:
                cr.execute('SELECT pg_blocking_pids(pid) FROM pg_stat_activity '
                           'WHERE pid = %s', (delete_pid,))
                row = cr.fetchone()
            if row and other_pid in row[0]:
                return ('blocked', None)
        self.fail('delete neither blocked on the lifecycle row nor failed closed')

    def exists(self, model, rec_id):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            return bool(env[model].browse(rec_id).exists())

    def drive(self, model, rec_id, first, second, fake, blocker_event, concurrent=None):
        """first runs and blocks inside the fake; second starts while it is held.

        Returns (second_state, second_result, first_result, second_result_final).
        """
        first_ready, second_ready = queue.Queue(), queue.Queue()
        first_res, second_res = queue.Queue(), queue.Queue()
        t1 = threading.Thread(target=self.run_tx, args=(
            model, rec_id, first, fake, first_ready, first_res))
        t2 = None
        t1.start()
        try:
            self.assertTrue(blocker_event.wait(timeout=15),
                            'fake operator side effect was not reached')
            pid1 = first_ready.get(timeout=5)
            t2 = threading.Thread(target=self.run_tx, args=(
                model, rec_id, second, fake, second_ready, second_res))
            t2.start()
            pid2 = second_ready.get(timeout=5)
            state, early = self.wait_blocked_or_done(pid2, pid1, second_res, concurrent)
            return state, early, first_res, second_res, t1, t2
        except BaseException:
            fake.release.set()
            t1.join(timeout=20)
            if t2:
                t2.join(timeout=20)
            raise

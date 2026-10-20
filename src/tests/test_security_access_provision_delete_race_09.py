# -*- coding: utf-8 -*-
"""RACE-09: real Odoo transactions around SA provision and delete.

The operator is an in-memory fake. The tests use committed fixtures and
independent registry cursors so PostgreSQL transaction ordering is exercised.
No sleeps, live Cloudflare, or HA calls.
"""
import queue
import threading
import time
import uuid
from unittest.mock import patch

import odoo
from odoo import api
from odoo.exceptions import UserError
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorClient,
    PaaSOperatorConnectionError,
)
from .publication_receipt_fixture import routes_receipt


_SA = 'woow_paas_platform.security_access'
_WORKSPACE = 'woow_paas_platform.workspace'
_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


def _deny_real_operator_http(*args, **kwargs):
    """Fail closed if any RACE-09 path bypasses the injected fake client."""
    raise AssertionError('RACE-09 fake-only guard blocked real operator HTTP')


class _FakeOperator:
    """Thread-safe remote inventory with a deterministic create-response gate."""

    def __init__(self, lose_response=False):
        self.lose_response = lose_response
        self.created = threading.Event()
        self.release_create_response = threading.Event()
        self._lock = threading.Lock()
        self.tunnels = {}
        self.deleted = []

    def create_tunnel(self, name, service_url):
        tunnel_id = 'race09-' + uuid.uuid4().hex
        result = {
            'tunnel_id': tunnel_id,
            'tunnel_name': name,
            'tunnel_token': 'fake-token-never-exported',
        }
        with self._lock:
            self.tunnels[tunnel_id] = dict(result)
        self.created.set()
        if not self.release_create_response.wait(timeout=15):
            raise AssertionError('test did not release fake create response')
        if self.lose_response:
            # The side effect has happened, but its identity never reaches Odoo.
            raise PaaSOperatorConnectionError(
                message='synthetic response lost after remote create')
        return result

    def sync_tunnel_routes(self, tunnel_id, routes, **kwargs):
        return routes_receipt(tunnel_id, routes, **kwargs)

    def delete_tunnel(self, tunnel_id, tunnel_name=None):
        with self._lock:
            self.deleted.append((tunnel_id, tunnel_name))
            self.tunnels.pop(tunnel_id, None)
        return {'status': 'deleted'}

    def live_ids(self):
        with self._lock:
            return set(self.tunnels)


@tagged('post_install', '-at_install', '-standard', 'race09_sa_provision_delete')
class TestSecurityAccessProvisionDeleteRace09(BaseCase):
    """One returned-ID interleave and one confirmed lost-response lifecycle."""

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'race09-' + uuid.uuid4().hex
        self.workspace_id = None
        self.sa_id = None
        self.addCleanup(self._cleanup_fixture)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            workspace = env[_WORKSPACE].create({
                'name': self.marker,
                'owner_id': odoo.SUPERUSER_ID,
            })
            sa = env[_SA].create({
                'name': 'RACE-09 fixture',
                'workspace_id': workspace.id,
            })
            self.workspace_id = workspace.id
            self.sa_id = sa.id
            cr.commit()

    def _run_in_transaction(self, operation, fake, started=None, result=None):
        try:
            with self.registry.cursor() as cr:
                cr.execute('SELECT pg_backend_pid()')
                pid = cr.fetchone()[0]
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                sa = env[_SA].browse(self.sa_id)
                # Force the transaction snapshot before the caller is released.
                _ = sa.state
                # Validate the main-thread patch from inside this DB worker and
                # before publishing readiness or invoking either product action.
                if sa._get_operator_client() is not fake:
                    raise AssertionError(
                        'RACE-09 transaction did not resolve the injected fake '
                        'operator client'
                    )
                if started is not None:
                    started.put(pid)
                if operation == 'provision':
                    sa.action_provision()
                elif operation == 'delete':
                    sa.action_delete()
                else:
                    raise AssertionError('unknown operation: %s' % operation)
                cr.commit()
                if result is not None:
                    result.put(('ok', None))
        except BaseException as exc:
            if result is not None:
                result.put(('error', exc))

    def _wait_until_delete_is_blocked_or_fails_closed(
            self, delete_pid, provision_pid, delete_result):
        """Observe either the PostgreSQL lock wait or an early fail-closed result."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                return ('completed', delete_result.get_nowait())
            except queue.Empty:
                pass
            with self.registry.cursor() as cr:
                cr.execute(
                    'SELECT pg_blocking_pids(pid) '
                    'FROM pg_stat_activity WHERE pid = %s',
                    (delete_pid,),
                )
                row = cr.fetchone()
            if row and provision_pid in row[0]:
                return ('blocked', None)
        self.fail(
            'delete neither entered a PostgreSQL lock wait nor returned a '
            'fail-closed result while the fake create response was blocked')

    def _sa_snapshot(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            sa = env[_SA].browse(self.sa_id).exists()
            if not sa:
                return None
            return {
                'state': sa.state,
                'tunnel_id': sa.tunnel_id,
                'error_message': sa.error_message or '',
                'provision_outcome_unknown': sa.provision_outcome_unknown,
            }

    def _cleanup_fixture(self):
        # The fake inventory is removed by the test-specific cleanup closure
        # installed by each test before the worker threads start.
        cleanup_fake = getattr(self, '_active_fake', None)
        if cleanup_fake is not None:
            for tunnel_id in list(cleanup_fake.live_ids()):
                cleanup_fake.delete_tunnel(tunnel_id)
        try:
            with patch(_GET_CLIENT, return_value=cleanup_fake):
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                    sa = env[_SA].browse(self.sa_id).exists()
                    if sa:
                        # The exact fake inventory was drained above; fixture
                        # cleanup may now clear the private reconciliation guard.
                        sa.sudo().write({'provision_outcome_unknown': False})
                        sa.action_delete()
                    workspace = env[_WORKSPACE].browse(
                        self.workspace_id).exists()
                    if workspace:
                        workspace.unlink()
                    cr.commit()
        except Exception:
            # Surface cleanup errors to the test runner instead of hiding an
            # externally meaningful fixture leak.
            raise

    def test_RACE09A_delete_waits_for_create_identity_or_fails_closed(self):
        fake = _FakeOperator()
        self._active_fake = fake
        provision_ready = queue.Queue()
        delete_ready = queue.Queue()
        provision_result = queue.Queue()
        delete_result = queue.Queue()
        provision_thread = threading.Thread(
            target=self._run_in_transaction,
            args=('provision', fake, provision_ready, provision_result),
            name='race09-provision',
        )
        delete_thread = None
        # Patch once in the parent thread for the complete worker lifetime.
        # Per-worker mock.patch contexts would race while restoring this module
        # global and could expose the real client to the other worker.
        with patch(_GET_CLIENT, return_value=fake), patch.object(
            PaaSOperatorClient, '_request', side_effect=_deny_real_operator_http
        ) as real_http:
            provision_thread.start()
            try:
                self.assertTrue(
                    fake.created.wait(timeout=15),
                    'fake operator create was not reached',
                )
                provision_pid = provision_ready.get(timeout=5)

                delete_thread = threading.Thread(
                    target=self._run_in_transaction,
                    args=('delete', fake, delete_ready, delete_result),
                    name='race09-delete',
                )
                delete_thread.start()
                delete_pid = delete_ready.get(timeout=5)
                delete_state, early_delete_result = (
                    self._wait_until_delete_is_blocked_or_fails_closed(
                        delete_pid, provision_pid, delete_result))
                self.assertFalse(
                    delete_state == 'completed' and early_delete_result[0] == 'ok',
                    'RACE-09 RED: delete completed while fake create was still '
                    'blocked before returning its identity',
                )

                # The fake remote object exists and delete is confirmed blocked on
                # the provision lock (or failed closed) before create returns its
                # exact identity.
                self.assertEqual(len(fake.live_ids()), 1)
                fake.release_create_response.set()
                provision_thread.join(timeout=20)
                delete_thread.join(timeout=20)
                self.assertFalse(provision_thread.is_alive(), 'provision thread hung')
                self.assertFalse(delete_thread.is_alive(), 'delete thread hung')

                p_result = provision_result.get(timeout=2)
                d_result = (early_delete_result if delete_state == 'completed'
                            else delete_result.get(timeout=2))
                if delete_state == 'completed':
                    self.assertIsInstance(d_result[1], UserError)
                snapshot = self._sa_snapshot()
                live_ids = fake.live_ids()
                if d_result[0] == 'ok':
                    self.assertIsNone(
                        snapshot,
                        'successful delete must remove the SA row',
                    )
                    self.assertFalse(
                        live_ids,
                        'RACE-09: delete committed while the fake operator still '
                        'owns a live tunnel',
                    )
                elif snapshot is not None and live_ids:
                    self.assertIn(
                        snapshot['tunnel_id'], live_ids,
                        'live tunnel must remain anchored by this SA',
                    )
                else:
                    self.assertFalse(
                        live_ids,
                        'unanchored remote tunnel survived the lifecycle race',
                    )
                self.assertEqual(p_result[0], 'ok')
                self.assertFalse(
                    real_http.called,
                    'RACE-09 attempted real operator HTTP despite fake injection',
                )
            finally:
                fake.release_create_response.set()
                provision_thread.join(timeout=20)
                if delete_thread is not None:
                    delete_thread.join(timeout=20)
                # Do not restore either patch while a worker could still be in
                # product code. The enclosing test job deadline bounds this
                # emergency drain if a database call hangs unexpectedly.
                if provision_thread.is_alive():
                    provision_thread.join()
                if delete_thread is not None and delete_thread.is_alive():
                    delete_thread.join()

    def test_RACE09B_lost_create_response_blocks_successful_delete(self):
        fake = _FakeOperator(lose_response=True)
        self._active_fake = fake
        provision_result = queue.Queue()
        provision_thread = threading.Thread(
            target=self._run_in_transaction,
            args=('provision', fake, None, provision_result),
            name='race09-lost-response',
        )
        delete_thread = None
        with patch(_GET_CLIENT, return_value=fake), patch.object(
            PaaSOperatorClient, '_request', side_effect=_deny_real_operator_http
        ) as real_http:
            provision_thread.start()
            try:
                self.assertTrue(
                    fake.created.wait(timeout=15),
                    'fake operator create was not reached',
                )
                tunnel_id = next(iter(fake.live_ids()))
                fake.release_create_response.set()
                provision_thread.join(timeout=20)
                self.assertFalse(provision_thread.is_alive(), 'provision thread hung')
                self.assertEqual(provision_result.get(timeout=2)[0], 'ok')

                # Provision committed its explicit unknown-outcome state. A second,
                # independent transaction must refuse to unlink this ownership anchor
                # until exact external identity has been reconciled.
                snapshot = self._sa_snapshot()
                self.assertIsNotNone(snapshot)
                self.assertIn('unreferenced-tunnel', snapshot['error_message'])
                self.assertTrue(snapshot['provision_outcome_unknown'])

                # The unknown result blocks both duplicate provision and direct
                # unlink before the public delete path is attempted.
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                    sa = env[_SA].browse(self.sa_id)
                    with self.assertRaises(UserError):
                        sa.action_provision()
                    with self.assertRaises(UserError):
                        sa.unlink()

                delete_result = queue.Queue()
                delete_thread = threading.Thread(
                    target=self._run_in_transaction,
                    args=('delete', fake, None, delete_result),
                    name='race09-delete-after-lost-response',
                )
                delete_thread.start()
                delete_thread.join(timeout=20)
                self.assertFalse(delete_thread.is_alive(), 'delete thread hung')
                result = delete_result.get(timeout=2)
                after = self._sa_snapshot()
                if result[0] == 'ok':
                    self.assertIsNone(after, 'successful delete must remove the SA')
                    self.assertNotIn(
                        tunnel_id, fake.live_ids(),
                        'RACE-09 RED: delete succeeded but lost-response tunnel '
                        'remains live',
                    )
                else:
                    self.assertIsNotNone(
                        after,
                        'failed delete must retain the SA ownership anchor',
                    )
                    self.assertIsInstance(result[1], UserError)
                    self.assertIn('unreferenced-tunnel', after['error_message'])
                    self.assertTrue(after['provision_outcome_unknown'])
                    self.assertIn(
                        tunnel_id, fake.live_ids(),
                        'blocked delete should leave exact unknown remote object '
                        'for reconciliation',
                    )
                self.assertFalse(
                    real_http.called,
                    'RACE-09 attempted real operator HTTP despite fake injection',
                )
            finally:
                fake.release_create_response.set()
                provision_thread.join(timeout=20)
                if delete_thread is not None:
                    delete_thread.join(timeout=20)
                if provision_thread.is_alive():
                    provision_thread.join()
                if delete_thread is not None and delete_thread.is_alive():
                    delete_thread.join()

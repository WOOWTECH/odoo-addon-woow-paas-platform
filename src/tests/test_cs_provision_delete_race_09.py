# -*- coding: utf-8 -*-
"""RACE-09 CS entry: CloudService deploy (helm install) vs unlink. Fake only."""
import logging
import threading
import uuid

import odoo
from odoo import api
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from ..models.cloud_service import _internal_provision
from ..services.naming import make_namespace, make_service_subdomain
from ..services.paas_operator import PaaSOperatorError
from .race09_harness import TwoTxCase
from .test_cs_sh_fail_closed_ac1011 import FakeOperator

_logger = logging.getLogger(__name__)
_CS = 'woow_paas_platform.cloud_service'
_WS = 'woow_paas_platform.workspace'
_TPL = 'woow_paas_platform.cloud_app_template'


class HelmGatedOperator(FakeOperator):
    def __init__(self):
        super().__init__()
        self.installing = threading.Event()
        self.uninstalling = threading.Event()
        self.release = threading.Event()
        self.releases = set()
        self.hold_uninstall = False
        self.install_calls = 0

    def install_release(self, *args, **kwargs):
        name = kwargs['release_name']
        self.install_calls += 1
        self.installing.set()
        if not self.release.wait(timeout=15):
            raise AssertionError('test did not release fake install')
        self.releases.add(name)
        return {'revision': 1, 'chart_version': '1.0.0'}

    def uninstall_release(self, namespace, release_name, **kwargs):
        if self.hold_uninstall:
            self.uninstalling.set()
            if not self.release.wait(timeout=15):
                raise AssertionError('test did not release fake uninstall')
        if release_name not in self.releases:
            # Real operator answers 404 for a release that is not there yet.
            raise PaaSOperatorError('release not found', status_code=404)
        self.releases.discard(release_name)
        return {}


@tagged('post_install', '-at_install', '-standard', 'race09_cs_provision_delete')
class TestCloudServiceDeployDeleteRace09(TwoTxCase):
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
            ws = env[_WS].create({'name': 'race09cs-' + uuid.uuid4().hex,
                                  'owner_id': odoo.SUPERUSER_ID})
            tpl = env[_TPL].create({
                'name': 'RACE09 chart', 'slug': 'race09-' + uuid.uuid4().hex[:8],
                'helm_repo_url': '', 'helm_chart_name': 'oci://example.invalid/race09',
                'helm_chart_version': '1.0.0', 'workload_type': 'container',
                'post_deploy_init_type': 'none', 'mcp_enabled': False,
                'ingress_enabled': True})
            ref = str(uuid.uuid4())
            self.release_name = 'svc-' + uuid.uuid4().hex[:8]
            cs = _internal_provision(env[_CS]).create({
                'name': 'RACE09 CS', 'workspace_id': ws.id, 'template_id': tpl.id,
                'reference_id': ref,
                'subdomain': make_service_subdomain(ws.ws_hash, ref, 'RACE09 CS'),
                'helm_namespace': make_namespace(ws.ws_hash),
                'helm_release_name': self.release_name,
                'state': 'pending', 'is_protected': False})
            self.ws_id, self.tpl_id, self.cs_id = ws.id, tpl.id, cs.id
            cr.commit()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        fake = self.fake or HelmGatedOperator()
        fake.release.set()
        fake.releases.clear()
        ps = self.patches(fake)
        for p in ps:
            p.start()
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                cs = env[_CS].browse(self.cs_id).exists()
                if cs:
                    cs.unlink()
                for model, rid in ((_WS, self.ws_id), (_TPL, self.tpl_id)):
                    rec = env[model].browse(rid).exists()
                    if rec:
                        rec.unlink()
                cr.commit()
        finally:
            for p in ps:
                p.stop()

    def test_RACE09_CS_unlink_waits_or_no_orphan_release(self):
        fake = self.fake = HelmGatedOperator()
        ps = self.patches(fake)
        for p in ps:
            p.start()
        try:
            state, early, first_res, second_res, t1, t2 = self.drive(
                _CS, self.cs_id, '_execute_deploy', 'unlink',
                fake, fake.installing)
            _logger.info('RACE09DIAG CS second_state=%s early=%s', state, early[0] if early else None)
            self.assertFalse(
                state == 'completed' and early[0] == 'ok',
                'RACE-09 RED: CS unlink completed while helm install was '
                'still in flight')
            fake.release.set()
            t1.join(timeout=20)
            t2.join(timeout=20)
            self.assertFalse(t1.is_alive() or t2.is_alive(), 'worker hung')
            d = early if state == 'completed' else second_res.get(timeout=2)
            gone = not self.exists(_CS, self.cs_id)
            _logger.info('RACE09DIAG CS final gone=%s releases=%s first=%s second=%s', gone, sorted(fake.releases), first_res.queue and first_res.queue[0][0], d[0])
            if gone:
                self.assertFalse(
                    fake.releases,
                    'RACE-09: CS row deleted but helm release %s lives on'
                    % sorted(fake.releases))
            else:
                self.assertEqual(d[0], 'error')
        finally:
            fake.release.set()
            for p in ps:
                p.stop()

    def test_RACE09_CS_deploy_after_controller_delete_no_orphan_release(self):
        """Reverse order: tenant delete (controller path) in flight, deploy cron arrives."""
        fake = self.fake = HelmGatedOperator()
        fake.hold_uninstall = True
        ps = self.patches(fake)
        for p in ps:
            p.start()

        def controller_delete(rec):
            # Mirrors controllers/paas.py::_delete_service (lock step is the
            # RACE-09 fix; absent on the pinned production code).
            if hasattr(rec, '_try_lock_lifecycle_rows') and not rec._try_lock_lifecycle_rows():
                raise UserError('lifecycle busy')
            rec.write({'state': 'deleting'})
            try:
                rec._uninstall_release(fake)
            except PaaSOperatorError as exc:
                if exc.status_code != 404:  # controller: 404 => still delete row
                    raise
            rec.unlink()

        t1 = t2 = None
        try:
            state, early, first_res, second_res, t1, t2 = self.drive(
                _CS, self.cs_id, controller_delete, '_execute_deploy',
                fake, fake.uninstalling, concurrent=fake.installing)
            _logger.info('RACE09DIAG CS-B second_state=%s early=%s',
                         state, early[0] if early else None)
            self.assertNotEqual(
                state, 'concurrent',
                'RACE-09 RED: deploy entered helm install while delete (uninstall) '
                'was still in flight on the same row')
            self.assertFalse(
                state == 'completed' and early[0] == 'ok' and fake.install_calls,
                'RACE-09 RED: deploy installed a release while delete was in flight')
            fake.release.set()
            t1.join(timeout=20)
            t2.join(timeout=20)
            self.assertFalse(t1.is_alive() or t2.is_alive(), 'worker hung')
            gone = not self.exists(_CS, self.cs_id)
            _logger.info('RACE09DIAG CS-B final gone=%s releases=%s installs=%s',
                         gone, sorted(fake.releases), fake.install_calls)
            if gone:
                self.assertFalse(
                    fake.releases,
                    'RACE-09: CS row deleted but helm release %s lives on'
                    % sorted(fake.releases))
        finally:
            fake.release.set()
            for t in (t1, t2):
                if t is not None:
                    t.join(timeout=30)
            for p in ps:
                p.stop()

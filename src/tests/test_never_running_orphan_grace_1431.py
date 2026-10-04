# -*- coding: utf-8 -*-
"""#1431：從未正常運作過的服務刪除時，孤兒 PVC 只保留 1 天；``first_running_at`` 是判準。

刪除服務後 PVC 依 #317 保留（正式站 90 天）、照樣佔 Longhorn 排程額度（#1415）。從沒進過
running 的服務沒有租戶資料要保護 ⇒ 平台刪除時請 operator 標 ``grace-period-days=1``。

守的事：

* ``first_running_at``：第一次進 running 時由 ``write()``／``create()`` 統一寫入，之後不變；
  error→running（#1416 自癒）也算；租戶寫不進去（偽裝「沒用過」＝縮短資料保留、逃避計費）；
* 三條刪除路徑（租戶刪除 ``_delete_service``、lifecycle ``_do_cleanup``、``unlink`` 兜底
  ``_cleanup_helm``）：從未 running ⇒ ``grace_period_days=1``；曾經 running ⇒ 呼叫形狀與
  #1431 之前完全相同（不帶參數）；
* client：參數走 query string（``subdomain``、``grace_period_days``）；
* migration（18.0.1.0.209）回填一律保守：只有明確還在開通中、沒有跑過跡象的留空。
"""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import NEVER_RUNNING_ORPHAN_GRACE_DAYS, _internal_provision
from ..services.paas_operator import PaaSOperatorClient

_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)
_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Grace 1431', 'slug': 'grace-1431', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/grace-1431',
            'helm_chart_version': '0.1.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'WS 1431'})
        self._seq = 0

    def _service(self, state='pending', **extra):
        self._seq += 1
        vals = {
            'name': 'svc-1431-%s' % self._seq, 'workspace_id': self.ws.id,
            'template_id': self.tpl.id, 'state': state,
            'helm_namespace': 'paas-ws-1431', 'helm_release_name': 'svc-1431-%s' % self._seq,
            'subdomain': 'paas-cs-1431-%s' % self._seq,
            'helm_values': json.dumps({}),
        }
        vals.update(extra)
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create(vals)


@tagged('post_install', '-at_install')
class TestFirstRunningAt1431(_Base):

    def test_stamped_on_first_entry_to_running_and_never_moved(self):
        svc = self._service('pending')
        self.assertFalse(svc.first_running_at)
        svc.write({'state': 'deploying'})
        svc.write({'state': 'initializing'})
        self.assertFalse(svc.first_running_at, '開通中不算 running')
        svc.write({'state': 'running'})
        first = svc.first_running_at
        self.assertTrue(first)
        svc.write({'state': 'upgrading'})
        svc.write({'state': 'error', 'error_message': 'boom'})
        svc.write({'state': 'suspended'})
        self.assertEqual(svc.first_running_at, first, '離開 running 不清')
        # 同一筆再次進 running 不覆寫第一次的時間。
        svc.invalidate_recordset()
        svc.write({'state': 'running'})
        self.assertEqual(svc.first_running_at, first)

    def test_timeout_error_recovering_to_running_counts(self):
        """#1416 的自癒（error → running）走同一個咽喉。"""
        svc = self._service('deploying')
        svc.write({'state': 'error', 'error_message': 'timed out'})
        self.assertFalse(svc.first_running_at)
        svc.write({'state': 'running', 'error_message': False})
        self.assertTrue(svc.first_running_at)

    def test_created_directly_as_running_is_stamped(self):
        self.assertTrue(self._service('running').first_running_at)
        self.assertFalse(self._service('error').first_running_at)

    def test_mixed_recordset_only_stamps_the_newcomers(self):
        old = self._service('running')
        stamp = datetime(2026, 1, 1, 0, 0, 0)
        old.write({'first_running_at': stamp})
        new = self._service('deploying')
        (old | new).write({'state': 'running'})
        self.assertEqual(old.first_running_at, stamp)
        self.assertTrue(new.first_running_at)
        self.assertNotEqual(new.first_running_at, stamp)

    def test_tenant_cannot_write_it(self):
        svc = self._service('error')
        tenant = self.env['res.users'].create({
            'name': 'Tenant 1431', 'login': 'tenant1431@tenant1431.tw',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        for value in (False, '2026-01-01 00:00:00'):
            try:
                svc.with_user(tenant).write({'first_running_at': value})
            except AccessError as e:
                self.assertIn('Service history fields are managed by the platform', str(e))
            else:
                self.fail('租戶不得寫 first_running_at（%r）' % (value,))


@tagged('post_install', '-at_install')
class TestOrphanGraceOnDelete1431(_Base):

    def _never_running(self):
        return self._service('error', error_message='Deployment failed')

    def _ever_running(self):
        svc = self._service('running')
        svc.write({'state': 'error', 'error_message': 'crashed later'})
        return svc

    def test_constant(self):
        self.assertEqual(NEVER_RUNNING_ORPHAN_GRACE_DAYS, 1)

    def test_lifecycle_cleanup_of_a_never_running_service_asks_for_one_day(self):
        svc = self._never_running()
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            svc._do_cleanup()
        client.uninstall_release.assert_called_once_with(
            svc.helm_namespace, svc.helm_release_name,
            subdomain=svc.subdomain, grace_period_days=1)

    def test_lifecycle_cleanup_of_a_service_that_ran_keeps_the_old_call(self):
        svc = self._ever_running()
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            svc._do_cleanup()
        client.uninstall_release.assert_called_once_with(
            svc.helm_namespace, svc.helm_release_name, subdomain=svc.subdomain)

    def test_unlink_fallback_follows_the_same_rule(self):
        never, ran = self._never_running(), self._ever_running()
        calls = {}
        client = MagicMock()
        client.uninstall_release.side_effect = (
            lambda ns, rel, **kw: calls.__setitem__(rel, kw))
        expected = {
            never.helm_release_name: {'subdomain': never.subdomain, 'grace_period_days': 1},
            ran.helm_release_name: {'subdomain': ran.subdomain},
        }
        with patch(_CLIENT_PATH, return_value=client):
            (never | ran).unlink()
        self.assertEqual(calls, expected)

    def test_tenant_delete_endpoint_passes_one_day_for_a_never_running_service(self):
        from ..controllers.paas import PaasController
        svc = self._never_running()
        svc_id, ns, rel, sub = svc.id, svc.helm_namespace, svc.helm_release_name, svc.subdomain
        client = MagicMock()
        with patch('%s.request' % _CONTROLLER, new=MagicMock(env=self.env)), \
                patch('%s.get_paas_operator_client' % _CONTROLLER, return_value=client), \
                patch(_CLIENT_PATH, return_value=client):
            result = PaasController()._delete_service(svc)
        self.assertTrue(result['success'], result)
        self.assertFalse(self.env['woow_paas_platform.cloud_service'].browse(svc_id).exists())
        # 第一次是 controller 自己的 uninstall，第二次是 unlink 兜底（真環境回 404＝已不在）。
        first = client.uninstall_release.call_args_list[0]
        self.assertEqual(first.args, (ns, rel))
        self.assertEqual(first.kwargs, {'subdomain': sub, 'grace_period_days': 1})

    def test_tenant_delete_endpoint_keeps_the_default_for_a_service_that_ran(self):
        from ..controllers.paas import PaasController
        svc = self._ever_running()
        client = MagicMock()
        with patch('%s.request' % _CONTROLLER, new=MagicMock(env=self.env)), \
                patch('%s.get_paas_operator_client' % _CONTROLLER, return_value=client), \
                patch(_CLIENT_PATH, return_value=client):
            result = PaasController()._delete_service(svc)
        self.assertTrue(result['success'], result)
        self.assertNotIn('grace_period_days', client.uninstall_release.call_args_list[0].kwargs)


@tagged('post_install', '-at_install')
class TestUninstallReleaseQuery1431(TransactionCase):
    """client：兩個參數都走 query string（舊 operator 對未宣告的 query 參數直接忽略）。"""

    def _call(self, **kwargs):
        client = PaaSOperatorClient('http://operator.test', 'k')
        response = MagicMock(status_code=200, content=b'{"message": "ok"}')
        response.json.return_value = {'message': 'ok'}
        with patch('requests.Session.request', return_value=response) as req:
            client.uninstall_release('paas-ws-a1b2c3d4', 'svc-x', **kwargs)
        return req.call_args.kwargs

    def test_grace_and_subdomain(self):
        kw = self._call(subdomain='paas-cs-x', grace_period_days=1)
        self.assertEqual(kw['method'], 'DELETE')
        self.assertTrue(kw['url'].endswith('/api/releases/paas-ws-a1b2c3d4/svc-x'), kw['url'])
        self.assertEqual(kw['params'], {'subdomain': 'paas-cs-x', 'grace_period_days': 1})

    def test_without_grace_is_the_pre_1431_request(self):
        self.assertEqual(self._call(subdomain='paas-cs-x')['params'], {'subdomain': 'paas-cs-x'})
        self.assertIsNone(self._call()['params'])


@tagged('post_install', '-at_install')
class TestFirstRunningBackfill1431(_Base):

    def _blank(self, *services):
        """模擬 18.0.1.0.208 的資料：欄位剛建、全部是空的。"""
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE woow_paas_platform_cloud_service SET first_running_at = NULL '
            'WHERE id IN %s', (tuple(s.id for s in services),))
        self.env.invalidate_all()

    def test_conservative_backfill(self):
        deployed = datetime(2026, 9, 1, 8, 0, 0)
        keep_empty = {
            'pending': self._service('pending'),
            'deploying': self._service('deploying', deployed_at=deployed),
            'initializing': self._service('initializing', deployed_at=deployed),
        }
        backfill = {
            'running': self._service('running', deployed_at=deployed),
            'error (never ran, unknown before 209)': self._service('error'),
            'upgrading': self._service('upgrading', deployed_at=deployed),
            'suspended': self._service('suspended', pre_suspend_state='running'),
            'deleting': self._service('deleting'),
            'initializing after an upgrade': self._service(
                'initializing', last_upgraded_at=deployed),
            'deploying with revision 2': self._service('deploying', helm_revision=2),
            'initializing, second init generation': self._service('initializing'),
            'pending with a pre-suspend state': self._service(
                'pending', pre_suspend_state='running'),
        }
        backfill['initializing, second init generation'].sudo().write({'init_generation': 2})
        self._blank(*keep_empty.values(), *backfill.values())

        done = self.env['woow_paas_platform.cloud_service']._woow_migrate_first_running_at_1431()

        for label, svc in keep_empty.items():
            self.assertFalse(svc.first_running_at, '%s 應留空' % label)
        for label, svc in backfill.items():
            self.assertTrue(svc.first_running_at, '%s 應回填（保守）' % label)
            self.assertIn(svc, done)
        self.assertEqual(backfill['running'].first_running_at, deployed, '有 deployed_at 用它')
        self.assertEqual(backfill['deleting'].first_running_at,
                         backfill['deleting'].create_date, '沒有就用 create_date')

    def test_idempotent_and_never_overwrites(self):
        stamp = datetime.now().replace(microsecond=0) - timedelta(days=3)
        svc = self._service('running')
        svc.write({'first_running_at': stamp})
        self.env['woow_paas_platform.cloud_service']._woow_migrate_first_running_at_1431()
        self.env['woow_paas_platform.cloud_service']._woow_migrate_first_running_at_1431()
        self.assertEqual(svc.first_running_at, stamp)

    def test_migration_script_calls_the_model_method(self):
        import importlib.util
        import pathlib
        path = (pathlib.Path(__file__).resolve().parent.parent
                / 'migrations' / '18.0.1.0.209' / 'post-migrate.py')
        spec = importlib.util.spec_from_file_location('woow_migrate_209', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        svc = self._service('error')
        self._blank(svc)
        module.migrate(self.env.cr, '18.0.1.0.208')
        svc.invalidate_recordset()
        self.assertTrue(svc.first_running_at)

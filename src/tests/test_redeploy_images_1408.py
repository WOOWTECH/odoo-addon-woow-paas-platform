# -*- coding: utf-8 -*-
"""#1408：Redeploy 要把範本改釘的 image 帶到存量服務，並在就緒後核對。

開通時 ``helm_default_values`` 被 deep-merge 進服務自己的 ``helm_values``（含
``image.repository／tag``），Redeploy 只讀服務的 ``helm_values`` ⇒ chart 升到範本
版本，image 卻停在開通當時。正式站實例：``svc-20c1c915``（pi-agent）Redeploy
兩次、helm upgrade 都成功，Deployment 仍是 ``pi-agent:20261002.2302``，範本已釘
``20261003.1400``——後台顯示成功、實際沒換。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import (
    _image_mismatches,
    _internal_provision,
    _refresh_image_refs,
    _split_image,
)
from .operator_contract import pod_info, status_response

_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)
_REPO = 'jcr-prod.woowtech.io/woow-paas-docker-local/pi-agent'
_OLD = '20261002.2302@sha256:0f33784e'
_NEW = '20261003.1400@sha256:a00d92d8'


@tagged('post_install', '-at_install')
class TestImageRefHelpers(TransactionCase):
    """純函式：覆蓋規則與核對規則。"""

    def test_template_image_overrides_frozen_service_value(self):
        service = {'image': {'repository': _REPO, 'tag': _OLD}, 'piWeb': {'x': 1}}
        template = {'image': {'repository': _REPO, 'tag': _NEW}}
        new, changes = _refresh_image_refs(service, template)
        self.assertEqual(new['image']['tag'], _NEW)
        self.assertEqual(new['piWeb'], {'x': 1}, '非 image 的服務值不得被動到')
        self.assertEqual(changes, [('image.tag', _OLD, _NEW)])
        self.assertEqual(service['image']['tag'], _OLD, '不得就地改呼叫端的 dict')

    def test_nested_refs_and_unchanged_refs(self):
        template = {'image': {'repository': 'a', 'tag': '2'},
                    'console': {'image': {'repository': 'c', 'tag': '9'}}}
        service = {'image': {'repository': 'a', 'tag': '1'},
                   'console': {'image': {'repository': 'c', 'tag': '9'}}}
        new, changes = _refresh_image_refs(service, template)
        self.assertEqual(changes, [('image.tag', '1', '2')])
        self.assertEqual(new['console']['image']['tag'], '9')

    def test_tenant_owned_keys_are_kept(self):
        """租戶可設定的鍵（例：n8n 版本）屬於租戶，Redeploy 不覆蓋。"""
        service = {'image': {'repository': 'n8nio/n8n', 'tag': '2.32.7'}}
        template = {'image': {'repository': 'n8nio/n8n', 'tag': '1.122.4'}}
        new, changes = _refresh_image_refs(service, template, {'image.tag'})
        self.assertEqual(new['image']['tag'], '2.32.7')
        self.assertEqual(changes, [])
        new, changes = _refresh_image_refs(service, template, {'image'})
        self.assertEqual(changes, [], '祖先鍵屬於租戶時整個 ref 都不動')

    def test_stale_digest_removed_when_template_moved_to_tag(self):
        service = {'image': {'repository': 'a', 'tag': '1', 'digest': 'sha256:old'}}
        template = {'image': {'repository': 'a', 'tag': '2@sha256:new'}}
        new, changes = _refresh_image_refs(service, template)
        self.assertNotIn('digest', new['image'])
        self.assertIn(('image.digest', 'sha256:old', None), changes)

    def test_template_without_images_changes_nothing(self):
        service = {'image': {'repository': 'n8nio/n8n', 'tag': '2.35.3'}}
        new, changes = _refresh_image_refs(service, {'main': {'persistence': {}}})
        self.assertEqual((new, changes), (service, []))

    def test_split_image(self):
        self.assertEqual(
            _split_image('jcr-prod.woowtech.io/x/pi-agent:1.0@sha256:abc'),
            ('x/pi-agent', '1.0', 'sha256:abc'))
        self.assertEqual(_split_image('nginxinc/nginx-unprivileged:1.27-alpine'),
                         ('nginxinc/nginx-unprivileged', '1.27-alpine', None))
        self.assertEqual(_split_image('docker.io/library/redis:7'), ('redis', '7', None))
        self.assertEqual(_split_image('localhost:5000/a/b'), ('a/b', None, None))

    def test_mismatch_detects_stale_tag_like_prod(self):
        values = {'image': {'repository': _REPO, 'tag': _NEW}}
        running = ['%s:%s' % (_REPO, _OLD), 'nginxinc/nginx-unprivileged:1.27-alpine']
        mismatches = _image_mismatches(values, running)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('20261003.1400', mismatches[0][0])
        self.assertIn('20261002.2302', mismatches[0][1][0])

    def test_match_by_digest_or_tag(self):
        values = {'image': {'repository': _REPO, 'tag': _NEW}}
        self.assertEqual(_image_mismatches(values, ['%s:%s' % (_REPO, _NEW)]), [])
        values = {'image': {'repository': 'n8nio/n8n', 'tag': '2.35.3'}}
        self.assertEqual(_image_mismatches(values, ['n8nio/n8n:2.35.3']), [])

    def test_component_not_running_is_skipped(self):
        """console 關掉、子 chart 沒開——repository 根本沒在跑就不算不符。"""
        values = {'console': {'image': {'repository': 'x/console', 'tag': '1'}}}
        self.assertEqual(_image_mismatches(values, ['x/main:1']), [])


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1408 Workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': '1408 pi-agent',
            'slug': 'pi-agent-1408',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/pi-agent',
            'helm_chart_version': '0.1.4',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
            'helm_default_values': json.dumps({
                'image': {'repository': _REPO, 'tag': _NEW},
                'piWeb': {'resources': {'limits': {'cpu': '2'}}},
            }),
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create({
                'name': 'svc-1408',
                'workspace_id': self.workspace.id,
                'template_id': self.template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-test',
                'helm_release_name': 'svc-1408',
                # 開通當時凍結的值：舊 image ＋ 租戶／方案疊上去的別的值。
                'helm_values': json.dumps({
                    'image': {'repository': _REPO, 'tag': _OLD},
                    'piWeb': {'resources': {'limits': {'cpu': '4'}}},
                }),
            })

    def _redeploy(self, upgrade_result=None, side_effect=None):
        client = MagicMock()
        client.get_release.return_value = {'name': 'svc-1408'}
        client.upgrade_release.return_value = upgrade_result or {
            'revision': 5, 'chart_version': '0.1.4'}
        if side_effect:
            client.upgrade_release.side_effect = side_effect
        with patch(_CLIENT_PATH, return_value=client):
            self.service.action_redeploy()
        return client

    def _values(self):
        self.service.invalidate_recordset()
        return json.loads(self.service.sudo().helm_values)


class TestRedeployRefreshesImages(_Base):

    def test_redeploy_sends_and_stores_template_image(self):
        client = self._redeploy()
        sent = client.upgrade_release.call_args.kwargs['values']
        self.assertEqual(sent['image']['tag'], _NEW, 'upgrade 必須帶範本現在釘的 image')
        self.assertEqual(self._values()['image']['tag'], _NEW,
                         '必須寫回服務的 helm_values，之後儲存設定才不會又退回舊 image')
        self.assertEqual(self._values()['piWeb']['resources']['limits']['cpu'], '4',
                         '服務自己的非 image 值（方案／租戶）保留')

    def test_failed_upgrade_does_not_keep_refreshed_values(self):
        """UserError 回捲整筆交易——用 savepoint 模擬 RPC 的回捲。"""
        from odoo.exceptions import UserError

        from ..services.paas_operator import PaaSOperatorError

        with self.assertRaises(UserError):
            with self.env.cr.savepoint():
                self._redeploy(side_effect=PaaSOperatorError(
                    'API error', status_code=409, detail='升級前檢查發現…'))
        self.assertEqual(self._values()['image']['tag'], _OLD)

    def test_operator_detail_reaches_the_admin(self):
        from odoo.exceptions import UserError

        from ..services.paas_operator import PaaSOperatorError

        with self.assertRaises(UserError) as ctx:
            with self.env.cr.savepoint():
                self._redeploy(side_effect=PaaSOperatorError(
                    'API error', status_code=409,
                    detail='磁碟（PVC）「svc-x-config」的 storageClassName'))
        self.assertIn('svc-x-config', str(ctx.exception))

    def test_reconfigure_path_does_not_refresh_images(self):
        """只有 Redeploy 換 image——儲存設定／重設密碼走 _assemble_deploy_values，不換。"""
        values = self.service._assemble_deploy_values()['values']
        self.assertEqual(values['image']['tag'], _OLD)


class TestRedeployVerification(_Base):

    def _poll(self, images):
        client = MagicMock()
        client.get_status.return_value = status_response(
            'deployed', [pod_info(name='svc-1408-abc', images=images)])
        self.service._poll_and_update_status(client)
        self.service.invalidate_recordset()

    def test_redeploy_marks_pending_and_chart_match_leaves_no_message(self):
        self._redeploy()
        self.assertTrue(self.service.redeploy_verify_pending)
        self.assertFalse(self.service.redeploy_check_message)

    def test_chart_version_mismatch_is_reported_immediately(self):
        self._redeploy({'revision': 5, 'chart_version': '0.1.3'})
        self.assertIn('0.1.3', self.service.redeploy_check_message)
        self.assertIn('0.1.4', self.service.redeploy_check_message)

    def test_poll_reports_stale_image_after_ready(self):
        self._redeploy()
        self._poll(['%s:%s' % (_REPO, _OLD)])
        self.assertFalse(self.service.redeploy_verify_pending)
        self.assertIn('20261003.1400', self.service.redeploy_check_message)
        self.assertIn('20261002.2302', self.service.redeploy_check_message)

    def test_poll_clears_pending_when_image_matches(self):
        self._redeploy()
        self._poll(['%s:%s' % (_REPO, _NEW)])
        self.assertFalse(self.service.redeploy_verify_pending)
        self.assertFalse(self.service.redeploy_check_message)

    def test_old_operator_without_images_only_clears_pending(self):
        self._redeploy()
        self._poll(None)
        self.assertFalse(self.service.redeploy_verify_pending)
        self.assertFalse(self.service.redeploy_check_message)

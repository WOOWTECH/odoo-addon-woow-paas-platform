# -*- coding: utf-8 -*-
"""商用驗收 AUDITFIX template-id-write-guard（#1216 的 template_id 那一格）。

## 缺陷

``cloud_service.template_id`` 沒有 ``groups=``、``write()`` 也沒有守衛；ACL 給 ``base.group_user``
1,1,1,1，``cloud_service_rule_owner_admin_manage`` 讓 workspace owner／admin 寫入，而 owner 資格可以
自我滿足（自建 workspace）⇒ ``group_user`` 的 owner 一筆 ``/web/dataset/call_kw`` 就能把服務改綁到
別的範本：計費、隱藏實例（``conceal_instances``）、chart、方案白名單都以 template 為準。portal 租戶
今天在 ACL 層就被擋（稽核當時 uid 205 是 portal，所以執行期沒驗到）；本檔用 ``group_user`` owner 走
真 RPC 驗「洞存在、修後被擋」。

## 修法

``write()`` 加 ``_reject_template_binding_writes``：非平台身分（``env.is_system()`` 為假）改
``template_id`` 成**不同**的值 ⇒ AccessError。平台自己的寫入（sudo／cron）照常；寫回相同的值
（例如整包回存表單）不擋。#1216 其餘平台控制欄位（state、helm_namespace、allocated_*…）需要先把
controller 以租戶身分寫 state 的路徑改走 sudo，不在本檔範圍。
"""
import json
import uuid

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _internal_provision


def _group_user(env, name):
    login = '%s_%s' % (name, uuid.uuid4().hex[:8])
    user = env['res.users'].sudo().create({
        'name': name, 'login': login, 'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class TestTemplateIdWriteGuardAuditfix(HttpCase):

    def setUp(self):
        super().setUp()
        self.owner, self.owner_login = _group_user(self.env, 'tplguard_owner')
        self.assertFalse(self.owner.has_group('base.group_system'), '前提：租戶不是系統管理員')
        self.ws = self.env['woow_paas_platform.workspace'].with_user(self.owner).create(
            {'name': 'Template guard WS'})
        self.tpl_a = self._template('tplguard-a')
        self.tpl_b = self._template('tplguard-b')
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create({
            'name': 'Template guard svc', 'workspace_id': self.ws.id,
            'template_id': self.tpl_a.id, 'state': 'running',
            'helm_namespace': 'paas-ws-test-tplguard',
            'helm_release_name': 'svc-tplguard-%s' % uuid.uuid4().hex[:6],
            'helm_values': json.dumps({}),
        })

    def _template(self, slug):
        return self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Template guard %s' % slug,
            'slug': '%s-%s' % (slug, uuid.uuid4().hex[:6]),
            'helm_repo_url': '', 'helm_chart_name': 'oci://example.com/%s' % slug,
            'helm_chart_version': '1.0.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })

    def _call_kw(self, method, args):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {
            'model': 'woow_paas_platform.cloud_service', 'method': method,
            'args': args, 'kwargs': {}}})
        resp = self.url_open('/web/dataset/call_kw', data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def test_owner_cannot_rebind_the_template_over_rpc(self):
        self.authenticate(self.owner_login, self.owner_login)
        # 陽性對照：同一個 owner、同一條 RPC 改名字可以——擋的是 template_id，不是整個寫入。
        renamed = self._call_kw('write', [[self.svc.id], {'name': 'Renamed by owner'}])
        self.assertTrue(renamed.get('result'), renamed)

        attacked = self._call_kw('write', [[self.svc.id], {'template_id': self.tpl_b.id}])

        self.assertIn('error', attacked, attacked)
        self.assertEqual(self.svc.template_id, self.tpl_a, 'template_id 不得被租戶改綁')

    def test_owner_orm_write_is_refused_but_same_value_is_allowed(self):
        with self.assertRaises(AccessError):
            self.svc.with_user(self.owner).write({'template_id': self.tpl_b.id})
        self.svc.with_user(self.owner).write({'template_id': self.tpl_a.id, 'name': 'Same tpl'})
        self.assertEqual(self.svc.name, 'Same tpl')

    def test_platform_identity_can_still_rebind(self):
        self.svc.sudo().write({'template_id': self.tpl_b.id})
        self.assertEqual(self.svc.template_id, self.tpl_b)

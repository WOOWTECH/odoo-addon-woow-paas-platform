# -*- coding: utf-8 -*-
"""#794 前端接上之後：`access_protection_lost` 必須出現在**畫面實際讀的那一份** payload。

## 守什麼

`AccessAppCard` 從這一版起在 `config.access_protection_lost` 為真時畫紅色警示、
把「Protected」改成「Protection not in effect」，三個呼叫端的卡頭計數也把它排除。
卡片只認 config 上的這個 key——key 不在就是 `undefined`，而 `undefined` 與「沒
事」在畫面上**長得一模一樣**：警示靜默消失，UI 回到 #794 的原狀（顯示已保護、
實際免登入即可存取），沒有任何測試或 console 會紅。

既有測試（`test_access_failopen_compensation_794.py::test_A7`）只釘了 SA
`route.to_dict()` 這一格；但畫面讀的不是它本身，而是**外層詳情 payload 裡嵌著
的那一份**，三腿各不相同：

  * SA：`SecurityAccessDetailPage` 讀 `security_access.to_dict()['routes']`
    （`controllers/security_access.py` 的 detail / 各寫入端點都回整包 SA）
  * SH：`SmartHomeAccessTab` 讀 `smart_home.to_dict(include_details=True)['routes']`
  * CS：`AccessTab` 讀 `_format_service(include_details=True)['access']`
    ＝`cloud_service._access_to_dict()`（config 寫入端點也回它）

所以本檔對三腿各釘兩格：旗標打開 ⇒ 詳情 payload 裡是 `True`；沒打開 ⇒ `False`
（不是缺席——缺席會讓「key 被拿掉」與「本來就沒事」不可區分，那正是要擋的）。
"""
import uuid

from odoo.tests import TransactionCase, tagged

from ..models.cloud_service import (
    _cs_skip_access_sync, _internal_cs_access_identity, _internal_provision,
)
from ..models.security_access import (
    _internal_identity as _internal_sa, _skip_route_sync,
)
from ..models.smart_home_route import (
    _internal_sm_route_identity, _sm_skip_access_sync,
)
from ..services.naming import make_service_subdomain
from .smart_home_fixture import sh_model

_KEY = 'access_protection_lost'
_SA_ROUTE = 'woow_paas_platform.security_access_route'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'
_CS = 'woow_paas_platform.cloud_service'


class _PayloadMixin:

    def _assert_flag(self, payload, expected, where):
        self.assertIn(
            _KEY, payload,
            '%s 少了 %s：AccessAppCard 讀到 undefined ⇒ fail-open 警示靜默消失'
            % (where, _KEY))
        self.assertIs(
            payload[_KEY], expected,
            '%s 的 %s 必須是 %r（前端以真假值判斷，型別要穩）'
            % (where, _KEY, expected))


@tagged('post_install', '-at_install')
class TestSaDetailPayloadCarriesProtectionLost(TransactionCase, _PayloadMixin):
    """SA 腿：詳情頁讀的是 `sa.to_dict()['routes']`，不是單獨的 route.to_dict()。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS lostui A %s' % uuid.uuid4().hex[:6]})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA lostui %s' % uuid.uuid4().hex[:6],
             'workspace_id': self.workspace.id})
        _internal_sa(self.sa).write(
            {'state': 'active', 'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8]})
        self.route = _skip_route_sync(self.env[_SA_ROUTE].sudo()).create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'r%s' % uuid.uuid4().hex[:10],
            'is_protected': True})

    def _detail_route(self):
        routes = self.sa.to_dict()['routes']
        match = [r for r in routes if r['id'] == self.route.id]
        self.assertEqual(len(match), 1, '前提：詳情 payload 帶得出這條 route')
        return match[0]

    def test_A1_flag_off_is_an_explicit_false(self):
        self._assert_flag(self._detail_route(), False, 'SA 詳情 routes[]')

    def test_A2_flag_on_reaches_the_detail_payload(self):
        self.route.access_protection_lost = True
        detail = self._detail_route()
        self._assert_flag(detail, True, 'SA 詳情 routes[]')
        self.assertTrue(
            detail['is_protected'],
            '前提：is_protected 仍是 True（意圖不變）——卡片要靠旗標而不是它')


@tagged('post_install', '-at_install')
class TestShDetailPayloadCarriesProtectionLost(TransactionCase, _PayloadMixin):
    """SH 腿：Access 頁籤讀的是 `to_dict(include_details=True)['routes']`。"""

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS lostui B %s' % uuid.uuid4().hex[:6]})
        sh = sh_model(self.env).create({
            'name': 'sh-lostui-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id, 'state': 'active',
            'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8]})
        sh.write({'subdomain': sh._generate_subdomain()})
        self.sh = sh.with_env(self.env)
        route = _sm_skip_access_sync(self.env[_SH_ROUTE]).create(
            {'smart_home_id': self.sh.id, 'is_primary': True})
        self.route = route.with_context(sm_skip_access_sync=False)
        _sm_skip_access_sync(self.route).write({'is_protected': True})
        _internal_sm_route_identity(self.route).access_app_id = 'app-live'

    def _detail_route(self):
        routes = self.sh.to_dict(include_details=True)['routes']
        match = [r for r in routes if r['id'] == self.route.id]
        self.assertEqual(len(match), 1, '前提：詳情 payload 帶得出這條 route')
        return match[0]

    def test_B1_flag_off_is_an_explicit_false(self):
        self._assert_flag(self._detail_route(), False, 'SH 詳情 routes[]')

    def test_B2_flag_on_reaches_the_detail_payload(self):
        self.route.access_protection_lost = True
        self._assert_flag(self._detail_route(), True, 'SH 詳情 routes[]')


@tagged('post_install', '-at_install')
class TestCsAccessPayloadCarriesProtectionLost(TransactionCase, _PayloadMixin):
    """CS 腿：`_access_to_dict()` 是詳情 payload 的 `access` 與 config 端點回應的
    單一來源（`controllers/paas.py`），釘它就釘住兩個出口。"""

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS lostui C %s' % uuid.uuid4().hex[:6]})
        template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'CS lostui tmpl',
                'slug': 'cslostui-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/cslostui',
                'helm_chart_version': '1.0.0', 'workload_type': 'container',
                'post_deploy_init_type': 'none', 'mcp_enabled': False})
        svc = _internal_provision(self.env[_CS].sudo()).create({
            'name': 'svc-lostui-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id,
            'template_id': template.id, 'state': 'running'})
        svc.subdomain = make_service_subdomain(
            self.workspace.ws_hash, svc.reference_id, svc.name)
        _cs_skip_access_sync(svc).write({'is_protected': True})
        _internal_cs_access_identity(svc).access_app_id = 'app-live'
        self.svc = svc

    def test_C1_flag_off_is_an_explicit_false(self):
        self._assert_flag(self.svc._access_to_dict(), False, 'CS access')

    def test_C2_flag_on_reaches_the_access_payload(self):
        self.svc.access_protection_lost = True
        self._assert_flag(self.svc._access_to_dict(), True, 'CS access')

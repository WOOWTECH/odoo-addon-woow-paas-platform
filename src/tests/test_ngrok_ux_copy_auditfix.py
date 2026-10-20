# -*- coding: utf-8 -*-
"""商用驗收 ngrok-ux-copy：ngrok 文案與對外位址回讀（INFRA-EMQX-020-1/2/3、INFRA-HS-013-2/3/5）。

## 缺陷（2026-10-16／17 商用驗收，EMQX 248、Headscale 238）

1. **ERR_NGROK_9014 文案誤導**（EMQX-020-1）：legacy／免費 ngrok 帳號被拒的是 PROXY protocol
   （chart 0.1.6 起 `ngrok.proxyProtocol=true`），token 其實有效；服務頁卻走通用句「請檢查 authtoken
   與網域」，租戶永遠查不到問題。⇒ 專屬文案，**不得**寫成「不支援 TCP」。
2. **撤銷說明前後矛盾**（EMQX-020-2、HS-013-5）：authtoken 欄位說明寫「清空即撤銷」，但欄位
   placeholder 是「留空表示不變更」，實際要勾「清除 ngrok Authtoken」。
3. **明文 TCP 沒有風險提示**（EMQX-020-3）：原生 MQTT 經 ngrok 是明文 TCP、經第三方。
4. **Headscale 失敗時靜默退回叢集內網址**（HS-013-3）：頁面只說 ngrok 的原因，沒說「登入網址已
   暫時改成叢集內網址、外部裝置暫時離線」。
5. **回讀落後 1–9 分鐘、沒有資料時間**（HS-013-2）：running 服務的對外位址只有每日對帳會再探；
   設定變更後轉回 running 的那一輪 poll 常常早於 agent 連上。⇒ 進入 running 起 10 分鐘內每分鐘
   重探，並把「最後檢查時間」給服務頁。
6. **PROXY protocol 無法關閉**（charts#222 根因）：chart 0.1.6 起就有 `ngrok.proxyProtocol` 開關，
   配置頁沒有開放。⇒ 開放成選填欄位，**預設維持開啟**（#1529 的安全設計不變）。

範本說明、服務頁兩則提示都是範本 record 的**資料值**（seed 寫中文原文、不進 .po）；
9014 文案與「最後檢查」是程式碼字串（走 .po）。
"""
import json
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import PUBLIC_TUNNEL_RECHECK_MINUTES, _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_CLIENT_FACTORY = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
ADDRESS = 'https://abc-123.ngrok-free.app'


def _po_code_entries():
    po = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
    return {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'ngrok ux workspace',
        })

    def _template(self, slug, has_public_tunnel_address=True, **extra):
        vals = {
            'name': 'ngrok ux %s' % slug, 'slug': slug, 'category': 'automation',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.1.0', 'mcp_enabled': False,
            'has_public_tunnel_address': has_public_tunnel_address,
        }
        vals.update(extra)
        return self.env['woow_paas_platform.cloud_app_template'].create(vals)

    def _service(self, template, state='running', **extra):
        vals = {
            'name': 'svc-%s' % template.slug, 'workspace_id': self.workspace.id,
            'template_id': template.id, 'state': state,
            'helm_namespace': 'paas-ws-test', 'helm_release_name': 'svc-%s' % template.slug,
            'helm_values': json.dumps({}),
        }
        vals.update(extra)
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create(vals)

    @staticmethod
    def _client(payload=None, raises=None):
        client = MagicMock()
        if raises is not None:
            client.get_public_address.side_effect = raises
        else:
            client.get_public_address.return_value = payload
        return client


# --------------------------------------------------------------------------- #
#  1. ERR_NGROK_9014                                                           #
# --------------------------------------------------------------------------- #

@tagged('post_install', '-at_install')
class TestNgrok9014Message(_Base):

    def test_9014_says_plan_and_proxy_protocol_not_token(self):
        service = self._service(self._template('ux-9014'))
        text = service._public_tunnel_error_text('ERR_NGROK_9014')
        self.assertIn('ERR_NGROK_9014', text)
        self.assertIn('PROXY protocol', text)
        self.assertIn('plan', text)
        self.assertNotIn('Check the ngrok authtoken and domain', text,
                         'token 有效：通用句叫人檢查 token／網域正是 EMQX-020-1 的缺陷')
        self.assertNotIn('TCP', text, '被拒的是 PROXY protocol，不是 TCP（更正版缺陷）')

    def test_other_unknown_codes_keep_the_generic_sentence(self):
        service = self._service(self._template('ux-9999'))
        self.assertIn('Check the ngrok authtoken and domain',
                      service._public_tunnel_error_text('ERR_NGROK_9999'))

    def test_9014_has_traditional_chinese(self):
        code = _po_code_entries()
        hit = [v for s, v in code.items() if 'ERR_NGROK_9014' in s and 'PROXY protocol' in s]
        self.assertTrue(hit and hit[0], 'zh_TW.po 缺 ERR_NGROK_9014 的翻譯')
        self.assertNotIn('TCP', hit[0])

    def test_9014_in_creator_language(self):
        self.env['res.lang']._activate_lang('zh_TW')
        service = self._service(self._template('ux-9014-zh'))
        service.create_uid.sudo().lang = 'zh_TW'
        text = service._public_tunnel_error_text('ERR_NGROK_9014')
        self.assertIn('PROXY protocol', text)
        self.assertIn('付費方案', text)


# --------------------------------------------------------------------------- #
#  2./3./6. 範本欄位說明（seed）                                                #
# --------------------------------------------------------------------------- #

@tagged('post_install', '-at_install')
class TestNgrokSpecCopy(TransactionCase):

    def _optional(self, xmlid):
        tpl = self.env.ref('woow_paas_platform.%s' % xmlid)
        return {s['key']: s for s in tpl._parse_helm_value_specs()['optional']}

    def test_authtoken_tells_how_to_revoke(self):
        for xmlid in ('cloud_app_emqx', 'cloud_app_headscale'):
            with self.subTest(template=xmlid):
                desc = self._optional(xmlid)['ngrok.authtoken']['description']
                self.assertNotIn('清空即撤銷', desc,
                                 'placeholder 是「留空表示不變更」，照「清空即撤銷」做不會撤銷')
                self.assertIn('清除 ngrok Authtoken', desc,
                              '撤銷要勾「清除已設定的金鑰 → 清除 ngrok Authtoken」')
                self.assertIn('留空', desc)

    def test_emqx_enabled_warns_about_plaintext(self):
        desc = self._optional('cloud_app_emqx')['ngrok.enabled']['description']
        self.assertIn('明文', desc)
        self.assertIn('第三方', desc)
        self.assertIn('wss://', desc)

    def test_emqx_proxy_protocol_switch(self):
        spec = self._optional('cloud_app_emqx')['ngrok.proxyProtocol']
        self.assertEqual(spec['type'], 'boolean')
        self.assertIs(spec['default'], True, '#1529：預設維持開啟（看得到真實來源位址）')
        self.assertIn('ERR_NGROK_9014', spec['description'])
        self.assertIn('127.0.0.1', spec['description'], '關閉的代價要寫清楚')

    def test_proxy_protocol_passes_the_whitelist(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_emqx')
        allowed, rejected = tpl._filter_allowed_helm_values({'ngrok.proxyProtocol': False})
        self.assertEqual(allowed, {'ngrok.proxyProtocol': False})
        self.assertEqual(rejected, [])

    def test_headscale_has_no_proxy_protocol_knob(self):
        self.assertNotIn('ngrok.proxyProtocol', self._optional('cloud_app_headscale'),
                         'Headscale 走 ngrok https，沒有 PROXY protocol 這回事')


# --------------------------------------------------------------------------- #
#  3./4. 服務頁提示（範本資料值）                                               #
# --------------------------------------------------------------------------- #

@tagged('post_install', '-at_install')
class TestTunnelNotes(_Base):

    def test_seeded_notes(self):
        emqx = self.env.ref('woow_paas_platform.cloud_app_emqx')
        self.assertIn('明文', emqx.public_tunnel_address_note)
        self.assertIn('wss://', emqx.public_tunnel_address_note)
        self.assertFalse(emqx.public_tunnel_error_note,
                         'EMQX 的 ngrok 失敗不影響 broker 與 WebSocket，不需要額外提示')
        headscale = self.env.ref('woow_paas_platform.cloud_app_headscale')
        self.assertIn('叢集內網址', headscale.public_tunnel_error_note)
        self.assertIn('外部裝置', headscale.public_tunnel_error_note)
        self.assertFalse(headscale.public_tunnel_address_note)

    def test_notes_are_data_values_not_translated(self):
        fields_ = self.env['woow_paas_platform.cloud_app_template']._fields
        for name in ('public_tunnel_address_note', 'public_tunnel_error_note'):
            with self.subTest(field=name):
                self.assertFalse(fields_[name].translate)

    def test_payload_carries_notes_and_checked_at(self):
        tpl = self._template('ux-payload', public_tunnel_address_note='addr note',
                             public_tunnel_error_note='err note')
        service = self._service(tpl)
        checked = fields.Datetime.now().replace(microsecond=0)
        service.sudo().write({'public_tunnel_checked_at': checked})
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            payload = PaasController()._format_service(service, include_details=True)
        self.assertEqual(payload['public_tunnel_address_note'], 'addr note')
        self.assertEqual(payload['public_tunnel_error_note'], 'err note')
        self.assertTrue(payload['public_tunnel_checked_at'].endswith('+00:00'),
                        '帶 UTC offset（同 last_release_sync_at，#1456）')

    def test_payload_empty_values_are_none(self):
        service = self._service(self._template('ux-payload-empty'))
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            payload = PaasController()._format_service(service, include_details=True)
        self.assertIsNone(payload['public_tunnel_address_note'])
        self.assertIsNone(payload['public_tunnel_error_note'])
        self.assertIsNone(payload['public_tunnel_checked_at'])


# --------------------------------------------------------------------------- #
#  5. 回讀：進入 running 後的重探窗口 ＋ 最後檢查時間                           #
# --------------------------------------------------------------------------- #

@tagged('post_install', '-at_install')
class TestTunnelRecheckWindow(_Base):

    def _run_cron(self, client):
        with patch(_CLIENT_FACTORY, return_value=client):
            self.env['woow_paas_platform.cloud_service']._cron_recheck_public_tunnels()

    def test_entering_running_arms_the_window(self):
        service = self._service(self._template('ux-arm'), state='upgrading')
        before = fields.Datetime.now()
        service.write({'state': 'running'})
        self.assertTrue(service.public_tunnel_recheck_until)
        self.assertGreaterEqual(
            service.public_tunnel_recheck_until,
            before + timedelta(minutes=PUBLIC_TUNNEL_RECHECK_MINUTES - 1))

    def test_products_without_the_capability_are_not_armed(self):
        service = self._service(self._template('ux-arm-off', False), state='upgrading')
        service.write({'state': 'running'})
        self.assertFalse(service.public_tunnel_recheck_until)

    def test_running_to_running_does_not_rearm(self):
        service = self._service(self._template('ux-arm-same'), state='running')
        service.sudo().write({'public_tunnel_recheck_until': False})
        service.write({'state': 'running'})
        self.assertFalse(service.public_tunnel_recheck_until)

    def test_cron_picks_up_the_address_within_the_window(self):
        """HS-013-2：轉回 running 那一輪 poll 早於 agent 連上 → 下一分鐘的重探要補上。"""
        service = self._service(self._template('ux-cron'), state='upgrading')
        service.write({'state': 'running'})
        service.sudo().write({'public_tunnel_error': 'stale: no authtoken'})
        client = self._client({'public_url': ADDRESS})
        self._run_cron(client)
        service.invalidate_recordset()
        self.assertEqual(service.public_tunnel_url, ADDRESS)
        self.assertFalse(service.public_tunnel_error, '有位址時舊的錯誤原因要清掉')
        self.assertTrue(service.public_tunnel_checked_at)

    def test_cron_updates_a_fixed_error(self):
        service = self._service(self._template('ux-cron-err'), state='upgrading')
        service.write({'state': 'running'})
        self._run_cron(self._client({'public_url': None, 'error_code': 'ERR_NGROK_105'}))
        service.invalidate_recordset()
        self.assertFalse(service.public_tunnel_url)
        self.assertIn('ERR_NGROK_105', service.public_tunnel_error)

    def test_cron_skips_expired_window(self):
        service = self._service(self._template('ux-cron-expired'), state='running')
        service.sudo().write({
            'public_tunnel_recheck_until': fields.Datetime.now() - timedelta(minutes=1)})
        client = self._client({'public_url': ADDRESS})
        self._run_cron(client)
        client.get_public_address.assert_not_called()

    def test_cron_skips_services_that_left_running(self):
        service = self._service(self._template('ux-cron-left'), state='upgrading')
        service.write({'state': 'running'})
        service.write({'state': 'upgrading'})
        client = self._client({'public_url': ADDRESS})
        self._run_cron(client)
        client.get_public_address.assert_not_called()

    def test_cron_operator_failure_writes_nothing(self):
        service = self._service(self._template('ux-cron-down'), state='upgrading')
        service.write({'state': 'running'})
        with self.assertLogs('odoo.addons.woow_paas_platform.models.cloud_service',
                             level='WARNING'):
            self._run_cron(self._client(raises=RuntimeError('down')))
        service.invalidate_recordset()
        self.assertFalse(service.public_tunnel_checked_at, '沒探到就不能宣稱檢查過')

    def test_cron_never_touches_state(self):
        service = self._service(self._template('ux-cron-state'), state='upgrading')
        service.write({'state': 'running'})
        self._run_cron(self._client({'public_url': None, 'error_code': 'ERR_NGROK_334'}))
        service.invalidate_recordset()
        self.assertEqual(service.state, 'running')
        self.assertFalse(service.error_message)

    def test_release_facts_stamp_checked_at_only_when_probed(self):
        service = self._service(self._template('ux-facts'))
        release = {'revision': 3, 'chart_version': '0.1.0'}
        self.assertNotIn('public_tunnel_checked_at',
                         service._release_facts(release, tunnel_address=None))
        self.assertIn('public_tunnel_checked_at',
                      service._release_facts(release, tunnel_address=''))

    def test_cron_record_runs_every_minute(self):
        cron = self.env.ref('woow_paas_platform.ir_cron_recheck_public_tunnels')
        self.assertEqual((cron.interval_number, cron.interval_type), (1, 'minutes'))
        self.assertTrue(cron.active)


@tagged('post_install', '-at_install')
class TestTunnelObservationFieldsArePlatformOnly(_Base):

    def test_tenant_cannot_write_recheck_fields(self):
        user = new_test_user(self.env, login='ngrok-ux-tenant', groups='base.group_user')
        service = self._service(self._template('ux-acl'))
        for name in ('public_tunnel_recheck_until', 'public_tunnel_checked_at'):
            with self.subTest(field=name), self.assertRaises(AccessError):
                service.with_user(user).write({name: fields.Datetime.now()})


@tagged('post_install', '-at_install')
class TestCheckedAtTranslation(TransactionCase):

    def test_last_checked_has_traditional_chinese(self):
        code = _po_code_entries()
        self.assertTrue(code.get('Last checked: %s'), 'OverviewTab「最後檢查」缺中文')

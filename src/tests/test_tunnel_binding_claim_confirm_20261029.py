"""HA issue #38：設定流程的新建設定項要明確確認，才能接手 legacy 綁定。

`legacy → claimed` 既是「同一台機器把元件從沒有身分的舊版升級到新版」走的路，也是「另一台
機器來認領」走的路，平台分不出來，所以不能一律擋（會把升級後的既有安裝鎖在外面）。改由元件
在設定流程的取權杖請求自己說明：`new_entry=1`（這是新建設定項）與 `confirmed=1`（使用者從
清單明確選了這個服務）。只有「新建設定項、沒有確認、對象是 legacy 綁定」才拒絕。

這裡釘住兩側（Smart Home 與 Security Access 是鏡像端點，只收一側等於把攻擊面搬家）：
  * 拒絕的條件與它不能擴大的邊界（升級、舊版元件、未綁定、已被別台持有）；
  * 端點層：409 `already_bound` 且不發權杖；帶 `confirmed=1` 才放行；不帶旗標與亂帶旗標都
    維持原行為。
"""
import uuid
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.tunnel_binding_mixin import (
    TunnelBindingConflict,
)

from .test_tunnel_binding_1766 import _SCOPES, UID_A, UID_B, _BindingFixture


@tagged('post_install', '-at_install')
class TestNewEntryClaimModel(_BindingFixture, TransactionCase):
    """claim 的規則（model 層）。"""

    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def _records(self):
        return (('sh', self.home), ('sa', self.sa))

    def test_new_entry_over_a_legacy_binding_needs_confirmation(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim('', '')
                with self.assertRaises(TunnelBindingConflict) as caught:
                    record._tunnel_binding_claim(UID_B, 'new machine', new_entry=True)
                self.assertTrue(caught.exception.bound_to)
                # 什麼都沒寫：仍是 legacy，沒有被記成別人的
                self.assertEqual(record.binding_state, 'legacy')
                self.assertFalse(record.bound_instance_uid)

    def test_confirmed_new_entry_takes_over_a_legacy_binding(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim('', '')
                state = record._tunnel_binding_claim(
                    UID_B, 'new machine', new_entry=True, confirmed=True)
                self.assertEqual(state, 'claimed')
                self.assertEqual(record.bound_instance_uid, UID_B)

    def test_upgrade_of_an_existing_entry_is_not_affected(self):
        """同一台機器升級元件：不帶 new_entry，legacy → claimed 照舊放行。"""
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim('', '')
                self.assertEqual(record._tunnel_binding_claim(UID_A, 'same machine'), 'claimed')
                self.assertEqual(record.bound_instance_uid, UID_A)

    def test_a_free_service_needs_no_confirmation(self):
        for label, record in self._records():
            with self.subTest(label):
                self.assertEqual(
                    record._tunnel_binding_claim(UID_A, 'first machine', new_entry=True), 'claimed')

    def test_confirmation_does_not_override_another_installation(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim(UID_A, 'first machine')
                with self.assertRaises(TunnelBindingConflict):
                    record._tunnel_binding_claim(
                        UID_B, 'second machine', new_entry=True, confirmed=True)
                self.assertEqual(record.bound_instance_uid, UID_A)

    def test_the_holder_may_ask_again_without_confirmation(self):
        for label, record in self._records():
            with self.subTest(label):
                record._tunnel_binding_claim(UID_A, 'first machine')
                self.assertEqual(
                    record._tunnel_binding_claim(UID_A, 'first machine', new_entry=True), 'claimed')


@tagged('post_install', '-at_install')
class TestNewEntryClaimEndpoints(_BindingFixture, HttpCase):
    """端點層（真 HTTP）：Smart Home 與 Security Access 兩側同一套規則。"""

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.client_rec = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')

    def _get(self, url):
        token = self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': 'ha-claim-' + uuid.uuid4().hex,
            'scope': _SCOPES,
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'client_id': self.client_rec.id,
            'user_id': self.owner.id,
        })
        return self.url_open(url, headers={'Authorization': 'Bearer ' + token.access_token})

    def _targets(self):
        return (
            ('sh', self.home, '/api/smarthome/homes/%d/tunnel-token' % self.home.id),
            ('sa', self.sa, '/api/security-access/accesses/%d/tunnel-token' % self.sa.id),
        )

    def _state(self, record):
        record.invalidate_recordset()
        return record.binding_state, record.bound_instance_uid or ''

    def test_unconfirmed_new_entry_is_refused_and_gets_no_token(self):
        for label, record, base in self._targets():
            with self.subTest(label):
                self.assertEqual(self._get(base).status_code, 200)  # the older component, no identity
                self.assertEqual(self._state(record), ('legacy', ''))
                refused = self._get(base + '?instance_uid=%s&new_entry=1' % UID_B)
                self.assertEqual(refused.status_code, 409, refused.text)
                body = refused.json()
                self.assertEqual(body['error'], 'already_bound')
                self.assertNotIn('tunnel_token', body)
                self.assertEqual(self._state(record), ('legacy', ''))

    def test_confirmed_new_entry_gets_the_token_and_the_binding(self):
        for label, record, base in self._targets():
            with self.subTest(label):
                self.assertEqual(self._get(base).status_code, 200)
                accepted = self._get(base + '?instance_uid=%s&new_entry=1&confirmed=1' % UID_B)
                self.assertEqual(accepted.status_code, 200, accepted.text)
                self.assertTrue(accepted.json()['tunnel_token'])
                self.assertEqual(self._state(record), ('claimed', UID_B))

    def test_upgrade_and_older_components_are_not_affected(self):
        """不帶 new_entry（既有設定項的重啟／升級，以及這個參數出現之前的所有元件）維持原行為。"""
        for label, record, base in self._targets():
            with self.subTest(label):
                self.assertEqual(self._get(base).status_code, 200)
                self.assertEqual(self._get(base + '?instance_uid=%s' % UID_A).status_code, 200)
                self.assertEqual(self._state(record), ('claimed', UID_A))

    def test_only_explicit_true_values_count_as_flags(self):
        for label, record, base in self._targets():
            with self.subTest(label):
                self.assertEqual(self._get(base).status_code, 200)
                for junk in ('yes', '0', 'false', ''):
                    unconfirmed_junk = self._get(base + '?instance_uid=%s&new_entry=%s' % (UID_B, junk))
                    # a junk new_entry is not a new_entry: the upgrade path stays open ...
                    self.assertEqual(unconfirmed_junk.status_code, 200, (junk, unconfirmed_junk.text))
                    self.assertEqual(self._state(record), ('claimed', UID_B))
                    record.action_release_binding()
                    self._get(base)  # ... and back to a legacy binding for the next value
                    self.assertEqual(self._state(record), ('legacy', ''))
                # a junk `confirmed` does not confirm
                refused = self._get(base + '?instance_uid=%s&new_entry=1&confirmed=yes' % UID_B)
                self.assertEqual(refused.status_code, 409, refused.text)

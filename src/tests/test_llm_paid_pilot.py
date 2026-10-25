"""Explicit cost-preserving pilot: cache/profile binding and upstream key boundaries."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import tagged

from .. import hooks
from ..models import llm_common
from ..models.llm_common import _llm_internal
from ..services import llm_pricing as pricing
from .test_llm_platform_foundation import _ConfiguredCase
from .test_llm_api_key_management import TestLlmPaidKeyCreate as _PaidKeyCase


def _source():
    # Public tariff fixtures, not real upstream credentials or inference receipts.
    return {'group_ratio': {'default': 1}, 'data': [
        {'model_name': 'anthropic/claude-haiku-5.5', 'enable_groups': ['default'],
         'quota_type': 0, 'model_ratio': '0.05', 'completion_ratio': 5},
        {'model_name': 'chatgpt/gpt-6-luna', 'enable_groups': ['internal', 'default'],
         'quota_type': 0, 'model_ratio': '0.05', 'completion_ratio': 5},
        {'model_name': 'deepseek/deepseek-v4.1-flash', 'enable_groups': ['default'],
         'quota_type': 0, 'model_ratio': '0.01188', 'completion_ratio': '30.30303'},
    ]}


@tagged('post_install', '-at_install', 'llm_paid_pilot')
class TestLlmPaidPilotPolicy(_ConfiguredCase):

    def setUp(self):
        super().setUp()
        self.icp = self.env['ir.config_parameter'].sudo()
        self.Cache = self.env['woow_paas_platform.llm_swr_cache']

    def _enable(self):
        self.icp.set_param(llm_common.PARAM_PILOT_PARTNER_IDS, str(self.env.user.partner_id.id))
        self.icp.set_param(llm_common.PARAM_PRICING_PROFILE, pricing.PAID_PILOT_PROFILE)

    def _cache(self, payload):
        vals = {'payload': json.dumps(payload), 'fetched_at': fields.Datetime.now(),
                'last_result': 'ok'}
        row = self.Cache.sudo().search([('account_id', '=', False), ('kind', '=', 'pricing'),
                                        ('cache_key', '=', False)], limit=1)
        if row:
            _llm_internal(row).write(vals)
        else:
            _llm_internal(self.Cache.sudo()).create(dict(vals, kind='pricing'))

    def test_profile_registered_validated_and_default_fingerprint_preserved(self):
        self.assertIn(llm_common.PARAM_PRICING_PROFILE, hooks._CONFIG_PARAMETERS)
        legacy = '\n'.join('%s=%s' % (key, (self.icp.get_param(key) or '').strip())
                           for key in sorted(llm_common.CONFIG_CHANGE_CODES)
                           if key != llm_common.PARAM_PRICING_PROFILE)
        self.assertEqual(llm_common.config_fingerprint(self.env),
                         hashlib.sha256(legacy.encode()).hexdigest()[:32])
        self.icp.set_param(llm_common.PARAM_PRICING_PROFILE, '   ')
        self.assertEqual(llm_common.config_fingerprint(self.env),
                         hashlib.sha256(legacy.encode()).hexdigest()[:32])
        with self.assertRaises(ValidationError):
            self.icp.set_param(llm_common.PARAM_PRICING_PROFILE, 'arbitrary-ratio')
        self._enable()
        self.assertNotEqual(llm_common.config_fingerprint(self.env),
                            hashlib.sha256(legacy.encode()).hexdigest()[:32])

    def test_ratio_one_only_under_explicit_pilot_and_prices_are_unchanged(self):
        with self.assertRaises(pricing.PricingInvalid):
            pricing.normalize_pricing(_source())
        dto = pricing.normalize_pricing(_source(), profile=pricing.PAID_PILOT_PROFILE)
        haiku = next(item for item in dto['items'] if item['model'].startswith('anthropic/'))
        self.assertEqual(haiku['price']['input'], '3.2')
        self.assertEqual(haiku['price']['output'], '16')
        for ratio in (1.3, 0, 'bad'):
            data = _source()
            data['group_ratio']['default'] = ratio
            with self.subTest(ratio=ratio), self.assertRaises(pricing.PricingInvalid):
                pricing.normalize_pricing(data, profile=pricing.PAID_PILOT_PROFILE)

    def test_unrelated_models_filtered_and_mixed_groups_rejected(self):
        data = _source()
        data['data'].append({'model_name': 'legacy/unrelated', 'enable_groups': ['default']})
        dto = pricing.normalize_pricing(data, profile=pricing.PAID_PILOT_PROFILE)
        self.assertEqual({item['model'] for item in dto['items']},
                         {item['model_name'] for item in _source()['data']})
        data['data'][0]['enable_groups'] = ['default', 'free']
        with self.assertRaises(pricing.PricingInvalid):
            pricing.normalize_pricing(data, profile=pricing.PAID_PILOT_PROFILE)

    def test_cache_profile_binding_on_enable_disable_and_pilot_removal(self):
        dto = pricing.normalize_pricing(_source(), profile=pricing.PAID_PILOT_PROFILE)
        self._cache(dto)
        self.assertEqual(self.Cache._llm_models_payload()['state'], 'maintenance')
        self._enable()
        public = self.Cache._llm_models_payload()
        self.assertEqual(public['state'], 'ok')
        self.assertNotIn('pricing_profile', public)
        self.icp.set_param(llm_common.PARAM_PILOT_PARTNER_IDS, False)
        self.assertEqual(self.Cache._llm_models_payload()['state'], 'maintenance')
        self.icp.set_param(llm_common.PARAM_PRICING_PROFILE, False)
        self.assertEqual(self.Cache._llm_models_payload()['state'], 'maintenance')

    def test_old_catalog_cannot_be_reused_when_pilot_is_enabled(self):
        self._cache({'state': 'ok', 'items': [{'model': 'old/model', 'group': 'default'}]})
        self._enable()
        self.assertEqual(self.Cache._llm_models_payload()['state'], 'maintenance')

    def test_refresh_uses_selected_profile_and_api_never_returns_privileged_envelope(self):
        self._enable()
        source = _source()
        source['privileged_fixture'] = 'must not be copied'
        client = MagicMock()
        client.ap_pricing.return_value = SimpleNamespace(ok=True, data=source)
        dto, label = self.Cache._llm_swr_refresh_one(
            client, llm_common.llm_config(self.env), None, 'pricing', None)
        self.assertEqual(label, 'ok')
        self.assertEqual(dto['pricing_profile'], pricing.PAID_PILOT_PROFILE)
        self.assertNotIn('privileged_fixture', dto)
        client.ap_pricing.assert_called_once()

    def test_empty_or_invalid_pilot_cannot_enable_cost_profile(self):
        for pilots in (frozenset(), frozenset({-1}), frozenset({0}), [3]):
            with self.subTest(pilots=pilots), self.assertRaises(pricing.PricingInvalid):
                pricing.pricing_profile_for_config({'pricing_profile': pricing.PAID_PILOT_PROFILE,
                                                   'pilot': pilots})


@tagged('post_install', '-at_install', 'llm_paid_pilot')
class TestLlmPaidPilotKeyFlow(_PaidKeyCase):
    """Reuse the actual fake-backed create/update fixture; only own test methods run."""

    def setUp(self):
        super().setUp()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param(llm_common.PARAM_PILOT_PARTNER_IDS, str(self.user.partner_id.id))
        icp.set_param(llm_common.PARAM_PRICING_PROFILE, pricing.PAID_PILOT_PROFILE)

    def _catalogue(self):
        return pricing.normalize_pricing(_source(), profile=pricing.PAID_PILOT_PROFILE)

    def test_create_all_models_uses_explicit_upstream_allowlist(self):
        created = self._create(models=None)
        [token] = self.fake.tokens.values()
        expected = ','.join(item['model'] for item in self._catalogue()['items'])
        self.assertTrue(token['model_limits_enabled'])
        self.assertEqual(token['model_limits'], expected)
        mirror = self.env['woow_paas_platform.llm_api_key'].sudo().browse(created['key_id'])
        self.assertEqual(mirror.model_allowlist, expected)

    def test_update_all_models_keeps_allowlist_and_rejects_outside_catalog(self):
        key = self._management_key()
        self._management_remote()
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            self.service._llm_update_key(self.user.partner_id, key.id, user=self.user,
                                         real_ip=self.real_ip, models=None)
            self.assertEqual(self._error(lambda: self.service._llm_update_key(
                self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
                models=['legacy/unrelated']))[0], 'llm_key_invalid')
        expected = ','.join(item['model'] for item in self._catalogue()['items'])
        self.assertTrue(self.fake.tokens[501]['model_limits_enabled'])
        self.assertEqual(self.fake.tokens[501]['model_limits'], expected)
        self.assertEqual(key.model_allowlist, expected)

    def test_limit_only_edit_narrows_legacy_unrestricted_key(self):
        key = self._management_key(model_allowlist=False)
        self._management_remote()
        self.fake.tokens[501].update(model_limits_enabled=False, model_limits='')
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            self.service._llm_update_key(self.user.partner_id, key.id, user=self.user,
                                         real_ip=self.real_ip, limit_points=100)
        expected = ','.join(item['model'] for item in self._catalogue()['items'])
        self.assertTrue(self.fake.tokens[501]['model_limits_enabled'])
        self.assertEqual(self.fake.tokens[501]['model_limits'], expected)
        self.assertEqual(key.model_allowlist, expected)

    def test_limit_only_edit_rejects_obsolete_explicit_selection_before_io(self):
        key = self._management_key(model_allowlist='legacy/unrelated')
        self._management_remote()
        before = len(self.fake.calls)
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            error = self._error(lambda: self.service._llm_update_key(
                self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
                limit_points=100))
        self.assertEqual(error[0], 'llm_key_invalid')
        self.assertEqual(len(self.fake.calls), before)

    def test_extension_narrows_legacy_unrestricted_key(self):
        key = self._management_key(model_allowlist=False)
        self._management_remote()
        self.fake.tokens[501].update(model_limits_enabled=False, model_limits='')
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            self.service._llm_extend_key(self.user.partner_id, key.id, user=self.user,
                                         real_ip=self.real_ip)
        expected = ','.join(item['model'] for item in self._catalogue()['items'])
        self.assertTrue(self.fake.tokens[501]['model_limits_enabled'])
        self.assertEqual(self.fake.tokens[501]['model_limits'], expected)
        self.assertEqual(key.model_allowlist, expected)

    def test_extension_rejects_obsolete_selection_before_io(self):
        key = self._management_key(model_allowlist='legacy/unrelated')
        self._management_remote()
        before = len(self.fake.calls)
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            error = self._error(lambda: self.service._llm_extend_key(
                self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip))
        self.assertEqual(error[0], 'llm_key_invalid')
        self.assertEqual(len(self.fake.calls), before)

    def test_enable_legacy_unrestricted_key_requires_narrowing_first(self):
        key = self._management_key(model_allowlist=False, state='disabled', disabled_by='user')
        self._management_remote()
        before = len(self.fake.calls)
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            error = self._error(lambda: self.service._llm_set_key_status(
                self.user.partner_id, key.id, True, user=self.user, real_ip=self.real_ip))
        self.assertEqual(error[0], 'llm_key_invalid')
        self.assertEqual(len(self.fake.calls), before)

    def test_enable_checks_remote_scope_and_disable_still_works(self):
        selected = ','.join(item['model'] for item in self._catalogue()['items'])
        key = self._management_key(model_allowlist=selected, state='disabled', disabled_by='user')
        self._management_remote()
        self.fake.tokens[501].update(status=2, model_limits_enabled=False, model_limits='')
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            error = self._error(lambda: self.service._llm_set_key_status(
                self.user.partner_id, key.id, True, user=self.user, real_ip=self.real_ip))
            self.assertEqual(error[0], 'llm_key_invalid')
            self.assertEqual(self.fake.tokens[501]['status'], 2)
            self.fake.tokens[501].update(model_limits_enabled=True, model_limits=selected)
            self.service._llm_set_key_status(self.user.partner_id, key.id, True,
                                             user=self.user, real_ip=self.real_ip)
        self.assertEqual(self.fake.tokens[501]['status'], 1)
        self.env['ir.config_parameter'].sudo().set_param(llm_common.PARAM_PILOT_PARTNER_IDS, False)
        self.service._llm_set_key_status(self.user.partner_id, key.id, False,
                                         user=self.user, real_ip=self.real_ip)
        self.assertEqual(self.fake.tokens[501]['status'], 2)

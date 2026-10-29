"""Authorization-change revocation (the trigger scope of #1707 on top of the REV-01 hold).

Removing a policy is not the only way to take access away.  Every persisted edit that
changes the Access app's *effective authorization semantics* (an allow-list / require /
exclude rule, a decision, precedence, the login methods, the session duration) starts the
same revocation generation as a policy removal: the origin closes, the provider revokes the
app's tokens, a bounded hold follows, and only a re-confirmed Access sync re-opens it.
Cosmetic edits (names, selector spelling, rule order inside a group) never do, and neither
does an edit that provably only *widens* the authorization (a new Allow policy, more addresses
in an Allow policy's include group, one more login method, a longer session): no issued token
can have lost access.

No external network; the operator is the AC1011 fake.  The scenarios run for each of the
three anchor kinds (Security Access route, Smart Home route, Cloud Service).
"""
import contextlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..controllers.paas import PaasController
from ..controllers.security_access import SecurityAccessController
from ..controllers.smart_home import SmartHomeController
from ..models.cloud_service import _internal_cs_access_identity
from ..models.security_access import _internal_identity as _sa_identity
from ..models.smart_home_route import _internal_sm_route_identity
from ..services.paas_operator import PaaSOperatorAPIError
from ..services.access_state import (
    ACCESS_REVOKE_HOLD_PARAM,
    ACCESS_REVOKE_ON_CHANGE_PARAM,
    access_authorization_fingerprint,
    access_authorization_widens_only,
    access_configuration_ready,
    access_mutation_result,
    access_revoke_hold_seconds,
    access_revoke_on_authorization_change,
)
from .test_access_publish_pending_ac1011 import PendingFixture

SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
             'get_paas_operator_client')
ANCHOR_FIELD = {'sa': 'route_id', 'sh': 'smart_home_route_id',
                'cs': 'cloud_service_id'}
#: controller class, its pinned-policy lookup (needs a request) and its reorder method
REORDER = {
    'sa': (SecurityAccessController, '_sa_policy', '_reorder_policies'),
    'sh': (SmartHomeController, '_sh_policy', '_reorder_sh_access_policies'),
    'cs': (PaasController, '_service_policy', '_reorder_service_access_policies'),
}


def _policy(decision='allow', precedence=10, include=None, require=None,
            exclude=None, name='policy'):
    return {
        'name': name, 'decision': decision, 'precedence': precedence,
        'include': [{'rule_type': 'email_list',
                     'rule_value': 'a@example.test,b@example.test'}]
        if include is None else include,
        'require': require or [], 'exclude': exclude or [],
    }


def _payload(**changes):
    base = {
        'enabled': True, 'primary_hostname': 'app.example.test',
        'protected_hostnames': ['app.example.test'], 'session_duration': '24h',
        'login_idp_types': ['google'], 'policies': [_policy()],
        'existing_app_id': 'app-1', 'existing_policy_ids': ['p-1'],
        'revoke_tokens': False, 'app_name': 'app', 'canonical_name': 'canon',
    }
    base.update(changes)
    return base


class TestAuthorizationFingerprint(TransactionCase):
    """The decision function behind the triggers, without any anchor."""

    def fp(self, **changes):
        return access_authorization_fingerprint(_payload(**changes))

    def test_cosmetic_differences_do_not_change_the_fingerprint(self):
        base = self.fp()
        cosmetic = [
            {'policies': [_policy(name='another name')]},
            {'existing_app_id': 'other', 'existing_policy_ids': [],
             'revoke_tokens': True, 'app_name': 'x', 'canonical_name': 'y'},
            {'policies': [_policy(include=[{
                'rule_type': 'email_list',
                'rule_value': ' B@Example.Test , a@example.test,, a@example.test'}])]},
            {'primary_hostname': 'APP.example.test',
             'protected_hostnames': ['App.Example.Test']},
            {'session_duration': None},
            {'login_idp_types': ['google', 'google']},
        ]
        for changes in cosmetic:
            self.assertEqual(self.fp(**changes), base, changes)
        # Order and duplicates inside a group are a set; so are two copies of a rule.
        two = [{'rule_type': 'email_domain', 'rule_value': '@Example.Test'},
               {'rule_type': 'country', 'rule_value': 'tw, jp'}]
        flipped = [{'rule_type': 'country', 'rule_value': 'JP,TW,tw'},
                   {'rule_type': 'email_domain', 'rule_value': 'example.test'},
                   {'rule_type': 'email_domain', 'rule_value': 'EXAMPLE.TEST'}]
        self.assertEqual(self.fp(policies=[_policy(include=two)]),
                         self.fp(policies=[_policy(include=flipped)]))
        everyone = [{'rule_type': 'everyone', 'rule_value': None}]
        noisy = [{'rule_type': 'everyone', 'rule_value': ' whatever '}]
        self.assertEqual(self.fp(policies=[_policy(include=everyone)]),
                         self.fp(policies=[_policy(include=noisy)]))
        # The policy list is a set too (identity is not part of the semantics).
        a, b = _policy(precedence=10), _policy(
            decision='deny', precedence=20, include=[{
                'rule_type': 'email_list', 'rule_value': 'z@example.test'}])
        self.assertEqual(self.fp(policies=[a, b]), self.fp(policies=[b, a]))

    def test_semantic_differences_change_the_fingerprint(self):
        base = self.fp()
        grow = {'rule_type': 'email_list',
                'rule_value': 'a@example.test,b@example.test,c@example.test'}
        shrink = {'rule_type': 'email_list', 'rule_value': 'a@example.test'}
        semantic = {
            'one more address': {'policies': [_policy(include=[grow])]},
            'one address fewer': {'policies': [_policy(include=[shrink])]},
            'decision': {'policies': [_policy(decision='deny')]},
            'precedence': {'policies': [_policy(precedence=5)]},
            'require rule': {'policies': [_policy(require=[
                {'rule_type': 'email_domain', 'rule_value': 'example.test'}])]},
            'exclude rule': {'policies': [_policy(exclude=[
                {'rule_type': 'email_list', 'rule_value': 'a@example.test'}])]},
            'rule moved to exclude': {'policies': [_policy(include=[], exclude=[
                {'rule_type': 'email_list',
                 'rule_value': 'a@example.test,b@example.test'}])]},
            'another policy': {'policies': [_policy(), _policy(
                precedence=20, include=[shrink])]},
            'no policy': {'policies': []},
            'session duration': {'session_duration': '8h'},
            'login method added': {'login_idp_types': ['google', 'onetimepin']},
            'login method swapped': {'login_idp_types': ['onetimepin']},
            'not enforced': {'enabled': False},
            'hostname': {'primary_hostname': 'other.example.test',
                         'protected_hostnames': ['other.example.test']},
        }
        for label, changes in semantic.items():
            self.assertNotEqual(self.fp(**changes), base, label)
        # ... and they differ from each other, so one edit can not hide as another.
        self.assertEqual(len({repr(self.fp(**c)) for c in semantic.values()}),
                         len(semantic))

    def test_only_a_provable_widening_is_exempt(self):
        def widens(before, after):
            return access_authorization_widens_only(
                access_authorization_fingerprint(before),
                access_authorization_fingerprint(after))

        def with_include(*values, **kw):
            return _payload(policies=[_policy(include=[
                {'rule_type': 'email_list', 'rule_value': v} for v in values],
                **kw)])
        base = _payload()
        two = ('a@example.test,b@example.test',)
        wider = {
            'identical': base,
            'one more address in the same rule': with_include(
                'a@example.test,b@example.test,c@example.test'),
            'one more include rule': with_include(*two, 'c@example.test'),
            'a new allow policy': _payload(policies=[_policy(), _policy(
                precedence=20, include=[{'rule_type': 'email_list',
                                         'rule_value': 'z@example.test'}])]),
            'a longer session': _payload(session_duration='168h'),
            'a login method added': _payload(
                login_idp_types=['google', 'onetimepin']),
            'a rename': _payload(policies=[_policy(name='renamed')]),
        }
        for label, after in wider.items():
            self.assertTrue(widens(base, after), label)
        narrower = {
            'an address removed': with_include('a@example.test'),
            'an address replaced': with_include('a@example.test,z@example.test'),
            'a new block policy': _payload(policies=[_policy(), _policy(
                decision='deny', precedence=20, include=[{
                    'rule_type': 'email_list', 'rule_value': 'z@example.test'}])]),
            'a new bypass policy': _payload(policies=[_policy(), _policy(
                decision='bypass', precedence=20, include=[{
                    'rule_type': 'ip_range', 'rule_value': '10.0.0.0/8'}])]),
            'a policy removed': _payload(policies=[]),
            'the decision changed': _payload(policies=[_policy(decision='deny')]),
            'the precedence changed': _payload(policies=[_policy(precedence=5)]),
            'a require rule added': _payload(policies=[_policy(require=[
                {'rule_type': 'email_domain', 'rule_value': 'example.test'}])]),
            'an exclude rule added': _payload(policies=[_policy(exclude=[
                {'rule_type': 'email_list', 'rule_value': 'a@example.test'}])]),
            'a shorter session': _payload(session_duration='8h'),
            'an unknown session': _payload(session_duration='forever'),
            'a login method removed': _payload(login_idp_types=[]),
            'login methods swapped': _payload(login_idp_types=['onetimepin']),
            'not enforced': _payload(enabled=False),
            'another hostname': _payload(
                primary_hostname='other.example.test',
                protected_hostnames=['other.example.test']),
        }
        for label, after in narrower.items():
            self.assertFalse(widens(base, after), label)
        # A block policy may not even widen through its include group, and an
        # empty login-method list ("every provider") is never the smallest set.
        block = _payload(policies=[_policy(decision='deny')])
        more_block = _payload(policies=[_policy(
            decision='deny', include=[{'rule_type': 'email_list',
                                       'rule_value': 'a@example.test,b@example.test,c@example.test'}])])
        self.assertFalse(widens(block, more_block))
        self.assertFalse(widens(_payload(login_idp_types=[]),
                                _payload(login_idp_types=['google'])))
        # Something not enforced before cannot be "widened".
        self.assertFalse(widens(_payload(enabled=False), _payload()))

    def test_the_first_element_says_whether_the_app_is_enforced(self):
        self.assertTrue(self.fp()[0])
        self.assertFalse(self.fp(enabled=False)[0])
        self.assertFalse(self.fp(enabled=None)[0])
        self.assertFalse(access_authorization_fingerprint({})[0])

    def test_the_switch_only_turns_off_on_an_explicit_off_value(self):
        params = self.env['ir.config_parameter'].sudo()
        params.search([('key', '=', ACCESS_REVOKE_ON_CHANGE_PARAM)]).unlink()
        self.assertTrue(access_revoke_on_authorization_change(self.env))
        for raw in ('', '1', 'true', 'yes', 'on', 'maybe', 'disabled', 'None'):
            params.set_param(ACCESS_REVOKE_ON_CHANGE_PARAM, raw)
            self.assertTrue(access_revoke_on_authorization_change(self.env), raw)
        for raw in ('0', 'false', 'FALSE', ' off ', 'No'):
            params.set_param(ACCESS_REVOKE_ON_CHANGE_PARAM, raw)
            self.assertFalse(access_revoke_on_authorization_change(self.env), raw)

    def test_mutation_result_says_what_is_going_on(self):
        def anchors(publish, revoke):
            return [SimpleNamespace(
                access_publish_pending=publish, access_token_revoke_pending=revoke,
                is_protected=True, access_app_id='app', access_protection_lost=False)]
        done = access_mutation_result(anchors(False, False), {'id': 1})
        self.assertTrue(done['success'] and done['saved'])
        self.assertTrue(done['access_configuration_ready'])
        self.assertNotIn('error', done)
        publish = access_mutation_result(anchors(True, False), {'id': 1})
        self.assertFalse(publish['success'])
        self.assertTrue(publish['saved'])
        self.assertIn('Retry', publish['error'])
        held = access_mutation_result(anchors(True, True), {'id': 1})
        self.assertFalse(held['success'])
        self.assertTrue(held['saved'])
        self.assertFalse(held['access_configuration_ready'])
        # Retrying cannot shorten a revocation hold, so the message must not say so.
        self.assertNotIn('Retry', held['error'])
        self.assertIn('revoked', held['error'])


@tagged('post_install', '-at_install')
class TestAuthorizationChangeRevoke(PendingFixture, TransactionCase):
    """One scenario set, run for each of the three anchor kinds."""

    def setUp(self):
        super().setUp()
        self.install_fake()
        self.resources()
        patcher = patch(SA_CLIENT, return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        original = self.client.sync_access_app

        def sync_access_app(payload):
            result = original(payload)
            result['token_revocation'] = (
                'accepted' if payload.get('revoke_tokens') else 'not_required')
            return result
        self.client.sync_access_app = self.good_sync = sync_access_app
        params = self.env['ir.config_parameter'].sudo()
        params.set_param(ACCESS_REVOKE_HOLD_PARAM, '60')
        params.search([('key', '=', ACCESS_REVOKE_ON_CHANGE_PARAM)]).unlink()
        self.hold = access_revoke_hold_seconds(self.env)

    # ---- helpers -----------------------------------------------------------

    def identity(self, kind, anchor):
        return {'sa': _sa_identity, 'sh': _internal_sm_route_identity,
                'cs': _internal_cs_access_identity}[kind](anchor)

    @contextlib.contextmanager
    def edits_do_not_revoke(self):
        """Setup only: build the baseline without the edit triggers."""
        params = self.env['ir.config_parameter'].sudo()
        params.set_param(ACCESS_REVOKE_ON_CHANGE_PARAM, '0')
        try:
            yield
        finally:
            params.search([('key', '=', ACCESS_REVOKE_ON_CHANGE_PARAM)]).unlink()

    def protected_with_two_policies(self, kind):
        anchor = self.anchor(kind)
        self.client.origin = True
        self.assertTrue(anchor.write({'is_protected': True}))
        # Creating Allow policies only adds principals: no revocation.
        policies = self.env['woow_paas_platform.security_access_access_policy'
                            ].create([{
                                ANCHOR_FIELD[kind]: anchor.id,
                                'name': 'allow-%d' % index, 'decision': 'allow',
                                'rule_type': 'email_list',
                                'rule_value': 'user%d@example.test' % index,
                            } for index in (1, 2)])
        anchor.invalidate_recordset()
        self.assertTrue(self.client.origin)
        self.assertFalse(anchor.access_publish_pending)
        self.assertFalse(anchor.access_token_revoke_pending)
        self.client.events.clear()
        return anchor, policies

    def revocations(self):
        return [event for event in self.client.events
                if event[0] == 'access' and event[1].get('revoke_tokens')]

    def syncs(self):
        return [event for event in self.client.events if event[0] == 'access']

    def assert_closed_in_hold(self, anchor):
        anchor.invalidate_recordset()
        self.assertFalse(self.client.origin)
        self.assertTrue(anchor.access_token_revoke_pending)
        self.assertTrue(anchor.access_publish_pending)
        self.assertTrue(anchor.access_token_revoke_accepted_at)
        self.assertFalse(access_configuration_ready(anchor))

    def assert_untouched_by_revocation(self, anchor):
        anchor.invalidate_recordset()
        self.assertTrue(self.client.origin)
        self.assertFalse(anchor.access_token_revoke_pending)
        self.assertFalse(anchor.access_publish_pending)
        self.assertFalse(self.revocations())
        self.assertTrue(access_configuration_ready(anchor))

    # ---- semantic edits revoke ---------------------------------------------

    def rule_value_edit_revokes(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[0].rule_ids.write({'rule_value': 'changed@example.test'})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def decision_edit_revokes(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[0].write({'decision': 'deny'})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def precedence_edit_revokes(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[1].write({'sequence': 5})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def login_method_edit_revokes(self, kind):
        anchor, _policies = self.protected_with_two_policies(kind)
        self.assertTrue(anchor.login_google)
        # Google out, e-mail code in: a user who signed in with Google lost access.
        anchor.write({'login_google': False, 'login_email_otp': True})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def session_duration_edit_revokes(self, kind):
        anchor, _policies = self.protected_with_two_policies(kind)
        anchor.write({'session_duration': '8h'})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def require_rule(self, policy):
        # A require rule on an e-mail domain consumes no seat, so it is valid for
        # every fixture plan.
        return {'policy_id': policy.id, 'group': 'require',
                'rule_type': 'email_domain', 'rule_value': 'example.test'}

    def added_rule_revokes(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        self.env['woow_paas_platform.security_access_access_policy_rule'].create(
            self.require_rule(policies[0]))
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def removed_rule_revokes(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        with self.edits_do_not_revoke():
            extra = self.env['woow_paas_platform.security_access_access_policy_rule'
                             ].create(self.require_rule(policies[0]))
        self.assert_untouched_by_revocation(anchor)
        self.client.events.clear()
        extra.unlink()
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def inline_rule_replace_revokes_once(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[0].write({'rule_ids': [
            (5, 0, 0),
            (0, 0, {'group': 'include', 'rule_type': 'email_list',
                    'rule_value': 'replacement@example.test'}),
            (0, 0, {'group': 'require', 'rule_type': 'email_domain',
                    'rule_value': 'example.test'}),
        ]})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def policy_created_with_inline_rules_revokes_once(self, kind):
        anchor, _policies = self.protected_with_two_policies(kind)
        # A block policy with two include rules: no seat is consumed, and the
        # nested rule creates must not each revoke the app again.
        self.env['woow_paas_platform.security_access_access_policy'].create({
            ANCHOR_FIELD[kind]: anchor.id, 'name': 'block-list',
            'decision': 'deny',
            'rule_ids': [
                (0, 0, {'group': 'include', 'rule_type': 'email_domain',
                        'rule_value': 'blocked.example.test'}),
                (0, 0, {'group': 'include', 'rule_type': 'country',
                        'rule_value': 'KP'}),
            ]})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def reorder(self, kind, anchor, policy_ids):
        """The reorder endpoint writes the sequences with the sync suppressed and
        syncs once at the end, so it decides the revocation itself."""
        controller, finder, method = REORDER[kind]
        Policy = self.env['woow_paas_platform.security_access_access_policy']
        parent = {'sa': lambda: anchor.security_access_id,
                  'sh': lambda: anchor.smart_home_id,
                  'cs': lambda: anchor}[kind]()
        with patch.object(controller, finder,
                          lambda _self, _parent, pid: Policy.browse(pid).exists()):
            return getattr(controller(), method)(
                parent, {'policy_ids': policy_ids})

    def reorder_revokes_only_when_the_order_changes(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        # The fixture creates both policies in one call, so they share a
        # precedence; settle on 10 / 20 first (setup, no revocation).
        with self.edits_do_not_revoke():
            self.assertTrue(self.reorder(
                kind, anchor, [policies[0].id, policies[1].id])['success'])
        self.client.events.clear()
        same = self.reorder(kind, anchor, [policies[0].id, policies[1].id])
        self.assertTrue(same['success'])
        self.assertTrue(self.syncs())
        self.assert_untouched_by_revocation(anchor)
        swapped = self.reorder(kind, anchor, [policies[1].id, policies[0].id])
        # Saved, but the new precedence is only live after the revocation hold.
        self.assertFalse(swapped['success'])
        self.assertTrue(swapped['saved'])
        self.assertEqual(swapped['data']['reordered'],
                         [policies[1].id, policies[0].id])
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    # ---- cosmetic and widening edits do not --------------------------------

    def widening_edits_do_not_revoke(self, kind):
        """Adding principals is the common edit: it must not close the service."""
        anchor, policies = self.protected_with_two_policies(kind)
        Policy = self.env['woow_paas_platform.security_access_access_policy']
        # (Addresses already on the subscription keep the seat count unchanged.)
        Policy.create({
            ANCHOR_FIELD[kind]: anchor.id, 'name': 'another allow',
            'decision': 'allow', 'rule_type': 'email_list',
            'rule_value': 'user1@example.test'})
        self.assert_untouched_by_revocation(anchor)
        policies[0].write({'rule_ids': [(0, 0, {
            'group': 'include', 'rule_type': 'email_list',
            'rule_value': 'user2@example.test'})]})
        self.assert_untouched_by_revocation(anchor)
        anchor.write({'login_email_otp': True})
        self.assert_untouched_by_revocation(anchor)
        anchor.write({'session_duration': '168h'})
        self.assert_untouched_by_revocation(anchor)
        self.assertTrue(self.syncs())  # every one of them still reached the provider

    def explicit_empty_anchors_are_not_record_ids(self, kind):
        """``create`` / ``copy`` vals carry ``False`` for an empty Many2one, and
        ``False`` is an ``int`` in Python: it must not reach ``browse``."""
        anchor, _policies = self.protected_with_two_policies(kind)
        vals = {field: False for field in ANCHOR_FIELD.values()}
        vals.update({ANCHOR_FIELD[kind]: anchor.id, 'name': 'explicit empties',
                     'decision': 'allow', 'rule_type': 'email_list',
                     'rule_value': 'user1@example.test'})
        policy = self.env['woow_paas_platform.security_access_access_policy'
                          ].create(vals)
        self.env['woow_paas_platform.security_access_access_policy_rule'].create({
            'policy_id': policy.id, 'group': 'include', 'rule_type': 'email_list',
            'rule_value': 'user2@example.test'})
        self.assert_untouched_by_revocation(anchor)

    def block_policy_created_revokes(self, kind):
        anchor, _policies = self.protected_with_two_policies(kind)
        self.env['woow_paas_platform.security_access_access_policy'].create({
            ANCHOR_FIELD[kind]: anchor.id, 'name': 'block', 'decision': 'deny',
            'rule_type': 'email_domain', 'rule_value': 'blocked.example.test'})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def name_edit_does_not_revoke(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[0].write({'name': 'renamed'})
        self.assertTrue(self.syncs())  # the provider still learns the new name
        self.assert_untouched_by_revocation(anchor)

    def rule_order_edit_does_not_revoke(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[0].rule_ids.write({'sequence': 99})
        self.assert_untouched_by_revocation(anchor)

    def unchanged_write_does_not_revoke(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        anchor.write({'session_duration': anchor.session_duration or '24h',
                      'login_google': anchor.login_google})
        policies[0].write({'decision': policies[0].decision,
                           'sequence': policies[0].sequence})
        self.assert_untouched_by_revocation(anchor)

    def unprotected_anchor_never_revokes(self, kind):
        anchor = self.anchor(kind)
        self.client.events.clear()
        self.assertFalse(anchor.access_app_id)
        anchor.write({'login_email_otp': True, 'session_duration': '8h'})
        self.env['woow_paas_platform.security_access_access_policy'].create({
            ANCHOR_FIELD[kind]: anchor.id, 'name': 'first', 'decision': 'allow',
            'rule_type': 'email_list', 'rule_value': 'first@example.test'})
        anchor.invalidate_recordset()
        self.assertFalse(self.revocations())
        self.assertFalse(anchor.access_token_revoke_pending)

    # ---- hold interplay, the switch, failures -------------------------------

    def edit_during_the_hold_starts_a_new_generation(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[1].unlink()
        self.assert_closed_in_hold(anchor)
        self.identity(kind, anchor).access_token_revoke_accepted_at = (
            fields.Datetime.now() - timedelta(seconds=self.hold - 10))
        anchor.invalidate_recordset()
        older = anchor.access_token_revoke_accepted_at
        anchor.write({'session_duration': '8h'})
        # The new semantics apply to tokens issued since the first revocation
        # too: a second revocation is requested and the hold restarts.
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 2)
        self.assertGreater(anchor.access_token_revoke_accepted_at, older)

    def cosmetic_edit_during_the_hold_does_not_restart_it(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[1].unlink()
        self.assert_closed_in_hold(anchor)
        accepted_at = anchor.access_token_revoke_accepted_at
        policies[0].write({'name': 'renamed while held'})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(anchor.access_token_revoke_accepted_at, accepted_at)
        self.assertEqual(len(self.revocations()), 1)

    def the_switch_turns_the_edit_triggers_off_but_not_the_removal(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        self.env['ir.config_parameter'].sudo().set_param(
            ACCESS_REVOKE_ON_CHANGE_PARAM, '0')
        policies[0].rule_ids.write({'rule_value': 'changed@example.test'})
        anchor.write({'session_duration': '8h'})
        self.assert_untouched_by_revocation(anchor)
        policies[1].unlink()
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def failed_provider_call_keeps_a_closure_marker(self, kind):
        """The origin is parked before the provider call.  If the call fails the
        edit rolls back, but the parked edge cannot: a durable closure marker must
        remain for the cron to re-open it (and nothing was revoked)."""
        anchor, policies = self.protected_with_two_policies(kind)
        rule = policies[0].rule_ids

        def provider_down(payload):
            raise PaaSOperatorAPIError('provider down', status_code=502)
        self.client.sync_access_app = provider_down
        # Not ``assertRaises``: it rolls the whole block back, including the
        # marker the failure handler writes.  The controllers catch the error and
        # let the request commit; do the same.
        try:
            rule.write({'rule_value': 'changed@example.test'})
        except PaaSOperatorAPIError:
            pass
        else:
            self.fail('the failing provider call must surface')
        rule.invalidate_recordset()
        self.assertEqual(rule.rule_value, 'user1@example.test')  # rolled back
        anchor.invalidate_recordset()
        self.assertFalse(self.client.origin)  # parked before the failing call
        self.assertTrue(anchor.access_publish_pending)
        self.assertFalse(anchor.access_token_revoke_pending)
        self.client.sync_access_app = self.good_sync
        self.env['woow_paas_platform.access_resync_cron'
                 ]._cron_resync_access_protection()
        anchor.invalidate_recordset()
        self.assertFalse(anchor.access_publish_pending)
        self.assertTrue(self.client.origin)
        self.assertEqual(len(self.revocations()), 0)

    # ---- the table: every scenario for every anchor kind ---------------------

    def test_sa_reorder_revokes_only_when_the_order_changes(self):
        self.reorder_revokes_only_when_the_order_changes('sa')

    def test_sh_reorder_revokes_only_when_the_order_changes(self):
        self.reorder_revokes_only_when_the_order_changes('sh')

    def test_cs_reorder_revokes_only_when_the_order_changes(self):
        self.reorder_revokes_only_when_the_order_changes('cs')

    def test_sa_rule_value_edit_revokes(self):
        self.rule_value_edit_revokes('sa')

    def test_sh_rule_value_edit_revokes(self):
        self.rule_value_edit_revokes('sh')

    def test_cs_rule_value_edit_revokes(self):
        self.rule_value_edit_revokes('cs')

    def test_sa_decision_edit_revokes(self):
        self.decision_edit_revokes('sa')

    def test_sh_decision_edit_revokes(self):
        self.decision_edit_revokes('sh')

    def test_cs_decision_edit_revokes(self):
        self.decision_edit_revokes('cs')

    def test_sa_precedence_edit_revokes(self):
        self.precedence_edit_revokes('sa')

    def test_sh_precedence_edit_revokes(self):
        self.precedence_edit_revokes('sh')

    def test_cs_precedence_edit_revokes(self):
        self.precedence_edit_revokes('cs')

    def test_sa_login_method_edit_revokes(self):
        self.login_method_edit_revokes('sa')

    def test_sh_login_method_edit_revokes(self):
        self.login_method_edit_revokes('sh')

    def test_cs_login_method_edit_revokes(self):
        self.login_method_edit_revokes('cs')

    def test_sa_session_duration_edit_revokes(self):
        self.session_duration_edit_revokes('sa')

    def test_sh_session_duration_edit_revokes(self):
        self.session_duration_edit_revokes('sh')

    def test_cs_session_duration_edit_revokes(self):
        self.session_duration_edit_revokes('cs')

    def test_sa_added_rule_revokes(self):
        self.added_rule_revokes('sa')

    def test_sh_added_rule_revokes(self):
        self.added_rule_revokes('sh')

    def test_cs_added_rule_revokes(self):
        self.added_rule_revokes('cs')

    def test_sa_removed_rule_revokes(self):
        self.removed_rule_revokes('sa')

    def test_sh_removed_rule_revokes(self):
        self.removed_rule_revokes('sh')

    def test_cs_removed_rule_revokes(self):
        self.removed_rule_revokes('cs')

    def test_sa_inline_rule_replace_revokes_once(self):
        self.inline_rule_replace_revokes_once('sa')

    def test_sh_inline_rule_replace_revokes_once(self):
        self.inline_rule_replace_revokes_once('sh')

    def test_cs_inline_rule_replace_revokes_once(self):
        self.inline_rule_replace_revokes_once('cs')

    def test_sa_policy_created_with_inline_rules_revokes_once(self):
        self.policy_created_with_inline_rules_revokes_once('sa')

    def test_sh_policy_created_with_inline_rules_revokes_once(self):
        self.policy_created_with_inline_rules_revokes_once('sh')

    def test_cs_policy_created_with_inline_rules_revokes_once(self):
        self.policy_created_with_inline_rules_revokes_once('cs')

    def test_sa_widening_edits_do_not_revoke(self):
        self.widening_edits_do_not_revoke('sa')

    def test_sh_widening_edits_do_not_revoke(self):
        self.widening_edits_do_not_revoke('sh')

    def test_cs_widening_edits_do_not_revoke(self):
        self.widening_edits_do_not_revoke('cs')

    def test_sa_explicit_empty_anchors_are_not_record_ids(self):
        self.explicit_empty_anchors_are_not_record_ids('sa')

    def test_sh_explicit_empty_anchors_are_not_record_ids(self):
        self.explicit_empty_anchors_are_not_record_ids('sh')

    def test_cs_explicit_empty_anchors_are_not_record_ids(self):
        self.explicit_empty_anchors_are_not_record_ids('cs')

    def test_sa_block_policy_created_revokes(self):
        self.block_policy_created_revokes('sa')

    def test_sh_block_policy_created_revokes(self):
        self.block_policy_created_revokes('sh')

    def test_cs_block_policy_created_revokes(self):
        self.block_policy_created_revokes('cs')

    def test_sa_name_edit_does_not_revoke(self):
        self.name_edit_does_not_revoke('sa')

    def test_sh_name_edit_does_not_revoke(self):
        self.name_edit_does_not_revoke('sh')

    def test_cs_name_edit_does_not_revoke(self):
        self.name_edit_does_not_revoke('cs')

    def test_sa_rule_order_edit_does_not_revoke(self):
        self.rule_order_edit_does_not_revoke('sa')

    def test_sh_rule_order_edit_does_not_revoke(self):
        self.rule_order_edit_does_not_revoke('sh')

    def test_cs_rule_order_edit_does_not_revoke(self):
        self.rule_order_edit_does_not_revoke('cs')

    def test_sa_unchanged_write_does_not_revoke(self):
        self.unchanged_write_does_not_revoke('sa')

    def test_sh_unchanged_write_does_not_revoke(self):
        self.unchanged_write_does_not_revoke('sh')

    def test_cs_unchanged_write_does_not_revoke(self):
        self.unchanged_write_does_not_revoke('cs')

    def test_sa_unprotected_anchor_never_revokes(self):
        self.unprotected_anchor_never_revokes('sa')

    def test_sh_unprotected_anchor_never_revokes(self):
        self.unprotected_anchor_never_revokes('sh')

    def test_cs_unprotected_anchor_never_revokes(self):
        self.unprotected_anchor_never_revokes('cs')

    def test_sa_edit_during_the_hold_starts_a_new_generation(self):
        self.edit_during_the_hold_starts_a_new_generation('sa')

    def test_sh_edit_during_the_hold_starts_a_new_generation(self):
        self.edit_during_the_hold_starts_a_new_generation('sh')

    def test_cs_edit_during_the_hold_starts_a_new_generation(self):
        self.edit_during_the_hold_starts_a_new_generation('cs')

    def test_sa_cosmetic_edit_during_the_hold_does_not_restart_it(self):
        self.cosmetic_edit_during_the_hold_does_not_restart_it('sa')

    def test_sh_cosmetic_edit_during_the_hold_does_not_restart_it(self):
        self.cosmetic_edit_during_the_hold_does_not_restart_it('sh')

    def test_cs_cosmetic_edit_during_the_hold_does_not_restart_it(self):
        self.cosmetic_edit_during_the_hold_does_not_restart_it('cs')

    def test_sa_the_switch_turns_the_edit_triggers_off_but_not_the_removal(self):
        self.the_switch_turns_the_edit_triggers_off_but_not_the_removal('sa')

    def test_sh_the_switch_turns_the_edit_triggers_off_but_not_the_removal(self):
        self.the_switch_turns_the_edit_triggers_off_but_not_the_removal('sh')

    def test_cs_the_switch_turns_the_edit_triggers_off_but_not_the_removal(self):
        self.the_switch_turns_the_edit_triggers_off_but_not_the_removal('cs')

    def test_sa_failed_provider_call_keeps_a_closure_marker(self):
        self.failed_provider_call_keeps_a_closure_marker('sa')

    def test_sh_failed_provider_call_keeps_a_closure_marker(self):
        self.failed_provider_call_keeps_a_closure_marker('sh')

    def test_cs_failed_provider_call_keeps_a_closure_marker(self):
        self.failed_provider_call_keeps_a_closure_marker('cs')

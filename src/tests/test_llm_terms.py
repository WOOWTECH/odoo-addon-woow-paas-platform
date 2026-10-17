"""LLM-04：《AI 模型服務條款》的版本與同意流程（DESIGN §2.7、§6.4、§3.2；D10、D15）。

驗收標準（票 LLM-04）在這裡的對照：

1. 沒帶 ``tos_version`` 或帶舊版開通 ⇒ ``llm_tos_required``（:class:`TestLlmTermsConsent`）；前端擋在
   ``tests/ui/test_llm_terms.mjs``。
2. 發佈 ``requires_reaccept=True`` 的新版 ⇒ 建金鑰 ``llm_tos_outdated``、``/api/llm/state`` 的
   ``tos.blocking``；同意後恢復；既有金鑰與 new-api 上的 token 原封不動（:class:`TestLlmTermsHttp`）。
3. 每一次同意都有一筆 ``llm_event``（``tos_accept``：版本＝對象 ``llm_terms``、時間、uid、真實 IP；
   偽造 ``X-Forwarded-For`` 不影響）。
4. ``content_html`` 的 ``<script>``／事件屬性在寫入時就被伺服器端清掉（前端另有 DOMPurify）。

全部是 HttpCase：test mode 下 ``registry.cursor()`` 是同一條交易的 TestCursor，``cr.postcommit.run()``
寫的稽核看得到、也跟著測試回捲（同 ``test_llm_platform_foundation._ConfiguredCase``）。HTTP 請求的
TestCursor commit 時**不跑** postcommit，所以稽核的斷言一律在 service 層做。
"""
import copy
import json
import os
import uuid
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models import llm_common
from ..models.llm_common import LlmError, _llm_internal
from ..models.llm_terms import TERMS_TABLE
from .llm_fake_newapi import FakeNewApi
from .llm_test_support import (
    ACCOUNT,
    EVENT,
    KEY,
    TERMS,
    TEST_TOS,
    LlmClientAdapter,
    configure,
    llm_env,
    publish_test_terms,
    scope_claims,
)

TOS_V2 = 'qa-terms-2'
TOS_V3 = 'qa-terms-3'
CLIENT_IP = '203.0.113.7'


class _TermsCase(HttpCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.password = 'llm-terms-%s' % suffix
        self.alice = new_test_user(
            self.env, login='llm_terms_alice_%s' % suffix, password=self.password,
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        self.bob = new_test_user(
            self.env, login='llm_terms_bob_%s' % suffix, password=self.password,
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        configure(self.env)
        env_patch = patch.dict(os.environ, llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.fake = FakeNewApi()
        LlmClientAdapter(self, self.fake, full_factory=True)
        own_partners = [self.alice.partner_id.id, self.bob.partner_id.id]
        scope_claims(self, lambda env: env[ACCOUNT].sudo().search(
            [('partner_id', 'in', own_partners)]).ids)
        self.Terms = self.env[TERMS].sudo()
        self.v1 = self.Terms.search([('version', '=', TEST_TOS)])

    def _draft(self, version, content='<p>QA terms</p>', requires_reaccept=True):
        return self.Terms.create({'version': version, 'title': 'QA %s' % version,
                                  'content_html': content,
                                  'requires_reaccept': requires_reaccept})

    def _events(self, **domain):
        return self.env[EVENT].sudo().search([(k, '=', v) for k, v in domain.items()])

    def _activate(self, user, version=TEST_TOS, ip=CLIENT_IP):
        return self.env[ACCOUNT]._llm_activate(user.partner_id, version, user, ip)

    def _activate_and_provision(self, user):
        self._activate(user)
        self.env[ACCOUNT]._cron_llm_provision()
        self.env.invalidate_all()
        account = self.env[ACCOUNT]._llm_account_for_partner(user.partner_id)
        self.assertEqual(account.state, 'active', account.last_error_code)
        return account

    def _code(self, call):
        with self.assertRaises(LlmError) as caught:
            call()
        return caught.exception.code, caught.exception.params

    def _tos(self, user):
        return self.env[ACCOUNT]._llm_state_payload(user.partner_id)['account']['tos']


# ====================================================================== #
#  model：版本、發佈、凍結、sanitize、D15                                   #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_terms')
class TestLlmTermsModel(_TermsCase):

    def test_version_must_be_unique_and_well_formed(self):
        for bad in ('', ' x', 'a b', '-lead', 'x' * 65, 'v1/../x', '草稿'):
            with self.subTest(version=bad), self.assertRaises(ValidationError), \
                    self.env.cr.savepoint():
                self._draft(bad)
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError), \
                self.env.cr.savepoint():
            self._draft(TEST_TOS)

    def test_created_as_draft_and_state_only_changes_by_publishing(self):
        with self.assertRaises(AccessError):
            self.Terms.create({'version': 'qa-x', 'title': 'x', 'content_html': '<p>x</p>',
                               'state': 'published'})
        draft = self._draft(TOS_V2)
        self.assertEqual(draft.state, 'draft')
        for vals in ({'state': 'published'}, {'published_at': '2026-10-17 00:00:00'},
                     {'publish_seq': 99}, {'retired_at': '2026-10-17 00:00:00'}):
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                draft.write(vals)
        draft.write({'title': 'still editable'})
        self.assertEqual(draft.title, 'still editable')

    def test_publish_retires_the_previous_version_and_records_an_event(self):
        draft = self._draft(TOS_V2, requires_reaccept=False)
        draft.action_publish()
        self.env.invalidate_all()
        self.assertEqual((self.v1.state, draft.state), ('retired', 'published'))
        self.assertTrue(self.v1.retired_at)
        self.assertTrue(draft.published_at)
        self.assertEqual(draft.publish_seq, self.v1.publish_seq + 1)
        self.assertEqual(self.env[TERMS]._llm_current().version, TOS_V2)
        with self.assertRaises(UserError):
            draft.action_publish()
        self.assertFalse(self._events(action='tos_publish', target_id=draft.id),
                         'commit 之前不寫')
        self.env.cr.postcommit.run()
        event = self._events(action='tos_publish', target_id=draft.id)
        self.assertEqual((len(event), event.target_model, event.uid.id, event.result),
                         (1, TERMS, self.env.uid, 'ok'))
        self.assertEqual(json.loads(event.details_json), {'requires_reaccept': False})

    def test_database_allows_only_one_published_version(self):
        self._draft(TOS_V2).action_publish()
        self.env.flush_all()
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError), \
                self.env.cr.savepoint():
            self.env.cr.execute("UPDATE %s SET state = 'published' WHERE id = %%s" % TERMS_TABLE,
                                (self.v1.id,))

    def test_published_and_retired_versions_are_frozen(self):
        draft = self._draft(TOS_V2)
        draft.action_publish()
        self.env.invalidate_all()
        for record in (self.v1, draft):   # retired、published
            for vals in ({'title': 'changed'}, {'content_html': '<p>changed</p>'},
                         {'version': 'qa-renamed'}, {'requires_reaccept': False}):
                with self.subTest(state=record.state, vals=vals), self.assertRaises(UserError):
                    record.write(vals)
            with self.assertRaises(UserError):
                record.unlink()
        # 改條款＝複製成新的草稿
        clone = draft.copy()
        self.assertEqual((clone.version, clone.state, clone.publish_seq, clone.published_at),
                         (TOS_V2 + '-copy', 'draft', 0, False))
        clone.unlink()
        self.assertFalse(clone.exists())

    def test_content_is_sanitized_on_write(self):
        draft = self._draft(TOS_V2, content=(
            '<p>ok</p><script>alert(1)</script>'
            '<img src="x" onerror="alert(2)">'
            '<a href="javascript:alert(3)">bad</a>'
            '<p style="color:red" onclick="alert(4)">styled</p>'
            '<a href="https://github.com/QuantumNous/new-api" target="_blank" '
            'rel="noopener noreferrer">source</a>'))
        html = str(draft.content_html)
        for forbidden in ('<script', 'alert(1)', 'onerror', 'onclick', 'javascript:', 'style='):
            self.assertNotIn(forbidden, html)
        self.assertIn('href="https://github.com/QuantumNous/new-api"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('<p>ok</p>', html)

    def test_draft_text_is_published_on_staging_only(self):
        draft = self._draft(TOS_V2, content='<p><strong>草稿，尚未生效。</strong></p><p>x【待法務確認 Q1】</p>')
        self.assertTrue(draft.is_draft_text)
        self.assertFalse(self.v1.is_draft_text)
        # 正式站（前綴沒設＝wp、block 0）：不能發佈草稿文字（D15）。
        configure(self.env, prefix=False, block=0)
        with self.assertRaises(UserError):
            draft.action_publish()
        self.assertEqual(draft.state, 'draft')
        # stg（非 wp 前綴）：可以。
        configure(self.env)
        draft.action_publish()
        self.assertEqual(draft.state, 'published')
        state = self.env[ACCOUNT]._llm_state_payload(self.alice.partner_id)
        self.assertEqual((state['tos_current_version'], state['tos_current_draft']),
                         (TOS_V2, True))

    def test_empty_content_cannot_be_published(self):
        draft = self._draft(TOS_V2, content='<p><br></p>')
        with self.assertRaises(UserError):
            draft.action_publish()

    def test_portal_and_internal_users_cannot_read_or_publish(self):
        internal = new_test_user(self.env, login='llm_terms_user_%s' % uuid.uuid4().hex[:6],
                                 groups='base.group_user')
        draft = self._draft(TOS_V2)
        for user in (self.alice, internal):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.env[TERMS].with_user(user).search([])
                with self.assertRaises(AccessError):
                    self.env[TERMS].with_user(user).create(
                        {'version': 'qa-y', 'title': 'y', 'content_html': '<p>y</p>'})
                with self.assertRaises(AccessError):
                    draft.with_user(user).action_publish()
        self.assertEqual(draft.state, 'draft')


# ====================================================================== #
#  同意與 gate（service 層；稽核在這裡驗）                                    #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_terms')
class TestLlmTermsConsent(_TermsCase):

    def test_activation_needs_the_published_version(self):
        partner = self.alice.partner_id
        Account = self.env[ACCOUNT]
        for version in (None, '', '  ', 42, 'draft-0', 'qa-terms-0', TOS_V2):
            with self.subTest(version=version):
                self.assertEqual(self._code(lambda: Account._llm_activate(
                    partner, version, self.alice, CLIENT_IP)),
                    (llm_common.ERR_LLM_TOS_REQUIRED, {'current_version': TEST_TOS}))
        self.assertFalse(Account._llm_account_for_partner(partner))

        account = self._activate(self.alice)
        self.assertEqual((account.tos_version, account.tos_accepted_uid, account.tos_accepted_ip),
                         (TEST_TOS, self.alice, CLIENT_IP))
        self.assertTrue(account.tos_accepted_at)
        self.assertFalse(self._events(account_id=account.id, action='tos_accept'))
        self.env.cr.postcommit.run()
        event = self._events(account_id=account.id, action='tos_accept')
        self.assertEqual(
            (len(event), event.partner_id, event.uid, event.real_ip, event.target_model,
             event.target_id),
            (1, partner, self.alice, CLIENT_IP, TERMS, self.v1.id))
        self.assertTrue(event.create_date)

    def test_no_published_terms_means_nothing_can_be_accepted(self):
        _llm_internal(self.v1).write({'state': 'retired'})
        self.assertEqual(self._code(lambda: self._activate(self.alice))[0],
                         llm_common.ERR_LLM_TOS_UNAVAILABLE)
        state = self.env[ACCOUNT]._llm_state_payload(self.alice.partner_id)
        self.assertEqual((state['tos_current_version'], state['tos_current_draft']), (None, False))

    def test_republished_terms_block_key_creation_until_accepted(self):
        account = self._activate_and_provision(self.alice)
        self.env.cr.postcommit.run()
        Key = self.env[KEY]
        first = Key._llm_create_free_trial_key(self.alice.partner_id, str(uuid.uuid4()),
                                               self.alice, CLIENT_IP)
        tokens = copy.deepcopy(self.fake.tokens)
        publish_test_terms(self.env, TOS_V2, requires_reaccept=True)
        self.assertEqual(self._tos(self.alice), {
            'accepted_version': TEST_TOS, 'current_version': TOS_V2,
            'needs_reaccept': True, 'blocking': True})
        calls = len(self.fake.calls)
        self.assertEqual(
            self._code(lambda: Key._llm_create_free_trial_key(
                self.alice.partner_id, str(uuid.uuid4()), self.alice, CLIENT_IP)),
            (llm_common.ERR_LLM_TOS_OUTDATED, {'current_version': TOS_V2}))
        self.assertEqual(len(self.fake.calls), calls, '條款 gate 在第一次外呼之前')
        # 既有金鑰不受影響：鏡像、清單與 new-api 上的 token 都原封不動。
        mirror = Key.sudo().browse(first['key_id'])
        self.assertEqual(mirror.state, 'active')
        self.assertEqual([k['id'] for k in Key._llm_keys_payload(self.alice.partner_id)['items']],
                         [mirror.id])
        self.assertEqual(self.fake.tokens, tokens)

        Account = self.env[ACCOUNT]
        self.assertEqual(
            self._code(lambda: Account._llm_accept_terms(
                self.alice.partner_id, TEST_TOS, self.alice, CLIENT_IP)),
            (llm_common.ERR_LLM_TOS_OUTDATED, {'current_version': TOS_V2}))
        self.assertEqual(
            self._code(lambda: Account._llm_accept_terms(
                self.alice.partner_id, None, self.alice, CLIENT_IP)),
            (llm_common.ERR_LLM_TOS_REQUIRED, {'current_version': TOS_V2}))
        result = Account._llm_accept_terms(self.alice.partner_id, ' %s ' % TOS_V2, self.alice,
                                           '2001:db8::7')
        self.assertEqual(result, {'tos': {
            'accepted_version': TOS_V2, 'current_version': TOS_V2,
            'needs_reaccept': False, 'blocking': False}})
        self.assertEqual((account.tos_version, account.tos_accepted_ip), (TOS_V2, '2001:db8::7'))
        self.env.cr.postcommit.run()
        accepted = self._events(account_id=account.id, action='tos_accept').sorted('id')
        v2 = self.Terms.search([('version', '=', TOS_V2)])
        self.assertEqual([(e.target_id, e.real_ip, e.uid) for e in accepted],
                         [(self.v1.id, CLIENT_IP, self.alice), (v2.id, '2001:db8::7', self.alice)])
        # gate 放行：接著撞的是「每帳號一把試用金鑰」，不是條款。
        self.assertEqual(
            self._code(lambda: Key._llm_create_free_trial_key(
                self.alice.partner_id, str(uuid.uuid4()), self.alice, CLIENT_IP))[0],
            llm_common.ERR_LLM_FREE_KEY_EXISTS)

    def test_non_blocking_update_only_shows_the_banner(self):
        account = self._activate(self.alice)
        publish_test_terms(self.env, TOS_V2, requires_reaccept=False)
        self.assertEqual(self._tos(self.alice), {
            'accepted_version': TEST_TOS, 'current_version': TOS_V2,
            'needs_reaccept': True, 'blocking': False})
        account._llm_require_current_terms()   # 不擋

    def test_skipped_blocking_version_still_blocks(self):
        account = self._activate(self.alice)
        publish_test_terms(self.env, TOS_V2, requires_reaccept=True)
        publish_test_terms(self.env, TOS_V3, requires_reaccept=False)
        self.assertTrue(self._tos(self.alice)['blocking'],
                        'v2 要求重新同意、v3 不要求：沒同意過 v2 的實質變更，照樣擋')
        self.assertEqual(self._code(account._llm_require_current_terms)[0],
                         llm_common.ERR_LLM_TOS_OUTDATED)
        self.env[ACCOUNT]._llm_accept_terms(self.alice.partner_id, TOS_V3, self.alice, CLIENT_IP)
        self.assertFalse(self._tos(self.alice)['blocking'])

    def test_accepting_after_the_blocking_version_does_not_block(self):
        publish_test_terms(self.env, TOS_V2, requires_reaccept=True)
        self._activate(self.alice, TOS_V2)
        publish_test_terms(self.env, TOS_V3, requires_reaccept=False)
        self.assertEqual(self._tos(self.alice)['blocking'], False)

    def test_unknown_accepted_version_blocks(self):
        """``draft-0``（LLM-01b 的常數，沒有任何條文）：沒有同意過任何條文 ⇒ 有 published 就擋。"""
        account = self._activate(self.alice)
        _llm_internal(account).write({'tos_version': 'draft-0'})
        publish_test_terms(self.env, TOS_V2, requires_reaccept=False)
        self.assertEqual(self._tos(self.alice)['blocking'], True)
        _llm_internal(self.Terms.search([('version', '=', TOS_V2)])).write({'state': 'retired'})
        self.assertEqual(self._tos(self.alice), {
            'accepted_version': 'draft-0', 'current_version': None,
            'needs_reaccept': False, 'blocking': False}, '沒有 published：沒有東西可以同意')

    def test_accepting_again_is_a_no_op(self):
        account = self._activate(self.alice)
        self.env.cr.postcommit.run()
        accepted_at = account.tos_accepted_at
        self.env[ACCOUNT]._llm_accept_terms(self.alice.partner_id, TEST_TOS, self.bob, '198.51.100.1')
        self.env.cr.postcommit.run()
        self.assertEqual((account.tos_accepted_at, account.tos_accepted_uid, account.tos_accepted_ip),
                         (accepted_at, self.alice, CLIENT_IP))
        self.assertEqual(len(self._events(account_id=account.id, action='tos_accept')), 1)

    def test_accept_terms_needs_an_account(self):
        self.assertEqual(self._code(lambda: self.env[ACCOUNT]._llm_accept_terms(
            self.alice.partner_id, TEST_TOS, self.alice, CLIENT_IP))[0],
            llm_common.ERR_LLM_NOT_ACTIVATED)


# ====================================================================== #
#  HTTP：/api/llm/terms、/api/llm/account/accept_terms、真實 IP              #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_terms')
class TestLlmTermsHttp(_TermsCase):

    def _rpc(self, path, params=None, headers=None):
        response = self.url_open(path, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params or {}}),
            headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
        body = response.json()
        self.assertNotIn('error', body, 'JSON-RPC error 而不是結構化回應：%s' % body.get('error'))
        return body['result']

    def _account(self, user):
        account = self.env[ACCOUNT].sudo().search([('partner_id', '=', user.partner_id.id)])
        account.invalidate_recordset()
        return account

    def test_terms_endpoint_serves_published_and_retired_but_not_drafts(self):
        self.authenticate(self.alice.login, self.password)
        current = self._rpc('/api/llm/terms')
        self.assertTrue(current['success'], current)
        self.assertEqual(
            {k: current['data'][k] for k in ('version', 'title', 'content_html', 'state',
                                              'is_draft', 'requires_reaccept')},
            {'version': TEST_TOS, 'title': 'QA terms %s' % TEST_TOS,
             'content_html': '<p>QA terms</p>', 'state': 'published', 'is_draft': False,
             'requires_reaccept': True})
        self.assertRegex(current['data']['published_at'], r'\+00:00$')
        draft = self._draft(TOS_V2)
        for version in (TOS_V2, 'nope', '../x', 7):
            with self.subTest(version=version):
                self.assertEqual(self._rpc('/api/llm/terms', {'version': version})['code'],
                                 'llm_tos_unavailable')
        draft.action_publish()
        retired = self._rpc('/api/llm/terms', {'version': TEST_TOS})['data']
        self.assertEqual((retired['version'], retired['state']), (TEST_TOS, 'retired'))
        self.assertEqual(self._rpc('/api/llm/terms')['data']['version'], TOS_V2)
        _llm_internal(draft).write({'state': 'retired'})
        self.assertEqual(self._rpc('/api/llm/terms')['code'], 'llm_tos_unavailable')

    def test_consent_flow_over_http_records_the_peer_not_forwarded_for(self):
        forged = {'X-Forwarded-For': '198.51.100.66', 'X-Real-IP': '198.51.100.67',
                  'CF-Connecting-IP': '198.51.100.68'}
        self.authenticate(self.alice.login, self.password)
        missing = self._rpc('/api/llm/account/activate', {}, headers=forged)
        self.assertEqual((missing['code'], missing['current_version']),
                         ('llm_tos_required', TEST_TOS))
        self.assertEqual(self._rpc('/api/llm/account/accept_terms', {'tos_version': TEST_TOS})
                         ['code'], 'llm_not_activated')
        activated = self._rpc('/api/llm/account/activate', {'tos_version': TEST_TOS},
                              headers=forged)
        self.assertTrue(activated['success'], activated)
        account = self._account(self.alice)
        self.assertEqual((account.tos_version, account.tos_accepted_uid, account.tos_accepted_ip),
                         (TEST_TOS, self.alice, '127.0.0.1'), '記直接對端，不讀 client 自帶的標頭')
        self.env[ACCOUNT]._cron_llm_provision()
        publish_test_terms(self.env, TOS_V2, requires_reaccept=True)

        state = self._rpc('/api/llm/state')['data']
        self.assertEqual(state['tos_current_version'], TOS_V2)
        self.assertEqual((state['account']['tos']['needs_reaccept'],
                          state['account']['tos']['blocking']), (True, True))
        calls = len(self.fake.calls)
        blocked = self._rpc('/api/llm/keys/create',
                            {'client_request_id': str(uuid.uuid4()), 'kind': 'free_trial'})
        self.assertEqual((blocked['code'], blocked['current_version']),
                         ('llm_tos_outdated', TOS_V2))
        self.assertEqual(len(self.fake.calls), calls)

        stale = self._rpc('/api/llm/account/accept_terms', {'tos_version': TEST_TOS})
        self.assertEqual((stale['code'], stale['current_version']), ('llm_tos_outdated', TOS_V2))
        accepted = self._rpc('/api/llm/account/accept_terms', {'tos_version': TOS_V2},
                             headers=forged)
        self.assertEqual(accepted, {'success': True, 'data': {'tos': {
            'accepted_version': TOS_V2, 'current_version': TOS_V2,
            'needs_reaccept': False, 'blocking': False}}})
        account = self._account(self.alice)
        self.assertEqual((account.tos_version, account.tos_accepted_ip), (TOS_V2, '127.0.0.1'))
        created = self._rpc('/api/llm/keys/create',
                            {'client_request_id': str(uuid.uuid4()), 'kind': 'free_trial'})
        self.assertTrue(created['success'], created)

    def test_accept_terms_only_touches_the_callers_account(self):
        self._activate(self.alice)
        self._activate(self.bob)
        publish_test_terms(self.env, TOS_V2, requires_reaccept=True)
        self.authenticate(self.bob.login, self.password)
        result = self._rpc('/api/llm/account/accept_terms',
                           {'tos_version': TOS_V2, 'partner_id': self.alice.partner_id.id,
                            'account_id': self._account(self.alice).id})
        self.assertTrue(result['success'], result)
        self.assertEqual(self._account(self.bob).tos_version, TOS_V2)
        self.assertEqual(self._account(self.alice).tos_version, TEST_TOS)

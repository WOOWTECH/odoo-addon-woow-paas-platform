"""LLM-01b：AI 模型分頁的 Odoo 後端最小版（DESIGN §1.3.1–1.3.3、§2、§3、§8.2）。

唯一允許 mock 的邊界是代理後面的 new-api（``llm_fake_newapi.FakeNewApi``，換在
``llm_account._llm_client``）；model、cron、獨立 cursor 與 commit 一律跑真的。

* ``TestLlmQuotaInteger``／``TestLlmConfigRegistered``／``TestLlmConfigGuard``：純 DB／設定。
* ``TestLlmUnconfiguredZeroOutbound``（N27）：未設定的 DB 上手動觸發 LLM cron ⇒ 0 外呼、
  0 incident、帳戶不動；連 ``requests`` 都換成「被叫到就失敗」。
* ``TestLlmNeutralize``：``data/neutralize.sql`` 的 LLM 段（密文 sentinel 只改有值的列、
  設定 upsert），以及 sentinel 下 fail closed。
* ``TestLlmTracerHttp``（HttpCase）：portal 使用者經 ``/api/llm/*`` 走完開通 → 開戶 →
  免費試用金鑰 → 用量；他人看不到；冪等；開關；OAuth bearer 進不來；catch-all 不漏字。
  HttpCase 的 ``registry.cursor()`` 是 TestCursor（同一條交易的 savepoint），所以獨立
  cursor 的寫回在這裡看得到——但它測不到「跨連線不卡死」，那是下一個 class 的事。
* ``TestLlmProvisionRealCron``：**真的跑 cron、不 mock commit**——fixture 以真 cursor
  commit，cron 以 ``ir.cron.method_direct_trigger()`` 在另一條真連線上跑；P2 之後、P7
  之後各模擬一次行程被殺，重跑收斂成剛好一個 new-api 使用者、存到的 UP 可用、孤兒 PAT
  最多 1 把。所有列在 cleanup 精確刪掉。
"""
import json
import logging
import os
import time
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from psycopg2 import IntegrityError

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError, ValidationError
from odoo.modules import neutralize
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models import llm_common
from ..models.llm_account import ACCOUNT_TABLE, PROVISION_CRON_XMLID
from ..models.llm_api_key import KEY_TABLE
from ..models.llm_common import _llm_internal
from ..services import llm_crypto
from ..services import newapi_client as nac
from .llm_fake_newapi import FakeNewApi, SimulatedCrash

_logger = logging.getLogger(__name__)

ACCOUNT = 'woow_paas_platform.llm_account'
KEY = 'woow_paas_platform.llm_api_key'
MODULE = 'woow_paas_platform'
STG_PREFIX = 'wsqa'
STG_BLOCK = 7
_SIGNING = 'k1:' + 'A' * 40


def _llm_env():
    """一組測試用的 env：兩組簽章金鑰＋每次新產生的加密金鑰（值只存在這個行程裡）。"""
    return {
        nac.ENV_AUTH_WEB: _SIGNING,
        nac.ENV_AUTH_CRON: 'k2:' + 'B' * 40,
        llm_crypto.ENV_SECRET_KEYS: Fernet.generate_key().decode(),
    }


def _configure(env, show=True):
    icp = env['ir.config_parameter'].sudo()
    icp.set_param(llm_common.PARAM_PROXY_URL, nac.PROXY_URL_STG)
    icp.set_param(llm_common.PARAM_USERNAME_PREFIX, STG_PREFIX)
    icp.set_param(llm_common.PARAM_IP_BLOCK, str(STG_BLOCK))
    icp.set_param(llm_common.PARAM_SHOW_LLM, 'True' if show else 'False')


def _account_class(env):
    return type(env[ACCOUNT])


# ====================================================================== #
#  欄位與設定                                                             #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmQuotaInteger(TransactionCase):
    """〔REV〕30：quota 欄位是 int8；寫入 2^40 讀回相等。"""

    def test_quota_columns_are_int8_and_hold_2_pow_40(self):
        self.env.cr.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = %s AND column_name IN "
            "('quota_cached', 'used_quota_cached', 'request_count_cached')", (ACCOUNT_TABLE,))
        self.assertEqual(dict(self.env.cr.fetchall()), {
            'quota_cached': 'bigint', 'used_quota_cached': 'bigint',
            'request_count_cached': 'bigint'})
        partner = self.env['res.partner'].create({'name': 'llm quota 2^40'})
        account = _llm_internal(self.env[ACCOUNT].sudo()).create({'partner_id': partner.id})
        big = 2 ** 40
        _llm_internal(account).write({'quota_cached': big, 'used_quota_cached': big + 1})
        self.env.flush_all()
        self.env.cr.execute('SELECT quota_cached, used_quota_cached FROM %s WHERE id = %%s'
                            % ACCOUNT_TABLE, (account.id,))
        self.assertEqual(self.env.cr.fetchone(), (big, big + 1))
        account.invalidate_recordset()
        self.assertEqual(account.quota_cached, big)

    def test_plain_integer_is_not_replaced(self):
        """子類別不得取代 ``fields.Integer`` 成為 'integer' 的預設實作。"""
        from odoo import fields
        self.assertIs(fields.MetaField.by_type['integer'], fields.Integer)


@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmConfigRegistered(TransactionCase):
    """本票的設定 key 全部登記在 ``hooks._CONFIG_PARAMETERS``（repo-lint 對這種寫法是盲區）。"""

    def test_llm_params_are_registered_for_uninstall(self):
        from ..hooks import _CONFIG_PARAMETERS
        for key in llm_common.LLM_CONFIG_PARAMETERS:
            self.assertIn(key, _CONFIG_PARAMETERS,
                          '%s 沒進 hooks.py 的 _CONFIG_PARAMETERS ⇒ 卸載後留孤兒紀錄' % key)


@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmConfigGuard(TransactionCase):
    """DESIGN §2.11：``newapi_proxy_url`` 只收叢集內代理位址與黑洞值；讀取時再驗一次。"""

    def setUp(self):
        super().setUp()
        self.icp = self.env['ir.config_parameter'].sudo()

    def test_proxy_url_rejects_external_hosts(self):
        for bad in ('http://evil.example', 'https://newapi.woowtech.io',
                    'http://newapi-proxy.new-api.svc.cluster.local:8081',
                    'http://newapi-proxy.new-api.svc.cluster.local.:8080'):
            with self.assertRaises(ValidationError, msg=bad):
                self.icp.set_param(llm_common.PARAM_PROXY_URL, bad)
        self.assertFalse(self.icp.get_param(llm_common.PARAM_PROXY_URL))

    def test_proxy_url_accepts_allow_list_and_blackhole(self):
        for good in (nac.PROXY_URL_PROD, nac.PROXY_URL_STG, nac.PROXY_URL_BLACKHOLE):
            self.icp.set_param(llm_common.PARAM_PROXY_URL, good)
            self.assertEqual(self.icp.get_param(llm_common.PARAM_PROXY_URL), good)
        # 更新既有那一列也受同一個守衛管
        with self.assertRaises(ValidationError):
            self.icp.set_param(llm_common.PARAM_PROXY_URL, 'http://evil.example')

    def test_prefix_block_and_lists_are_validated(self):
        for key, bad in ((llm_common.PARAM_USERNAME_PREFIX, 'xx'),
                         (llm_common.PARAM_USERNAME_PREFIX, 'wabcd'),
                         (llm_common.PARAM_IP_BLOCK, '32'),
                         (llm_common.PARAM_IP_BLOCK, '-1'),
                         (llm_common.PARAM_PILOT_PARTNER_IDS, '1,abc'),
                         (llm_common.PARAM_CANARY_PARTNER_ID, 'me')):
            with self.assertRaises(ValidationError, msg='%s=%s' % (key, bad)):
                self.icp.set_param(key, bad)
        self.icp.set_param(llm_common.PARAM_USERNAME_PREFIX, 'wsab')
        self.icp.set_param(llm_common.PARAM_IP_BLOCK, '31')
        self.icp.set_param(llm_common.PARAM_PILOT_PARTNER_IDS, '3, 5')
        self.assertEqual(llm_common.llm_config(self.env)['pilot'], frozenset({3, 5}))

    def test_sql_written_bad_url_reads_as_unconfigured(self):
        self.env.cr.execute(
            "INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (llm_common.PARAM_PROXY_URL, 'http://evil.example'))
        self.env.registry.clear_cache()
        self.icp.set_param(llm_common.PARAM_SHOW_LLM, 'True')
        config = llm_common.llm_config(self.env)
        self.assertEqual(config['proxy_state'], 'invalid')
        self.assertTrue(llm_common.llm_unconfigured(self.env, config))
        with self.assertRaises(nac.NewApiConfigError):
            nac.get_newapi_client(self.env, nac.ROLE_CRON, environ=_llm_env())

    def test_prefix_and_block_must_pair(self):
        """prod 固定 wp＋block 0；沙盒前綴沒配 block（預設 0）⇒ identity 不成立、不開戶。"""
        self.icp.set_param(llm_common.PARAM_USERNAME_PREFIX, 'wsab')
        self.assertFalse(llm_common.llm_config(self.env)['identity_ok'])
        self.icp.set_param(llm_common.PARAM_IP_BLOCK, '3')
        self.assertTrue(llm_common.llm_config(self.env)['identity_ok'])

    def test_visibility_follows_show_pilot_and_configuration(self):
        partner = self.env['res.partner'].create({'name': 'llm visibility'})
        self.assertFalse(llm_common.llm_visible(self.env, partner), '未設定 ⇒ 看不到')
        _configure(self.env)
        self.assertTrue(llm_common.llm_visible(self.env, partner))
        self.icp.set_param(llm_common.PARAM_PILOT_PARTNER_IDS, str(partner.id + 100000))
        self.assertFalse(llm_common.llm_visible(self.env, partner), '不在 pilot 名單')
        self.icp.set_param(llm_common.PARAM_PILOT_PARTNER_IDS, str(partner.id))
        self.assertTrue(llm_common.llm_visible(self.env, partner))
        self.icp.set_param(llm_common.PARAM_SHOW_LLM, 'False')
        self.assertFalse(llm_common.llm_visible(self.env, partner))
        self.assertFalse(llm_common.llm_unconfigured(self.env), 'pilot 名單非空 ⇒ cron 照跑')


@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmModelGuards(TransactionCase):
    """DESIGN §2.1：所有群組只讀；sudo 也要內部把手才能寫；身分欄位設定後不能改。"""

    def test_create_and_write_need_the_internal_handle(self):
        partner = self.env['res.partner'].create({'name': 'llm guard'})
        with self.assertRaises(AccessError):
            self.env[ACCOUNT].sudo().create({'partner_id': partner.id})
        with self.assertRaises(AccessError):
            self.env[ACCOUNT].sudo().with_context(woow_llm_internal_write=True).create(
                {'partner_id': partner.id})
        account = _llm_internal(self.env[ACCOUNT].sudo()).create({'partner_id': partner.id})
        with self.assertRaises(AccessError):
            account.write({'state': 'active'})
        _llm_internal(account).write({'newapi_user_id': 41})
        with self.assertRaises(AccessError):
            _llm_internal(account).write({'newapi_user_id': 42})
        with self.assertRaises(AccessError):
            account.unlink()

    def test_portal_and_internal_users_cannot_touch_the_models(self):
        portal = new_test_user(self.env, login='llm_guard_portal_%s' % uuid.uuid4().hex[:6],
                               groups='base.group_portal')
        internal = new_test_user(self.env, login='llm_guard_user_%s' % uuid.uuid4().hex[:6],
                                 groups='base.group_user')
        for user in (portal, internal):
            for model in (ACCOUNT, KEY):
                with self.assertRaises(AccessError, msg='%s %s' % (user.login, model)):
                    self.env[model].with_user(user).search([])
                with self.assertRaises(AccessError):
                    self.env[model].with_user(user).create({})

    def test_one_open_account_per_partner(self):
        partner = self.env['res.partner'].create({'name': 'llm unique'})
        Account = _llm_internal(self.env[ACCOUNT].sudo())
        Account.create({'partner_id': partner.id})
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError), \
                self.env.cr.savepoint():
            Account.create({'partner_id': partner.id})


# ====================================================================== #
#  N27：未設定的 DB 零外呼                                                 #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmUnconfiguredZeroOutbound(TransactionCase):

    def setUp(self):
        super().setUp()
        self.icp = self.env['ir.config_parameter'].sudo()
        partner = self.env['res.partner'].create({'name': 'llm unconfigured'})
        self.account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': partner.id, 'newapi_username': 'wp%010d' % 1})
        env_patch = patch.dict(os.environ, _llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run_all_llm_crons(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('outbound HTTP in an unconfigured DB')
        incident_count = self.env['woow.billing.incident'].sudo().search_count([])
        with patch('requests.sessions.Session.request', side_effect=forbidden) as http, \
                patch.object(_account_class(self.env), '_llm_client') as client:
            self.env.ref(PROVISION_CRON_XMLID).sudo().method_direct_trigger()
            self.env[ACCOUNT]._cron_llm_provision()
        self.assertFalse(client.called, 'cron 建了 client')
        self.assertFalse(http.called)
        self.assertEqual(self.env['woow.billing.incident'].sudo().search_count([]),
                         incident_count)
        self.account.invalidate_recordset()
        self.assertEqual((self.account.state, self.account.provision_attempts,
                          self.account.provision_lease_token),
                         ('provisioning', 0, False))

    def test_no_proxy_url(self):
        self.icp.set_param(llm_common.PARAM_SHOW_LLM, 'True')
        self._run_all_llm_crons()

    def test_blackhole_proxy_url(self):
        self.icp.set_param(llm_common.PARAM_PROXY_URL, nac.PROXY_URL_BLACKHOLE)
        self.icp.set_param(llm_common.PARAM_SHOW_LLM, 'True')
        self._run_all_llm_crons()

    def test_proxy_set_but_nothing_switched_on(self):
        self.icp.set_param(llm_common.PARAM_PROXY_URL, nac.PROXY_URL_PROD)
        self._run_all_llm_crons()

    def test_positive_control_configured_db_does_call_out(self):
        """陽性對照：設定齊全時同一支 cron 確實會建 client（否則上面三格是假綠）。"""
        _configure(self.env)
        self.icp.set_param(llm_common.PARAM_USERNAME_PREFIX, 'wp')
        self.icp.set_param(llm_common.PARAM_IP_BLOCK, '0')
        with patch.object(_account_class(self.env), '_llm_client') as client, \
                patch.object(_account_class(self.env), '_llm_claim_for_provision',
                             return_value=None):
            self.env[ACCOUNT]._cron_llm_provision()
        self.assertTrue(client.called)


# ====================================================================== #
#  neutralize 與 sentinel                                                 #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmNeutralize(TransactionCase):

    def test_neutralize_sql_llm_section(self):
        _configure(self.env)
        self.env['ir.config_parameter'].sudo().set_param(
            llm_common.PARAM_CANARY_PARTNER_ID, '7')
        Account = _llm_internal(self.env[ACCOUNT].sudo())
        live = Account.create({'partner_id': self.env['res.partner'].create({'name': 'n1'}).id})
        empty = Account.create({'partner_id': self.env['res.partner'].create({'name': 'n2'}).id})
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE %s SET newapi_password_enc = %%s, up_token_enc = %%s WHERE id = %%s'
            % ACCOUNT_TABLE, ('gAAAA-live-password', 'gAAAA-live-up', live.id))
        self.env.cr.execute(
            'UPDATE %s SET newapi_password_enc = NULL, up_token_enc = %%s WHERE id = %%s'
            % ACCOUNT_TABLE, ('', empty.id))
        for query in neutralize.get_neutralization_queries([MODULE]):
            self.env.cr.execute(query)
        self.env.cr.execute('SELECT id, newapi_password_enc, up_token_enc FROM %s WHERE id IN %%s'
                            % ACCOUNT_TABLE, ((live.id, empty.id),))
        rows = {r[0]: r[1:] for r in self.env.cr.fetchall()}
        self.assertEqual(rows[live.id], ('neutralized', 'neutralized'))
        self.assertEqual(rows[empty.id], (None, ''), '空值保持空值')
        self.env.cr.execute(
            'SELECT key, value FROM ir_config_parameter WHERE key IN %s',
            ((llm_common.PARAM_PROXY_URL, llm_common.PARAM_SHOW_LLM,
              llm_common.PARAM_CANARY_PARTNER_ID),))
        self.assertEqual(dict(self.env.cr.fetchall()), {
            llm_common.PARAM_PROXY_URL: nac.PROXY_URL_BLACKHOLE,
            llm_common.PARAM_SHOW_LLM: 'False',
            llm_common.PARAM_CANARY_PARTNER_ID: ''})
        self.env.registry.clear_cache()
        self.assertTrue(llm_common.llm_unconfigured(self.env))
        # prod-clone 斷言（assertions.toml E05）的同一條 SQL
        self.env.cr.execute(
            "SELECT count(*) FROM %s WHERE (newapi_password_enc IS NOT NULL AND "
            "newapi_password_enc <> '' AND newapi_password_enc <> 'neutralized') OR "
            "(up_token_enc IS NOT NULL AND up_token_enc <> '' AND up_token_enc <> 'neutralized')"
            % ACCOUNT_TABLE)
        self.assertEqual(self.env.cr.fetchone()[0], 0)

    def test_sentinel_fails_closed(self):
        with patch.dict(os.environ, _llm_env()):
            with self.assertRaises(llm_crypto.LlmSecretNeutralized):
                llm_crypto.decrypt('neutralized', 1, 'wp0000000001', llm_crypto.FIELD_UP_TOKEN)


# ====================================================================== #
#  HTTP：portal 使用者走完 tracer                                          #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmTracerHttp(HttpCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.password = 'llm-tracer-%s' % suffix
        self.alice = new_test_user(
            self.env, login='llm_alice_%s' % suffix, password=self.password,
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        self.bob = new_test_user(
            self.env, login='llm_bob_%s' % suffix, password=self.password,
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        _configure(self.env)
        env_patch = patch.dict(os.environ, _llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.fake = FakeNewApi()
        client_patch = patch.object(_account_class(self.env), '_llm_client',
                                    return_value=self.fake)
        self.client_mock = client_patch.start()
        self.addCleanup(client_patch.stop)

    def _rpc(self, path, params=None):
        response = self.url_open(path, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params or {}}),
            headers={'Content-Type': 'application/json'})
        self._raw = response.text
        body = response.json()
        self.assertNotIn('error', body, 'JSON-RPC error 而不是結構化回應：%s' % body.get('error'))
        return body['result']

    def _account(self, user):
        return self.env[ACCOUNT].sudo().search([('partner_id', '=', user.partner_id.id)])

    def _activate_and_provision(self, user):
        self.authenticate(user.login, self.password)
        result = self._rpc('/api/llm/account/activate', {'tos_version': 'draft-0'})
        self.assertEqual(result, {'success': True, 'data': {'state': 'provisioning'}})
        self.env[ACCOUNT]._cron_llm_provision()
        account = self._account(user)
        account.invalidate_recordset()
        self.assertEqual(account.state, 'active', account.last_error_code)
        return account

    def test_activation_provisioning_and_free_trial_key(self):
        self.authenticate(self.alice.login, self.password)
        state = self._rpc('/api/llm/state')
        self.assertTrue(state['success'])
        self.assertIsNone(state['data']['account'])
        self.assertEqual(self._rpc('/api/llm/keys/list')['code'], 'llm_not_activated')

        self.assertEqual(self._rpc('/api/llm/account/activate', {})['code'], 'llm_tos_required')
        outdated = self._rpc('/api/llm/account/activate', {'tos_version': 'draft-x'})
        self.assertEqual((outdated['code'], outdated['current_version']),
                         ('llm_tos_outdated', 'draft-0'))
        account = self._activate_and_provision(self.alice)
        # 冪等：再按一次不會多一個帳戶
        again = self._rpc('/api/llm/account/activate', {'tos_version': 'draft-0'})
        self.assertEqual(again['data']['state'], 'active')
        self.assertEqual(len(self._account(self.alice)), 1)

        username = '%s%010d' % (STG_PREFIX, account.id)
        self.assertEqual(account.newapi_username, username)
        [user] = [u for u in self.fake.users.values() if u['username'] == username]
        self.assertEqual((user['role'], user['group'], user['quota']), (1, 'default', 0))
        self.assertEqual(account.newapi_user_id, user['id'])
        tenant_ip = nac.tenant_ip(account.id, STG_BLOCK)
        user_calls = {ip for name, ip in self.fake.calls if name.startswith('user_')}
        self.assertEqual(user_calls, {tenant_ip}, '使用者身分一律帶本帳戶的合成 IP')
        self.assertEqual(len(self.fake.pats_of(user['id'])), 1)
        self.assertFalse(any(s['active'] for s in self.fake.sessions.values()), 'P8 撤 session')

        state = self._rpc('/api/llm/state')['data']
        self.assertEqual(state['account']['state'], 'active')
        self.assertEqual(state['account']['balance_points'], 0)

        request_id = str(uuid.uuid4())
        created = self._rpc('/api/llm/keys/create',
                            {'client_request_id': request_id, 'kind': 'free_trial'})
        self.assertTrue(created['success'], created)
        key = created['data']['key']
        self.assertRegex(key, r'^sk-[A-Za-z0-9]{48}$')
        [token] = self.fake.tokens.values()
        self.assertEqual(key, 'sk-' + token['key'])
        self.assertEqual((token['group'], token['remain_quota'], token['unlimited_quota'],
                          token['model_limits']),
                         ('free', 1, False, 'qwen3-4b-aicoder,qwen3-4b-cluster1'))
        mirror = self.env[KEY].sudo().browse(created['data']['key_id'])
        self.assertEqual((mirror.state, mirror.kind, mirror.newapi_token_id),
                         ('active', 'free_trial', token['id']))
        self.assertNotIn(token['key'], json.dumps(mirror.read()[0], default=str),
                         '明文金鑰不進 DB')

        listed = self._rpc('/api/llm/keys/list')['data']['items']
        self.assertEqual([k['id'] for k in listed], [mirror.id])
        self.assertNotIn(token['key'], self._raw, '清單只有遮罩')
        self.assertEqual(listed[0]['masked_key'], 'sk-%s…%s' % (token['key'][:4],
                                                              token['key'][-4:]))

        same = self._rpc('/api/llm/keys/create',
                         {'client_request_id': request_id, 'kind': 'free_trial'})
        self.assertEqual(same['code'], 'llm_key_already_shown')
        second = self._rpc('/api/llm/keys/create',
                           {'client_request_id': str(uuid.uuid4()), 'kind': 'free_trial'})
        self.assertEqual(second['code'], 'llm_free_key_exists')
        self.assertEqual(len(self.fake.tokens), 1)
        paid = self._rpc('/api/llm/keys/create',
                         {'client_request_id': str(uuid.uuid4()), 'kind': 'paid'})
        self.assertEqual((paid['code'], paid['field']), ('llm_key_invalid', 'kind'))

        # 用量：一筆免費試用呼叫；channel 被剝掉、金鑰對回鏡像
        self.fake.add_log(user['id'], token['name'])
        recent = self._rpc('/api/llm/usage/recent')['data']
        self.assertEqual(recent['today_count'], 1)
        self.assertEqual(recent['items'][0]['key_id'], mirror.id)
        self.assertNotIn('channel', recent['items'][0])

    def test_other_partner_sees_nothing_of_alice(self):
        self._activate_and_provision(self.alice)
        self.authenticate(self.alice.login, self.password)
        created = self._rpc('/api/llm/keys/create',
                            {'client_request_id': str(uuid.uuid4()), 'kind': 'free_trial'})
        self.assertTrue(created['success'])

        self.authenticate(self.bob.login, self.password)
        self.assertIsNone(self._rpc('/api/llm/state')['data']['account'])
        self.assertEqual(self._rpc('/api/llm/keys/list')['code'], 'llm_not_activated')
        self.assertEqual(self._rpc('/api/llm/usage/recent')['code'], 'llm_not_activated')
        bob_account = self._activate_and_provision(self.bob)
        self.assertEqual(self._rpc('/api/llm/keys/list')['data']['items'], [])
        self.assertNotEqual(bob_account, self._account(self.alice))
        # Bob 自己的試用金鑰不受 Alice 那把影響
        mine = self._rpc('/api/llm/keys/create',
                         {'client_request_id': str(uuid.uuid4()), 'kind': 'free_trial'})
        self.assertTrue(mine['success'])
        self.assertNotEqual(mine['data']['key_id'], created['data']['key_id'])

    def test_switches_and_session_only(self):
        self.authenticate(self.alice.login, self.password)
        self.env['ir.config_parameter'].sudo().set_param(llm_common.PARAM_SHOW_LLM, 'False')
        for path, params in (('/api/llm/state', {}),
                             ('/api/llm/account/activate', {'tos_version': 'draft-0'}),
                             ('/api/llm/keys/list', {}),
                             ('/api/llm/keys/create', {'client_request_id': str(uuid.uuid4()),
                                                       'kind': 'free_trial'}),
                             ('/api/llm/usage/recent', {})):
            self.assertEqual(self._rpc(path, params)['code'], 'llm_disabled', path)
        self.env['ir.config_parameter'].sudo().set_param(
            llm_common.PARAM_PROXY_URL, nac.PROXY_URL_BLACKHOLE)
        self.env['ir.config_parameter'].sudo().set_param(llm_common.PARAM_SHOW_LLM, 'True')
        self.assertEqual(self._rpc('/api/llm/state')['code'], 'llm_disabled', '未設定')
        self.assertFalse(self.fake.calls)

        # OAuth bearer（HA 元件 / device flow）不是 session：進不來
        self.logout()
        response = self.url_open('/api/llm/state', data=json.dumps(
            {'jsonrpc': '2.0', 'method': 'call', 'params': {}}),
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer some-oauth-access-token'})
        body = response.json()
        self.assertNotIn('result', body)
        self.assertIn('error', body)

    def test_catch_all_never_leaks_exception_text(self):
        secret = 'nap_' + 'Z' * 43
        self.authenticate(self.alice.login, self.password)
        with patch.object(_account_class(self.env), '_llm_state_payload',
                          side_effect=RuntimeError('boom %s Bearer %s' % (secret, secret))), \
                self.assertLogs('odoo.addons.woow_paas_platform.controllers.llm',
                                level=logging.ERROR) as logs:
            result = self._rpc('/api/llm/state')
        self.assertEqual(result['code'], 'llm_internal_error')
        self.assertNotIn(secret, self._raw)
        self.assertNotIn(secret, '\n'.join(logs.output))

    def test_neutralized_secret_fails_closed_without_calls(self):
        self.authenticate(self.alice.login, self.password)
        self._rpc('/api/llm/account/activate', {'tos_version': 'draft-0'})
        account = self._account(self.alice)
        self.env.flush_all()
        self.env.cr.execute('UPDATE %s SET newapi_password_enc = %%s WHERE id = %%s'
                            % ACCOUNT_TABLE, ('neutralized', account.id))
        self.env[ACCOUNT]._cron_llm_provision()
        account.invalidate_recordset()
        self.assertEqual((account.state, account.last_error_code),
                         ('error', 'llm_secret_neutralized'))
        self.assertFalse(self.fake.calls, 'sentinel 下不外呼、不重產密碼')
        self.assertFalse(self.fake.users)

    def test_signing_key_with_trailing_newline_still_signs(self):
        """k8s Secret 用 --from-file 建時常多一個 ``\\n``：strip 後照常可用、錯誤不帶值。"""
        with patch.dict(os.environ, {nac.ENV_AUTH_CRON: _SIGNING + '\n'}):
            kid, secret = nac.parse_signing_key(os.environ[nac.ENV_AUTH_CRON])
        self.assertEqual(kid, 'k1')
        with patch.dict(os.environ, {nac.ENV_AUTH_CRON: 'k1:short\n'}):
            with self.assertRaises(nac.NewApiConfigError) as caught:
                nac.parse_signing_key(os.environ[nac.ENV_AUTH_CRON])
        self.assertNotIn('short', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_controller_never_reads_up_or_ciphertext(self):
        """DESIGN §3.1：UP 一律由 service method 從呼叫者自己的帳戶取；controller 不碰。"""
        import inspect
        from ..controllers import llm as controller
        source = inspect.getsource(controller)
        for forbidden in ('up_token_enc', 'newapi_password_enc', '_llm_up_token',
                          'llm_crypto', '.sudo('):
            self.assertNotIn(forbidden, source)


# ====================================================================== #
#  真的跑 cron（真 cursor、真 commit；不 mock commit）                       #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmProvisionRealCron(TransactionCase):
    """〔REV〕25：搶戶與每一步的寫回都在獨立 cursor，主 cursor 不鎖同一列 ⇒ 不會卡住；
    中途當掉重跑會收斂（DESIGN §1.3.1、§8.2）。

    fixture 以 ``registry.cursor()`` 真的 commit（TransactionCase 自己的 cursor 看不到、
    也不碰這些列），cron 以 ``method_direct_trigger()`` 在另一條真連線上跑。cleanup 只刪
    本 test 建的列、還原本 test 改的設定。
    """

    def setUp(self):
        super().setUp()
        self.tag = uuid.uuid4().hex[:8]
        env_patch = patch.dict(os.environ, _llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.fake = FakeNewApi()
        client_patch = patch.object(_account_class(self.env), '_llm_client',
                                    return_value=self.fake)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.addCleanup(self._purge)
        self._saved_params = {}
        self.partner_id = None
        self.account_id = None
        self._trigger_floor = None
        self._progress_floor = None
        with self.registry.cursor() as cr:
            env = self._env(cr)
            cr.execute('SELECT COALESCE(MAX(id), 0) FROM ir_cron_trigger')
            self._trigger_floor = cr.fetchone()[0]
            self._progress_floor = 0
            cr.execute("SELECT to_regclass('ir_cron_progress')")
            if cr.fetchone()[0]:
                cr.execute('SELECT COALESCE(MAX(id), 0) FROM ir_cron_progress')
                self._progress_floor = cr.fetchone()[0]
            cr.execute("SELECT count(*) FROM %s WHERE state = 'provisioning'" % ACCOUNT_TABLE)
            self.assertEqual(cr.fetchone()[0], 0, '前提：DB 裡沒有別的待開戶帳戶')
            icp = env['ir.config_parameter']
            for key in llm_common.LLM_CONFIG_PARAMETERS:
                self._saved_params[key] = icp.get_param(key)
            _configure(env)
            partner = env['res.partner'].with_context(
                mail_create_nolog=True, mail_create_nosubscribe=True, tracking_disable=True,
            ).create({'name': 'llm real cron %s' % self.tag})
            self.partner_id = partner.id
            account = env[ACCOUNT]._llm_activate(partner, 'draft-0', None, '')
            self.account_id = account.id

    def _env(self, cr):
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, SUPERUSER_ID, {})

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                env = self._env(cr)
                if self.account_id:
                    cr.execute('DELETE FROM %s WHERE account_id = %%s' % KEY_TABLE,
                               (self.account_id,))
                    cr.execute('DELETE FROM %s WHERE id = %%s' % ACCOUNT_TABLE,
                               (self.account_id,))
                if self.partner_id:
                    cr.execute('DELETE FROM res_partner WHERE id = %s', (self.partner_id,))
                cron = env.ref(PROVISION_CRON_XMLID)
                if self._trigger_floor is not None:
                    cr.execute('DELETE FROM ir_cron_trigger WHERE cron_id = %s AND id > %s',
                               (cron.id, self._trigger_floor))
                cr.execute("SELECT to_regclass('ir_cron_progress')")
                if cr.fetchone()[0] and self._progress_floor is not None:
                    cr.execute('DELETE FROM ir_cron_progress WHERE cron_id = %s AND id > %s',
                               (cron.id, self._progress_floor))
                for key, value in self._saved_params.items():
                    cr.execute('DELETE FROM ir_config_parameter WHERE key = %s', (key,))
                    if value is not False and value is not None:
                        cr.execute('INSERT INTO ir_config_parameter (key, value) '
                                   'VALUES (%s, %s)', (key, value))
            self.registry.clear_cache()
        except Exception:  # noqa: BLE001 - cleanup must not mask the test result
            _logger.exception('LLM-01b real-cron fixture cleanup failed (tag=%s)', self.tag)

    def _run_cron(self):
        started = time.monotonic()
        with self.registry.cursor() as cr:
            self._env(cr).ref(PROVISION_CRON_XMLID).method_direct_trigger()
        # 不卡住：獨立 cursor 的 lock_timeout 是 5 秒，一戶的 deadline 60 秒。
        self.assertLess(time.monotonic() - started, 60)

    def _row(self):
        with self.registry.cursor() as cr:
            cr.execute('SELECT state, newapi_user_id, newapi_username, up_token_enc, '
                       'provision_lease_token, last_error_code, provision_attempts '
                       'FROM %s WHERE id = %%s' % ACCOUNT_TABLE, (self.account_id,))
            return cr.dictfetchone()

    def _expire_lease(self):
        """模擬「行程死掉 5 分鐘後」：lease 到期，下一個 tick 可以接手。"""
        with self.registry.cursor() as cr:
            cr.execute("UPDATE %s SET provision_lease_until = (now() at time zone 'UTC') "
                       "- interval '1 minute' WHERE id = %%s" % ACCOUNT_TABLE,
                       (self.account_id,))

    def _assert_converged(self, expected_pats):
        row = self._row()
        self.assertEqual(row['state'], 'active', row['last_error_code'])
        users = [u for u in self.fake.users.values() if u['username'] == row['newapi_username']]
        self.assertEqual(len(users), 1, '剛好一個 new-api 使用者')
        self.assertEqual(row['newapi_user_id'], users[0]['id'])
        up = llm_crypto.decrypt(row['up_token_enc'], self.account_id, row['newapi_username'],
                                llm_crypto.FIELD_UP_TOKEN)
        self.assertEqual(self.fake.user_id_for_up(up), users[0]['id'], '存到的 UP 可用')
        self.assertEqual(len(self.fake.pats_of(users[0]['id'])), expected_pats)
        self.assertIsNone(row['provision_lease_token'])
        self.assertEqual(self.fake.pats[up]['last_used_ip'],
                         nac.tenant_ip(self.account_id, STG_BLOCK))

    def test_cron_provisions_for_real(self):
        self._run_cron()
        self._assert_converged(expected_pats=1)

    def test_crash_after_p2_converges(self):
        self.fake.crash_before['user_login'] = SimulatedCrash
        with self.assertRaises(SimulatedCrash):
            self._run_cron()
        row = self._row()
        self.assertEqual(row['state'], 'provisioning')
        self.assertTrue(row['newapi_user_id'], 'P2 已以獨立 cursor 存下 id（主交易回捲也留得住）')
        self.assertTrue(row['provision_lease_token'], 'lease 還在：行程死掉不會釋放')
        self._run_cron()
        self.assertEqual(self._row()['state'], 'provisioning', 'lease 沒到期前沒有人接手')
        self._expire_lease()
        self._run_cron()
        self._assert_converged(expected_pats=1)
        self.assertEqual([n for n, _ip in self.fake.calls].count('ap_create_user'), 1)

    def test_crash_after_p7_converges_with_at_most_one_orphan(self):
        self.fake.crash_after['user_create_access_token'] = SimulatedCrash
        with self.assertRaises(SimulatedCrash):
            self._run_cron()
        row = self._row()
        self.assertFalse(row['up_token_enc'], 'P7 的回應沒存到')
        self._expire_lease()
        self._run_cron()
        self._assert_converged(expected_pats=2)
        self.assertIn('user_list_access_tokens', [n for n, _ip in self.fake.calls],
                      '重試時 P5 先數 PAT')

    def test_collision_on_retry_is_claimed_by_login(self):
        """P1 回應遺失：重跑時 P1 撞名 → P2 找到 → P4 用存好的密碼確認才存 id。"""
        self.fake.crash_after['ap_create_user'] = SimulatedCrash
        with self.assertRaises(SimulatedCrash):
            self._run_cron()
        self.assertFalse(self._row()['newapi_user_id'])
        self._expire_lease()
        self._run_cron()
        self._assert_converged(expected_pats=1)

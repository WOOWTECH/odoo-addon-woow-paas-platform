"""LLM-01b：AI 模型分頁的 Odoo 後端最小版（DESIGN §1.3.1–1.3.3、§2、§3、§8.2）。

**mock 的邊界是 client（``NewApiClient`` 介面），不是代理後面**：``llm_account._llm_client`` 回
``llm_fake_newapi.FakeNewApi`` 的角色視圖，所以簽章、標頭、query 序列化、§3.4 的分類、deadline
在這裡都沒有執行（那些在 ``tests/test_llm_services.py``，含與代理 ``routetable.py`` 的契約測試）。
這裡真的跑的是 model、cron、獨立 cursor 與 commit，以及 WEB／CRON 金鑰的選擇（adapter 先跑真的
``get_newapi_client``／``signing_key_for_role``，見 ``llm_test_support``）。

**共用 DB**：沙盒 DB 不一定乾淨（LLM 沙盒 ``aimodels`` 的 post-create 已設好代理、前綴、block、
``show_llm``），而且測試行程旁邊有一個正在服務的 Odoo（它的 cron thread 會被 NOTIFY 叫醒）。所以：

* 每個 class 的 setUp 先在測試交易內把 LLM 設定**全部歸零**再設自己要的值；
* 所有會跑 cron 的測試把搶戶**限縮到自己建的帳戶**（``scope_claims``）；
* 「真 cursor、真 commit」的兩個 class（``TestLlmProvisionRealCron``、``TestLlmActivateRace``）
  用 ``CommittedLlmFixture``：先停用 ``ir_cron_llm_provision``（cleanup 還原）、**不 commit 任何
  LLM 設定**（只在本行程 patch）、前綴用代理一定拒絕的 ``wqa``。**前提：這段期間 live server
  的這支 cron 是停住的**——測試行程被硬殺時要手動還原（``python3 .sandbox/llm-acceptance.py --slug <slug> cron-resume``）。

Class 一覽：

* ``TestLlmQuotaInteger``／``TestLlmConfigRegistered``／``TestLlmConfigGuard``／
  ``TestLlmModelGuards``／``TestLlmCronContract``：純 DB／設定／靜態契約。
* ``TestLlmUnconfiguredZeroOutbound``（N27）：未設定 ⇒ cron 零外呼、**零搶戶**。
* ``TestLlmSigningKeyFailClosed``（AC7）：尾端空白 strip 後照常；中間夾 ``\\n``／太短 ⇒ cron 在
  搶戶之前 fail closed、log 只有原因碼。
* ``TestLlmNeutralize``：``data/neutralize.sql`` 的 LLM 段與 sentinel。
* ``TestLlmTracerHttp``（HttpCase）：portal 使用者走完開通 → 開戶 → 試用金鑰 → 用量；他人看不到；
  開關；有效的 OAuth token 也進不來；catch-all 不漏字；WEB 金鑰壞掉；列表失敗不重送；P8 一定撤。
* ``TestLlmWoowSessionFlag``（HttpCase）：``/woow`` 的 ``woow_show_llm`` 隨設定改變。
* ``TestLlmProvisionRealCron``：真 cursor 跑 cron；P2／P7 之後當掉會收斂；只搶自己的帳戶；別人
  拿著列鎖時那一步失敗、不卡死。
* ``TestLlmActivateRace``：兩條真連線同時開通同一個 partner。
"""
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.modules import neutralize
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models import llm_common
from ..models.llm_account import ACCOUNT_TABLE, PROVISION_CRON_XMLID, _LeaseLost
from ..models.llm_api_key import _STALE_CREATING_SECONDS, KEY_TABLE
from ..models.llm_common import LlmError, _llm_internal
from ..models.llm_event import LlmAlert, LlmIncident
from ..services import llm_crypto
from ..services import newapi_client as nac
from .llm_fake_newapi import FakeNewApi, SimulatedCrash
from .llm_test_support import (
    ACCOUNT,
    EVENT,
    KEY,
    TEST_BLOCK,
    TEST_PREFIX,
    TEST_TOS,
    CommittedLlmFixture,
    LlmClientAdapter,
    configure,
    env_for,
    llm_env,
    reset_llm_config,
    scope_claims,
    scope_swr_queue,
)

MODULE = 'woow_paas_platform'
TOS = TEST_TOS
ACCOUNT_LOGGER = 'odoo.addons.woow_paas_platform.models.llm_account'
KEY_LOGGER = 'odoo.addons.woow_paas_platform.models.llm_api_key'
CONTROLLER_LOGGER = 'odoo.addons.woow_paas_platform.controllers.llm'
#: cron 路徑（CRON 金鑰）與 web 路徑（WEB 金鑰）各自會叫的 client 方法。LLM-02 起 usage/recent 是
#: SWR：``user_log_self`` 由背景刷新 cron 以 CRON 金鑰叫（端點本身不外呼）。
CRON_ONLY_METHODS = ('ap_create_user', 'ap_search_user', 'user_login', 'user_list_access_tokens',
                     'user_verify_generate', 'user_create_access_token', 'user_delete_session',
                     'user_log_self')
WEB_PATH_METHODS = ('user_create_token', 'user_list_tokens', 'user_reveal_token')


def _broken_signing_value(kid):
    """strip 之後仍不合格（中間夾 ``\\n``）的簽章金鑰值，與它的兩段秘密（斷言 log／回應不含）。"""
    first, second = secrets.token_hex(12), secrets.token_hex(12)
    return '%s:%s\n%s' % (kid, first, second), (first, second)


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

    def test_state_payload_carries_the_integer_quota(self):
        """LLM-01c review 10：``balance_points`` 是捨去後的整數點數，前端要顯示 2 位小數
        （12.99 點，不是假的 12.00）就得拿整數 quota；透支時是負的 quota。"""
        partner = self.env['res.partner'].create({'name': 'llm quota payload'})
        account = _llm_internal(self.env[ACCOUNT].sudo()).create({'partner_id': partner.id})
        per_point = llm_common.QUOTA_PER_POINT
        _llm_internal(account).write({'quota_cached': 12 * per_point + per_point - 1})
        dto = self.env[ACCOUNT]._llm_state_payload(partner)['account']
        self.assertEqual((dto['balance_quota'], dto['balance_points'], dto['overdraft_points']),
                         (13 * per_point - 1, 12, 0))
        _llm_internal(account).write({'quota_cached': -1})
        dto = self.env[ACCOUNT]._llm_state_payload(partner)['account']
        self.assertEqual((dto['balance_quota'], dto['balance_points'], dto['overdraft_points']),
                         (-1, 0, 1))

    def test_plain_integer_is_not_replaced(self):
        """子類別不得取代 ``fields.Integer`` 成為 'integer' 的預設實作。"""
        from odoo import fields as odoo_fields
        self.assertIs(odoo_fields.MetaField.by_type['integer'], odoo_fields.Integer)
        self.assertEqual(self.env[ACCOUNT]._fields['quota_cached'].column_type, ('int8', 'int8'))
        self.assertEqual(self.env[ACCOUNT]._fields['provision_attempts'].column_type,
                         ('int4', 'int4'))


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
        reset_llm_config(self.env)

    def test_orders_switch_default_validation_and_fingerprint(self):
        from ..hooks import _CONFIG_PARAMETERS
        key = llm_common.PARAM_ORDERS_ENABLED
        self.assertIn(key, llm_common.LLM_CONFIG_PARAMETERS)
        self.assertIn(key, _CONFIG_PARAMETERS)
        self.assertNotIn(key, llm_common.CONFIG_CHANGE_CODES)
        self.assertTrue(llm_common.llm_config(self.env)['orders_enabled'])
        configure(self.env)
        fingerprint = llm_common.config_fingerprint(self.env)
        for value, enabled in (('False', False), ('0', False), ('True', True), ('1', True)):
            self.icp.set_param(key, value)
            config = llm_common.llm_config(self.env)
            self.assertIs(config['orders_enabled'], enabled)
            self.assertFalse(llm_common.llm_unconfigured(self.env, config),
                             'Paid orders must continue when new operations stop')
            self.assertEqual(llm_common.config_fingerprint(self.env), fingerprint)
        self.icp.set_param(key, False)
        self.assertTrue(llm_common.llm_config(self.env)['orders_enabled'])
        with self.assertRaises(ValidationError):
            self.icp.set_param(key, 'maybe')

    def test_service_admin_id_is_optional_validated_and_fingerprinted(self):
        """LLM-08 never trusts a log's actor as its own proof anchor."""
        from ..hooks import _CONFIG_PARAMETERS
        key = llm_common.PARAM_SERVICE_ADMIN_ID
        self.assertIn(key, llm_common.LLM_CONFIG_PARAMETERS)
        self.assertIn(key, _CONFIG_PARAMETERS)
        self.assertIn(key, llm_common.CONFIG_CHANGE_CODES)
        self.assertIsNone(llm_common.llm_config(self.env)['service_admin_id'])
        before = llm_common.config_fingerprint(self.env)
        self.icp.set_param(key, '7')
        self.assertEqual(llm_common.llm_config(self.env)['service_admin_id'], 7)
        self.assertNotEqual(llm_common.config_fingerprint(self.env), before)
        self.icp.set_param(key, str(nac.INT32_MAX))
        self.assertEqual(llm_common.llm_config(self.env)['service_admin_id'], nac.INT32_MAX)
        for bad in ('0', '-1', '1.5', 'True', str(nac.INT32_MAX + 1)):
            with self.assertRaises(ValidationError, msg=bad):
                self.icp.set_param(key, bad)
        # Direct SQL must not turn a malformed trust anchor into usable evidence.
        self.env.cr.execute(
            "INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, str(nac.INT32_MAX + 1)))
        self.env.registry.clear_cache()
        self.assertIsNone(llm_common.llm_config(self.env)['service_admin_id'])

    def test_sql_written_invalid_orders_switch_fails_closed(self):
        self.env.cr.execute(
            "INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (llm_common.PARAM_ORDERS_ENABLED, 'maybe'))
        self.env.registry.clear_cache()
        self.assertFalse(llm_common.llm_config(self.env)['orders_enabled'])

    def test_proxy_url_rejects_external_hosts(self):
        for bad in ('http://evil.example', 'https://newapi.woowtech.io',
                    'http://newapi-proxy.new-api.svc.cluster.local:8081',
                    'http://newapi-proxy.new-api.svc.cluster.local.:8080'):
            with self.assertRaises(ValidationError, msg=bad):
                self.icp.set_param(llm_common.PARAM_PROXY_URL, bad)
        self.assertFalse(self.icp.get_param(llm_common.PARAM_PROXY_URL))

    def test_proxy_url_accepts_allow_list_and_blackhole(self):
        self.icp.set_param(llm_common.PARAM_PROXY_URL, nac.PROXY_URL_STG)
        old_fingerprint = llm_common.config_fingerprint(self.env)
        self.icp.set_param(llm_common.PARAM_PROXY_URL, nac.PROXY_URL_LLMDEV)
        self.assertEqual(llm_common.llm_config(self.env)['proxy_state'], 'usable')
        self.assertNotEqual(llm_common.config_fingerprint(self.env), old_fingerprint)
        for good in (nac.PROXY_URL_PROD, nac.PROXY_URL_STG,
                     nac.PROXY_URL_LLMDEV, nac.PROXY_URL_BLACKHOLE):
            self.icp.set_param(llm_common.PARAM_PROXY_URL, good)
            self.assertEqual(self.icp.get_param(llm_common.PARAM_PROXY_URL), good)
        self.assertTrue(llm_common.llm_unconfigured(self.env))
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
            nac.get_newapi_client(self.env, nac.ROLE_CRON, environ=llm_env())

    def test_prefix_and_block_must_pair(self):
        """prod 固定 wp＋block 0；沙盒前綴沒配 block（預設 0）⇒ identity 不成立、不開戶。"""
        self.assertTrue(llm_common.llm_config(self.env)['identity_ok'], '歸零後＝wp＋0')
        self.icp.set_param(llm_common.PARAM_USERNAME_PREFIX, 'wsab')
        self.assertFalse(llm_common.llm_config(self.env)['identity_ok'])
        self.icp.set_param(llm_common.PARAM_IP_BLOCK, '3')
        self.assertTrue(llm_common.llm_config(self.env)['identity_ok'])
        self.icp.set_param(llm_common.PARAM_USERNAME_PREFIX, False)
        self.assertFalse(llm_common.llm_config(self.env)['identity_ok'], 'wp 配 block 3 也不行')

    def test_visibility_follows_show_pilot_and_configuration(self):
        partner = self.env['res.partner'].create({'name': 'llm visibility'})
        self.assertFalse(llm_common.llm_visible(self.env, partner), '未設定 ⇒ 看不到')
        configure(self.env)
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
        self.assertFalse(llm_common._is_llm_internal(account.env), 'create 回傳的記錄不帶把手')
        with self.assertRaises(AccessError):
            account.write({'state': 'active'})
        _llm_internal(account).write({'newapi_user_id': 41})
        with self.assertRaises(AccessError):
            _llm_internal(account).write({'newapi_user_id': 42})
        with self.assertRaises(AccessError):
            account.unlink()

    def test_activate_returns_a_record_without_the_internal_handle(self):
        """finding 7：``_llm_activate`` 回的帳戶（與從它走到的東西）過不了 create／write 守衛。"""
        configure(self.env)
        partner = self.env['res.partner'].create({'name': 'llm handle'})
        account = self.env[ACCOUNT]._llm_activate(partner, TOS, None, '')
        self.assertFalse(llm_common._is_llm_internal(account.env))
        with self.assertRaises(AccessError):
            account.write({'state': 'active'})
        with self.assertRaises(AccessError):
            account.api_key_ids.create({'account_id': account.id, 'kind': 'free_trial'})

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

    def test_controller_never_reads_up_or_ciphertext(self):
        """DESIGN §3.1：UP 一律由 service method 從呼叫者自己的帳戶取；controller 不碰。"""
        import inspect
        from ..controllers import llm as controller
        source = inspect.getsource(controller)
        for forbidden in ('up_token_enc', 'newapi_password_enc', '_llm_up_token',
                          'llm_crypto', '.sudo('):
            self.assertNotIn(forbidden, source)


@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmCronContract(TransactionCase):
    """finding 18：``llm_cron.xml`` 是 noupdate，發版的 ``-u`` 不會改 DB 裡的 code。

    方法名與所在 model 是凍結契約；這裡斷言**已安裝 DB** 裡的值（不是 XML）。LLM-02b 起逐支看
    ``llm_common.LLM_CRONS``，另外反查：DB 裡掛在 LLM model 上的 cron 都要在登記表裡。
    """

    def test_installed_cron_calls_the_current_method_as_superuser(self):
        self.assertIn(PROVISION_CRON_XMLID, llm_common.LLM_CRONS)
        for xmlid, (model, method) in sorted(llm_common.LLM_CRONS.items()):
            with self.subTest(cron=xmlid):
                cron = self.env.ref(xmlid)
                self.assertEqual(cron.model_id.model, model)
                self.assertEqual(cron.code.strip(), 'model.%s()' % method)
                self.assertTrue(callable(getattr(type(self.env[model]), method, None)))
                # LLM model 沒有任何群組的 write ACL；ir.actions.server.run() 對非 superuser 會先
                # check_access('write') ⇒ cron 的執行者必須是 superuser。
                self.assertEqual(cron.user_id.id, 1)
                if xmlid == 'woow_paas_platform.ir_cron_llm_status_monitor':
                    self.assertFalse(cron.active, 'LLM-14 review slice must remain inactive')
                else:
                    self.assertTrue(cron.active,
                                    '_trigger() 對停用的 cron 不建立立即觸發（被測試停住沒還原？）')

    def test_every_llm_cron_is_registered(self):
        registered = {self.env.ref(xmlid).id for xmlid in llm_common.LLM_CRONS}
        on_llm_models = self.env['ir.cron'].with_context(active_test=False).sudo().search(
            [('model_id.model', '=like', 'woow_paas_platform.llm_%')])
        self.assertEqual(set(on_llm_models.ids), registered,
                         '新的 LLM cron 要登記在 llm_common.LLM_CRONS（凍結契約與零外呼的測試以它為準）')


# ====================================================================== #
#  N27：未設定的 DB 零外呼、零搶戶                                          #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmUnconfiguredZeroOutbound(TransactionCase):

    def setUp(self):
        super().setUp()
        self.icp = self.env['ir.config_parameter'].sudo()
        reset_llm_config(self.env)
        partner = self.env['res.partner'].create({'name': 'llm unconfigured'})
        self.account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': partner.id, 'newapi_username': 'wp%010d' % 1})
        env_patch = patch.dict(os.environ, llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run_all_llm_crons(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('outbound HTTP in an unconfigured DB')
        incident_count = self.env['woow.billing.incident'].sudo().search_count([])
        event_count = self.env[EVENT].sudo().search_count([])
        mail_count = self.env['mail.mail'].sudo().search_count([])
        Account = type(self.env[ACCOUNT])
        Event = type(self.env[EVENT])
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        State = type(self.env['woow_paas_platform.llm_runtime_state'])
        with patch('requests.sessions.Session.request', side_effect=forbidden) as http, \
                patch.object(Account, '_llm_client') as client, \
                patch.object(Account, '_llm_claim_for_provision',
                             side_effect=AssertionError('claimed an account')) as claim, \
                patch.object(Account, '_llm_balance_refresh_tick',
                             side_effect=AssertionError('balance refresh ran')) as refresh, \
                patch.object(Event, '_llm_event_cleanup_tick',
                             side_effect=AssertionError('event cleanup ran')) as cleanup, \
                patch.object(Event, '_llm_alert_delivery_tick',
                             side_effect=AssertionError('alert delivery ran')) as delivery, \
                patch.object(Cache, '_llm_swr_refresh_tick',
                             side_effect=AssertionError('swr refresh ran')) as swr, \
                patch.object(State, '_llm_connection_test_tick',
                             side_effect=AssertionError('connection test ran')) as probe, \
                patch.object(State, '_llm_circuit_gate',
                             side_effect=AssertionError('circuit gate ran')) as gate:
            # 「手動觸發」＝跑 cron 的 server action（與 method_direct_trigger 同一段 code、同一
            # 個執行者），但不經 _try_lock：live server 剛好在跑同一支 cron 時不會 UserError。
            for xmlid, (model, method) in sorted(llm_common.LLM_CRONS.items()):
                cron = self.env.ref(xmlid).sudo()
                cron.ir_actions_server_id.with_user(cron.user_id).run()
                getattr(self.env[model], method)()
            # 告警與 incident helper 本身也是第二道（N27）：未設定時什麼都不寫。
            self.assertFalse(self.env[EVENT]._llm_alert(LlmAlert.TEST))
            self.assertFalse(self.env[EVENT]._llm_incident(
                LlmIncident.ACCOUNT_ERROR, account_id=self.account.id,
                error_code=llm_common.ERR_LLM_INTERNAL))
        self.assertFalse(client.called, 'cron 建了 client')
        self.assertFalse(claim.called, 'cron 搶了戶')
        self.assertFalse(refresh.called, '未設定時餘額刷新不能跑')
        self.assertFalse(cleanup.called, '未設定時清理也不跑（所有 LLM cron 一致）')
        self.assertFalse(delivery.called, '未設定時 webhook 也不送')
        self.assertFalse(swr.called, '未設定時 SWR 刷新不跑（LLM-02）')
        self.assertFalse(probe.called, '未設定時測試連線不跑（LLM-02）')
        self.assertFalse(gate.called, '未設定時連 circuit 的健康檢查都不做')
        self.assertFalse(http.called)
        self.assertEqual(self.env['woow.billing.incident'].sudo().search_count([]),
                         incident_count)
        self.assertEqual(self.env[EVENT].sudo().search_count([]), event_count, '0 筆 llm_event')
        self.assertEqual(self.env['mail.mail'].sudo().search_count([]), mail_count, '0 封信')
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
        """陽性對照：設定齊全時同一支 cron 確實會建 client、也會去搶戶（否則上面三格是假綠）。"""
        configure(self.env, prefix='wp', block=0)
        Account = type(self.env[ACCOUNT])
        Event = type(self.env[EVENT])
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        State = type(self.env['woow_paas_platform.llm_runtime_state'])
        # circuit 的閘門讀的是共用 DB（另一條連線）裡 live server 的狀態：這裡固定成「關著」。
        with patch.object(llm_common, 'circuit_allows', return_value=True) as gate, \
                patch.object(Account, '_llm_client') as client, \
                patch.object(Account, '_llm_claim_for_provision', return_value=None) as claim, \
                patch.object(Account, '_llm_balance_refresh_tick', return_value=False) as refresh, \
                patch.object(Event, '_llm_event_cleanup_tick', return_value=False) as cleanup, \
                patch.object(Event, '_llm_alert_delivery_tick', return_value=False) as delivery, \
                patch.object(Cache, '_llm_swr_refresh_tick', return_value=False) as swr, \
                patch.object(State, '_llm_connection_test_tick', return_value=False) as probe:
            self.env[ACCOUNT]._cron_llm_provision()
            self.env[ACCOUNT]._cron_llm_balance_refresh()
            self.env[EVENT]._cron_llm_event_cleanup()
            self.env[EVENT]._cron_llm_alert_delivery()
            self.env['woow_paas_platform.llm_swr_cache']._cron_llm_swr_refresh()
            self.env['woow_paas_platform.llm_runtime_state']._cron_llm_connection_test()
        client.assert_called_once_with(nac.ROLE_CRON)
        claim.assert_called_once()
        refresh.assert_called_once()
        cleanup.assert_called_once()
        delivery.assert_called_once()
        swr.assert_called_once()
        probe.assert_called_once()
        # 外呼類 cron（開戶、餘額刷新、SWR）才過 circuit 的閘門；清理、webhook、測試連線不看它。
        self.assertEqual(gate.call_count, 3)


# ====================================================================== #
#  AC7：簽章金鑰 env 的 strip 與 fail closed                                #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmSigningKeyFailClosed(TransactionCase):
    """AC7 的解讀（finding 11）：尾端空白（k8s Secret ``--from-file`` 常多一個 ``\\n``）＝strip
    後照常可用；strip 後仍不合格（中間夾 ``\\n``、secret 不到 32 字元）＝fail closed——cron 在
    **搶戶之前**就停、零外呼、帳戶不動，log 只有原因碼。WEB 那一半在 ``TestLlmTracerHttp``。
    """

    def setUp(self):
        super().setUp()
        configure(self.env)
        env_patch = patch.dict(os.environ, llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.fake = FakeNewApi()
        self.client = LlmClientAdapter(self, self.fake, full_factory=True)
        partner = self.env['res.partner'].create({'name': 'llm signing key'})
        self.account = self.env[ACCOUNT]._llm_activate(partner, TOS, None, '')
        # TransactionCase：告警 helper 的獨立 cursor 是真的第二條連線，會真的 commit 進共用 DB。
        # 這裡只斷言「有告警、碼對」（告警本身在 test_llm_platform_foundation 驗）。
        alert = patch.object(type(self.env[EVENT]), '_llm_alert', return_value=False)
        self.alert = alert.start()
        self.addCleanup(alert.stop)

    def test_trailing_newline_is_stripped_and_still_signs(self):
        good = os.environ[nac.ENV_AUTH_CRON]
        kid, secret = nac.parse_signing_key(good + '\n')
        self.assertEqual((kid, secret), nac.parse_signing_key(good))
        with patch.dict(os.environ, {nac.ENV_AUTH_CRON: good + '\n'}), \
                patch.object(type(self.env[ACCOUNT]), '_llm_claim_for_provision',
                             return_value=None) as claim:
            self.env[ACCOUNT]._cron_llm_provision()
        claim.assert_called_once()
        self.assertEqual(self.client.roles_used(), [nac.ROLE_CRON])
        self.assertFalse(self.alert.called)

    def test_malformed_cron_key_fails_closed_before_claiming(self):
        embedded, parts = _broken_signing_value('kcron')
        short = 'kcron:%s' % secrets.token_hex(8)
        for bad, secret_parts in ((embedded, parts), (short, (short.split(':', 1)[1],))):
            with patch.dict(os.environ, {nac.ENV_AUTH_CRON: bad}), \
                    patch.object(type(self.env[ACCOUNT]), '_llm_claim_for_provision',
                                 side_effect=AssertionError('claimed an account')) as claim, \
                    self.assertLogs('odoo.addons.woow_paas_platform', level=logging.DEBUG) as logs:
                self.env[ACCOUNT]._cron_llm_provision()
            output = '\n'.join(logs.output)
            self.assertIn('signing_key_invalid', output)
            for part in secret_parts:
                self.assertNotIn(part, output)
            self.assertFalse(claim.called)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.client.roles_used(), [nac.ROLE_CRON, nac.ROLE_CRON])
        # 設定類的 fail closed 一律告警（審查 LLM-02b finding 2）；碼是封閉的，不帶值。
        self.assertEqual(
            [(c.args, c.kwargs) for c in self.alert.call_args_list],
            [((LlmAlert.CONFIG_UNAVAILABLE,),
              {'error_code': llm_common.CONFIG_PROXY_AUTH_UNAVAILABLE})] * 2)
        self.account.invalidate_recordset()
        self.assertEqual((self.account.state, self.account.provision_attempts,
                          self.account.provision_lease_token), ('provisioning', 0, False))


# ====================================================================== #
#  neutralize 與 sentinel                                                 #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmNeutralize(TransactionCase):

    def setUp(self):
        super().setUp()
        reset_llm_config(self.env)

    def test_neutralize_sql_llm_section(self):
        configure(self.env, canary='7')
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param(llm_common.PARAM_ALERT_EMAIL, 'owner@example.com')
        icp.set_param(llm_common.PARAM_ALERT_WEBHOOK_URL, 'https://hooks.example.com/llm')
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
              llm_common.PARAM_CANARY_PARTNER_ID, llm_common.PARAM_ALERT_EMAIL,
              llm_common.PARAM_ALERT_WEBHOOK_URL),))
        self.assertEqual(dict(self.env.cr.fetchall()), {
            llm_common.PARAM_PROXY_URL: nac.PROXY_URL_BLACKHOLE,
            llm_common.PARAM_SHOW_LLM: 'False',
            llm_common.PARAM_CANARY_PARTNER_ID: '',
            llm_common.PARAM_ALERT_EMAIL: '',
            llm_common.PARAM_ALERT_WEBHOOK_URL: ''})
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
        with patch.dict(os.environ, llm_env()):
            with self.assertRaises(llm_crypto.LlmSecretNeutralized):
                llm_crypto.decrypt('neutralized', 1, 'wp0000000001', llm_crypto.FIELD_UP_TOKEN)


# ====================================================================== #
#  HTTP：portal 使用者走完 tracer                                          #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmTracerHttp(HttpCase):
    """HttpCase 的 ``registry.cursor()`` 是 TestCursor（同一條交易的 savepoint，
    ``odoo/modules/registry.py`` ``Registry.cursor`` 的 test mode），所以獨立 cursor 的寫回在這裡
    看得到，也全部跟著測試回捲；它測不到「跨連線不卡死」——那是 ``TestLlmProvisionRealCron``。
    """

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
        configure(self.env)
        env_patch = patch.dict(os.environ, llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.fake = FakeNewApi()
        self.client = LlmClientAdapter(self, self.fake, full_factory=True)
        own_partners = [self.alice.partner_id.id, self.bob.partner_id.id]
        scope_claims(self, lambda env: env[ACCOUNT].sudo().search(
            [('partner_id', 'in', own_partners)]).ids)

    def _rpc(self, path, params=None, headers=None):
        response = self.url_open(path, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params or {}}),
            headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
        self._raw = response.text
        body = response.json()
        self.assertNotIn('error', body, 'JSON-RPC error 而不是結構化回應：%s' % body.get('error'))
        return body['result']

    def _account(self, user):
        return self.env[ACCOUNT].sudo().search([('partner_id', '=', user.partner_id.id)])

    def _activate_and_provision(self, user):
        self.authenticate(user.login, self.password)
        result = self._rpc('/api/llm/account/activate', {'tos_version': TOS})
        self.assertEqual(result, {'success': True, 'data': {'state': 'provisioning'}})
        self.env[ACCOUNT]._cron_llm_provision()
        account = self._account(user)
        account.invalidate_recordset()
        self.assertEqual(account.state, 'active', account.last_error_code)
        return account

    def _create_key(self):
        return self._rpc('/api/llm/keys/create',
                         {'client_request_id': str(uuid.uuid4()), 'kind': 'free_trial'})

    def test_activation_provisioning_and_free_trial_key(self):
        self.authenticate(self.alice.login, self.password)
        state = self._rpc('/api/llm/state')
        self.assertTrue(state['success'])
        self.assertIsNone(state['data']['account'])
        self.assertEqual(self._rpc('/api/llm/keys/list')['code'], 'llm_not_activated')

        self.assertEqual(self._rpc('/api/llm/account/activate', {})['code'], 'llm_tos_required')
        # LLM-04 AC1：帶舊版（或亂填）的版本號也是「還沒同意目前的條款」。
        outdated = self._rpc('/api/llm/account/activate', {'tos_version': 'draft-x'})
        self.assertEqual((outdated['code'], outdated['current_version']),
                         ('llm_tos_required', TOS))
        account = self._activate_and_provision(self.alice)
        # 冪等：再按一次不會多一個帳戶
        again = self._rpc('/api/llm/account/activate', {'tos_version': TOS})
        self.assertEqual(again['data']['state'], 'active')
        self.assertEqual(len(self._account(self.alice)), 1)

        username = '%s%010d' % (TEST_PREFIX, account.id)
        self.assertEqual(account.newapi_username, username)
        [user] = [u for u in self.fake.users.values() if u['username'] == username]
        self.assertEqual((user['role'], user['group'], user['quota']), (1, 'default', 0))
        self.assertEqual(account.newapi_user_id, user['id'])
        tenant_ip = nac.tenant_ip(account.id, TEST_BLOCK)
        user_calls = {ip for name, ip in self.fake.calls if name.startswith('user_')}
        self.assertEqual(user_calls, {tenant_ip}, '使用者身分一律帶本帳戶的合成 IP')
        self.assertEqual(len(self.fake.pats_of(user['id'])), 1)
        self.assertFalse(any(s['active'] for s in self.fake.sessions.values()), 'P8 撤 session')

        state = self._rpc('/api/llm/state')['data']
        self.assertEqual(state['account']['state'], 'active')
        self.assertEqual(state['account']['balance_points'], 0)
        self.assertEqual(state['account']['balance_quota'], 0)

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
        self.assertEqual(self._create_key()['code'], 'llm_free_key_exists')
        self.assertEqual(len(self.fake.tokens), 1)
        calls = len(self.fake.calls)
        paid = self._rpc('/api/llm/keys/create',
                         {'client_request_id': str(uuid.uuid4()), 'kind': 'paid'})
        # LLM-07 supports paid keys; an incomplete paid form now fails on name.
        self.assertEqual((paid['code'], paid['field']), ('llm_key_invalid', 'name'))
        unsupported = self._rpc('/api/llm/keys/create',
                                {'client_request_id': str(uuid.uuid4()), 'kind': 'unsupported'})
        self.assertEqual((unsupported['code'], unsupported['field']), ('llm_key_invalid', 'kind'))
        self.assertEqual(len(self.fake.calls), calls, 'Invalid forms must not call new-api')

        # 用量：一筆免費試用呼叫；channel 被剝掉、金鑰對回鏡像。LLM-02：端點只讀快取（第一次是空的、
        # 排一次背景刷新），刷新 cron 以 CRON 金鑰讀完之後才看得到。
        self.fake.add_log(user['id'], token['name'])
        calls = len(self.fake.calls)
        first = self._rpc('/api/llm/usage/recent')['data']
        self.assertEqual((first['items'], first['data_as_of']), ([], None))
        self.assertEqual(len(self.fake.calls), calls, '端點不外呼（N26）')
        scope_swr_queue(self.env, [account.id])
        self.env.invalidate_all()
        # SWR cron 也會讀 token 清單；同一方法在兩個階段應各用自己的金鑰。
        web_role_calls = list(self.fake.role_calls)
        self.env['woow_paas_platform.llm_swr_cache']._cron_llm_swr_refresh()
        recent = self._rpc('/api/llm/usage/recent')['data']
        self.assertEqual(recent['today_count'], 1)
        self.assertEqual(recent['items'][0]['key_id'], mirror.id)
        self.assertNotIn('channel', recent['items'][0])

        # finding 12：web 路徑取 WEB 金鑰、cron 取 CRON 金鑰（取 client 的 role 與實際呼叫都對）
        self.assertEqual(self.client.roles_used(), [nac.ROLE_CRON, nac.ROLE_WEB, nac.ROLE_CRON])
        self.assertEqual({r for n, r in web_role_calls if n in WEB_PATH_METHODS},
                         {nac.ROLE_WEB})
        swr_role_calls = self.fake.role_calls[len(web_role_calls):]
        self.assertEqual({r for _, r in swr_role_calls}, {nac.ROLE_CRON})
        self.assertEqual({(n, r) for n, r in swr_role_calls
                          if n in WEB_PATH_METHODS},
                         {('user_list_tokens', nac.ROLE_CRON)})
        self.assertEqual({r for n, r in self.fake.role_calls if n in CRON_ONLY_METHODS},
                         {nac.ROLE_CRON})

    def test_other_partner_sees_nothing_of_alice(self):
        self._activate_and_provision(self.alice)
        self.authenticate(self.alice.login, self.password)
        created = self._create_key()
        self.assertTrue(created['success'])

        self.authenticate(self.bob.login, self.password)
        self.assertIsNone(self._rpc('/api/llm/state')['data']['account'])
        self.assertEqual(self._rpc('/api/llm/keys/list')['code'], 'llm_not_activated')
        self.assertEqual(self._rpc('/api/llm/usage/recent')['code'], 'llm_not_activated')
        bob_account = self._activate_and_provision(self.bob)
        self.assertEqual(self._rpc('/api/llm/keys/list')['data']['items'], [])
        self.assertNotEqual(bob_account, self._account(self.alice))
        # Bob 自己的試用金鑰不受 Alice 那把影響
        mine = self._create_key()
        self.assertTrue(mine['success'])
        self.assertNotEqual(mine['data']['key_id'], created['data']['key_id'])

    def test_usage_logs_are_bounded_scoped_and_allowlisted(self):
        today = datetime.now(ZoneInfo('Asia/Taipei')).date()
        today_range = {'start_date': today.isoformat(), 'end_date': today.isoformat()}
        account = self._activate_and_provision(self.alice)
        self.authenticate(self.alice.login, self.password)
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value={
                'state': 'ok', 'items': [{'model': 'paid-a', 'group': 'default'}]}):
            selected = self._rpc('/api/llm/keys/create', {
                'kind': 'paid', 'client_request_id': str(uuid.uuid4()),
                'name': 'Selected Alice usage key', 'limit_points': 1,
                'expires': '30d', 'models': ['paid-a'],
            })
        self.assertTrue(selected['success'], selected)
        selected_key_id = selected['data']['key_id']
        self.assertEqual(selected['data']['display_name'], 'Selected Alice usage key')
        other = self._create_key()
        self.assertTrue(other['success'], other)

        selected_tokens = [token for token in self.fake.tokens.values()
                           if token['group'] == 'default']
        other_tokens = [token for token in self.fake.tokens.values() if token['group'] == 'free']
        self.assertEqual((len(selected_tokens), len(other_tokens)), (1, 1))
        selected_token, other_token = selected_tokens[0], other_tokens[0]
        self.assertEqual({selected_token['user_id'], other_token['user_id']},
                         {account.newapi_user_id})
        self.fake.add_usage_log(account.newapi_user_id, selected_token['name'])
        self.fake.add_usage_log(account.newapi_user_id, '', log_type=5, model='')
        self.fake.add_log(account.newapi_user_id, other_token['name'], model='paid-a', group='default')
        other_request_id = self.fake.logs[-1]['request_id']
        # An otherwise matching row just beyond the inclusive end timestamp
        # must not leak into today's result.
        tomorrow_start = datetime.combine(
            today + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo('Asia/Taipei'))
        self.fake.logs.append(dict(
            self.fake.usage_log_rows[0], user_id=account.newapi_user_id,
            token_name=selected_token['name'], created_at=int(tomorrow_start.timestamp()),
            id=self.fake._id('log'), request_id='req_llm09_future_001',
        ))

        before = len(self.fake.calls)
        invalid = self._rpc('/api/llm/usage/logs', {
            'start_date': (today - timedelta(days=31)).isoformat(),
            'end_date': today.isoformat()})  # 32 inclusive days
        self.assertFalse(invalid['success'])
        self.assertEqual(len(self.fake.calls), before, 'invalid range must not issue upstream I/O')
        before = len(self.fake.calls)
        oversized = self._rpc('/api/llm/usage/logs', {**today_range, 'page': 1, 'page_size': 51})
        self.assertFalse(oversized['success'])
        self.assertEqual(len(self.fake.calls), before, 'oversized page must not issue upstream I/O')

        result = self._rpc('/api/llm/usage/logs', {
            'start_date': (today - timedelta(days=30)).isoformat(),
            'end_date': today.isoformat(), 'page': 1, 'page_size': 50})
        self.assertTrue(result['success'], result)
        data = result['data']
        self.assertLessEqual(len(data['items']), 50)
        self.assertEqual(data['total'], 2, 'future log is excluded by end_timestamp')

        page_one = self._rpc('/api/llm/usage/logs', {
            **today_range, 'page': 1, 'page_size': 1})['data']
        page_two = self._rpc('/api/llm/usage/logs', {
            **today_range, 'page': 2, 'page_size': 1})['data']
        self.assertEqual((page_one['total'], page_two['total']), (2, 2))
        self.assertNotEqual(page_one['items'][0]['request_id'], page_two['items'][0]['request_id'])
        no_model_match = self._rpc('/api/llm/usage/logs', {
            **today_range, 'model': 'other-model', 'page': 1, 'page_size': 50})['data']
        self.assertEqual(no_model_match['total'], 0,
                         'model_name filter is exact rather than a substring match')

        selected_result = self._rpc('/api/llm/usage/logs', {
            **today_range, 'key_id': selected_key_id, 'page': 1, 'page_size': 50})
        self.assertTrue(selected_result['success'], selected_result)
        selected_rows = selected_result['data']['items']
        self.assertEqual(len(selected_rows), 1)
        self.assertLessEqual(len(selected_rows), 50)
        self.assertEqual({row['request_id'] for row in selected_rows},
                         {'req_llm09_known_001'})
        selected_row = selected_rows[0]
        self.assertEqual(set(selected_row), {
            'time_tpe', 'key_id', 'key_name', 'model', 'input_tokens', 'output_tokens',
            'cache_tokens', 'duration_ms', 'quota', 'spend_points', 'request_id',
            'status', 'error_code',
        })
        self.assertEqual(selected_row['key_id'], selected_key_id)
        self.assertEqual(selected_row['key_name'], 'Selected Alice usage key')
        self.assertEqual(selected_row['status'], 'success', 'type=2 is the default success page')
        self.assertIsNone(selected_row['error_code'])
        selected_serialized = json.dumps(selected_row)
        for forbidden in ('private-channel-name', 'raw upstream prose',
                          'upstream private explanation', 'raw upstream error body',
                          '"channel"', '"channel_name"', '"admin_info"'):
            self.assertNotIn(forbidden, selected_serialized)
        self.assertNotIn(selected_token['name'], selected_serialized)
        self.assertNotIn(other_request_id, json.dumps(selected_rows))

        error_result = self._rpc('/api/llm/usage/logs', {
            **today_range, 'status': 'error', 'page': 1, 'page_size': 50})
        self.assertTrue(error_result['success'], error_result)
        [error_row] = error_result['data']['items']
        self.assertEqual(error_row['status'], 'error')
        self.assertEqual(error_row['error_code'], 'rate_limit')
        self.assertEqual(error_row['request_id'], 'req_llm09_error_001')
        self.assertIsNone(error_row['model'])
        self.assertIsNone(error_row['key_id'])
        self.assertIsNone(error_row['key_name'])
        error_payload = json.dumps(error_row)
        self.assertNotIn('raw provider error text', error_payload)
        self.assertNotIn('provider-private-code', error_payload)
        self.assertNotIn('admin_info', error_payload)

        bob_account = self._activate_and_provision(self.bob)
        self.authenticate(self.bob.login, self.password)
        bob_result = self._rpc('/api/llm/usage/logs', {
            **today_range, 'page': 1, 'page_size': 50})
        self.assertTrue(bob_result['success'], bob_result)
        bob_payload = json.dumps(bob_result['data'])
        self.assertNotIn('req_llm09_known_001', bob_payload)
        self.assertNotIn(other_request_id, bob_payload)
        self.assertNotEqual(bob_account, self._account(self.alice))
        before = len(self.fake.calls)
        bob_selected = self._rpc('/api/llm/usage/logs', {
            **today_range, 'key_id': selected_key_id, 'page': 1, 'page_size': 50})
        self.assertFalse(bob_selected['success'])
        self.assertEqual(len(self.fake.calls), before,
                         'another account\'s key selector must not issue upstream I/O')

    def test_stale_creating_trial_key_does_not_hide_the_create_button(self):
        """LLM-01c review 16：卡在 creating 的試用金鑰（處理它的 worker 在請求途中被殺）超過
        ``_STALE_CREATING_SECONDS`` 就不再算「已經有一把」——否則前端藏起〔建立免費試用金鑰〕，
        使用者永遠送不出會替它收尾的那一次 keys/create。"""
        account = self._activate_and_provision(self.alice)
        expires = fields.Datetime.now() + timedelta(days=90)
        key_id, _name = self.env[KEY].sudo()._llm_insert_creating(
            account, 'free_trial', str(uuid.uuid4()), self.alice, '', expires,
            ','.join(nac.FREE_TRIAL_MODELS))
        # 剛建立的 creating：照常算數（另一個分頁正在建）、清單顯示建立中。
        state = self._rpc('/api/llm/state')['data']
        self.assertEqual(state['free_trial']['key_id'], key_id)
        [row] = self._rpc('/api/llm/keys/list')['data']['items']
        self.assertEqual((row['id'], row['status']), (key_id, 'creating'))

        self.env.cr.execute(
            'UPDATE %s SET create_date = %%s WHERE id = %%s' % KEY_TABLE,
            (fields.Datetime.now() - timedelta(seconds=_STALE_CREATING_SECONDS + 60), key_id))
        self.env[KEY].invalidate_model()
        state = self._rpc('/api/llm/state')['data']
        self.assertIsNone(state['free_trial']['key_id'], '過期的 creating 不算已經有一把')
        [row] = self._rpc('/api/llm/keys/list')['data']['items']
        self.assertEqual((row['id'], row['status']), (key_id, 'failed_reveal'))

        created = self._create_key()
        self.assertTrue(created['success'], created)
        stale = self.env[KEY].sudo().browse(key_id)
        stale.invalidate_recordset()
        self.assertEqual(stale.state, 'failed_reveal', 'keys/create 替它收尾')
        state = self._rpc('/api/llm/state')['data']
        self.assertEqual(state['free_trial']['key_id'], created['data']['key_id'])

    def test_switches(self):
        self.authenticate(self.alice.login, self.password)
        self.env['ir.config_parameter'].sudo().set_param(llm_common.PARAM_SHOW_LLM, 'False')
        for path, params in (('/api/llm/state', {}),
                             ('/api/llm/account/activate', {'tos_version': TOS}),
                             ('/api/llm/terms', {}),
                             ('/api/llm/account/accept_terms', {'tos_version': TOS}),
                             ('/api/llm/keys/list', {}),
                             ('/api/llm/keys/create', {'client_request_id': str(uuid.uuid4()),
                                                       'kind': 'free_trial'}),
                             ('/api/llm/usage/recent', {})):
            self.assertEqual(self._rpc(path, params)['code'], 'llm_disabled', path)
        configure(self.env, proxy=nac.PROXY_URL_BLACKHOLE)
        self.assertEqual(self._rpc('/api/llm/state')['code'], 'llm_disabled', '未設定')
        self.assertFalse(self.fake.calls)

    def test_valid_oauth_bearer_without_session_gets_nothing(self):
        """finding 16c：有效的 HA／device-flow token（陽性對照：在 OAuth 端點 200）也進不來。"""
        suffix = uuid.uuid4().hex[:8]
        oauth_client = self.env['woow_paas_platform.oauth_client'].sudo().create({
            'name': 'llm oauth %s' % suffix, 'client_id': 'llm-oauth-%s' % suffix,
            'client_type': 'public', 'require_pkce': True,
            'redirect_uris': 'http://localhost:8123/auth/callback',
            'scopes': 'workspace:read smarthome:read',
            'grant_types': 'authorization_code,refresh_token',
        })
        token = self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': secrets.token_urlsafe(36),
            'refresh_token': secrets.token_urlsafe(36),
            'scope': 'workspace:read smarthome:read',
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'user_id': self.alice.id,
            'client_id': oauth_client.id,
        })
        bearer = {'Authorization': 'Bearer %s' % token.access_token}
        # 本測試從沒 authenticate：沒有登入 session（HttpCase.logout() 只在 authenticate 之後能用）。
        self.opener.cookies.clear()
        control = self.url_open('/api/smarthome/workspaces', headers=bearer)
        self.assertEqual(control.status_code, 200, '陽性對照：token 本身有效：%s' % control.text)
        response = self.url_open('/api/llm/state', data=json.dumps(
            {'jsonrpc': '2.0', 'method': 'call', 'params': {}}),
            headers=dict({'Content-Type': 'application/json'}, **bearer))
        body = response.json()
        self.assertNotIn('result', body)
        self.assertIn('error', body)
        self.assertFalse(self.fake.calls)

    def test_catch_all_never_leaks_exception_text(self):
        secret = 'nap_' + 'Z' * 43
        self.authenticate(self.alice.login, self.password)
        with patch.object(type(self.env[ACCOUNT]), '_llm_state_payload',
                          side_effect=RuntimeError('boom %s Bearer %s' % (secret, secret))), \
                self.assertLogs(CONTROLLER_LOGGER, level=logging.ERROR) as logs:
            result = self._rpc('/api/llm/state')
        self.assertEqual(result['code'], 'llm_internal_error')
        self.assertNotIn(secret, self._raw)
        self.assertNotIn(secret, '\n'.join(logs.output))

    def test_neutralized_secret_fails_closed_without_calls(self):
        self.authenticate(self.alice.login, self.password)
        self._rpc('/api/llm/account/activate', {'tos_version': TOS})
        account = self._account(self.alice)
        self.env.flush_all()
        self.env.cr.execute('UPDATE %s SET newapi_password_enc = %%s WHERE id = %%s'
                            % ACCOUNT_TABLE, ('neutralized', account.id))
        # 開戶放棄（state=error）會記一行 ERROR：這裡是預期內的，不算測試失敗。
        with mute_logger(ACCOUNT_LOGGER):
            self.env[ACCOUNT]._cron_llm_provision()
        account.invalidate_recordset()
        self.assertEqual((account.state, account.last_error_code),
                         ('error', 'llm_secret_neutralized'))
        self.assertFalse(self.fake.calls, 'sentinel 下不外呼、不重產密碼')
        self.assertFalse(self.fake.users)

    def test_broken_web_signing_key_fails_closed_on_keys_create(self):
        """AC7 的 WEB 那一半（finding 11）：回 llm_unavailable、不建鏡像列、回應與 log 不含值。"""
        account = self._activate_and_provision(self.alice)
        bad, parts = _broken_signing_value('kweb')
        with patch.dict(os.environ, {nac.ENV_AUTH_WEB: bad}), \
                self.assertLogs('odoo.addons.woow_paas_platform', level=logging.DEBUG) as logs:
            result = self._create_key()
        self.assertEqual(result['code'], 'llm_unavailable')
        output = '\n'.join(logs.output)
        self.assertIn('signing_key_invalid', output)
        for part in parts:
            self.assertNotIn(part, self._raw)
            self.assertNotIn(part, output)
        self.assertFalse(self.env[KEY].sudo().search([('account_id', '=', account.id)]))
        self.assertFalse(self.fake.tokens)
        self.assertEqual(self.client.roles_used(), [nac.ROLE_CRON, nac.ROLE_WEB])

    def test_unknown_create_with_failed_list_is_not_sent_twice(self):
        """finding 4：POST 結果未知、列表本身也失敗 ⇒ 不送第二次 POST、鏡像 failed_reveal。"""
        account = self._activate_and_provision(self.alice)
        # 第一次 POST 其實建成了（回應遺失），列表打不到代理。
        self.fake.fail_next['user_create_token'] = nac.Unknown('proxy_timeout', status=504)
        self.fake.fail_next['user_list_tokens'] = nac.NotExecuted('proxy_connect', status=502)
        result = self._create_key()
        self.assertEqual(result['code'], 'llm_unavailable')
        self.assertEqual([n for n, _ip in self.fake.calls].count('user_create_token'), 1)
        self.assertEqual(len(self.fake.tokens), 1, 'new-api 上只有一把')
        mirror = self.env[KEY].sudo().search([('account_id', '=', account.id)])
        self.assertEqual(mirror.state, 'failed_reveal')

    def test_unknown_create_with_empty_list_is_sent_again(self):
        """對照組：POST 結果未知、列表**成功**而且沒有 ⇒ 同名重送一次，成功。"""
        self._activate_and_provision(self.alice)
        self.fake.fail_before['user_create_token'] = nac.Unknown('proxy_timeout', status=504)
        result = self._create_key()
        self.assertTrue(result['success'], result)
        self.assertEqual([n for n, _ip in self.fake.calls].count('user_create_token'), 2)
        self.assertEqual(len(self.fake.tokens), 1)

    def test_session_is_revoked_when_the_pending_id_write_loses_the_lease(self):
        """finding 5：P4 之後的寫回丟 _LeaseLost，P8 照樣撤掉剛簽發的 session。"""
        self.authenticate(self.alice.login, self.password)
        self._rpc('/api/llm/account/activate', {'tos_version': TOS})
        # P1 建成了但回應遺失 ⇒ P2 找到的是候選 id，P4 登入確認後才寫回（那一步丟 _LeaseLost）。
        self.fake.fail_next['ap_create_user'] = nac.Unknown('no_message_key', status=200)
        Account = type(self.env[ACCOUNT])
        original = Account._llm_guarded_update

        def guarded(records, account_id, token, values):
            if set(values) == {'newapi_user_id'}:
                raise _LeaseLost()
            return original(records, account_id, token, values)

        with patch.object(Account, '_llm_guarded_update', guarded):
            self.env[ACCOUNT]._cron_llm_provision()
        names = [n for n, _ip in self.fake.calls]
        self.assertIn('user_login', names)
        self.assertIn('user_delete_session', names)
        self.assertTrue(self.fake.sessions)
        self.assertFalse(any(s['active'] for s in self.fake.sessions.values()), 'P8 撤 session')
        account = self._account(self.alice)
        account.invalidate_recordset()
        self.assertEqual((account.state, account.newapi_user_id), ('provisioning', 0))


@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmWoowSessionFlag(HttpCase):
    """finding 16a：``/woow`` 注入的 ``woow_show_llm`` 與 ``/api/llm/*`` 的 gate 是同一個判準。

    比照 ``test_show_projects_flag.TestWoowSessionInfoShowProjects`` 用 substring 斷言。
    """

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.password = 'llm-flag-%s' % suffix
        self.alice = new_test_user(
            self.env, login='llm_flag_%s' % suffix, password=self.password,
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        reset_llm_config(self.env)

    def _flag(self):
        response = self.url_open('/woow', timeout=30)
        self.assertEqual(response.status_code, 200, response.text[:200])
        if '"woow_show_llm": true' in response.text:
            return True
        if '"woow_show_llm": false' in response.text:
            return False
        self.fail('session_info 沒有 woow_show_llm')

    def test_flag_follows_switch_pilot_and_configuration(self):
        self.authenticate(self.alice.login, self.password)
        partner_id = self.alice.partner_id.id
        self.assertIs(self._flag(), False, '未設定')
        configure(self.env)
        self.assertIs(self._flag(), True)
        configure(self.env, pilot=str(partner_id + 100000))
        self.assertIs(self._flag(), False, '不在 pilot 名單')
        configure(self.env, pilot=str(partner_id))
        self.assertIs(self._flag(), True, '在 pilot 名單')
        configure(self.env, proxy=nac.PROXY_URL_BLACKHOLE)
        self.assertIs(self._flag(), False, '黑洞 URL＝未設定')
        configure(self.env, show=False)
        self.assertIs(self._flag(), False, 'show_llm 關')


# ====================================================================== #
#  真的跑 cron（真 cursor、真 commit；不 mock commit）                       #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmProvisionRealCron(TransactionCase):
    """〔REV〕25：搶戶與每一步的寫回都在獨立 cursor，主 cursor 不鎖同一列 ⇒ 不會卡住；
    中途當掉重跑會收斂（DESIGN §1.3.1、§8.2）。

    fixture 以 ``CommittedLlmFixture`` 真的 commit（TransactionCase 自己的 cursor 看不到、也不碰
    這些列）；cron 在另一條真連線上**直接呼叫** ``_cron_llm_provision()``——一樣是真 cursor、真
    commit，但不經 ``ir.cron._try_lock``，不會和 live server 搶 ir_cron 的列鎖。

    ⚠ 前提（finding 9）：這組測試在 LLM 沙盒裡跑時，**live server 的 ``ir_cron_llm_provision``
    是停住的**——fixture 先以 committed SQL 設 ``active=False``、cleanup 還原；不 commit 任何 LLM
    設定（本行程 patch ``llm_account.llm_config``）；前綴 ``wqa`` 代理一定拒絕。cleanup 只刪本
    test 建的列。
    """

    def setUp(self):
        super().setUp()
        env_patch = patch.dict(os.environ, llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.fake = FakeNewApi()
        self.client = LlmClientAdapter(self, self.fake, full_factory=False)
        self.fixture = CommittedLlmFixture(self).start()
        scope_claims(self, self.fixture.own_ids)
        self.tag = uuid.uuid4().hex[:8]
        self.account_id = self.fixture.activate(
            self.fixture.partner('llm real cron %s' % self.tag))

    def _run_cron(self):
        started = time.monotonic()
        with self.registry.cursor() as cr:
            env_for(self.registry, cr)[ACCOUNT]._cron_llm_provision()
        elapsed = time.monotonic() - started
        # 不卡住：獨立 cursor 的 lock_timeout 是 5 秒，一戶的 deadline 60 秒。
        self.assertLess(elapsed, 60)
        return elapsed

    def _row(self, account_id=None):
        with self.registry.cursor() as cr:
            cr.execute('SELECT state, newapi_user_id, newapi_username, up_token_enc, '
                       "COALESCE(newapi_password_enc, '') <> '' AS has_password, "
                       'provision_lease_token, provision_lease_until, last_error_code, '
                       'provision_attempts FROM %s WHERE id = %%s' % ACCOUNT_TABLE,
                       (account_id or self.account_id,))
            return cr.dictfetchone()

    def _expire_lease(self, account_id=None):
        """模擬「行程死掉 5 分鐘後」：lease 到期，下一個 tick 可以接手。"""
        with self.registry.cursor() as cr:
            cr.execute("UPDATE %s SET provision_lease_until = (now() at time zone 'UTC') "
                       "- interval '1 minute' WHERE id = %%s" % ACCOUNT_TABLE,
                       (account_id or self.account_id,))

    def _assert_converged(self, expected_pats, account_id=None):
        account_id = account_id or self.account_id
        row = self._row(account_id)
        self.assertEqual(row['state'], 'active', row['last_error_code'])
        users = [u for u in self.fake.users.values() if u['username'] == row['newapi_username']]
        self.assertEqual(len(users), 1, '剛好一個 new-api 使用者')
        self.assertEqual(row['newapi_user_id'], users[0]['id'])
        up = llm_crypto.decrypt(row['up_token_enc'], account_id, row['newapi_username'],
                                llm_crypto.FIELD_UP_TOKEN)
        self.assertEqual(self.fake.user_id_for_up(up), users[0]['id'], '存到的 UP 可用')
        self.assertEqual(len(self.fake.pats_of(users[0]['id'])), expected_pats)
        self.assertIsNone(row['provision_lease_token'])
        self.assertEqual(self.fake.pats[up]['last_used_ip'],
                         nac.tenant_ip(account_id, TEST_BLOCK))

    def test_cron_provisions_for_real(self):
        self._run_cron()
        self._assert_converged(expected_pats=1)
        self.assertTrue(self._row()['newapi_username'].startswith(TEST_PREFIX))
        self.assertEqual(self.client.roles_used(), [nac.ROLE_CRON])
        self.assertEqual({r for _n, r in self.fake.role_calls}, {nac.ROLE_CRON})

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

    def test_claim_is_limited_to_this_tests_accounts(self):
        """finding 8：共用 DB 裡 id 比較小的別人的 provisioning 帳戶不會被搶。"""
        self._run_cron()
        self._assert_converged(expected_pats=1)
        foreign_id = self.fixture.activate(self.fixture.partner('llm foreign %s' % self.tag))
        self.fixture.account_ids.remove(foreign_id)   # 仍由 partner 精確清掉，只是不算「自己的」
        own_id = self.fixture.activate(self.fixture.partner('llm own2 %s' % self.tag))
        self.assertLess(foreign_id, own_id, '前提：沒有限縮的話 ORDER BY id 會先搶別人的')
        self._run_cron()
        self._assert_converged(expected_pats=1, account_id=own_id)
        foreign = self._row(foreign_id)
        self.assertEqual((foreign['state'], foreign['provision_attempts'],
                          foreign['provision_lease_token'], foreign['has_password']),
                         ('provisioning', 0, None, False))

    def test_held_row_lock_fails_the_step_not_the_tick(self):
        """finding 16b：搶到戶之後別人拿著那一列的鎖 ⇒ 寫回在 lock_timeout 後失敗、tick 照常結束。"""
        holder = self.registry.cursor()
        self.addCleanup(holder.close)
        Account = type(self.env[ACCOUNT])
        claim = Account._llm_claim_for_provision

        def claim_then_hold(records, account_ids=None):
            claimed = claim(records, account_ids=account_ids)
            if claimed:
                holder.execute('SELECT id FROM %s WHERE id = %%s FOR UPDATE' % ACCOUNT_TABLE,
                               (claimed[0],))
            return claimed

        with patch.object(Account, '_llm_claim_for_provision', claim_then_hold), \
                self.assertLogs(ACCOUNT_LOGGER, level=logging.WARNING) as logs:
            elapsed = self._run_cron()
        self.assertLess(elapsed, 15, '兩次 5 秒的 lock_timeout，不是卡死')
        output = '\n'.join(logs.output)
        self.assertIn('pgcode=55P03', output)
        self.assertIn('llm_db_write_failed', output)
        self.assertNotIn('gAAAA', output, 'log 不帶密文（log_exceptions=False）')
        row = self._row()
        self.assertEqual(row['state'], 'provisioning')
        self.assertTrue(row['provision_lease_token'], 'lease 還在（失敗也記不下）')
        self.assertEqual(row['provision_attempts'], 1)
        self.assertFalse(row['has_password'], 'P0 寫不進去')
        self.assertFalse(self.fake.calls, 'P0 沒存好就不外呼')
        holder.rollback()
        self._expire_lease()
        self._run_cron()
        self._assert_converged(expected_pats=1)


@tagged('post_install', '-at_install', 'llm_tracer')
class TestLlmActivateRace(TransactionCase):
    """finding 19：同一個 partner 兩條真連線同時開通 ⇒ 只有一列；輸的那筆回 ``llm_provisioning``
    （預期結果，不是 ``llm_internal_error``），下一個交易冪等回贏家那一列。fixture 同
    ``TestLlmProvisionRealCron``（cron 停住、設定只在本行程 patch）。
    """

    def setUp(self):
        super().setUp()
        env_patch = patch.dict(os.environ, llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.fixture = CommittedLlmFixture(self).start()
        self.partner_id = self.fixture.partner('llm activate race %s' % uuid.uuid4().hex[:8])

    def test_two_connections_activating_the_same_partner(self):
        with self.registry.cursor() as loser_cr:
            loser = env_for(self.registry, loser_cr)
            partner = loser['res.partner'].browse(self.partner_id)
            # 釘住 loser 的 snapshot（REPEATABLE READ：第一個查詢就決定）——就像 HTTP 請求在 gate
            # 與 llm_visible 已經讀過 DB。
            self.assertFalse(loser[ACCOUNT]._llm_account_for_partner(partner))
            winner_id = self.fixture.activate(self.partner_id)   # 另一條連線 commit
            with mute_logger('odoo.sql_db'), self.assertRaises(LlmError) as caught:
                loser[ACCOUNT]._llm_activate(partner, TOS, None, '')
            self.assertEqual(caught.exception.code, llm_common.ERR_LLM_PROVISIONING)
            loser_cr.rollback()
        with self.registry.cursor() as cr:
            cr.execute('SELECT id, state FROM %s WHERE partner_id = %%s' % ACCOUNT_TABLE,
                       (self.partner_id,))
            self.assertEqual(cr.fetchall(), [(winner_id, 'provisioning')])
            env = env_for(self.registry, cr)
            again = env[ACCOUNT]._llm_activate(env['res.partner'].browse(self.partner_id),
                                               TOS, None, '')
            self.assertEqual(again.id, winner_id, '下一個交易看得到贏家：冪等回它')

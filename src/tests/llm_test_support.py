"""``test_llm_tracer`` 的共用 fixture（只供 tests；不新增任何 production context 閘門）。

四件事，都是審查（LLM-01b findings 8、9、11、12）抓到「在共用沙盒 DB 會紅或會寫壞資料」之後補的：

1. **設定歸零**（:func:`reset_llm_config`）：沙盒 DB 不一定是乾淨的——LLM 沙盒（例
   ``aimodels``）的 post-create 已經設好 ``newapi_proxy_url``／``show_llm``／前綴／block。
   每個 class 的 setUp 先在**測試交易內**把 ``llm_common.LLM_CONFIG_PARAMETERS`` 全部歸零，
   再設本測試要的值（含 pilot 與 canary）。
2. **client adapter**（:class:`LlmClientAdapter`）：``llm_account._llm_client`` 換成「先跑真的
   金鑰選擇（``get_newapi_client``／``signing_key_for_role``：讀該 role 的 env、驗格式）→ 回
   :class:`~.llm_fake_newapi.FakeNewApi` 的角色視圖」，``roles`` 記每次取 client 的 role。
   mock 的是 client，不是代理後面（見 ``llm_fake_newapi`` 的 docstring）。
3. **搶戶限縮**（:func:`scope_claims`）：``_llm_claim_for_provision``／``_llm_provision_pending``
   照 ``committed_cron_scope.isolated()`` 的作法只在本測試建的帳戶裡挑——共用 DB 裡別人的
   provisioning 帳戶（例如跑過 AC7 留下的）不會被測試的 cron 搶走、加密、寫成 error。
4. **committed fixture**（:class:`CommittedLlmFixture`）：給「真 cursor、真 commit」的測試用。
   沙盒測試是 ``kubectl exec … odoo --test-enable --stop-after-init`` 在**正在服務的 Odoo
   pod** 裡另開一個行程（plugin ``scripts/test.sh``），原本的 server 照常跑、它的 cron thread
   LISTEN ``cron_trigger``（chart ``max_cron_threads = 1``）。所以 fixture：

   * 先以真 cursor commit 把 ``ir_cron_llm_provision`` 設成 ``active=False``（等 live 那邊正在
     跑的那一次做完，最多 :data:`CRON_PAUSE_LOCK_TIMEOUT`），cleanup 還原原值——停用的 cron
     不會被 live server 撿去跑，``_trigger()`` 也不會建立立即觸發（``ir_cron._trigger_list``）；
   * **不 commit 任何 LLM 設定**：設定只在本行程裡 patch（``llm_account.llm_config``），共用
     DB 裡的設定原封不動，live server 看到的永遠是它自己的設定；
   * 帳戶前綴用 :data:`TEST_PREFIX`（``wqa``）：ICP 守衛收、代理的 ``^ws[a-z]{2}\\d{10}$`` 一定
     拒絕——萬一真的有東西外呼，代理 403 ``policy`` 是第二道保險；live server 若撿到這種帳戶，
     也會因為前綴和它自己的設定不符直接 ``llm_identity_mismatch``（零外呼）；
   * ``ir.cron._notifydb`` 在本行程裡換成 no-op（第三道保險：不叫醒 live server）。

   測試行程被硬殺時 cleanup 不會跑：cron 會留在停用狀態。手動還原見
   ``python3 .sandbox/llm-acceptance.py --slug <slug> cron-status``／``cron-resume``。
"""
import logging
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from odoo import SUPERUSER_ID, api

from ..models import llm_account as llm_account_module
from ..models import llm_common
from ..models.llm_account import ACCOUNT_TABLE, PROVISION_CRON_XMLID
from ..models.llm_api_key import KEY_TABLE
from ..services import llm_crypto
from ..services import newapi_client as nac

_logger = logging.getLogger(__name__)

ACCOUNT = 'woow_paas_platform.llm_account'
KEY = 'woow_paas_platform.llm_api_key'
#: ICP 守衛（``w[a-z]{1,3}``）收、代理（``^ws[a-z]{2}\d{10}$``）一定拒絕的前綴。
TEST_PREFIX = 'wqa'
TEST_BLOCK = 7
#: 停用 cron 時等 live server 正在跑的那一次（一戶的 deadline 60 秒＋寫回）。
CRON_PAUSE_LOCK_TIMEOUT = '75s'


def signing_value(kid):
    """格式合法、每次不同的簽章金鑰值（``kid:secret``）。"""
    return '%s:%s' % (kid, Fernet.generate_key().decode().rstrip('=').replace('-', 'x'))


def llm_env():
    """測試用 env：兩組簽章金鑰＋每次新產生的加密金鑰（值只存在這個行程裡）。"""
    return {
        nac.ENV_AUTH_WEB: signing_value('kweb'),
        nac.ENV_AUTH_CRON: signing_value('kcron'),
        llm_crypto.ENV_SECRET_KEYS: Fernet.generate_key().decode(),
    }


def reset_llm_config(env):
    """在 ``env`` 的交易內把本票的全部 LLM 設定歸零（``set_param(key, False)`` ＝刪掉那一列）。"""
    icp = env['ir.config_parameter'].sudo()
    for key in llm_common.LLM_CONFIG_PARAMETERS:
        icp.set_param(key, False)


def configure(env, show=True, proxy=nac.PROXY_URL_STG, prefix=TEST_PREFIX, block=TEST_BLOCK,
              pilot=False, canary=False):
    """歸零後設本測試要的值；每一個 key 都明確給（含 pilot 與 canary）。"""
    reset_llm_config(env)
    icp = env['ir.config_parameter'].sudo()
    icp.set_param(llm_common.PARAM_PROXY_URL, proxy or False)
    icp.set_param(llm_common.PARAM_USERNAME_PREFIX, prefix or False)
    icp.set_param(llm_common.PARAM_IP_BLOCK, str(block) if block is not None else False)
    icp.set_param(llm_common.PARAM_SHOW_LLM, 'True' if show else 'False')
    icp.set_param(llm_common.PARAM_PILOT_PARTNER_IDS, pilot or False)
    icp.set_param(llm_common.PARAM_CANARY_PARTNER_ID, canary or False)


#: committed 測試在本行程裡用的設定（不寫進 DB；見模組 docstring 第 4 點）。
TEST_CONFIG = {
    'proxy_state': 'usable',
    'proxy_url': nac.PROXY_URL_STG,
    'show': True,
    'pilot': frozenset(),
    'canary': '',
    'prefix': TEST_PREFIX,
    'block': TEST_BLOCK,
    'identity_ok': True,
}


class LlmClientAdapter:
    """``llm_account._llm_client`` → 真的金鑰選擇 ＋ :class:`FakeNewApi` 的角色視圖。

    ``full_factory=True``（HttpCase）：跑完整的 ``get_newapi_client``（proxy URL 白名單＋role
    的 env）；``False``（committed 測試，DB 裡的 proxy URL 是別人的）：只跑
    ``signing_key_for_role``。兩者都會在 env 金鑰缺或不合時丟 ``NewApiConfigError``——和
    正式環境同一條路。
    """

    def __init__(self, case, fake, full_factory=True):
        self.fake = fake
        self.roles = MagicMock(name='llm_client_roles')
        adapter = self

        def _llm_client(records, role):
            adapter.roles(role)
            if full_factory:
                nac.get_newapi_client(records.env, role)
            else:
                nac.signing_key_for_role(role)
            return fake.client(role)

        patcher = patch.object(type(case.env[ACCOUNT]), '_llm_client', _llm_client)
        patcher.start()
        case.addCleanup(patcher.stop)

    def roles_used(self):
        return [c.args[0] for c in self.roles.call_args_list]


def scope_claims(case, own_ids):
    """搶戶與「還有待開戶」只看 ``own_ids(env)`` 回的帳戶 id（保留真的 SQL、排序與鎖）。"""
    cls = type(case.env[ACCOUNT])
    claim = cls._llm_claim_for_provision
    pending = cls._llm_provision_pending

    def scoped_claim(records, account_ids=None):
        return claim(records, account_ids=own_ids(records.env))

    def scoped_pending(records, account_ids=None):
        return pending(records, account_ids=own_ids(records.env))

    stack = ExitStack()
    stack.enter_context(patch.object(cls, '_llm_claim_for_provision', scoped_claim))
    stack.enter_context(patch.object(cls, '_llm_provision_pending', scoped_pending))
    case.addCleanup(stack.close)


def env_for(registry, cr):
    if cr.transaction is None:
        cr.transaction = api.Transaction(registry)
    return api.Environment(cr, SUPERUSER_ID, {})


class CommittedLlmFixture:
    """真 cursor commit 的 fixture（見模組 docstring 第 4 點）。

    只登記本 test 建的列，cleanup 以 id 精確刪除；``start()`` 之前就掛好 cleanup，setUp
    中途失敗也會清掉已建的東西、還原 cron。
    """

    def __init__(self, case):
        self.case = case
        self.registry = case.registry
        self.partner_ids = []
        self.account_ids = []
        self.cron_id = case.env.ref(PROVISION_CRON_XMLID).id
        self._cron_active = None
        self._trigger_floor = None
        self._progress_floor = None

    def env(self, cr):
        return env_for(self.registry, cr)

    def start(self):
        self.case.addCleanup(self.purge)
        stack = ExitStack()
        stack.enter_context(patch.object(llm_account_module, 'llm_config',
                                         return_value=dict(TEST_CONFIG)))
        stack.enter_context(patch.object(type(self.case.env['ir.cron']), '_notifydb',
                                         lambda records: None))
        self.case.addCleanup(stack.close)
        with self.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s', (CRON_PAUSE_LOCK_TIMEOUT,))
            cr.execute('SELECT active FROM ir_cron WHERE id = %s FOR NO KEY UPDATE',
                       (self.cron_id,))
            self._cron_active = cr.fetchone()[0]
            cr.execute('UPDATE ir_cron SET active = false WHERE id = %s', (self.cron_id,))
            cr.execute('SELECT COALESCE(MAX(id), 0) FROM ir_cron_trigger')
            self._trigger_floor = cr.fetchone()[0]
            cr.execute("SELECT to_regclass('ir_cron_progress')")
            if cr.fetchone()[0]:
                cr.execute('SELECT COALESCE(MAX(id), 0) FROM ir_cron_progress')
                self._progress_floor = cr.fetchone()[0]
        return self

    def partner(self, name):
        with self.registry.cursor() as cr:
            partner = self.env(cr)['res.partner'].with_context(
                mail_create_nolog=True, mail_create_nosubscribe=True, tracking_disable=True,
            ).create({'name': name})
            self.partner_ids.append(partner.id)
        return self.partner_ids[-1]

    def activate(self, partner_id):
        """以真 cursor 開通並 commit；回帳戶 id。"""
        with self.registry.cursor() as cr:
            env = self.env(cr)
            account = env[ACCOUNT]._llm_activate(
                env['res.partner'].browse(partner_id), llm_common.TOS_VERSION_CURRENT, None, '')
            self.account_ids.append(account.id)
        return self.account_ids[-1]

    def own_ids(self, env=None):
        return list(self.account_ids)

    def purge(self):
        try:
            with self.registry.cursor() as cr:
                if self.partner_ids:
                    cr.execute('SELECT id FROM %s WHERE partner_id = ANY(%%s)' % ACCOUNT_TABLE,
                               (self.partner_ids,))
                    ids = sorted(set(self.account_ids) | {row[0] for row in cr.fetchall()})
                else:
                    ids = sorted(set(self.account_ids))
                if ids:
                    cr.execute('DELETE FROM %s WHERE account_id = ANY(%%s)' % KEY_TABLE, (ids,))
                    cr.execute('DELETE FROM %s WHERE id = ANY(%%s)' % ACCOUNT_TABLE, (ids,))
                if self.partner_ids:
                    cr.execute('DELETE FROM res_partner WHERE id = ANY(%s)', (self.partner_ids,))
                if self._trigger_floor is not None:
                    cr.execute('DELETE FROM ir_cron_trigger WHERE cron_id = %s AND id > %s',
                               (self.cron_id, self._trigger_floor))
                if self._progress_floor is not None:
                    cr.execute('DELETE FROM ir_cron_progress WHERE cron_id = %s AND id > %s',
                               (self.cron_id, self._progress_floor))
                if self._cron_active is not None:
                    cr.execute('UPDATE ir_cron SET active = %s WHERE id = %s',
                               (self._cron_active, self.cron_id))
        except Exception:  # noqa: BLE001 - cleanup must not mask the test result
            _logger.exception(
                'LLM committed fixture cleanup failed (cron %s may still be paused)', self.cron_id)

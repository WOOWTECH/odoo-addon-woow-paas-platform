"""#745（CS AC T3）：CS 的 operator access sync／CF 命名／reconciler 左集。

## 守什麼

T2 把欄位與 teardown hook 放好了，但**沒有任何東西會打 operator**。本檔守的是
接上去之後的四件事：

1. **payload 形狀**與 SA route 逐欄位對齊（operator 那端只有一個 schema，兩邊
   形狀分家會在 422 才發現，而 422 訊息不會說是哪一邊寫錯）。
2. **CF reusable policy 前綴帶 env token**。CS 的 `subdomain` 是 create 當下用
   `env=''` 算的（`controllers/paas.py`），所以「拿 stored subdomain 當前綴」
   在 prod 看起來完全正確、到 stg 才變成無法自證歸屬的 CF 物件。A2 的判別式
   因此不是「有沒有 token」而是「**它不等於 stored subdomain**」——只斷言前者
   的話，把實作改成 `self.subdomain` 再手動接一個 token 也會綠。
3. **錨點分派**：policy／rule 的三個 sync 觸點原本寫死 `mapped('route_id')`，
   對 CS 錨定的列是空集 ⇒ 什麼都不會發生。「沒打 operator」與「打了而且沒事」
   在測試輸出上不可區分，所以 C 系列一律斷言 `sync_access_app.called` 本身，
   而不是斷言「沒有例外」。
4. **reconciler 左集**：CS 長出來的 Access app／policy 不進左集就會被 L4 判孤兒
   清掉（右集是 CF 帳號級 list-all，看得到它們）。

## TDD 紀律

* 否定性斷言配陽性對照（B2/B3 的 no-op 都先證明同一組 fixture 在 B1 會打）。
* mock 的回傳一律是**真的 response 形狀**（含 `orphaned_policy_ids`），不用裸
  MagicMock——裸 mock 會讓回存那段拿到 MagicMock 而不是字串，測試綠但 prod 炸。
"""
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

# #787：`cs_skip_access_sync` 只認模組私有 sentinel，不能傳 `True`
# （呼叫端 context 會被 `call_kw` 整包換進 env ⇒ truthy 值是租戶可偽造的）。
from ..models.cloud_service import (
    _cs_skip_access_sync, _internal_cs_access_identity, _internal_provision,
)
# #536：`sa_skip_route_sync` 只認模組私有 sentinel，不能傳 `True`
# （呼叫端 context 會被 `call_kw` 整包換進 env ⇒ truthy 值是租戶可偽造的）。
from ..models.security_access import _skip_route_sync
from ..services.naming import make_service_subdomain
from ..services.reconciler import collect_left_set

# #798：`cf_policy_id` 有欄位級寫入守衛（create 壓值／write 拒絕，對 su 也生效）。
# fixture 擺狀態必須經開閘 helper，否則會撞到自己要測的守衛。
from ..models.security_access import (
    _internal_policy_cf_write as _internal_policy_cf,
)

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.'
    'get_paas_operator_client'
)

_POLICY = 'woow_paas_platform.security_access_access_policy'
_POLICY_RULE = 'woow_paas_platform.security_access_access_policy_rule'


def _sync_result(app_id='app-745', policy_ids=None, unresolved=None):
    """operator `PUT /api/access/sync` 的真實回傳形狀（五個 key 一個都不少）。"""
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': list(unresolved or []),
        'orphaned_policy_ids': [],
    }


@tagged('post_install', '-at_install')
class _CsAccessSyncFixture(TransactionCase):
    """一台有 subdomain 的 running CS ＋ 一個 SA route 當回歸對照。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')

        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Policy = self.env[_POLICY]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS 745 %s' % uuid.uuid4().hex[:6]})
        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'CS AC 745 tmpl',
                'slug': 'csac-745-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/csac745',
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })
        self.svc = self._service('svc-745')

    def _service(self, name):
        """subdomain 用**production 公式**算（`env=''`，見 controllers/paas.py）。

        fixture 隨手塞一個字串也能讓大部分斷言通過，但 A2 的判別式正是
        「canonical 不等於 stored subdomain」——subdomain 不是真實形狀時，那條
        斷言會因為無關的理由變綠。
        """
        svc = _internal_provision(self.Service.sudo()).create({
            'name': name,
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
        })
        svc.subdomain = make_service_subdomain(
            self.workspace.ws_hash, svc.reference_id, name)
        return svc

    def _policy(self, service, name=None, sequence=None, cf_policy_id=None):
        vals = {
            'cloud_service_id': service.id,
            'name': name or 'pol-745-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        if sequence is not None:
            vals['sequence'] = sequence
        pol = _skip_route_sync(self.Policy.sudo()).create(vals)
        if cf_policy_id:
            _internal_policy_cf(pol).cf_policy_id = cf_policy_id
        # 🔴 `create()` 回的 recordset **帶著呼叫時的 context**——直接回它，
        # 之後對這筆 policy 的每一次 write 都還套著 `sa_skip_route_sync=True`，
        # 於是「錨點分派沒接好」與「fixture 把 sync 關掉了」在測試輸出上長得
        # 一模一樣（C2 實際踩過：write 分派已經寫好了，測試仍然紅）。
        return pol.with_context(sa_skip_route_sync=False)

    def _route(self):
        sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA 745', 'workspace_id': self.workspace.id})
        return _skip_route_sync(
            self.env['woow_paas_platform.security_access_route'].sudo()
        ).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r745-%s' % uuid.uuid4().hex[:6]})

    def _mock(self, result=None):
        client = MagicMock()
        client.configure_tunnel.side_effect = configure_receipt
        client.sync_tunnel_routes.side_effect = routes_receipt
        # AC1011: the origin boundary now consumes the real parking contract.
        client.park_release_route.return_value = {'status': 'parked'}
        client.unpark_release_route.return_value = {'status': 'unparked'}
        if result is not None:
            client.sync_access_app.return_value = result
        else:
            client.sync_access_app.side_effect = lambda payload: _sync_result(
                policy_ids=['policy-%s' % index for index, _policy in
                            enumerate(payload.get('policies', []), 1)]) | {
                    'allowed_idps': ['idp-'+kind for kind in
                                     payload.get('login_idp_types', [])]}
        return client


class TestCsAccessPayload(_CsAccessSyncFixture):
    """A 系列：payload 形狀與 CF 命名前綴。"""

    def test_A1_payload_mirrors_the_sa_route_shape(self):
        _cs_skip_access_sync(self.svc).write({
            'is_protected': True, 'login_google': True,
            'login_email_otp': True, 'session_duration': '8h'})
        pol = self._policy(self.svc, name='p1', sequence=10)
        payload = self.svc._build_access_payload()

        self.assertEqual(
            sorted(payload),
            sorted(['enabled', 'canonical_name', 'app_name',
                    'primary_hostname', 'protected_hostnames',
                    'session_duration', 'login_idp_types', 'policies',
                    'existing_app_id', 'existing_policy_ids']),
            'payload 的 key 集合必須與 operator 的 AccessSyncRequest 一致')
        host = '%s.woowtech.io' % self.svc.subdomain
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['primary_hostname'], host)
        self.assertEqual(payload['protected_hostnames'], [host])
        self.assertEqual(payload['session_duration'], '8h')
        self.assertEqual(sorted(payload['login_idp_types']),
                         ['google', 'onetimepin'])
        self.assertEqual(payload['app_name'], self.svc.subdomain)
        self.assertEqual(len(payload['policies']), 1)
        self.assertEqual(payload['policies'][0]['name'], 'p1')
        self.assertEqual(payload['policies'][0]['decision'], 'allow')
        self.assertEqual(payload['policies'][0]['precedence'], 10)
        self.assertEqual(
            payload['policies'][0]['include'],
            [{'rule_type': 'email_list', 'rule_value': 'a@example.com'}])
        self.assertEqual(payload['policies'][0]['require'], [])
        self.assertEqual(payload['policies'][0]['exclude'], [])
        self.assertIsNone(payload['existing_app_id'])
        self.assertEqual(payload['existing_policy_ids'], [])
        del pol

    def test_A2_canonical_name_carries_env_token_and_is_not_the_subdomain(self):
        """stg 下前綴帶 token，且**不等於** stored subdomain。

        後半句是本條的判別式：CS 的 subdomain 是用 `env=''` 算的，任何「拿
        subdomain 當前綴」的實作在 prod 都看不出差別，只有這條會紅。
        """
        self.ICP.set_param('woow_paas_platform.deploy_env', 'stg')
        canonical = self.svc._access_canonical_name()

        self.assertTrue(
            canonical.startswith('paas-stg-cs-'),
            'stg 的 CF Access 命名根必須帶 env token，得到 %r' % canonical)
        self.assertNotEqual(
            canonical, self.svc.subdomain,
            'CS 的 subdomain 沒有 env token ⇒ 命名不可反推自 subdomain'
            '（design checklist 6）')
        self.assertEqual(
            canonical,
            make_service_subdomain(self.workspace.ws_hash,
                                   self.svc.reference_id, self.svc.name,
                                   env='stg'),
            'env token 要走 naming.py 的單一根，不要在別處自己拼')

    def test_A3_prod_keeps_the_no_token_convention(self):
        """prod／空值＝不加 token（`_env_token` 的既有慣例，跨資源一致）。"""
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        canonical = self.svc._access_canonical_name()
        self.assertTrue(canonical.startswith('paas-cs-'), canonical)
        self.assertNotIn('paas-prod-', canonical)

    def test_A4_unprotected_payload_is_a_teardown_declaration(self):
        """`is_protected=False` ⇒ enabled False ＋ 空 destinations。

        operator 的 teardown 分支判的是 `not enabled or not
        protected_hostnames`——兩者都要對，只有 enabled 對而 hostname 還留著的
        payload 在別的分支下會變成「重建一個沒人保護得到的 app」。
        """
        _internal_cs_access_identity(self.svc).access_app_id = 'app-old'
        payload = self.svc._build_access_payload()
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['protected_hostnames'], [])
        self.assertEqual(payload['existing_app_id'], 'app-old')

    def test_A5_existing_policy_ids_include_the_doomed_ones(self):
        """unlink 掉的 policy 的 cf id 必須一起送去刪——但來源是**帳本**不是 context。

        不送＝operator 永遠不會刪它（它唯一的刪除來源就是這個清單）⇒ 每刪一條
        policy 就漏一顆永久 CF 孤兒。

        #783：供給這顆 id 的通道從「呼叫端 context」換成 `cf_access_policy_reap`
        帳本。`unlink()` 在同一個 savepoint 內先落帳才推 sync ⇒ 對生產路徑而言是
        同一批 id，行為不變；差別是帳本只收**列上讀到的**值，而 context 是
        `call_kw` 直達的裸值。第三格斷言把那條腿釘死。
        """
        self._policy(self.svc, cf_policy_id='cf-live')
        self.env['woow_paas_platform.cf_access_policy_reap']._record(
            self.svc, ['cf-doomed'])
        payload = self.svc.with_context(
            sa_unlinked_cf_policy_ids=['cf-forged'])._build_access_payload()
        self.assertEqual(sorted(payload['existing_policy_ids']),
                         ['cf-doomed', 'cf-live'],
                         '列值 ∪ 帳本，兩個來源都要在')
        self.assertNotIn(
            'cf-forged', payload['existing_policy_ids'],
            '呼叫端 context 塞的 id 不得進 operator 的帳號級刪除清單（#783）')


class TestCsAccessSyncTrigger(_CsAccessSyncFixture):
    """B 系列：寫入 → sync → 回存。"""

    def test_B1_enabling_protection_pushes_and_stores_the_app_id(self):
        client = self._mock(_sync_result(app_id='app-new'))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.svc.write({'is_protected': True})
        self.assertTrue(client.sync_access_app.called,
                        '開啟保護必須打 operator')
        payload = client.sync_access_app.call_args.args[0]
        self.assertTrue(payload['enabled'])
        self.assertEqual(self.svc.access_app_id, 'app-new',
                         'operator 回的 app_id 必須回存（sentinel 路徑）')

    def test_B2_no_subdomain_never_calls_the_operator(self):
        """沒有對外 hostname ⇒ 沒有可保護的錨點。陽性對照＝B1。"""
        self.svc.subdomain = False
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.svc.write({'is_protected': True})
        self.assertFalse(client.sync_access_app.called,
                         '沒有 subdomain 的服務不該建 Access app')

    def test_B3_unprotected_without_app_is_a_no_op(self):
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.svc.write({'session_duration': '1h'})
        self.assertFalse(client.sync_access_app.called,
                         '從未保護過的服務改 session_duration 不必打 CF')

    def test_B4_unresolved_idp_types_are_persisted(self):
        client = self._mock(_sync_result(unresolved=['google']))
        with patch(MOCK_GET_CLIENT, return_value=client):
            with self.assertRaisesRegex(UserError, 'not confirmed'):
                self.svc.write({'is_protected': True, 'login_google': True})
        self.svc.invalidate_recordset()
        self.assertFalse(self.svc.is_protected)
        self.assertFalse(self.svc.access_app_id)

    def test_B5_returned_policy_ids_map_back_in_precedence_order(self):
        p1 = self._policy(self.svc, name='p1', sequence=10)
        p2 = self._policy(self.svc, name='p2', sequence=20)
        client = self._mock(_sync_result(policy_ids=['cf-1', 'cf-2']))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.svc.write({'is_protected': True})
        self.assertEqual(p1.cf_policy_id, 'cf-1')
        self.assertEqual(p2.cf_policy_id, 'cf-2')

    def test_B6_disabling_tears_down_and_clears_the_stored_ids(self):
        p1 = self._policy(self.svc, cf_policy_id='cf-1')
        _internal_cs_access_identity(self.svc).access_app_id = 'app-live'
        client = self._mock(_sync_result(app_id=None))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.svc.write({'is_protected': False})
        self.assertTrue(client.sync_access_app.called)
        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-live')
        self.assertFalse(self.svc.access_app_id, '拆完才可以清掉 app id')
        self.assertFalse(p1.cf_policy_id, '拆完 policy 的 cf id 也要清掉')

    def test_B7_skip_context_suppresses_the_push(self):
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            _cs_skip_access_sync(self.svc).write(
                {'is_protected': True})
        self.assertFalse(client.sync_access_app.called)
        self.assertTrue(self.svc.is_protected, 'DB 那一側仍然要寫進去')

    def test_B8_deleting_service_is_not_re_pushed(self):
        """`deleting` ＝ teardown 已經跑過；再推一次等於把剛拆掉的 app 建回來。"""
        _internal_cs_access_identity(self.svc).access_app_id = 'app-live'
        _cs_skip_access_sync(self.svc).write(
            {'state': 'deleting'})
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.svc.write({'is_protected': True})
        self.assertFalse(client.sync_access_app.called)

    def test_B9_sync_failure_rolls_the_db_write_back(self):
        """sync 炸掉 ⇒ `is_protected` 不得留在 DB 裡。

        controller 會把例外收成 `success: False` 的**正常回應**（交易照樣
        commit），所以沒有這個 savepoint 的話，DB 會宣稱「已保護」而 CF 上
        什麼都沒有——正是 fail-open 的方向。
        """
        client = MagicMock()
        client.configure_tunnel.side_effect = configure_receipt
        client.sync_tunnel_routes.side_effect = routes_receipt
        # AC1011: the origin boundary now consumes the real parking contract.
        client.park_release_route.return_value = {'status': 'parked'}
        client.unpark_release_route.return_value = {'status': 'unparked'}
        client.sync_access_app.side_effect = RuntimeError('operator down')
        with patch(MOCK_GET_CLIENT, return_value=client):
            with self.assertRaises(RuntimeError):
                self.svc.write({'is_protected': True})
        self.svc.invalidate_recordset()
        self.assertFalse(self.svc.is_protected,
                         'sync 失敗後 DB 不得留下「已保護」')


class TestPolicyAnchorDispatch(_CsAccessSyncFixture):
    """C 系列：policy／rule 的 sync 觸點要走錨點分派，不是寫死 route。"""

    def _protected(self):
        svc = self.svc
        _cs_skip_access_sync(svc).write(
            {'is_protected': True})
        _internal_cs_access_identity(svc).access_app_id = 'app-live'
        return svc

    def test_C1_creating_a_cs_anchored_policy_syncs_that_service(self):
        svc = self._protected()
        client = self._mock(_sync_result(policy_ids=['cf-new']))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.Policy.sudo().create({
                'cloud_service_id': svc.id,
                'name': 'c1',
                'decision': 'allow',
                'rule_ids': [(0, 0, {'group': 'include',
                                     'rule_type': 'email_list',
                                     'rule_value': 'c1@example.com'})],
            })
        self.assertTrue(
            client.sync_access_app.called,
            'CS 錨定 policy 的 create 必須打 operator'
            '（寫死 mapped("route_id") 時這裡是空集，什麼都不會發生）')
        payload = client.sync_access_app.call_args.args[0]
        self.assertIn('c1', [p['name'] for p in payload['policies']])

    def test_C2_writing_a_cs_anchored_policy_syncs_that_service(self):
        svc = self._protected()
        pol = self._policy(svc, name='c2')
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            pol.write({'name': 'c2-renamed'})
        self.assertTrue(client.sync_access_app.called)
        payload = client.sync_access_app.call_args.args[0]
        self.assertIn('c2-renamed', [p['name'] for p in payload['policies']])

    def test_C3_unlinking_reaps_the_doomed_cf_policy_id(self):
        svc = self._protected()
        pol = self._policy(svc, cf_policy_id='cf-doomed')
        # Access remains enabled after removing the final policy (deny all).
        client = self._mock(_sync_result())
        with patch(MOCK_GET_CLIENT, return_value=client):
            pol.unlink()
        self.assertTrue(client.sync_access_app.called)
        payload = client.sync_access_app.call_args.args[0]
        self.assertIn('cf-doomed', payload['existing_policy_ids'],
                      '被刪掉那一列的 cf id 必須一起送去刪，否則是永久孤兒')
        del svc

    def test_C4_route_anchored_policies_still_sync_their_route_only(self):
        """回歸對照：錨點分派不得把既有 route 那一腿改壞。"""
        route = self._route()
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client), \
                patch.object(type(route), '_sync_access_to_operator') as m_route, \
                patch.object(type(self.svc), '_sync_access_to_operator') as m_cs:
            self.Policy.sudo().create({
                'route_id': route.id,
                'name': 'c4',
                'decision': 'allow',
                'rule_ids': [(0, 0, {'group': 'include',
                                     'rule_type': 'email_list',
                                     'rule_value': 'c4@example.com'})],
            })
        self.assertTrue(m_route.called, 'route 錨定仍須同步它自己的 route')
        self.assertFalse(m_cs.called, 'route 錨定不該去碰任何 cloud_service')

    def test_C5_rule_edits_on_a_cs_anchored_policy_sync_the_service(self):
        svc = self._protected()
        pol = self._policy(svc, name='c5')
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            rule = self.env[_POLICY_RULE].sudo().create({
                'policy_id': pol.id, 'group': 'exclude',
                'rule_type': 'email_domain', 'rule_value': 'evil.com'})
        self.assertTrue(client.sync_access_app.called,
                        'rule create 要讓錨點重推一次完整狀態')

        client.sync_access_app.reset_mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            rule.write({'rule_value': 'worse.com'})
        self.assertTrue(client.sync_access_app.called, 'rule write 同上')

        client.sync_access_app.reset_mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            rule.unlink()
        self.assertTrue(client.sync_access_app.called, 'rule unlink 同上')


class TestCsAccessTeardownPush(_CsAccessSyncFixture):
    """D 系列：T2 留下的 `_push_access_teardown` seam 真的接上 operator。"""

    def test_D1_teardown_pushes_a_disabled_payload(self):
        client = self._mock(_sync_result(app_id=None))
        _internal_cs_access_identity(self.svc).access_app_id = 'app-live'
        self._policy(self.svc, cf_policy_id='cf-1')
        with patch(MOCK_GET_CLIENT, return_value=client):
            ok = self.svc._teardown_access_if_any()
        self.assertTrue(ok)
        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-live')
        self.assertEqual(payload['existing_policy_ids'], ['cf-1'])
        self.assertTrue(payload['canonical_name'].startswith('paas-'),
                        'teardown 也要帶命名根（reusable policy 前綴同源）')
        self.assertFalse(self.svc.access_app_id)

    def test_D2_teardown_failure_keeps_the_retry_anchor(self):
        """清不掉 ⇒ 回 False 且 `access_app_id` 留著。

        清掉 id 而 CF 上的 app 還在＝永久孤兒且沒有任何路徑找得回來。
        """
        client = MagicMock()
        client.configure_tunnel.side_effect = configure_receipt
        client.sync_tunnel_routes.side_effect = routes_receipt
        # AC1011: the origin boundary now consumes the real parking contract.
        client.park_release_route.return_value = {'status': 'parked'}
        client.unpark_release_route.return_value = {'status': 'unparked'}
        client.sync_access_app.side_effect = RuntimeError('cf down')
        _internal_cs_access_identity(self.svc).access_app_id = 'app-live'
        with patch(MOCK_GET_CLIENT, return_value=client):
            ok = self.svc._teardown_access_if_any()
        self.assertFalse(ok, '拆不掉必須回 False（呼叫端據此 fail-loud）')
        self.assertEqual(self.svc.access_app_id, 'app-live',
                         '失敗時 app id 是唯一的重試錨點，不可清除')

    def test_D3_nothing_to_tear_down_is_true_without_calling_the_operator(self):
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            ok = self.svc._teardown_access_if_any()
        self.assertTrue(ok)
        self.assertFalse(client.sync_access_app.called,
                         '從來沒有 Access app 的服務不必打 operator')


class TestReconcilerLeftSetCoversCs(_CsAccessSyncFixture):
    """E 系列：CS 的 Access 資源要進 L4 左集，否則會被判孤兒清掉。"""

    def test_E1_cs_access_app_id_enters_the_left_set(self):
        _internal_cs_access_identity(self.svc).access_app_id = 'cf-app-e1'
        ls = collect_left_set(self.env)
        self.assertIn('cf-app-e1', ls.access_app_ids,
                      'CS 的 Access app 不進左集 ⇒ L4 會把活著的 app 判孤兒')

    def test_E2_cs_anchored_policy_cf_id_enters_the_left_set(self):
        self._policy(self.svc, cf_policy_id='cf-pol-e2')
        ls = collect_left_set(self.env)
        self.assertIn('cf-pol-e2', ls.access_policy_ids,
                      'CS 錨定 policy 的 cf id 走的是另一條 arc'
                      '（SA→route→policy 的 walk 到不了）')

    def test_E3_empty_ids_do_not_enter_the_left_set(self):
        """空值不入左集（否則 `''` 會變成一把萬用鑰匙，讓真孤兒逃掉）。"""
        ls = collect_left_set(self.env)
        self.assertNotIn('', ls.access_app_ids)
        self.assertNotIn(False, ls.access_app_ids)

from .publication_receipt_fixture import configure_receipt, routes_receipt

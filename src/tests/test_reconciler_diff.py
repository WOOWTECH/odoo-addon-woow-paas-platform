"""TS-3c/d: L4 reconciler 差集引擎（右集 − 左集）+ operator inventory client + 端到端。

方向恆為 **右集 − 左集**（CF 有、Odoo 沒有 = 孤兒）：故右集退化 / 空 → 少報（safe miss），
**絕不**把左集全判孤兒。首版只做結構差集：所有候選 reconcilable=False / env_hint=unknown
（分類是 TS-4）。client `get_reconcile_inventory` 對 404/503/連線 fail-loud（B.0）。

設計 doc Phase B.1 / B.2、§機械守線（ReconcileResponse golden）。
"""
import unittest
from unittest.mock import MagicMock

from odoo.tests import TransactionCase, tagged

from ..services.paas_operator import PaaSOperatorClient
from ..services.reconciler import (
    LeftSet,
    ReconcileResult,
    compute_orphans,
    reconcile_orphans,
)


class _FakeClient:
    """最小 operator client 替身：回固定 inventory（避免真打 HTTP）。"""

    def __init__(self, inventory):
        self._inv = inventory

    def get_reconcile_inventory(self):
        return self._inv


@tagged('post_install', '-at_install')
class TestComputeOrphans(TransactionCase):
    """純差集邏輯（compute_orphans）—— 不需 DB，但在 Odoo 套件內跑。"""

    def test_resource_in_left_is_not_orphan(self):
        ls = LeftSet(tunnel_ids={'t-keep'})
        res = compute_orphans([{'id': 't-keep', 'name': 'x', 'kind': 'tunnel'}], ls)
        self.assertEqual(res.orphan_candidates, [])

    def test_resource_not_in_left_is_orphan(self):
        ls = LeftSet(tunnel_ids={'t-keep'})
        res = compute_orphans([{'id': 't-orphan', 'name': 'gone', 'kind': 'tunnel'}], ls)
        self.assertEqual(len(res.orphan_candidates), 1)
        c = res.orphan_candidates[0]
        self.assertEqual(c.id, 't-orphan')
        self.assertEqual(c.kind, 'tunnel')
        self.assertFalse(c.reconcilable)
        self.assertEqual(c.env_hint, 'unknown')
        self.assertTrue(c.reason)

    def test_diff_direction_empty_right_yields_no_orphans(self):
        """方向恆 右−左：右集空 → 零候選（絕不把左集全判孤兒 = 災難防線）。"""
        ls = LeftSet(tunnel_ids={'a', 'b', 'c'}, access_app_ids={'x'})
        res = compute_orphans([], ls)
        self.assertEqual(res.orphan_candidates, [])

    def test_empty_left_all_resources_are_orphans(self):
        """左集空但右有資源 → 全孤兒（正確：CF 有、Odoo 不認）。"""
        ls = LeftSet()
        res = compute_orphans([
            {'id': 'x', 'name': '', 'kind': 'access_app'},
            {'id': 'y', 'name': '', 'kind': 'tunnel'},
        ], ls)
        self.assertEqual(len(res.orphan_candidates), 2)

    def test_each_kind_matches_its_own_leftset(self):
        ls = LeftSet(tunnel_ids={'t1'}, access_app_ids={'a1'}, access_policy_ids={'p1'})
        res = compute_orphans([
            {'id': 't1', 'kind': 'tunnel'}, {'id': 't2', 'kind': 'tunnel'},
            {'id': 'a1', 'kind': 'access_app'}, {'id': 'a2', 'kind': 'access_app'},
            {'id': 'p1', 'kind': 'access_policy'}, {'id': 'p2', 'kind': 'access_policy'},
        ], ls)
        self.assertEqual({c.id for c in res.orphan_candidates}, {'t2', 'a2', 'p2'})

    def test_cross_kind_id_does_not_falsely_match(self):
        """同 id 不同 kind 不互救：'shared' 在 tunnel_ids，但同 id 的 access_app 仍是孤兒。"""
        ls = LeftSet(tunnel_ids={'shared'})
        res = compute_orphans([{'id': 'shared', 'kind': 'access_app'}], ls)
        self.assertEqual(len(res.orphan_candidates), 1)

    def test_partial_input_propagates(self):
        ls = LeftSet(tunnel_ids={'t1'})
        res = compute_orphans([{'id': 't1', 'kind': 'tunnel'}], ls, partial=True)
        self.assertTrue(res.partial)

    def test_unsupported_kind_skipped_and_marks_partial(self):
        """未對映 kind（如 dns_record 在 inventory 補上、但左集對映未加前）→ 跳過 + 標 partial
        （不亂判、不靜默漏 = no-silent-cap）。"""
        ls = LeftSet()
        res = compute_orphans([{'id': 'd1', 'kind': 'dns_record'}], ls)
        self.assertEqual(res.orphan_candidates, [])
        self.assertTrue(res.partial)

    def test_malformed_item_without_id_skipped_marks_partial(self):
        ls = LeftSet()
        res = compute_orphans([{'kind': 'tunnel'}], ls)  # 缺 id
        self.assertEqual(res.orphan_candidates, [])
        self.assertTrue(res.partial)

    def test_all_candidates_reconcilable_false_env_unknown(self):
        """首版鐵律：所有候選 reconcilable=False、env_hint=unknown（分類是 TS-4）。"""
        ls = LeftSet()
        res = compute_orphans([
            {'id': 't', 'kind': 'tunnel'}, {'id': 'a', 'kind': 'access_app'},
        ], ls)
        self.assertTrue(res.orphan_candidates)
        self.assertTrue(all(not c.reconcilable for c in res.orphan_candidates))
        self.assertTrue(all(c.env_hint == 'unknown' for c in res.orphan_candidates))

    def test_result_dict_shape_matches_golden_contract(self):
        """ReconcileResult → dict 對齊 ReconcileResponse golden 的 key 集（防 wire/報表 shape drift）。"""
        import dataclasses
        res = compute_orphans([{'id': 'x', 'name': 'n', 'kind': 'tunnel'}], LeftSet())
        d = dataclasses.asdict(res)
        self.assertEqual(set(d.keys()), {'dry_run', 'partial', 'orphan_candidates'})
        self.assertEqual(
            set(d['orphan_candidates'][0].keys()),
            {'id', 'name', 'kind', 'env_hint', 'reconcilable', 'reason'},
        )


# operator inventory 必填的 shared tunnel id（TS-4 S2 後 _parse_inventory 要求；非空即可，
# 本檔測差集/兩端一致，shared tunnel protected 行為在 test_reconciler_classify 另測）。
_SHARED_TUN = 'shared-tun-uuid-0000'


@tagged('post_install', '-at_install')
class TestReconcileOrphansEndToEnd(TransactionCase):
    """reconcile_orphans(env, client)：env-pin self-check + 蒐左集 + 取右集（client）+ 差集。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = cls.env['woow_paas_platform.workspace'].create({'name': 'L4 diff WS'})
        # TS-4 S2：reconcile_orphans 入口 env-pin self-check 要求 deploy_env ∈ {prod,stg}；
        # 鎖 stg 讓本檔（差集行為）能跑過 self-check（env-pin 的 abort 負向測試在
        # test_reconciler_env_pin.py）。inventory.paas_env 須與此一致（両端一致 check）。
        cls.env['ir.config_parameter'].sudo().set_param('woow_paas_platform.deploy_env', 'stg')

    def test_active_resource_not_orphan_ghost_is(self):
        """SA 持有 tun-keep → inventory 含 tun-keep（活躍）+ tun-ghost（孤兒）→ 只回 ghost。"""
        sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'SAk', 'workspace_id': self.ws.id})
        sa.write({'tunnel_id': 'tun-keep'})
        client = _FakeClient({
            'paas_env': 'stg', 'shared_tunnel_id': _SHARED_TUN,
            'partial': False, 'failed_kinds': [],
            'resources': [
                {'id': 'tun-keep', 'name': 'paas-stg-sa-keep', 'kind': 'tunnel'},
                {'id': 'tun-ghost', 'name': 'paas-stg-sm-ghost', 'kind': 'tunnel'},
            ],
        })
        res = reconcile_orphans(self.env, client=client)
        ids = {c.id for c in res.orphan_candidates}
        self.assertIn('tun-ghost', ids)
        self.assertNotIn('tun-keep', ids, "活躍 tunnel（Odoo 持有）不該被判孤兒")

    def test_propagates_partial_from_inventory(self):
        client = _FakeClient({
            'paas_env': 'stg', 'shared_tunnel_id': _SHARED_TUN,
            'partial': True, 'failed_kinds': ['tunnel'], 'resources': [],
        })
        res = reconcile_orphans(self.env, client=client)
        self.assertTrue(res.partial)

    def test_malformed_inventory_missing_resources_fails_loud(self):
        """200 但 body 缺 resources 鍵（gateway envelope / 欄位改名）→ raise，
        **不**靜默變成空候選 + partial=False（假性『掃完無孤兒』，silent-failure HIGH-1）。
        （只缺 resources，其餘必填鍵齊 → 隔離測『缺 resources』）。"""
        client = _FakeClient({
            'partial': False, 'paas_env': 'stg', 'shared_tunnel_id': _SHARED_TUN,
        })  # 缺 resources
        with self.assertRaises(Exception):
            reconcile_orphans(self.env, client=client)

    def test_malformed_inventory_missing_partial_fails_loud(self):
        """body 缺 partial 鍵 → raise（缺鍵與『完整掃描』無法區分，不 default 成完整，HIGH-2）。"""
        client = _FakeClient({
            'resources': [], 'paas_env': 'stg', 'shared_tunnel_id': _SHARED_TUN,
        })  # 缺 partial
        with self.assertRaises(Exception):
            reconcile_orphans(self.env, client=client)

    def test_resources_wrong_type_fails_loud(self):
        """resources 非 list（型別不符）→ raise，不嘗試對非清單取差集。"""
        client = _FakeClient({
            'partial': False, 'resources': 'oops',
            'paas_env': 'stg', 'shared_tunnel_id': _SHARED_TUN,
        })
        with self.assertRaises(Exception):
            reconcile_orphans(self.env, client=client)

    def test_resources_non_dict_element_fails_loud(self):
        """resources 是 list 但含非 dict 元素（半序列化 / envelope 污染，如 ['foo']）→ clean
        fail-loud RuntimeError，不放任 compute_orphans 的 r.get(...) 對 str 觸發未受控 AttributeError
        （code review silent-failure LOW）。"""
        client = _FakeClient({
            'partial': False, 'resources': ['not-a-dict'],
            'paas_env': 'stg', 'shared_tunnel_id': _SHARED_TUN,
        })
        with self.assertRaises(Exception):
            reconcile_orphans(self.env, client=client)

    def test_failed_kinds_forces_partial_even_if_flag_false(self):
        """operator 防禦性不一致：partial=False 但 failed_kinds 非空 → 結果仍 partial=True
        （不假設 operator 內部一致，MEDIUM-3）。"""
        client = _FakeClient({
            'paas_env': 'stg', 'shared_tunnel_id': _SHARED_TUN,
            'partial': False, 'failed_kinds': ['access_policy'], 'resources': [],
        })
        res = reconcile_orphans(self.env, client=client)
        self.assertTrue(res.partial, "failed_kinds 非空必須強制 partial，即使 operator partial 旗標為 False")

    def test_fail_loud_when_operator_unconfigured(self):
        """client=None 且 operator 未設定 → raise，**不**降級回空候選（B.0 fail-loud）。"""
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', '')
        ICP.set_param('woow_paas_platform.operator_api_key', '')
        with self.assertRaises(Exception):
            reconcile_orphans(self.env, client=None)


class TestGetReconcileInventoryClient(unittest.TestCase):
    """client.get_reconcile_inventory：GET /api/reconcile/inventory + fail-loud（純，無 DB）。"""

    def _client_with_response(self, status_code, payload):
        client = PaaSOperatorClient(base_url='http://op:8000', api_key='k')
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = b'{}'
        resp.json.return_value = payload
        client._session = MagicMock()
        client._session.request.return_value = resp
        return client

    def test_get_inventory_hits_correct_endpoint_and_parses(self):
        client = self._client_with_response(200, {
            'paas_env': 'stg', 'partial': False, 'failed_kinds': [],
            'resources': [{'id': 't1', 'name': 'n', 'kind': 'tunnel'}],
        })
        out = client.get_reconcile_inventory()
        self.assertEqual(out['resources'][0]['id'], 't1')
        args, kwargs = client._session.request.call_args
        # 必須是 GET /api/reconcile/inventory
        self.assertEqual(args[0] if args else kwargs.get('method'), 'GET')
        called_url = (args[1] if len(args) > 1 else kwargs.get('url', '')) or ''
        self.assertIn('/api/reconcile/inventory', called_url)

    def test_get_inventory_fail_loud_on_404(self):
        """404（operator 太舊無此端點）→ raise，絕不降級回空 inventory。"""
        client = self._client_with_response(404, {'detail': 'not found'})
        with self.assertRaises(Exception):
            client.get_reconcile_inventory()

    def test_get_inventory_fail_loud_on_503(self):
        """503（env-pin 未鎖）→ raise。"""
        client = self._client_with_response(503, {'detail': 'env not pinned'})
        with self.assertRaises(Exception):
            client.get_reconcile_inventory()

"""#277: L4 reconciler namespace 左集差集 + env 分類。

cluster 唯讀列 ``paas-ws-*`` ns 當右集；Odoo 對 live ``helm_namespace`` 左集差集出
「cluster 有、Odoo 無」的孤兒 ns（只報告、不刪；``reconcilable`` 恆 False）。

**最高衝擊正確性點**：左集必須 union **CS 共用 ns（``cloud_service.helm_namespace``）
∪ agent per-project ns（``agent_session.helm_namespace``）**——漏蒐 agent ns 會把所有
活躍 agent ns 誤判孤兒（災難）。``state='deleting'`` 的 CS 排除（過渡態）；helm_namespace
非唯一（多 CS 共用）→ 入 set 去重。

設計 spec：``docs/superpowers/specs/2026-07-07-w1-ns-quota-governance-design.md``。
"""
from odoo.tests import TransactionCase, tagged

from ..services import reconciler as R
from ..services.reconciler import (
    LeftSet,
    collect_left_set,
    compute_orphans,
    classify_candidates,
)


@tagged('post_install', '-at_install')
class TestNamespaceDiff(TransactionCase):
    """compute_orphans 對 kind='namespace' 走 namespace_names 左集差集。"""

    def test_orphan_namespace_not_in_left_set(self):
        """cluster 兩 ns，Odoo 左集只認得一個 → 另一個成孤兒；活躍 ns 不成孤兒。"""
        left = LeftSet()
        left.namespace_names = {'paas-ws-live'}   # Odoo 只認得這個
        # operator 右集回兩個 ns
        resources = [
            {'id': 'paas-ws-live', 'name': 'paas-ws-live', 'kind': 'namespace'},
            {'id': 'paas-ws-orphan', 'name': 'paas-ws-orphan', 'kind': 'namespace'},
        ]
        result = compute_orphans(resources, left)
        orphans = {c.id for c in result.orphan_candidates}
        self.assertEqual(orphans, {'paas-ws-orphan'},
                         "live ns 不該報、只有 cluster-有-Odoo-無 的 ns 成孤兒")
        # 差集鍵 = ns 名本身、非 partial（kind 有對映）
        self.assertFalse(result.partial,
                         "namespace kind 已有 _KIND_TO_LEFTSET_ATTR 對映 → 不該標 partial")

    def test_namespace_kind_not_partial(self):
        """namespace kind 有對映 → 不因『未對映』被 skip + 標 partial（同批上線鐵律）。"""
        left = LeftSet(namespace_names={'paas-ws-live'})
        result = compute_orphans(
            [{'id': 'paas-ws-live', 'name': 'paas-ws-live', 'kind': 'namespace'}], left)
        self.assertEqual(result.orphan_candidates, [])
        self.assertFalse(result.partial)


@tagged('post_install', '-at_install')
class TestNamespaceLeftSetUnion(TransactionCase):
    """collect_left_set 的 namespace_names = CS ∪ agent ns（漏 agent = 災難）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = cls.env['woow_paas_platform.workspace'].create({'name': 'NS WS'})
        cls.template = cls.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'NS Template',
            'slug': 'nst',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'nst',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1,
            'min_ram_gb': 1.0,
            'min_storage_gb': 1,
        })
        cls.project = cls.env['project.project'].create({'name': 'NS Proj'})
        cls.CS = cls.env['woow_paas_platform.cloud_service']
        cls.Agent = cls.env['woow_paas_platform.agent_session']

    def test_collect_left_set_unions_cs_and_agent_ns(self):
        """CS.helm_namespace ∪ agent_session.helm_namespace 都要進 namespace_names。"""
        self.CS.create({
            'name': 'CS-w', 'workspace_id': self.ws.id, 'template_id': self.template.id,
            'helm_namespace': 'paas-ws-w', 'helm_release_name': 'app-w', 'state': 'running',
        })
        self.Agent.create({
            'project_id': self.project.id,
            'helm_namespace': 'paas-ws-pa-x', 'state': 'active',
        })
        ls = collect_left_set(self.env)
        self.assertIn('paas-ws-w', ls.namespace_names,
                      "CS 共用 ns 未進左集")
        self.assertIn('paas-ws-pa-x', ls.namespace_names,
                      "漏 agent ns 會把所有活躍 agent ns 誤判孤兒（災難）")

    def test_deleting_cs_ns_excluded(self):
        """state='deleting' 的 CS ns 不進左集（過渡態，即將消失，避免誤判為活躍）。"""
        self.CS.create({
            'name': 'CS-del', 'workspace_id': self.ws.id, 'template_id': self.template.id,
            'helm_namespace': 'paas-ws-deleting', 'helm_release_name': 'app-d',
            'state': 'deleting',
        })
        ls = collect_left_set(self.env)
        self.assertNotIn('paas-ws-deleting', ls.namespace_names,
                         "deleting CS 的 ns 不該當活躍白名單")

    def test_shared_ns_deduped(self):
        """多 CS 共用同一 helm_namespace → set 去重（非唯一鍵）。"""
        self.CS.create({
            'name': 'CS-a', 'workspace_id': self.ws.id, 'template_id': self.template.id,
            'helm_namespace': 'paas-ws-shared', 'helm_release_name': 'app-a', 'state': 'running',
        })
        self.CS.create({
            'name': 'CS-b', 'workspace_id': self.ws.id, 'template_id': self.template.id,
            'helm_namespace': 'paas-ws-shared', 'helm_release_name': 'app-b', 'state': 'running',
        })
        ls = collect_left_set(self.env)
        # set 天生去重，斷言值在集合內即可（不會重覆）
        self.assertIn('paas-ws-shared', ls.namespace_names)

    def test_empty_helm_namespace_not_in_left_set(self):
        """未部署 CS（helm_namespace 為 False）不入左集（in-flight 半完成不誤當持有）。"""
        self.CS.create({
            'name': 'CS-nofield', 'workspace_id': self.ws.id, 'template_id': self.template.id,
            'state': 'pending',
        })
        ls = collect_left_set(self.env)
        self.assertNotIn(False, ls.namespace_names)
        self.assertNotIn('', ls.namespace_names)

    def test_agent_empty_helm_namespace_not_in_left_set(self):
        """spawning agent（helm_namespace 為 False）不入左集。

        agent leg 的顯式守護（CS 側已有 test_empty_helm_namespace_not_in_left_set）：
        _clean 對 CS/agent 兩表對稱，此測試鎖住 agent 半建 ns 不誤當 live、亦不因 leg
        漏蒐而讓活躍 agent ns 判孤兒。"""
        self.Agent.create({
            'project_id': self.project.id, 'state': 'spawning',
        })
        ls = collect_left_set(self.env)
        self.assertNotIn(False, ls.namespace_names)
        self.assertNotIn('', ls.namespace_names)


@tagged('post_install', '-at_install')
class TestNamespaceEnvHint(TransactionCase):
    """ns 專用 env 分類（_ns_env_hint）——classify_env_hint 的 paas-{env}-{sa|sm|cs} 格式不認
    paas-ws-* ns，故另寫；classify_candidates 對 kind='namespace' 用之覆寫 env_hint。"""

    def test_ns_env_hint_prefixes(self):
        self.assertEqual(R._ns_env_hint('paas-ws-test-op-abc'), 'dev')
        self.assertEqual(R._ns_env_hint('paas-ws-dev-abc'), 'dev')
        self.assertEqual(R._ns_env_hint('paas-ws-stg-abc'), 'stg')
        self.assertEqual(R._ns_env_hint('paas-ws-stg-pa-abc'), 'stg')
        self.assertEqual(R._ns_env_hint('paas-ws-abc'), 'prod')
        self.assertEqual(R._ns_env_hint('paas-ws-pa-abc'), 'prod')
        self.assertEqual(R._ns_env_hint('something-else'), 'unknown')

    def test_classify_overrides_ns_env_hint(self):
        """classify_candidates 對 namespace 候選用 _ns_env_hint 覆寫；reconcilable 仍恆 False。"""
        result = compute_orphans(
            [{'id': 'paas-ws-stg-orphan', 'name': 'paas-ws-stg-orphan', 'kind': 'namespace'}],
            LeftSet())
        classify_candidates(result, pinned_env='stg', shared_tunnel_id='shared-tun-x')
        c = result.orphan_candidates[0]
        self.assertEqual(c.env_hint, 'stg',
                         "ns 走 _ns_env_hint（classify_env_hint 不認 paas-ws-* → 會落 unknown）")
        self.assertFalse(c.reconcilable, "ns 不在 _RECONCILABLE_ELIGIBLE_KINDS → 恆 False")

    def test_classify_prod_ns_env_hint(self):
        """prod ns（paas-ws-*、無 stg/dev token）→ env_hint='prod'（非 unknown）。"""
        result = compute_orphans(
            [{'id': 'paas-ws-prodorphan', 'name': 'paas-ws-prodorphan', 'kind': 'namespace'}],
            LeftSet())
        classify_candidates(result, pinned_env='prod', shared_tunnel_id='shared-tun-x')
        self.assertEqual(result.orphan_candidates[0].env_hint, 'prod')
        self.assertFalse(result.orphan_candidates[0].reconcilable)

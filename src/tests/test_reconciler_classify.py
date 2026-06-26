"""TS-4 S3+S4：L4 reconciler 分類層 —— env attribution + 結構 allowlist + protected + tripwire。

``classify_candidates`` 對 ``compute_orphans`` 的 raw 候選 enrich：

- **S3 env attribution（R2）**：``env_hint`` 由 name 的 env token 判（``classify_env_hint``）；
  cross-env（explicit token != pinned）標 reason、reconcilable=False；無 token（prod/legacy
  byte-identical 不可分）→ ``env_hint='unknown'``。
- **S4 結構 allowlist（R1）**：``reconcilable`` 結構預設 False；升 True 需 kind∈owner-eligible
  （首版空集）AND env_hint≠unknown（硬不變式）→ 三類全 un-reconcilable → 恆 False。
- **S4 protected（R5）**：id == operator-reported ``shared_tunnel_id`` → reconcilable=False +
  reason='protected'（避免人照報表刪 shared tunnel 本體 = cf-ns-orphan 事故重演）。
- **S4 tripwire（R5）**：reconcilable=True 候選若 env_hint=unknown / cross-env / 是 shared
  tunnel 本體 → fail-loud raise（首版 reconcilable 恆 False，以合成注入測其真會炸）。

純邏輯（pure，不需 DB），但置於 TransactionCase 以走 Odoo test loader（與 TestComputeOrphans 同）。
設計 doc §C.4（1/2/3/9）、Phase C.1。
"""
from odoo.tests import TransactionCase, tagged

from ..services.reconciler import (
    LeftSet,
    OrphanCandidate,
    classify_candidates,
    compute_orphans,
    _run_tripwires,
)


def _classify(resources, left_set, pinned_env, shared_tunnel_id, partial=False):
    res = compute_orphans(resources, left_set, partial=partial)
    return classify_candidates(res, pinned_env=pinned_env, shared_tunnel_id=shared_tunnel_id)


@tagged('post_install', '-at_install')
class TestClassifyEnvAttribution(TransactionCase):
    """S3：env_hint 與 cross-env 標記。"""

    def test_same_env_token_is_own_env(self):
        r = _classify([{'id': 'g1', 'name': 'paas-stg-sm-ghost', 'kind': 'tunnel'}],
                      LeftSet(), 'stg', 'shared-x')
        c = r.orphan_candidates[0]
        self.assertEqual(c.env_hint, 'stg')
        self.assertFalse(c.reconcilable)
        self.assertNotIn('cross-env', c.reason)

    def test_cross_env_token_marked(self):
        """prod-pinned 看到 stg 資源 → cross-env、reconcilable=False。"""
        r = _classify([{'id': 'g2', 'name': 'paas-stg-sm-ghost', 'kind': 'tunnel'}],
                      LeftSet(), 'prod', 'shared-x')
        c = r.orphan_candidates[0]
        self.assertEqual(c.env_hint, 'stg')
        self.assertIn('cross-env', c.reason)
        self.assertFalse(c.reconcilable)

    def test_no_token_is_unknown_prod_legacy_indistinguishable(self):
        r = _classify([{'id': 'g3', 'name': 'paas-sm-ghost', 'kind': 'tunnel'}],
                      LeftSet(), 'stg', 'shared-x')
        c = r.orphan_candidates[0]
        self.assertEqual(c.env_hint, 'unknown')
        self.assertIn('不可分', c.reason)

    def test_non_paas_name_is_unknown(self):
        """官網 / apex / 行銷 → unknown（結構上拿不到 reconcilable，R1）。"""
        r = _classify([{'id': 'apex', 'name': 'woowtech.io', 'kind': 'tunnel'}],
                      LeftSet(), 'stg', 'shared-x')
        c = r.orphan_candidates[0]
        self.assertEqual(c.env_hint, 'unknown')
        self.assertFalse(c.reconcilable)


@tagged('post_install', '-at_install')
class TestClassifyProtectedAndAllowlist(TransactionCase):
    """S4：protected（shared tunnel 本體）+ 結構 reconcilable=False。"""

    def test_shared_tunnel_body_marked_protected(self):
        r = _classify([{'id': 'shared-uuid', 'name': 'anything', 'kind': 'tunnel'}],
                      LeftSet(), 'stg', 'shared-uuid')
        c = r.orphan_candidates[0]
        self.assertFalse(c.reconcilable)
        self.assertIn('protected', c.reason)
        self.assertIn('shared tunnel', c.reason)

    def test_similar_uuid_not_falsely_protected(self):
        """精確比對：只差末段的相似 UUID 不該被誤判 protected（防前綴過度匹配）。"""
        r = _classify([{'id': 'shared-uuid-DIFFERENT', 'name': 'paas-stg-sm-x', 'kind': 'tunnel'}],
                      LeftSet(), 'stg', 'shared-uuid')
        self.assertNotIn('protected', r.orphan_candidates[0].reason)

    def test_all_three_kinds_un_reconcilable(self):
        """首版三類（tunnel / access_app / access_policy）全 reconcilable=False（即使前綴對）。"""
        r = _classify([
            {'id': 't', 'name': 'paas-stg-sa-x', 'kind': 'tunnel'},
            {'id': 'a', 'name': 'paas-stg-sa-x', 'kind': 'access_app'},
            {'id': 'p', 'name': 'paas-stg-sa-x-allow', 'kind': 'access_policy'},
        ], LeftSet(), 'stg', 'shared-x')
        self.assertTrue(all(not c.reconcilable for c in r.orphan_candidates))

    def test_policy_candidate_has_resync_drift_note(self):
        """C.2：access_policy 候選補 resync 漂移人工核對註記（防誤刪活躍 policy）。"""
        r = _classify([{'id': 'p', 'name': 'paas-stg-sa-x-allow', 'kind': 'access_policy'}],
                      LeftSet(), 'stg', 'shared-x')
        c = r.orphan_candidates[0]
        self.assertIn('resync', c.reason)
        self.assertIn('CF app 實際引用', c.reason)

    def test_no_silent_drop_all_candidates_retained(self):
        """cross-env / protected 靠 reconcilable=False 達成 skip，**不**從清單移除（no-silent-drop）。"""
        r = _classify([
            {'id': 'a', 'name': 'paas-stg-sm-1', 'kind': 'tunnel'},
            {'id': 'shared-x', 'name': 'x', 'kind': 'tunnel'},
            {'id': 'b', 'name': 'paas-stg-sm-2', 'kind': 'tunnel'},
        ], LeftSet(), 'prod', 'shared-x')
        self.assertEqual(len(r.orphan_candidates), 3)

    def test_cross_env_structurally_gated_not_tripwire_abort(self):
        """code review option A：cross-env 由 _is_reconcilable **結構** gate 成 False（與 unknown
        對稱），而非靠 tripwire abort。patch eligible 含 tunnel 模擬未來開 DNS：cross-env tunnel
        仍 reconcilable=False（正常 cross-env 常態不該被升 True 再 tripwire 整輪 hard-fail）。"""
        from unittest.mock import patch
        from ..services import reconciler
        with patch.object(reconciler, '_RECONCILABLE_ELIGIBLE_KINDS', frozenset({'tunnel'})):
            # prod-pinned 看到 stg tunnel（cross-env）→ 結構 gate → False（不 raise）
            r = _classify([{'id': 'x', 'name': 'paas-stg-sm-1', 'kind': 'tunnel'}],
                          LeftSet(), 'prod', 'shared-x')
            self.assertFalse(r.orphan_candidates[0].reconcilable,
                             "cross-env 須結構 gate 成 False，不靠 tripwire abort")
            # 非 vacuity 對照：同 env + eligible kind → reconcilable=True（gate 不是恆 False）
            r2 = _classify([{'id': 'y', 'name': 'paas-stg-sm-2', 'kind': 'tunnel'}],
                           LeftSet(), 'stg', 'shared-x')
            self.assertTrue(r2.orphan_candidates[0].reconcilable,
                            "同 env + eligible kind → reconcilable=True（gate 非恆 False）")
            # 同 env + eligible 但 shared tunnel 本體 → protected-override 壓回 False（順序正確性）
            r3 = _classify([{'id': 'shared-x', 'name': 'paas-stg-sm-3', 'kind': 'tunnel'}],
                           LeftSet(), 'stg', 'shared-x')
            self.assertFalse(r3.orphan_candidates[0].reconcilable,
                             "shared tunnel 本體 → protected 壓回 False（predicate 之後）")


@tagged('post_install', '-at_install')
class TestClassifyTripwires(TransactionCase):
    """S4：三條 tripwire 合成注入（reconcilable 首版恆 False → 須手構 True 才測得到）。"""

    def test_tripwire_unknown_reconcilable_raises(self):
        bad = OrphanCandidate(id='x', name='paas-sm-1', kind='tunnel',
                              env_hint='unknown', reconcilable=True, reason='')
        with self.assertRaises(Exception):
            _run_tripwires([bad], pinned_env='stg', shared_tunnel_id='s')

    def test_tripwire_cross_env_reconcilable_raises(self):
        bad = OrphanCandidate(id='x', name='paas-stg-sm-1', kind='tunnel',
                              env_hint='stg', reconcilable=True, reason='')
        with self.assertRaises(Exception):
            _run_tripwires([bad], pinned_env='prod', shared_tunnel_id='s')

    def test_tripwire_shared_tunnel_reconcilable_raises(self):
        bad = OrphanCandidate(id='s', name='x', kind='tunnel',
                              env_hint='stg', reconcilable=True, reason='')
        with self.assertRaises(Exception):
            _run_tripwires([bad], pinned_env='stg', shared_tunnel_id='s')

    def test_tripwire_safe_reconcilable_does_not_raise(self):
        """非 vacuity：env_hint==pinned 且非 shared 且非 unknown 的 reconcilable=True 不該 raise
        （證明 tripwire 不是恆炸的擺設）。"""
        ok = OrphanCandidate(id='x', name='paas-stg-sm-1', kind='tunnel',
                             env_hint='stg', reconcilable=True, reason='')
        # 不應 raise
        _run_tripwires([ok], pinned_env='stg', shared_tunnel_id='s')

    def test_classify_candidates_invokes_run_tripwires(self):
        """釘住 classify_candidates 末端真的把 tripwire 接進 pipeline（拆掉該行 → 此測試紅）。

        必要性：reconcilable 首版恆 False → tripwire 在真實流程恆 no-op；若無此測試，刪掉
        classify 內的 _run_tripwires 呼叫（拆下最後一道 fail-loud 防線）46 測試仍全綠（合成
        測試繞過 classify 直呼 _run_tripwires）。用 mock 釘整合點存在。"""
        from unittest.mock import patch
        from ..services import reconciler
        with patch.object(reconciler, '_run_tripwires') as mock_tw:
            _classify([{'id': 'x', 'name': 'paas-stg-sm-1', 'kind': 'tunnel'}],
                      LeftSet(), 'stg', 'shared-x')
            mock_tw.assert_called_once()

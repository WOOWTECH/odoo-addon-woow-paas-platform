"""TS-5 Phase D Slice A：dry-run 孤兒報表 model `woow_paas_platform.reconcile_report`。

cron（Slice B）跑 ``reconcile_orphans`` 後，把 ``ReconcileResult.orphan_candidates`` 落成
**可呈現的 snapshot**（每筆候選一列），供 ``/woow`` admin 後台報表頁（Slice C/D）讀。

本片只測 **model + ACL + snapshot 持久化**：

- **欄位**：``kind / resource_uid / name / env_hint / reconcilable / reason / scanned_at``
  （design D.1 的 ``id`` 改名 ``resource_uid``——``id`` 是 Odoo 保留欄位、且 ``*_id`` 後綴會被
  誤認 Many2one；``resource_uid`` 為 inventory 資源的唯一識別，env-neutral 可涵蓋未來 ns/PVC）。
  另加 ``partial``（completeness gate 顯示用，design D.3）。
- **``_record_snapshot(result)``**：snapshot-**replace** 語意——每次 cron 跑 = 清前次 + 寫本次
  （報表是「最新一次掃描」的當前快照，非歷史累積）。**空 result → 清空**（真無孤兒，不是保留舊列）。
- **admin-only ACL**：只給 ``base.group_system`` RWCU、**不**給 ``base.group_user`` 任何列 →
  非 admin 連 ORM search 都 ``AccessError``（使用者要求「僅 admin 可見」的硬把關第一層；
  controller 端 ``has_group`` 是第二層，Slice C）。

設計 doc Phase D（§185-207）、§C.4 #7（報表層不得以 reconcilable 過濾，須全列呈現）。
"""
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

from ..services.reconciler import OrphanCandidate, ReconcileResult


@tagged('post_install', '-at_install')
class TestReconcileReportModel(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Report = cls.env['woow_paas_platform.reconcile_report']

    def _result(self, candidates, partial=False):
        return ReconcileResult(dry_run=True, partial=partial, orphan_candidates=candidates)

    def _candidate(self, **kw):
        base = dict(id='t-1', name='paas-stg-sm-x', kind='tunnel',
                    env_hint='stg', reconcilable=False, reason='r')
        base.update(kw)
        return OrphanCandidate(**base)

    # ── 欄位 shape ────────────────────────────────────────────────────────
    def test_model_has_required_fields(self):
        fields = self.Report._fields
        for f in ('kind', 'resource_uid', 'name', 'env_hint',
                  'reconcilable', 'reason', 'scanned_at', 'partial'):
            self.assertIn(f, fields, "reconcile_report 缺欄位 %s" % f)

    # ── _record_snapshot 映射 ────────────────────────────────────────────
    def test_record_snapshot_creates_rows_from_candidates(self):
        self.Report._record_snapshot(self._result([
            self._candidate(id='t-1', name='paas-stg-sm-a', kind='tunnel'),
            self._candidate(id='a-2', name='paas-stg-sa-b', kind='access_app', reason='r2'),
        ]))
        rows = self.Report.search([])
        self.assertEqual(len(rows), 2)
        by_uid = {r.resource_uid: r for r in rows}
        self.assertEqual(by_uid['t-1'].kind, 'tunnel')
        self.assertEqual(by_uid['t-1'].name, 'paas-stg-sm-a')
        self.assertEqual(by_uid['a-2'].kind, 'access_app')
        self.assertEqual(by_uid['a-2'].reason, 'r2')

    def test_record_snapshot_maps_reconcilable_and_env_hint(self):
        # 非 vacuity：分別給 True/False env_hint，確認逐筆忠實映射（非全部寫死同值）
        self.Report._record_snapshot(self._result([
            self._candidate(id='x', env_hint='stg', reconcilable=False),
            self._candidate(id='y', env_hint='unknown', reconcilable=False, reason='u'),
        ]))
        by_uid = {r.resource_uid: r for r in self.Report.search([])}
        self.assertEqual(by_uid['x'].env_hint, 'stg')
        self.assertEqual(by_uid['y'].env_hint, 'unknown')
        self.assertFalse(by_uid['x'].reconcilable)
        self.assertFalse(by_uid['y'].reconcilable)

    def test_record_snapshot_stores_partial(self):
        self.Report._record_snapshot(self._result([self._candidate()], partial=True))
        self.assertTrue(self.Report.search([], limit=1).partial,
                        "partial result → 每列標 partial（completeness gate 顯示）")

    def test_record_snapshot_stamps_scanned_at(self):
        self.Report._record_snapshot(self._result([self._candidate()]))
        self.assertTrue(self.Report.search([], limit=1).scanned_at, "scanned_at 應被戳記")

    # ── snapshot-replace 語意 ────────────────────────────────────────────
    def test_record_snapshot_replaces_prior(self):
        self.Report._record_snapshot(self._result([self._candidate(id='old')]))
        self.Report._record_snapshot(self._result([
            self._candidate(id='new1'), self._candidate(id='new2')]))
        uids = self.Report.search([]).mapped('resource_uid')
        self.assertNotIn('old', uids, "舊 snapshot 應被本次取代")
        self.assertEqual(set(uids), {'new1', 'new2'})

    def test_empty_snapshot_clears_all(self):
        """空 result（真無孤兒）→ 清光舊列，不保留（否則舊孤兒永遠掛在報表上誤導人工）。"""
        self.Report._record_snapshot(self._result([self._candidate()]))
        self.Report._record_snapshot(self._result([]))
        self.assertEqual(self.Report.search_count([]), 0)

    # ── no-silent-drop（§C.4 #7）：reconcilable=False 候選一樣入列 ─────────
    def test_unreconcilable_candidates_are_retained(self):
        """報表 model 不得以 reconcilable 過濾——cross-env / protected / unknown（全 False）
        仍須落列，靠 reconcilable=False 達成「skip 刪除」而非「從清單消失」。"""
        self.Report._record_snapshot(self._result([
            self._candidate(id='p', reconcilable=False, reason='protected：shared tunnel 本體'),
            self._candidate(id='c', reconcilable=False, reason='cross-env'),
        ]))
        self.assertEqual(self.Report.search_count([]), 2,
                         "reconcilable=False 候選也必須保留呈現（no-silent-drop）")

    # ── admin-only ACL（第一層硬把關）────────────────────────────────────
    def test_non_admin_cannot_read(self):
        non_admin = self.env['res.users'].sudo().create({
            'name': 'recon-nonadmin', 'login': 'recon-nonadmin',
            'email': 'recon-nonadmin@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.Report._record_snapshot(self._result([self._candidate()]))
        with self.assertRaises(AccessError):
            self.Report.with_user(non_admin).search([])

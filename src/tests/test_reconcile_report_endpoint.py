"""TS-5 Phase D Slice C：報表 controller endpoints（admin-only）+ /api/me is_admin。

HttpCase 驗 endpoint 層（admin 雙層把關第二層 + no-silent-drop + 單筆 TOCTOU 重查）：

- **``/api/me`` 加 ``is_admin``**：前端 nav 條件渲染用（admin 才顯示報表入口）。
- **``/api/reconcile-report/list``（admin-only）**：``has_group('base.group_system')`` 真擋；
  回**全部**候選含 reconcilable=False（cross-env / protected / unknown）——**不得以
  reconcilable 過濾**（§C.4 #7 no-silent-drop：上游保留、下游濾掉＝假性 no-silent-drop）。
- **``/api/reconcile-report/reverify``（admin-only）**：單筆唯讀重算（重跑 ``reconcile_orphans``
  看該 (kind, resource_uid) 是否仍為孤兒，防 TOCTOU：報表當時是孤兒、人去刪時已被新 provision
  重用同資源）。**唯讀、不刪、不改 stored row**。

admin 隱藏只是 cosmetic、controller ``has_group`` 才是真把關（admin-gating reader gotcha）。
設計 doc Phase D §D.2（TOCTOU）、§C.4 #7（no-silent-drop）。
"""
import json as json_lib
import uuid
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

from ..services.reconciler import OrphanCandidate, ReconcileResult

_EP_RECON = 'odoo.addons.woow_paas_platform.controllers.reconcile_report.reconcile_orphans'


@tagged('post_install', '-at_install')
class TestReconcileReportEndpoint(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.pwd = 'recon_pwd'
        group_user = cls.env.ref('base.group_user')
        group_system = cls.env.ref('base.group_system')
        cls.admin_user = cls.env['res.users'].create({
            'name': 'Recon Admin %s' % suffix,
            'login': 'recon_admin_%s' % suffix,
            'password': cls.pwd,
            'groups_id': [(6, 0, [group_system.id, group_user.id])],
        })
        cls.normal_user = cls.env['res.users'].create({
            'name': 'Recon User %s' % suffix,
            'login': 'recon_user_%s' % suffix,
            'password': cls.pwd,
            'groups_id': [(6, 0, [group_user.id])],
        })
        cls.Report = cls.env['woow_paas_platform.reconcile_report']

    def _post(self, path, params=None):
        resp = self.url_open(
            path,
            data=json_lib.dumps({'params': params or {}}),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        return resp.json()['result']

    def _seed(self, candidates, partial=False):
        self.Report._record_snapshot(
            ReconcileResult(dry_run=True, partial=partial, orphan_candidates=candidates))

    def _cand(self, uid, **kw):
        base = dict(id=uid, name='paas-stg-sm-%s' % uid, kind='tunnel',
                    env_hint='stg', reconcilable=False, reason='r')
        base.update(kw)
        return OrphanCandidate(**base)

    # ── /api/me is_admin ─────────────────────────────────────────────────
    def test_me_exposes_is_admin_true_for_admin(self):
        self.authenticate(self.admin_user.login, self.pwd)
        res = self._post('/api/me')
        self.assertTrue(res['success'], res)
        self.assertTrue(res['data']['is_admin'], "admin → is_admin=True")

    def test_me_exposes_is_admin_false_for_normal(self):
        self.authenticate(self.normal_user.login, self.pwd)
        res = self._post('/api/me')
        self.assertTrue(res['success'], res)
        self.assertFalse(res['data']['is_admin'], "非 admin → is_admin=False")

    # ── list：admin-only + no-silent-drop ────────────────────────────────
    def test_list_admin_sees_all_candidates_incl_unreconcilable(self):
        self._seed([
            self._cand('a', reconcilable=False, reason='cross-env'),
            self._cand('b', reconcilable=False, reason='protected'),
        ])
        self.authenticate(self.admin_user.login, self.pwd)
        res = self._post('/api/reconcile-report/list')
        self.assertTrue(res['success'], res)
        uids = {c['resource_uid'] for c in res['data']['candidates']}
        self.assertEqual(uids, {'a', 'b'},
                         "報表須全列呈現含 reconcilable=False（no-silent-drop §C.4#7）")

    def test_list_returns_scanned_at_and_partial(self):
        self._seed([self._cand('a')], partial=True)
        self.authenticate(self.admin_user.login, self.pwd)
        res = self._post('/api/reconcile-report/list')
        self.assertTrue(res['success'], res)
        self.assertTrue(res['data']['scanned_at'], "回 scanned_at 供 TOCTOU 顯示")
        self.assertTrue(res['data']['partial'], "partial 掃描旗標須上拋（completeness 顯示）")

    def test_list_rejects_non_admin(self):
        self._seed([self._cand('a')])
        self.authenticate(self.normal_user.login, self.pwd)
        res = self._post('/api/reconcile-report/list')
        self.assertFalse(res['success'], "非 admin 不得讀報表（controller 真把關）")

    # ── reverify：admin-only + 唯讀重算 ──────────────────────────────────
    def test_reverify_admin_still_orphan(self):
        self.authenticate(self.admin_user.login, self.pwd)
        fake = ReconcileResult(dry_run=True, partial=False,
                               orphan_candidates=[self._cand('z')])
        with patch(_EP_RECON, return_value=fake):
            res = self._post('/api/reconcile-report/reverify',
                             {'kind': 'tunnel', 'resource_uid': 'z'})
        self.assertTrue(res['success'], res)
        self.assertTrue(res['data']['still_orphan'], "重算仍在孤兒清單 → still_orphan=True")

    def test_reverify_no_longer_orphan_when_absent(self):
        self.authenticate(self.admin_user.login, self.pwd)
        fake = ReconcileResult(dry_run=True, partial=False, orphan_candidates=[])
        with patch(_EP_RECON, return_value=fake):
            res = self._post('/api/reconcile-report/reverify',
                             {'kind': 'tunnel', 'resource_uid': 'gone'})
        self.assertTrue(res['success'], res)
        self.assertFalse(res['data']['still_orphan'],
                         "重算已不在清單（可能已被新 provision 重用）→ still_orphan=False")

    def test_reverify_rejects_non_admin(self):
        self.authenticate(self.normal_user.login, self.pwd)
        res = self._post('/api/reconcile-report/reverify',
                         {'kind': 'tunnel', 'resource_uid': 'z'})
        self.assertFalse(res['success'], "非 admin 不得 reverify（controller 真把關）")

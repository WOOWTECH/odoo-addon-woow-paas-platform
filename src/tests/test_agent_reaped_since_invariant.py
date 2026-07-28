import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'
_CLIENT_PATH = (
    "odoo.addons.woow_paas_platform.services."
    "agent_runtime_client.get_agent_runtime_client"
)


@tagged('post_install', '-at_install')
class TestReapedSinceInvariant(TransactionCase):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(
            os.environ, {_ENV_NAMESPACE_SECRET: 'unit-test-secret-0123456789'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.project = self.env['project.project'].sudo().create({'name': 'RS'})
        self.session = self.Session.create({'project_id': self.project.id})

    def test_drift_style_write_stamps_reaped_since(self):
        # drift / webhook 只帶 state，無 reaped_since
        self.assertFalse(self.session.reaped_since)
        self.session.write({'state': 'reaped'})
        self.assertEqual(self.session.state, 'reaped')
        self.assertTrue(self.session.reaped_since)  # ← 被 override 補上

    def test_does_not_overwrite_existing_reaped_since(self):
        old = fields.Datetime.now() - timedelta(days=5)
        self.session.write({'state': 'reaped', 'reaped_since': old})
        # 再被 drift 寫一次 state='reaped'（無 reaped_since）→ 保留原值
        self.session.write({'state': 'reaped'})
        self.assertEqual(self.session.reaped_since, old)

    def test_explicit_reaped_since_in_vals_wins(self):
        explicit = fields.Datetime.now() - timedelta(days=3)
        self.session.write({'state': 'reaped', 'reaped_since': explicit})
        self.assertEqual(self.session.reaped_since, explicit)

    def test_leaving_reaped_clears_reaped_since(self):
        self.session.write({'state': 'reaped'})
        self.assertTrue(self.session.reaped_since)
        self.session.write({'state': 'active'})  # resume
        self.assertFalse(self.session.reaped_since)

    def test_re_reap_after_resume_gets_fresh_reaped_since(self):
        self.session.write({'state': 'reaped'})
        self.session.write({'state': 'active'})    # 清空
        self.assertFalse(self.session.reaped_since)
        self.session.write({'state': 'reaped'})    # 重新 reap → 新值
        self.assertTrue(self.session.reaped_since)

    def test_reaper_still_sets_reaped_since(self):
        # 回歸：reaper 同 vals 帶 reaped_since，不被覆蓋
        self.session.write({'state': 'active', 'last_active':
                            fields.Datetime.now() - timedelta(hours=2)})
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        self.assertEqual(self.session.state, 'reaped')
        self.assertTrue(self.session.reaped_since)
        client.reap.assert_called_once()

    def test_gc_now_collects_drift_reaped_session(self):
        # 本 gap 的整合守線：drift-style reaped（只寫 state）→ override 補 reaped_since
        # → GC-eligible。負 gc 窗讓 override 補的 now 立即過期，故本測試「真的依賴」
        # override：若 override 不補值，reaped_since=False → GC skip → delete 不被呼叫。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.agent_gc_seconds', '-10')
        self.session.write({'state': 'reaped'})       # 只寫 state（drift/webhook 樣式）
        self.assertTrue(self.session.reaped_since)     # override 必須補上
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_called_once_with(self.session.project_namespace_hash)
        self.assertEqual(self.session.state, 'absent')

    def test_create_with_reaped_state_stamps_reaped_since(self):
        # 對稱性：create({'state':'reaped'}) 也鏡像 invariant（future-proof，
        # 雖目前無生產 caller 直接 create reaped session）
        proj2 = self.env['project.project'].sudo().create({'name': 'RS2'})
        sess = self.Session.create({'project_id': proj2.id, 'state': 'reaped'})
        self.assertTrue(sess.reaped_since)

    def test_create_with_explicit_reaped_since_preserved(self):
        proj3 = self.env['project.project'].sudo().create({'name': 'RS3'})
        explicit = fields.Datetime.now() - timedelta(days=2)
        sess = self.Session.create({
            'project_id': proj3.id, 'state': 'reaped', 'reaped_since': explicit})
        self.assertEqual(sess.reaped_since, explicit)

    def test_migration_backfills_reaped_null(self):
        # 造一筆 reaped + reaped_since=NULL（migration 18.0.1.0.40 的目標）：
        # 先 flush ORM 寫入落 DB，再用 raw SQL 清成 NULL（繞過 write override 補值），
        # 否則 ORM pending write 會在後續 read flush 時覆寫 raw NULL。
        self.session.write({'state': 'reaped'})
        self.session.flush_recordset()
        self.env.cr.execute(
            "UPDATE woow_paas_platform_agent_session "
            "SET reaped_since = NULL WHERE id = %s", (self.session.id,))
        self.session.invalidate_recordset(['reaped_since'])
        self.assertFalse(self.session.reaped_since)
        self.Session._backfill_reaped_since_orphans()
        self.session.invalidate_recordset(['reaped_since'])
        self.assertTrue(self.session.reaped_since)

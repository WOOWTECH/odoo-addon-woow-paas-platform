"""自動撤權 v1：撤權帳本列必須活過**真正的**主交易 rollback。

`src/tests/test_sh_access_revision_ledger.py` 跑在 `TransactionCase`，主交易只是
外層交易裡的 savepoint；本檔照 `test_failopen_real_rollback_794.py` 的形狀，繼承
`BaseCase`（不進 test mode、`Registry.cursor()` 是真的另一條連線）用三筆真交易：

1. **交易 1**：建 fixture 並真的 commit。
2. **交易 2**：扮演那一次 request——route 開保護，operator（mock）回成功，帳本在
   獨立交易記一版；接著整筆 **`cr.rollback()`**（例如同 request 後段失敗）。
   同時記下交易 2 自己在 sync 之後讀到的版本：REPEATABLE READ 的快照在交易第一個
   查詢就定了，所以它**看不到**獨立交易剛 commit 的列——這就是「上一筆指紋」必須
   在獨立 cursor 內讀的原因。
3. **交易 3**：另一條連線讀：route 回到未保護（交易 2 的寫入真的被丟棄），帳本那
   一列仍在、版本＞0。

## 怎麼跑

    odoo --test-tags sh_access_revision_rollback -u woow_paas_platform \\
         --stop-after-init -d <db> -c <odoo.conf>

`-standard` ⇒ 預設套件不選中（本檔真的 commit）。`addCleanup` 以 raw SQL 清掉
自己建的列（帳本 ORM 層只能 insert）。
"""
import logging
import uuid
from unittest.mock import MagicMock, patch

import odoo
from odoo import api
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from ...models.smart_home_route import _sm_skip_access_sync
from ..publication_receipt_fixture import install_publication_receipts
from ..smart_home_fixture import sh_model

_logger = logging.getLogger(__name__)

MOCK_SH_CLIENT = ('odoo.addons.woow_paas_platform.models.smart_home.'
                  'get_paas_operator_client')
_REV_TABLE = 'woow_paas_platform_smart_home_access_revision'


@tagged('-standard', '-at_install', 'post_install',
        'sh_access_revision_rollback')
class TestAccessRevisionSurvivesRealRollback(BaseCase):

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'rev%s' % uuid.uuid4().hex[:8]
        self.ids = {}
        self.addCleanup(self._purge)
        # ── 交易 1：fixture 真的 commit ──────────────────────────────
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            env['ir.config_parameter'].set_param(
                'woow_paas_platform.paas_domain', 'woowtech.io')
            ws = env['woow_paas_platform.workspace'].create(
                {'name': 'WS %s' % self.marker})
            self.ids['workspace'] = ws.id
            sh = sh_model(env).create({
                'name': 'sh-%s' % self.marker, 'workspace_id': ws.id,
                'state': 'active', 'tunnel_id': 'tun-%s' % self.marker})
            self.ids['smart_home'] = sh.id
            sh.write({'subdomain': sh._generate_subdomain()})
            route = _sm_skip_access_sync(
                env['woow_paas_platform.smart_home_route']).create(
                    {'smart_home_id': sh.id, 'is_primary': True})
            self.ids['route'] = route.id

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                if self.ids.get('smart_home'):
                    cr.execute('DELETE FROM %s WHERE smart_home_ref = %%s'
                               % _REV_TABLE, (self.ids['smart_home'],))
                    cr.execute('DELETE FROM woow_paas_platform_smart_home '
                               'WHERE id = %s', (self.ids['smart_home'],))
                if self.ids.get('workspace'):
                    cr.execute('DELETE FROM woow_paas_platform_workspace '
                               'WHERE id = %s', (self.ids['workspace'],))
        except Exception:
            _logger.exception('撤權帳本 rollback 探針的清理失敗（marker=%s）',
                              self.marker)

    def _read_committed(self):
        with self.registry.cursor() as cr:
            cr.execute('SELECT is_protected FROM '
                       'woow_paas_platform_smart_home_route WHERE id = %s',
                       (self.ids['route'],))
            protected = cr.fetchone()[0]
            cr.execute('SELECT id, route_ref FROM %s WHERE smart_home_ref = %%s'
                       ' ORDER BY id' % _REV_TABLE, (self.ids['smart_home'],))
            rows = cr.fetchall()
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            revision = env['woow_paas_platform.smart_home_access_revision'
                           ]._revision_for_home(self.ids['smart_home'])
        return protected, rows, revision

    def test_revision_row_survives_request_rollback(self):
        protected, rows, revision = self._read_committed()
        self.assertEqual((protected, rows, revision), (False, [], 0), '前提')
        client = install_publication_receipts(MagicMock())
        # ── 交易 2：request 推成功之後整筆被丟棄 ─────────────────────
        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            route = env['woow_paas_platform.smart_home_route'].browse(
                self.ids['route'])
            with patch(MOCK_SH_CLIENT, return_value=client):
                route.write({'is_protected': True, 'login_email_otp': True})
            self.assertTrue(client.sync_access_app.called,
                            '前提：operator 真的被推了（提交點已跨過）')
            visible_in_request = env[
                'woow_paas_platform.smart_home_access_revision'
            ]._revision_for_home(self.ids['smart_home'])
            cr.rollback()
        finally:
            cr.close()
        # ── 交易 3：另一條連線讀 ────────────────────────────────────
        protected, rows, revision = self._read_committed()
        self.assertFalse(protected, '對照：交易 2 的 route 寫入真的被回捲了')
        self.assertEqual(len(rows), 1, '帳本列必須活過主交易 rollback')
        self.assertEqual(rows[0][1], self.ids['route'])
        self.assertEqual(revision, rows[0][0])
        self.assertGreater(revision, 0)
        self.assertEqual(
            visible_in_request, 0,
            'REPEATABLE READ：主交易看不到獨立交易 commit 的列（上一筆指紋因此'
            '必須在獨立 cursor 內讀）')

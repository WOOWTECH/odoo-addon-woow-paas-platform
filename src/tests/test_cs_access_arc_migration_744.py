"""#744：`18.0.1.0.143/post-migrate.py` 的四種世界。

這支 migration 的**全部價值**是那句 `UserError`：升級出事時，維運拿到的要是
「哪一項不成立、為什麼」，不是一個看不出所以然的 psycopg2 錯誤。所以測的不是
「有沒有跑完」，是**每一種失敗都停在講得清楚的地方**。

🔴 M3 是覆核退修的守線。補建 CHECK 的 `ALTER TABLE` 沒包 savepoint 時：

* 補建若因「壞列以外」的原因失敗（lock timeout、同名衝突、權限…），
  PostgreSQL 把整個 transaction 打成 aborted；
* 緊接著第 3 步查 `route_id` 可空性的那句 SELECT 會丟
  `InFailedSqlTransaction`——**沒有人捕捉**，於是它蓋掉底下組好的 `UserError`；
* 症狀：腳本自述「講得清楚地停下來」，實際吐一個 psycopg2 例外。

M3 直接斷言例外型別是 `UserError`、**且交易在那之後仍然可用**。少了第二個斷言，
一個「例外剛好也是 UserError」的世界會讓它為了錯的理由變綠。

⚠ M4 的順序性也在守線內：有壞列時**不可以**先去補建（補建必敗，而拿到的會是
PG 的 `CheckViolation`——說不出是哪幾列）。M4 因此同時斷言「沒有嘗試補建」。
"""
import importlib.util
import os

import psycopg2

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

_TABLE = 'woow_paas_platform_security_access_access_policy'
_CONSTRAINT = '%s_anchor_arc' % _TABLE
_VERSION = '18.0.1.0.142'  # 任何非空字串即可（腳本只用它判斷不是 fresh install）
_LOGGER = 'odoo.addons.woow_paas_platform.migrations.18.0.1.0.143.post-migrate'


def _migrate_fn():
    """以檔案路徑載入 migration（版本目錄不是 Python package，import 不到）。"""
    here = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(here, 'migrations', '18.0.1.0.143', 'post-migrate.py')
    spec = importlib.util.spec_from_file_location('arc_post_migrate', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


class TestArcMigration744(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.migrate = _migrate_fn()

    # ------------------------------------------------------------------ #
    #  helpers（DDL 在 TransactionCase 內隨測試 rollback，不會外溢）        #
    # ------------------------------------------------------------------ #
    def _drop_arc_constraint(self):
        self.env.cr.execute(
            "ALTER TABLE %s DROP CONSTRAINT IF EXISTS %s"
            % (_TABLE, _CONSTRAINT))

    def _arc_constraint_exists(self):
        self.env.cr.execute(
            """
            SELECT c.contype
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = %s AND c.conname = %s
            """,
            (_TABLE, _CONSTRAINT),
        )
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _squat_the_constraint_name(self):
        """用一個**同名但不是 CHECK** 的 constraint 佔住名字。

        這樣做出來的世界正是 M3 要的那一種：腳本的存在性探針只認
        `contype='c'` ⇒ 判定「CHECK 不存在」而去補建，補建卻因為**名字已被佔用**
        而失敗——一個與「資料有壞列」完全無關的失敗原因，也就是退修要防的那類。
        """
        self.env.cr.execute(
            "ALTER TABLE %s ADD CONSTRAINT %s UNIQUE (id)"
            % (_TABLE, _CONSTRAINT))

    def _make_a_bad_row(self):
        """造一列兩個錨點皆空的 policy（先拿掉 arc CHECK 才寫得進去）。"""
        sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'ARC MIG SA',
            'workspace_id': self.env['woow_paas_platform.workspace'].create(
                {'name': 'ARC MIG WS'}).id})
        route = self.env['woow_paas_platform.security_access_route'].create(
            {'security_access_id': sa.id, 'subdomain_prefix': 'arcmig744'})
        pol = self.Policy.create({
            'route_id': route.id, 'name': 'arc-mig', 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@x.com'})]})
        self.env.cr.execute(
            "UPDATE %s SET route_id = NULL, cloud_service_id = NULL "
            "WHERE id = %%s" % _TABLE, (pol.id,))
        return pol

    def _cursor_still_usable(self):
        """交易還活著嗎？aborted transaction 下這句會丟 InFailedSqlTransaction。"""
        try:
            self.env.cr.execute("SELECT 1")
            return self.env.cr.fetchone()[0] == 1
        except psycopg2.Error:
            return False

    # ------------------------------------------------------------------ #
    #  M 系列                                                              #
    # ------------------------------------------------------------------ #
    def test_M1_healthy_world_passes_silently(self):
        """常態（constraint 在、資料乾淨）⇒ 不丟例外，什麼都不動。"""
        self.assertEqual(self._arc_constraint_exists(), 'c',
                         '前提：fresh DB 上 arc CHECK 應該已經存在')
        self.migrate(self.env.cr, _VERSION)
        self.assertEqual(self._arc_constraint_exists(), 'c')

    @mute_logger(_LOGGER)
    def test_M2_missing_constraint_is_repaired_in_place(self):
        """CHECK 不見了但資料乾淨 ⇒ 就地補建，不丟例外。

        這是「從 P2.5 之前一路升上來」那條路徑的正常中間態：`_auto_init` 當下
        legacy 列兩個錨點都空著，constraint 加不上被排進 queue，而
        `finalize_constraints()` 晚於 post-migrate。
        """
        self._drop_arc_constraint()
        self.assertIsNone(self._arc_constraint_exists(), '前提：constraint 已移除')
        self.migrate(self.env.cr, _VERSION)
        self.assertEqual(
            self._arc_constraint_exists(), 'c',
            '資料乾淨時補建必然成功——沒補回來代表補建那段根本沒被走到')

    @mute_logger(_LOGGER)
    def test_M3_repair_failure_keeps_the_readable_error_and_a_live_txn(self):
        """🔴 退修守線：補建因「壞列以外」的原因失敗時——

        1. 丟的必須是 `UserError`（不是 `InFailedSqlTransaction`）；
        2. 訊息要說得出是補建失敗；
        3. **交易在那之後仍然可用**。

        第 3 點才是 savepoint 的實際效果。拿掉 `with cr.savepoint(flush=False)`
        之後：ALTER 失敗 → transaction aborted → 第 3 步那句 SELECT 丟
        `InFailedSqlTransaction` → 1 與 3 同時破。
        """
        self._drop_arc_constraint()
        self._squat_the_constraint_name()
        self.assertEqual(self._arc_constraint_exists(), 'u',
                         '前提：名字被一個非 CHECK 的 constraint 佔住')

        with self.assertRaises(UserError) as cm:
            self.migrate(self.env.cr, _VERSION)

        self.assertIn('補建失敗', str(cm.exception))
        self.assertIn(_CONSTRAINT, str(cm.exception))
        self.assertTrue(
            self._cursor_still_usable(),
            '補建失敗把交易毒死了——savepoint 沒有包住那句 ALTER')

    @mute_logger(_LOGGER)
    def test_M4_bad_rows_are_reported_and_repair_is_not_attempted(self):
        """有壞列 ⇒ 報列數並停；且**不去補建**。

        順序是設計的一部分：先驗資料再補建。反過來的話補建必敗，而拿到的是
        PG 的 `CheckViolation`——說不出是哪幾列，正是這支腳本要換掉的東西。
        """
        self._drop_arc_constraint()
        self._make_a_bad_row()

        with self.assertRaises(UserError) as cm:
            self.migrate(self.env.cr, _VERSION)

        message = str(cm.exception)
        self.assertIn('exclusive arc', message)
        self.assertIn('1 列', message)
        self.assertNotIn('補建失敗', message,
                         '有壞列時不該進入補建那條路徑')
        self.assertIsNone(
            self._arc_constraint_exists(),
            '有壞列時不可嘗試補建（補上去只會拿到說不清楚的 CheckViolation）')
        self.assertTrue(self._cursor_still_usable())

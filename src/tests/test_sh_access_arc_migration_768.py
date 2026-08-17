"""#768：`18.0.1.0.150/post-migrate.py` 的每一種世界。

這支 migration 做兩件在失敗時**完全沒有輸出**的事，本檔就是它們的判別式。

## 1) 換的是 CHECK 的「定義」，不是它的「名字」

#744 那支 migration 的存在性驗證只查 `conname`（constraint 在不在）。本次要換掉的
constraint **同名不同定義**——`_auto_init` 在 `_sql_constraints` 的定義字串變動時
會嘗試 drop + 重建，失敗只留一行 `_schema.warning` 就繼續跑
（`odoo/modules/registry.py :: Registry.finalize_constraints`）⇒ 升級「成功」、
Python 那層照常擋、所有行為測試全綠，而 DB 那層停在**舊的二元定義**上。

所以 M2 造的世界是「同名、舊定義」：只查名字的驗證會讓它原封不動地通過。查
`pg_get_constraintdef` 是唯一分得出「換好了」與「沒換」的判準，M2 就是那一格。
⚠ 把 M2 寫成「跑完之後 constraint 還在」＝完全沒有鑑別力，那正是本檔要防的東西。

## 2) 既有 SmartHome 的 primary route 回填

route 是 Access 的錨點載體。沒有這一步，既有 SH 的租戶就算方案帶席位也只會看到一個
空的 Access 分頁（後端沒有錨點可掛）——而那與「功能還沒開放」在畫面上不可區分。

B1 因此配一個**陰性對照**：`subdomain IS NULL` 的 SH 不該被回填（它還沒有對外
hostname）。少了陰性對照，一支「無條件對每台 SH 都插一列」的回填也會讓 B1 變綠。

## 陽性對照紀律

每一條「被擋下來」旁邊都有一條「合法路徑會成功」。沒有它，`UserError` 可能來自
fixture 壞掉、欄位不存在、或一支「什麼都擋」的壞腳本——那三種情況下所有負向斷言
都會為了錯的理由變綠。M1／M3／M4 各自在同一個 test 內做完負向再做正向。

⚠ 所有 DDL 都在 `TransactionCase` 內用裸 SQL 做，隨測試 rollback，不外溢。
"""
import importlib.util
import os

import psycopg2
from psycopg2 import IntegrityError

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from .smart_home_fixture import sh_model

_POLICY_TABLE = 'woow_paas_platform_security_access_access_policy'
_RULE_TABLE = 'woow_paas_platform_security_access_access_policy_rule'
_ROUTE_TABLE = 'woow_paas_platform_smart_home_route'
_CONSTRAINT = '%s_anchor_arc' % _POLICY_TABLE
_PRIMARY_INDEX = 'woow_paas_platform_smart_home_route_one_primary'

#: 升級前的**舊**二元定義。M2 用它以同名重建，造出「只查名字會通過」的世界。
_LEGACY_BINARY_CHECK = (
    'CHECK((route_id IS NULL) <> (cloud_service_id IS NULL))')

#: 任何非空字串即可——腳本只用它判斷「這不是 fresh install」。
_VERSION = '18.0.1.0.149'

#: M3 用來讓 arc constraint 變成**不可 drop** 的那張依賴表（見 `_squat_...`）。
_DEP_TABLE = 'woow_test_768_arc_dependent'

#: 載入 migration 時給的模組名。
#:
#: 🔴 這個字串同時就是**它的 logger 名**：migration 內是
#: `logging.getLogger(__name__)`，而 `spec_from_file_location(name, ...)` 決定了
#: 模組的 `__name__`。#744 那支測試 mute 的是 `odoo.addons.…144.post-migrate`
#: 這條點路徑——那條路徑底下**沒有任何 logger**，於是它靜默地什麼都沒 mute
#: （測試照樣綠，只是輸出很吵）。這裡讓兩者同源，mute 才真的作用得到。
_MIGRATION_MODULE = 'woow_paas_platform_sh_access_768_post_migrate'
_LOGGER = _MIGRATION_MODULE


def _migrate_fn():
    """以檔案路徑載入 migration（版本目錄不是 Python package，import 不到）。"""
    here = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(here, 'migrations', '18.0.1.0.150', 'post-migrate.py')
    spec = importlib.util.spec_from_file_location(_MIGRATION_MODULE, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


class _ArcMigrationFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.ShRoute = self.env['woow_paas_platform.smart_home_route']
        self.SmartHome = sh_model(self.env)
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'ARC MIG WS 768'})
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        self.migrate = _migrate_fn()

    # ------------------------------------------------------------------ #
    #  fixture builders（形狀沿用 test_sh_access_model_768.py）             #
    # ------------------------------------------------------------------ #
    def _smart_home(self, name='sh-mig-768', subdomain='paas-sm-mig-768-aaaa',
                    **extra):
        vals = {'name': name, 'workspace_id': self.workspace.id}
        vals.update(extra)
        # `.with_env(self.env)` 洗掉開閘 context：`with_context` 是 merge，
        # 不洗掉的話 fixture 自己把受測守衛關掉了。
        sh = self.SmartHome.create(vals).with_env(self.env)
        if subdomain is not None:
            sh.sudo().write({'subdomain': subdomain})
        # 🔴 必須 flush：`subdomain` 是 write 進去的，落在 `towrite` queue 裡而
        # 還沒進 DB。不 flush 的話 migration 的裸 SQL 讀到的是 NULL ⇒ 回填**一列
        # 都不建**，而症狀與「回填整段壞掉」一模一樣。
        self.env.flush_all()
        return sh

    def _sh_route(self, sh=None, **extra):
        sh = sh or self._smart_home()
        vals = {'smart_home_id': sh.id, 'is_primary': True}
        vals.update(extra)
        return self.ShRoute.create(vals)

    def _policy_vals(self, **extra):
        vals = {
            'name': 'arc-mig-768', 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        vals.update(extra)
        return vals

    # ------------------------------------------------------------------ #
    #  DDL / 觀測 helpers                                                  #
    # ------------------------------------------------------------------ #
    def _arc_def(self):
        """arc **CHECK** 的定義字串（`contype='c'`），不存在時回 ``None``。"""
        self.env.cr.execute(
            """
            SELECT pg_get_constraintdef(c.oid)
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = %s AND c.conname = %s AND c.contype = 'c'
            """,
            (_POLICY_TABLE, _CONSTRAINT),
        )
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _arc_contype(self):
        """占用這個**名字**的 constraint 是哪一種（`c` / `u` / ``None``）。"""
        self.env.cr.execute(
            """
            SELECT c.contype
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = %s AND c.conname = %s
            """,
            (_POLICY_TABLE, _CONSTRAINT),
        )
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _drop_arc_constraint(self):
        self.env.cr.execute(
            "ALTER TABLE %s DROP CONSTRAINT IF EXISTS %s"
            % (_POLICY_TABLE, _CONSTRAINT))

    def _install_legacy_binary_arc(self):
        """以**同名**裝回升級前的二元定義。

        這一步造出來的世界，正是「只查 `conname` 的驗證會通過、而 DB 那層其實
        還是舊語意」那一種：三腿錨定的列會被 PG 拒絕，而那要等前端接上去才會
        有人撞到。
        """
        self.env.cr.execute(
            "ALTER TABLE %s ADD CONSTRAINT %s %s"
            % (_POLICY_TABLE, _CONSTRAINT, _LEGACY_BINARY_CHECK))

    def _squat_the_name_with_an_undroppable_constraint(self):
        """讓 arc 這個名字被一個**drop 不掉**的 constraint 佔住。

        要的是「與資料無關的補建失敗」。單純用同名 UNIQUE constraint 佔位在本支
        migration **造不出**那個世界——它的補建是 `DROP CONSTRAINT IF EXISTS` ＋
        `ADD CONSTRAINT`，同名 constraint 會被第一句順手 drop 掉，補建照樣成功。
        （這與 #744 那支不同：那支只有 ADD，沒有 DROP。）

        所以這裡多掛一條外來鍵依賴到那個 UNIQUE 上：PostgreSQL 於是拒絕 drop
        （`cannot drop constraint … because other objects depend on it`），
        `IF EXISTS` 只吞「不存在」不吞依賴錯誤 ⇒ 補建在 DROP 那一句就失敗。

        用 `(id, name)` 這組欄位是刻意的：`id` 已經唯一 ⇒ 這個 UNIQUE 一定建得
        起來（不受表內既有資料影響），而 primary key 只蓋 `(id)` ⇒ 外來鍵只能綁
        到我們新建的這一個索引上，依賴一定落在我們要的那個 constraint。
        """
        self.env.cr.execute(
            "ALTER TABLE %s ADD CONSTRAINT %s UNIQUE (id, name)"
            % (_POLICY_TABLE, _CONSTRAINT))
        self.env.cr.execute(
            """
            CREATE TABLE %(dep)s (
                policy_id integer,
                policy_name varchar,
                CONSTRAINT %(dep)s_fk FOREIGN KEY (policy_id, policy_name)
                    REFERENCES %(policy)s (id, name)
            )
            """ % {'dep': _DEP_TABLE, 'policy': _POLICY_TABLE})

    def _release_the_squat(self):
        """拆掉上面那個佔位，讓補建重新變成一條**合法路徑**（陽性對照用）。"""
        self.env.cr.execute("DROP TABLE %s" % _DEP_TABLE)
        self.env.cr.execute(
            "ALTER TABLE %s DROP CONSTRAINT %s" % (_POLICY_TABLE, _CONSTRAINT))

    def _arc_name_is_undroppable(self):
        """前提探針：那個名字現在真的 drop 不掉嗎。

        沒有這一格的話，PostgreSQL 哪天不再擋依賴、或外來鍵綁錯索引，M3 的世界
        會**悄悄變回**「補建會成功」，而讀到的失敗訊息只會說「沒有丟 UserError」
        ——指不出真正壞掉的是佈景而不是受測碼。
        """
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    "ALTER TABLE %s DROP CONSTRAINT %s"
                    % (_POLICY_TABLE, _CONSTRAINT))
            return False
        except psycopg2.Error:
            return True

    def _bad_policy_rows(self):
        """三個錨點都不是「恰好一個」的列數（＝ migration 第 1 步的判準）。

        先 flush 再查：ORM 的待寫值還在 queue 裡時這句只看得到舊狀態，而
        「探針讀到過期的 DB」與「真的沒有壞列」輸出完全相同。
        """
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT count(*) FROM %s
             WHERE num_nonnulls(route_id, cloud_service_id,
                                smart_home_route_id) <> 1
            """ % _POLICY_TABLE)
        return self.env.cr.fetchone()[0]

    def _make_a_bad_policy_row(self):
        """造一列三個錨點皆空的 policy（先拿掉 arc CHECK 才寫得進去）。"""
        route = self._sh_route()
        pol = self.Policy.create(
            self._policy_vals(smart_home_route_id=route.id))
        # 造壞列前必須 flush：ORM 的待寫值還在 queue 裡時，下面這句裸 UPDATE
        # 會被之後的 flush 蓋回去，壞列根本不存在。
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE %s SET route_id = NULL, cloud_service_id = NULL, "
            "smart_home_route_id = NULL WHERE id = %%s" % _POLICY_TABLE,
            (pol.id,))
        self.env.invalidate_all()
        return pol.id

    def _delete_policy_row(self, policy_id):
        """把壞列連同它的 rule 一起裸刪（陽性對照：把資料修乾淨再跑一次）。"""
        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM %s WHERE policy_id = %%s" % _RULE_TABLE, (policy_id,))
        self.env.cr.execute(
            "DELETE FROM %s WHERE id = %%s" % _POLICY_TABLE, (policy_id,))
        self.env.invalidate_all()

    def _route_rows(self, sh):
        """這台 SH 的 route 列（裸 SQL——回填是裸 SQL 插的，ORM cache 看不到）。

        同樣先 flush：漏掉的話「回填沒發生」與「ORM 還沒把列送進 DB」在這個
        探針上不可區分。
        """
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT id, is_primary, session_duration, subdomain_prefix "
            "FROM %s WHERE smart_home_id = %%s ORDER BY id" % _ROUTE_TABLE,
            (sh.id,))
        return self.env.cr.fetchall()

    def _drop_routes_of(self, sh):
        """把這台 SH 的 route 全部裸刪 —— 造出「#768 之前」的世界。

        用裸 SQL 是因為 primary route 在 ORM 那層是不可刪的（`unlink()` 會丟
        `UserError`），而我們要重現的正是「這一列從來沒有被建過」。刪除動作對
        「create 現在會不會順手建一條」保持中立：無論將來 `SmartHome.create()`
        有沒有開始 seed，這裡跑完都是零列。
        """
        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM %s WHERE smart_home_id = %%s" % _ROUTE_TABLE,
            (sh.id,))
        self.env.invalidate_all()

    def _primary_index_def(self):
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes "
            " WHERE tablename = %s AND indexname = %s",
            (_ROUTE_TABLE, _PRIMARY_INDEX))
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _cursor_still_usable(self):
        """交易還活著嗎？aborted transaction 下這句會丟 InFailedSqlTransaction。"""
        try:
            self.env.cr.execute("SELECT 1")
            return self.env.cr.fetchone()[0] == 1
        except psycopg2.Error:
            return False


class TestArcDefinitionMigration768(_ArcMigrationFixture):
    """M 系列：三腿 arc 的四種世界。"""

    def test_M0_healthy_world_passes_silently(self):
        """常態（新定義已在、資料乾淨）⇒ 不丟例外，定義不變。

        這是整組 M／B 的總陽性對照：它若紅了，底下每一條「被擋下來」的斷言都
        失去意義（可能只是 migration 對任何世界都會炸）。
        """
        definition = self._arc_def()
        self.assertIsNotNone(
            definition,
            '前提：fresh DB 上 arc CHECK 應該已由 _auto_init 建好')
        self.assertIn('num_nonnulls', definition, '前提：fresh DB 上已是三腿定義')

        self.migrate(self.env.cr, _VERSION)

        self.assertIn(
            'num_nonnulls', self._arc_def() or '',
            '健康世界跑完之後定義竟然變了 ⇒ 腳本在不該動的時候動了 DDL')

    @mute_logger(_LOGGER)
    def test_M1_fresh_install_returns_immediately(self):
        """`version` 為空（fresh install）⇒ 整支直接 return，什麼都不碰。

        ⚠ 在**健康**世界上測這一格沒有鑑別力：「什麼都沒做」與「全部做對了」
        輸出相同。所以這裡先把世界弄成「跑下去一定會炸」（constraint 沒了、有壞
        列、SH 缺 primary route），再證明 `migrate(cr, None)` 安靜地什麼都沒改；
        結尾的陽性對照則證明這個世界真的是致命的——否則「沒有 raise」只是因為
        佈景其實沒問題。
        """
        self._drop_arc_constraint()
        self._make_a_bad_policy_row()
        sh = self._smart_home(subdomain='paas-sm-mig-768-m1')
        self._drop_routes_of(sh)

        self.migrate(self.env.cr, None)

        self.assertIsNone(
            self._arc_def(),
            'fresh install 不該補建 constraint —— 這一步在 `-i` 時是由 '
            '`_auto_init` 負責的，migration 插手只會蓋掉它的判斷')
        self.assertEqual(
            self._bad_policy_rows(), 1,
            'fresh install 不該去動資料（壞列被清掉代表整支跑過了）')
        self.assertEqual(
            self._route_rows(sh), [],
            'fresh install 不該回填 route —— 新裝的 DB 沒有「既有 SH」')

        # 陽性對照：同一個世界帶著版本號跑就會炸 ⇒ 上面的「沒有 raise」有鑑別力
        with self.assertRaises(UserError):
            self.migrate(self.env.cr, _VERSION)

    @mute_logger(_LOGGER)
    def test_M2_same_name_legacy_definition_is_replaced(self):
        """🔴 本檔的核心：同名、舊定義的 CHECK 必須被換成 `num_nonnulls` 形式。

        只查 `conname` 的驗證（#744 那支的做法）在這個世界會**通過**：名字在、
        `contype` 也是 `c`，而 DB 那層還是二元語意。查 `pg_get_constraintdef`
        是唯一分得出「換好了」與「沒換」的判準。
        """
        self._drop_arc_constraint()
        self._install_legacy_binary_arc()

        legacy = self._arc_def()
        self.assertIsNotNone(legacy, '前提：舊定義應該已經以同名裝回去')
        self.assertNotIn(
            'num_nonnulls', legacy,
            '前提壞了：裝回去的不是舊的二元定義，這個 test 沒有在測它要測的東西')
        self.assertNotIn(
            'smart_home_route_id', legacy,
            '前提壞了：舊定義不該提到第三個錨點')

        self.migrate(self.env.cr, _VERSION)

        migrated = self._arc_def()
        self.assertIsNotNone(
            migrated, 'constraint 整個不見了 ⇒ drop 成功但重建沒發生')
        self.assertIn(
            'num_nonnulls', migrated,
            '定義沒有被換掉（實際：%r）。只查 conname 的驗證會放過這一格，'
            '而症狀是第一筆 SH 錨定的 policy 被 PG 拒絕——要等前端接上去才有人'
            '撞到' % migrated)
        for column in ('route_id', 'cloud_service_id', 'smart_home_route_id'):
            self.assertIn(
                column, migrated,
                '換上去的定義少了 %s 這一腿（實際：%r）' % (column, migrated))

        # 陽性對照（行為面）：換的是**語意**不只是字串。同一筆列在舊定義下會被
        # PG 拒絕（route_id 與 cloud_service_id 皆空 ⇒ `TRUE <> TRUE` = FALSE）。
        route = self._sh_route(sh=self._smart_home(
            name='sh-mig-768-m2', subdomain='paas-sm-mig-768-m2'))
        pol = self.Policy.create(
            self._policy_vals(smart_home_route_id=route.id))
        self.env.flush_all()
        self.assertTrue(
            pol.id,
            '換完定義之後，SH 錨定的 policy 必須寫得進 DB —— 寫不進去代表換上'
            '去的是一個字面對、語意錯的謂詞')

    @mute_logger(_LOGGER)
    def test_M3_repair_failure_keeps_the_readable_error_and_a_live_txn(self):
        """補建因「壞列以外」的原因失敗時——

        1. 丟的必須是 `UserError`（不是裸的 psycopg2 例外）；
        2. 訊息要說得出是補建失敗（而不是把它講成資料有問題）；
        3. **交易在那之後仍然可用**。

        第 3 點才是 `with cr.savepoint(flush=False)` 的實際效果。拿掉 savepoint
        之後：ALTER 失敗 → transaction aborted → 緊接著的 `_constraint_def()`
        那句 SELECT 丟 `InFailedSqlTransaction` → 1 與 3 同時破，而腳本自述
        「講得清楚地停下來」。
        """
        self._drop_arc_constraint()
        self._squat_the_name_with_an_undroppable_constraint()

        self.assertEqual(
            self._arc_contype(), 'u',
            '前提：名字要被一個**非 CHECK** 的 constraint 佔住')
        self.assertIsNone(
            self._arc_def(), '前提：以 CHECK 身分查應該查不到（才會走補建）')
        with mute_logger('odoo.sql_db'):
            self.assertTrue(
                self._arc_name_is_undroppable(),
                '前提壞了：那個名字現在 drop 得掉 ⇒ 補建會成功，這個 test 測不'
                '到補建失敗那條路徑')

        with self.assertRaises(UserError) as cm:
            self.migrate(self.env.cr, _VERSION)

        message = str(cm.exception)
        self.assertIn('補建失敗', message)
        self.assertIn(_CONSTRAINT, message)
        self.assertNotIn(
            '不滿足', message,
            '資料是乾淨的，卻被報成「有列不滿足」⇒ 維運會去查一份根本沒問題的'
            '資料，而真正的原因（DDL 被擋）不會出現在訊息裡')
        self.assertTrue(
            self._cursor_still_usable(),
            '補建失敗把交易毒死了——savepoint 沒有包住那兩句 ALTER')
        self.assertIsNone(
            self._arc_def(),
            '補建失敗卻留下一個 CHECK ⇒ savepoint 沒有把半套的 DDL 回滾掉')

        # 陽性對照：把佔位拆掉，同一支腳本在同一個交易裡就補得起來
        self._release_the_squat()
        self.migrate(self.env.cr, _VERSION)
        self.assertIn(
            'num_nonnulls', self._arc_def() or '',
            '拆掉佔位之後補建仍然失敗 ⇒ 上面的 UserError 不是佔位造成的，'
            '是補建那段本身壞了')

    @mute_logger(_LOGGER)
    def test_M4_bad_rows_are_reported_and_repair_is_not_attempted(self):
        """有違規列 ⇒ 報列數並停；且**不去補建**。

        順序是設計的一部分：先驗資料再補建。反過來的話補建必敗，而拿到的是 PG
        的 `CheckViolation`——說不出是哪幾列，正是這支腳本要換掉的東西。
        """
        self._drop_arc_constraint()
        self.assertEqual(
            self._bad_policy_rows(), 0,
            '前提：造壞列之前表內不該已經有違規列（有的話「有 N 列」對不上，'
            '而那是佈景的問題不是受測碼的）')
        bad_id = self._make_a_bad_policy_row()
        self.assertEqual(
            self._bad_policy_rows(), 1, '前提：壞列要真的落地（裸 UPDATE 生效）')

        with self.assertRaises(UserError) as cm:
            self.migrate(self.env.cr, _VERSION)

        message = str(cm.exception)
        self.assertIn('不滿足', message)
        self.assertIn(
            '有 1 列', message,
            '訊息說不出「有幾列」⇒ 維運拿不到可以去查的東西（實際：%r）'
            % message)
        self.assertNotIn(
            '補建失敗', message, '有壞列時不該進入補建那條路徑')
        self.assertIsNone(
            self._arc_def(),
            '有壞列時不可嘗試補建（補上去只會拿到說不清楚的 CheckViolation）')
        self.assertTrue(self._cursor_still_usable())

        # 陽性對照：把那一列修掉，同一支腳本立刻補得起來 ⇒ 上面的 UserError 是
        # 那一列造成的，不是「腳本對任何世界都會炸」
        self._delete_policy_row(bad_id)
        self.migrate(self.env.cr, _VERSION)
        self.assertIn(
            'num_nonnulls', self._arc_def() or '',
            '資料修乾淨之後補建仍然沒發生 ⇒ 先驗後補的第二步從來沒被走到')


class TestPrimaryRouteBackfill768(_ArcMigrationFixture):
    """B 系列：既有 SmartHome 的 primary route 回填。"""

    def test_B1_provisioned_smart_homes_get_their_primary_route(self):
        """有 `subdomain` 的 SH 被回填恰好一條 primary route。

        陰性對照（同一個 test 內）：`subdomain IS NULL` 的 SH **不**被回填——它
        還沒有對外 hostname，它的 route 會在 `action_provision` 成功時才建立。
        少了這一格，一支「無條件對每台 SH 都插一列」的回填也會讓本 test 變綠。
        """
        provisioned = self._smart_home(
            name='sh-mig-768-b1', subdomain='paas-sm-mig-768-b1')
        self._drop_routes_of(provisioned)
        never_provisioned = self._smart_home(
            name='sh-mig-768-b1-null', subdomain=None)
        self._drop_routes_of(never_provisioned)

        self.assertEqual(
            self._route_rows(provisioned), [], '前提：回填前不該有 route')
        self.assertEqual(
            self._route_rows(never_provisioned), [], '前提：回填前不該有 route')

        self.migrate(self.env.cr, _VERSION)

        rows = self._route_rows(provisioned)
        self.assertEqual(
            len(rows), 1,
            '已 provision 的 SH 回填後應該恰好一條 route（實際 %d 條）——零條'
            '＝租戶買了席位卻看到空的 Access 分頁；兩條＝primary 的唯一性守線'
            '被自己的回填踩破' % len(rows))
        _route_id, is_primary, session_duration, subdomain_prefix = rows[0]
        self.assertTrue(
            is_primary,
            '回填出來的列不是 primary ⇒ 它的 hostname 不會衍生自 '
            'smart_home.subdomain，等於一條指不到任何地方的 route')
        self.assertEqual(
            session_duration, '24h',
            'session_duration 為空會讓 payload 的 `session_duration` 落空字串，'
            'operator 端拿到的是一個 Cloudflare 不接受的值')
        self.assertFalse(
            subdomain_prefix,
            'primary route 的 hostname 衍生自 SH 的 subdomain；填了 prefix 會'
            '讓 compute 走到非 primary 的分支，指向一個不存在的 hostname')

        self.assertEqual(
            self._route_rows(never_provisioned), [],
            '沒有 subdomain 的 SH 被回填了 ⇒ 它的 route hostname 恆空，'
            '而那條列會在之後真的 provision 時撞上 primary 的唯一性守衛')

    def test_B2_backfill_is_idempotent(self):
        """連跑兩次不會長出第二條 primary route，也不會 raise。

        「多跑一次」在升級路徑上是常態（重跑失敗的升級）。第二條 primary 會直接
        違反 partial unique index ⇒ 整個升級在一個看不出所以然的 `UniqueViolation`
        上停住。
        """
        sh = self._smart_home(
            name='sh-mig-768-b2', subdomain='paas-sm-mig-768-b2')
        self._drop_routes_of(sh)

        self.migrate(self.env.cr, _VERSION)
        first = self._route_rows(sh)
        self.assertEqual(len(first), 1, '前提：第一次跑應該回填一條')

        # 第二次：不 raise，而且列數不變（連 id 都該是同一個——重建一條新的會
        # 把租戶已經掛在舊列上的 Access 設定整組拋棄）
        self.migrate(self.env.cr, _VERSION)
        second = self._route_rows(sh)
        self.assertEqual(
            len(second), 1,
            '重跑長出了第 %d 條 route ⇒ 回填的 `WHERE NOT EXISTS` 沒有生效'
            % len(second))
        self.assertEqual(
            second[0][0], first[0][0],
            '重跑把 route 換成了另一列（id %s → %s）——租戶掛在原列上的 Access '
            '設定與 CF app id 會靜默消失' % (first[0][0], second[0][0]))

    def test_B3_partial_unique_index_is_created_by_the_migration(self):
        """「一台 SH 至多一條 primary route」的 DB 守線在升級路徑上也要有。

        它的**主要**建立點是 model 的 `init()`（fresh install 走那條）；這裡驗的
        是升級這條路徑上 migration 自己也建得起來（`IF NOT EXISTS` ⇒ 兩者冪等
        共存）。先 drop 再跑，是為了讓這一格分得出「migration 建的」與「本來就
        在那裡」——只斷言「跑完之後它在」對受測碼完全沒有鑑別力。
        """
        self.assertIsNotNone(
            self._primary_index_def(),
            '前提：install 之後 model 的 init() 應該已經建好這個 index')
        self.env.cr.execute("DROP INDEX %s" % _PRIMARY_INDEX)
        self.assertIsNone(
            self._primary_index_def(), '前提：index 已被移除')

        self.migrate(self.env.cr, _VERSION)

        indexdef = self._primary_index_def()
        self.assertIsNotNone(
            indexdef,
            'migration 沒有補建 partial unique index ⇒ 升級上來的環境只剩 '
            'Python 那半層守線（擋不住併發與裸 SQL），而新裝的環境有——兩邊的'
            '行為測試都會綠')
        self.assertIn(
            'UNIQUE', indexdef.upper(),
            '建出來的不是 unique index（實際：%r）' % indexdef)
        self.assertIn(
            'is_primary', indexdef,
            'index 沒有 partial 條件 ⇒ 它會連非 primary 的 route 一起管，'
            '一台 SH 從此連第二條普通 route 都建不出來（實際：%r）' % indexdef)

        # 行為面的判準：名字與定義字串都對，仍可能是一個擋不住東西的 index
        sh = self._smart_home(
            name='sh-mig-768-b3', subdomain='paas-sm-mig-768-b3')
        self._sh_route(sh=sh)
        self.env.flush_all()
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    """
                    INSERT INTO %s
                        (smart_home_id, is_primary, is_protected,
                         session_duration, login_google, login_email_otp,
                         create_uid, create_date, write_uid, write_date)
                    VALUES (%%s, TRUE, FALSE, '24h', FALSE, FALSE,
                            1, now(), 1, now())
                    """ % _ROUTE_TABLE, (sh.id,))

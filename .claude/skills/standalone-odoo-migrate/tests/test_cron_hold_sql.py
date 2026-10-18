"""cron-hold.sql／cron-release.sql 的行為測試（要一顆真的 PostgreSQL，沒有就 skip）。

STANDALONE_MIGRATE_TEST_PSQL＝能連到一顆可丟棄 PG 的 psql 指令前綴（superuser、免密碼），例如：
  STANDALONE_MIGRATE_TEST_PSQL="kubectl --context woow-k3s -n dev-sandbox-x exec -i pg -- psql -h 127.0.0.1 -U postgres"
測試會建（並覆蓋）資料庫 cronhold_test 與角色 odoo，只模擬 Odoo 的 ir_config_parameter／ir_cron／ir_cron_trigger。
"""
import os
import shlex
import subprocess
import unittest
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "scripts" / "target"
PSQL = os.environ.get("STANDALONE_MIGRATE_TEST_PSQL", "")
DB = "cronhold_test"

SCHEMA = """
CREATE TABLE ir_config_parameter (id serial PRIMARY KEY, key varchar NOT NULL UNIQUE, value text NOT NULL,
  create_uid int, create_date timestamp, write_uid int, write_date timestamp);
CREATE TABLE ir_cron (id serial PRIMARY KEY, name varchar, active boolean, nextcall timestamp NOT NULL, lastcall timestamp);
CREATE TABLE ir_cron_trigger (id serial PRIMARY KEY, cron_id int, call_at timestamp);
INSERT INTO ir_cron (id, name, active, nextcall) VALUES
  (1, 'overdue', true, (now() AT TIME ZONE 'UTC') - interval '1 day'),
  (2, 'future', true, (now() AT TIME ZONE 'UTC') + interval '1 day'),
  (3, 'inactive', false, (now() AT TIME ZONE 'UTC') - interval '1 hour');
INSERT INTO ir_cron_trigger (id, cron_id, call_at) VALUES (1, 2, (now() AT TIME ZONE 'UTC') - interval '5 minutes');
SELECT setval('ir_cron_trigger_id_seq', 1);
"""


def psql(sql, *, user="postgres", db=DB, args=()):
    cmd = shlex.split(PSQL)
    # 換連線角色：去掉前綴裡的 -U <x>，改成指定的 user
    if "-U" in cmd:
        i = cmd.index("-U")
        del cmd[i:i + 2]
    cmd += ["-U", user, "-d", db, "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", *args]
    out = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"psql failed ({out.returncode}): {out.stderr}")
    return out.stdout.strip()


@unittest.skipUnless(PSQL, "STANDALONE_MIGRATE_TEST_PSQL not set")
class CronHoldSqlTest(unittest.TestCase):
    def setUp(self):
        psql(f"DROP DATABASE IF EXISTS {DB};", db="postgres")
        psql("DO $$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'odoo') THEN CREATE ROLE odoo LOGIN; END IF; END$$;",
             db="postgres")
        psql(f"CREATE DATABASE {DB} OWNER odoo;", db="postgres")
        psql(SCHEMA, user="odoo")
        # 原值（還原後要一模一樣）
        self.orig_cron1 = psql("SELECT nextcall FROM ir_cron WHERE id = 1")
        self.orig_trig1 = psql("SELECT call_at FROM ir_cron_trigger WHERE id = 1")
        self.orig_cron2 = psql("SELECT nextcall FROM ir_cron WHERE id = 2")

    def hold(self, minutes=120):
        return psql((TARGET / "cron-hold.sql").read_text(), user="odoo", args=("-v", f"minutes={minutes}"))

    def release(self):
        return psql((TARGET / "cron-release.sql").read_text())

    def q(self, sql):
        return psql(sql, user="odoo")

    def test_hold_clamps_late_triggers_and_release_restores(self):
        out = self.hold()
        tag, until, n_crons, n_triggers = out.split("|")
        self.assertEqual((tag, n_crons, n_triggers), ("cronhold", "1", "1"))
        self.assertEqual(self.q("SELECT count(*) FROM ir_cron WHERE nextcall = '%s'" % until), "1")
        self.assertEqual(self.q("SELECT nextcall FROM ir_cron WHERE id = 2"), self.orig_cron2)  # 本來就晚於暫停時點：不動

        # 開機途中：loading.py 排的 autovacuum trigger（1 分鐘後）、-u all 改寫 nextcall、XML 重新啟用排程、遠期 trigger
        self.q("INSERT INTO ir_cron_trigger (cron_id, call_at) VALUES (2, (now() AT TIME ZONE 'UTC') + interval '1 minute')")
        self.q("UPDATE ir_cron SET nextcall = (now() AT TIME ZONE 'UTC') + interval '10 minutes' WHERE id = 2")
        self.q("UPDATE ir_cron SET active = true WHERE id = 3")
        self.q("INSERT INTO ir_cron_trigger (cron_id, call_at) VALUES (2, (now() AT TIME ZONE 'UTC') + interval '1 day')")
        self.assertEqual(self.q("SELECT call_at FROM ir_cron_trigger WHERE id = 2"), until)
        self.assertEqual(self.q("SELECT nextcall FROM ir_cron WHERE id = 2"), until)
        self.assertEqual(self.q("SELECT nextcall FROM ir_cron WHERE id = 3"), until)
        self.assertNotEqual(self.q("SELECT call_at FROM ir_cron_trigger WHERE id = 3"), until)
        # 暫停期間沒有任何東西會到期（模擬 Odoo cron thread 的取件條件）
        self.assertEqual(self.q("""SELECT count(*) FROM ir_cron WHERE active AND (nextcall <= (now() AT TIME ZONE 'UTC')
                                   OR id IN (SELECT cron_id FROM ir_cron_trigger WHERE call_at <= (now() AT TIME ZONE 'UTC')))"""),
                         "0")

        self.assertEqual(self.release(), "cronrelease|1|1|1|2|1")
        self.assertEqual(self.q("SELECT nextcall FROM ir_cron WHERE id = 1"), self.orig_cron1)
        self.assertEqual(self.q("SELECT call_at FROM ir_cron_trigger WHERE id = 1"), self.orig_trig1)
        due = "<= (now() AT TIME ZONE 'UTC')"
        self.assertEqual(self.q(f"SELECT count(*) FROM ir_cron WHERE id IN (2, 3) AND nextcall {due}"), "2")
        self.assertEqual(self.q(f"SELECT count(*) FROM ir_cron_trigger WHERE id = 2 AND call_at {due}"), "1")
        self.assertEqual(self.q(f"SELECT count(*) FROM ir_cron_trigger WHERE id = 3 AND call_at {due}"), "0")
        self.assertEqual(self.q("SELECT count(*) FROM pg_trigger WHERE tgname = 'standalone_odoo_migrate_cron_hold'"), "0")
        self.assertEqual(self.q("SELECT count(*) FROM pg_proc WHERE proname = 'standalone_odoo_migrate_cron_hold'"), "0")
        self.assertEqual(self.q("SELECT count(*) FROM ir_config_parameter WHERE key LIKE 'standalone_odoo_migrate.%'"), "0")
        # 放開之後 trigger 已移除：新排的 trigger 照原值
        self.q("INSERT INTO ir_cron_trigger (cron_id, call_at) VALUES (2, '2000-01-01')")
        self.assertEqual(self.q("SELECT call_at FROM ir_cron_trigger WHERE id = 4"), "2000-01-01 00:00:00")
        self.assertEqual(self.release(), "cronrelease|0|0|0|0|0")  # 重跑無害

    def test_expired_hold_is_inert_and_release_cleans_up(self):
        self.hold()
        # 模擬暫停時間已過：until 改到過去
        self.q("""UPDATE ir_config_parameter SET value = jsonb_set(value::jsonb, '{until}', '"2000-01-01 00:00:00"')::text
                   WHERE key = 'standalone_odoo_migrate.cron_hold'""")
        self.q("INSERT INTO ir_cron_trigger (cron_id, call_at) VALUES (2, '2001-01-01')")
        self.assertEqual(self.q("SELECT call_at FROM ir_cron_trigger WHERE id = 2"), "2001-01-01 00:00:00")
        before = self.q("SELECT nextcall FROM ir_cron WHERE id = 3")
        self.q("UPDATE ir_cron SET active = true WHERE id = 3")
        self.assertEqual(self.q("SELECT nextcall FROM ir_cron WHERE id = 3"), before)
        out = self.release()
        self.assertTrue(out.startswith("cronrelease|1|"), out)
        self.assertEqual(self.q("SELECT count(*) FROM pg_trigger WHERE tgname = 'standalone_odoo_migrate_cron_hold'"), "0")

    def test_hold_twice_keeps_single_guard(self):
        self.hold()
        self.hold()
        self.assertEqual(self.q("SELECT count(*) FROM pg_trigger WHERE tgname = 'standalone_odoo_migrate_cron_hold'"), "2")
        self.release()
        self.assertEqual(self.q("SELECT count(*) FROM pg_trigger WHERE tgname = 'standalone_odoo_migrate_cron_hold'"), "0")


if __name__ == "__main__":
    unittest.main()

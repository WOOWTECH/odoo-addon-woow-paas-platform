"""standalone-odoo-migrate 腳本的單元測試（不需要叢集）。

跑法：python3 -m unittest discover -s .claude/skills/standalone-odoo-migrate/tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import compat_check  # noqa: E402
from odoo_inspect import adapt_version, dir_digest, fingerprint_lines, scan_addons, version_key  # noqa: E402


def make_module(root, name, version="1.0", depends=("base",), python=(), extra_files=None):
    mod = Path(root) / name
    mod.mkdir(parents=True)
    manifest = {"name": name, "version": version, "depends": list(depends)}
    if python:
        manifest["external_dependencies"] = {"python": list(python)}
    (mod / "__manifest__.py").write_text(repr(manifest))
    (mod / "__init__.py").write_text("")
    for rel, content in (extra_files or {}).items():
        path = mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return mod


class TestVersionHelpers(unittest.TestCase):
    def test_adapt_version_matches_odoo(self):
        self.assertEqual(adapt_version("1.2"), "18.0.1.2")
        self.assertEqual(adapt_version("18.0.1.2"), "18.0.1.2")
        self.assertEqual(adapt_version("18.0"), "18.0.18.0")
        self.assertEqual(adapt_version(None), "18.0.1.0")

    def test_version_key_orders_numerically(self):
        self.assertLess(version_key("18.0.1.9"), version_key("18.0.1.10"))
        self.assertLess(version_key("18.0.20260810"), version_key("18.0.20260926"))


class TestDirDigest(unittest.TestCase):
    def test_ignores_bytecode_and_git_but_sees_content(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            make_module(a, "m", extra_files={"models/x.py": "x = 1\n"})
            make_module(b, "m", extra_files={"models/x.py": "x = 1\n", "models/__pycache__/x.pyc": "junk",
                                             ".git/HEAD": "ref: refs/heads/main\n"})
            self.assertEqual(dir_digest(Path(a) / "m")["sha256"], dir_digest(Path(b) / "m")["sha256"])
            (Path(b) / "m" / "models" / "x.py").write_text("x = 2\n")
            self.assertNotEqual(dir_digest(Path(a) / "m")["sha256"], dir_digest(Path(b) / "m")["sha256"])


class TestScanAddons(unittest.TestCase):
    def test_first_path_wins_and_shadow_is_reported(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            make_module(first, "dup", version="2.0")
            make_module(second, "dup", version="1.0")
            result = scan_addons([first, second], digest_filter=lambda _d: True)
            self.assertEqual(result["modules"]["dup"]["version"], "18.0.2.0")
            self.assertEqual(result["shadowed"][0]["shadowed_by"], first)


def inventory(modules, source_addons, **db):
    base_db = {"name": "odoo", "server_version": "17.10", "extensions": {"plpgsql": "1.0"},
               "module_states": {"installed": len(modules)}, "modules": modules, "params_present": []}
    base_db.update(db)
    return {"mode": "source", "core_date": "18.0.20260810", "conf": {}, "db": base_db,
            "addons": source_addons, "filestore": {"missing_referenced": 0},
            "pip": {"distributions": {"openai": "1.0"}, "import_map": {"Crypto": ["pycryptodome"]}}}


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src_extra = root / "src-extra"
        self.all_addons = root / "all-addons"
        self.ci = root / "ci"
        make_module(self.src_extra, "custom_only", version="1.0", python=("Crypto",))
        make_module(self.src_extra, "in_ci_same", version="1.0")
        make_module(self.src_extra, "in_ci_diff", version="1.0", extra_files={"a.py": "1"})
        make_module(self.src_extra, "in_image", version="1.0")
        make_module(self.all_addons, "in_image", version="1.0")
        make_module(self.ci / "addons", "in_ci_same", version="1.0")
        make_module(self.ci / "addons", "in_ci_diff", version="1.0", extra_files={"a.py": "2"})
        make_module(self.ci / "addons", "in_ci_old", version="0.9")
        (self.ci / "config").mkdir()
        (self.ci / "config" / "cloud-addons.list").write_text("# comment\nin_image\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_classify(self, db_modules):
        src = scan_addons([str(self.src_extra)], digest_filter=lambda _d: True)
        src["modules"]["base"] = {"dir": "/core", "version": "18.0.1.3"}
        tgt = {"mode": "image", "core_date": "18.0.20260926",
               "addons": scan_addons([str(self.all_addons)], digest_filter=lambda _d: True),
               "pip": {"distributions": {"openai": "1.0"}, "import_map": {}}}
        tgt["addons"]["modules"]["base"] = {"dir": "/core", "version": "18.0.1.3"}
        inv = inventory(db_modules, src)
        ci = compat_check.load_ci(str(self.ci))
        rows = compat_check.classify(inv, tgt, ci, select_dirs={str(self.all_addons)})
        return {r["module"]: r for r in rows}, inv, tgt, ci

    def test_statuses(self):
        mods = {name: {"state": "installed", "version": "18.0.1.0"} for name in
                ("custom_only", "in_ci_same", "in_ci_diff", "in_image")}
        mods["base"] = {"state": "installed", "version": "18.0.1.3"}
        mods["in_ci_old"] = {"state": "installed", "version": "18.0.1.0"}
        mods["ghost"] = {"state": "installed", "version": "18.0.1.0"}
        mods["not_installed"] = {"state": "to install", "version": None}
        rows, inv, tgt, ci = self.run_classify(mods)
        self.assertEqual(rows["base"]["status"], "OK")
        self.assertEqual(rows["custom_only"]["status"], "NEED_IMPORT")
        self.assertEqual(rows["in_ci_same"]["status"], "NEED_LIST")
        self.assertEqual(rows["in_ci_diff"]["status"], "DIVERGED")
        self.assertEqual(rows["in_image"]["status"], "SELECT")
        self.assertEqual(rows["in_ci_old"]["status"], "BLOCK_DOWNGRADE")
        self.assertEqual(rows["ghost"]["status"], "BLOCK_MISSING")
        self.assertNotIn("not_installed", rows)
        deps = compat_check.python_deps(list(rows.values()), inv, ci)
        inv["pip"]["distributions"]["google_genai"] = "1.0"
        missing, source_only = compat_check.pip_gaps(deps, inv, tgt)
        self.assertEqual([m["requirement"] for m in missing], ["Crypto"])
        self.assertEqual(missing[0]["source_dists"], ["pycryptodome"])
        self.assertEqual(source_only, ["google_genai"])

    def test_db_newer_than_image_blocks(self):
        rows, *_ = self.run_classify({"in_image": {"state": "installed", "version": "18.0.1.1"}})
        self.assertEqual(rows["in_image"]["status"], "BLOCK_DOWNGRADE")

    def test_cli_exit_codes(self):
        rows, inv, tgt, _ci = self.run_classify({"base": {"state": "installed", "version": "18.0.1.3"}})
        with tempfile.TemporaryDirectory() as d:
            inv_path, tgt_path = Path(d) / "inv.json", Path(d) / "tgt.json"
            inv_path.write_text(json.dumps({"inspect": inv, "source": {"namespace": "demo"}}))
            tgt_path.write_text(json.dumps(tgt))
            cmd = [sys.executable, str(SCRIPTS / "compat_check.py"), "--inventory", str(inv_path),
                   "--target", str(tgt_path)]
            ok = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("READY", ok.stdout)
            inv["db"]["extensions"]["postgis"] = "3.4"
            inv_path.write_text(json.dumps({"inspect": inv}))
            blocked = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(blocked.returncode, 3)
            self.assertIn("postgis", blocked.stdout)


class FakeCursor:
    """記錄執行過的語句；SELECT 回傳固定結果，generator 查詢回傳兩條子查詢。"""

    def __init__(self):
        self.executed, self._rows = [], []

    def execute(self, sql):
        self.executed.append(sql.strip())
        head = sql.strip().split(None, 1)[0].upper()
        if "format(" in sql:
            self._rows = [("SELECT 'table|a|1|x'",), ("SELECT 'table|b|2|y'",)]
        elif head == "SELECT":
            self._rows = [(sql.strip().split("'")[1],)] if "'" in sql else [("v",)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class TestFingerprintLines(unittest.TestCase):
    def test_parses_real_sql_and_never_ends_the_callers_transaction(self):
        sql = (SCRIPTS / "fingerprint.sql").read_text()
        cur = FakeCursor()
        out = fingerprint_lines(cur, sql)
        heads = [stmt.split(None, 1)[0].upper().rstrip(";") for stmt in cur.executed]
        self.assertNotIn("COMMIT", heads)
        self.assertNotIn("BEGIN", heads)
        self.assertIn("SET", heads)
        self.assertTrue(out.endswith("table|a|1|x\ntable|b|2|y\n"), out)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""manifest 版號 gate 的自測（#538）。

🔴 **這支測試本身就是 gate 的注入式驗證**，不是附屬品。理由（#538 驗收段）：
一道從沒被看過會失敗的檢查，與一道什麼都沒檢查的檢查，輸出長得一模一樣。
所以 CI 每次跑 gate 之前都先跑這支——把「驗過一次」升級成「持續被驗證」。

覆蓋兩層：

1. **純比較器**（不碰 git）：嚴格大於的四組判準，含**跨百位**兩個方向。
   只驗「同值」那組的話，用字串比較的錯誤實作會照樣全綠通過
   （'18.0.1.0.100' > '18.0.1.0.99' 在字串比較下是 False）。
2. **端到端**（真 git repo）：三點 diff（`base...HEAD`）取的是 merge-base 之後
   **本 PR 自己**動的檔案。兩點 diff 會把 base 分支自己的改動也算進來 ⇒
   「只改 docs 的 PR」會被誤判成動了 src/ ⇒ 假紅。

跑法：
    python3 scripts/ci/test_manifest_version_gate.py
    # 或 python3 -m pytest scripts/ci/test_manifest_version_gate.py
"""
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_GATE_PATH = _HERE / "manifest-version-gate.py"

_spec = importlib.util.spec_from_file_location("manifest_version_gate", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
# 必須先登記進 sys.modules 再 exec：@dataclasses.dataclass 會用 cls.__module__
# 回查 sys.modules，沒登記的話 3.12+ 會 AttributeError: 'NoneType' has no '__dict__'。
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)


# --------------------------------------------------------------------------
# 1. 版號解析：數值 tuple，不是字串
# --------------------------------------------------------------------------
class TestParseVersion(unittest.TestCase):
    def test_dotted_to_int_tuple(self):
        self.assertEqual(gate.parse_version_tuple("18.0.1.0.116"), (18, 0, 1, 0, 116))

    def test_cross_hundred_orders_numerically(self):
        # 🔴 本 repo 已跨百位。字串比較在這裡會判反。
        self.assertGreater(
            gate.parse_version_tuple("18.0.1.0.100"),
            gate.parse_version_tuple("18.0.1.0.99"),
        )

    def test_shorter_version_pads_with_zero(self):
        # 18.0.1.0 應視為 18.0.1.0.0，嚴格小於 18.0.1.0.1
        self.assertLess(
            gate.parse_version_tuple("18.0.1.0"),
            gate.parse_version_tuple("18.0.1.0.1"),
        )
        self.assertEqual(
            gate.compare_versions("18.0.1.0", "18.0.1.0.0"), 0
        )

    def test_non_numeric_segment_fails_loud(self):
        # 不做「盡力而為」的猜測——猜錯的方向正好是假綠。
        with self.assertRaises(ValueError):
            gate.parse_version_tuple("18.0.1.0.1b")

    def test_empty_fails_loud(self):
        with self.assertRaises(ValueError):
            gate.parse_version_tuple("")


class TestReadManifestVersion(unittest.TestCase):
    def test_double_quoted(self):
        self.assertEqual(
            gate.read_manifest_version('{\n "name": "x",\n "version": "18.0.1.0.116",\n}\n'),
            "18.0.1.0.116",
        )

    def test_single_quoted(self):
        self.assertEqual(
            gate.read_manifest_version("{'version': '18.0.1.0.7'}"),
            "18.0.1.0.7",
        )

    def test_missing_version_fails_loud(self):
        with self.assertRaises(ValueError):
            gate.read_manifest_version('{"name": "x"}')


# --------------------------------------------------------------------------
# 2. 豁免判準：universal 而非 existence（#538 留言 3）
# --------------------------------------------------------------------------
class TestBumpRequirement(unittest.TestCase):
    def test_no_src_change_is_out_of_scope(self):
        # extra/paas-operator 是另一條獨立發版線；docs/.gitea/scripts 更無關。
        req = gate.classify_bump_requirement(
            [
                "docs/index.md",
                ".gitea/workflows/manifest-version-gate.yml",
                "scripts/ci/manifest-version-gate.py",
                "extra/paas-operator/src/main.py",
            ]
        )
        self.assertFalse(req.required)
        self.assertEqual(req.reason, "out-of-scope")

    def test_tests_only_is_exempt(self):
        req = gate.classify_bump_requirement(
            ["src/tests/test_a.py", "src/tests/sub/test_b.py", "docs/x.md"]
        )
        self.assertFalse(req.required)
        self.assertEqual(req.reason, "tests-only")

    def test_one_impl_file_among_tests_revokes_exemption(self):
        # existence 判準（「有動 src/tests/ 就免」）在這裡會放行 ⇒ 繞路成立。
        req = gate.classify_bump_requirement(
            ["src/tests/test_a.py", "src/tests/test_b.py", "src/models/foo.py"]
        )
        self.assertTrue(req.required)
        self.assertIn("src/models/foo.py", req.offending_paths)
        self.assertNotIn("src/tests/test_a.py", req.offending_paths)

    def test_src_tests_prefix_is_directory_boundary_not_string_prefix(self):
        # src/tests_helpers.py 不是 src/tests/ 底下的檔案，不得被豁免。
        req = gate.classify_bump_requirement(["src/tests_helpers.py"])
        self.assertTrue(req.required)
        self.assertEqual(req.offending_paths, ["src/tests_helpers.py"])

    def test_src_prefix_is_directory_boundary_too(self):
        # srcfoo/bar.py 不在 src/ 底下。
        req = gate.classify_bump_requirement(["srcfoo/bar.py"])
        self.assertFalse(req.required)
        self.assertEqual(req.reason, "out-of-scope")

    def test_empty_changeset_is_out_of_scope(self):
        req = gate.classify_bump_requirement([])
        self.assertFalse(req.required)


# --------------------------------------------------------------------------
# 3. 🔴 四組注入式驗證的核心：嚴格大於，含跨百位兩個方向
# --------------------------------------------------------------------------
_SRC_CHANGE = ["src/models/cloud_service.py"]


class TestEvaluateBump(unittest.TestCase):
    def _verdict(self, base, head, changed=_SRC_CHANGE):
        return gate.evaluate_bump(base_version=base, head_version=head, changed_paths=changed)

    def test_case1_equal_is_red(self):
        v = self._verdict("18.0.1.0.116", "18.0.1.0.116")
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "equal")

    def test_case2_behind_is_red(self):
        v = self._verdict("18.0.1.0.116", "18.0.1.0.113")
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "behind")

    def test_case3_cross_hundred_forward_is_green(self):
        # develop .99 / PR .100 ⇒ 綠。字串比較的實作在這裡會判成假紅。
        v = self._verdict("18.0.1.0.99", "18.0.1.0.100")
        self.assertTrue(v.ok, msg=f"跨百位被誤判為未遞增：{v.detail}")
        self.assertEqual(v.reason, "ok")

    def test_case4_cross_hundred_backward_is_red(self):
        # develop .100 / PR .99 ⇒ 紅。字串比較的實作在這裡會判成假綠。
        v = self._verdict("18.0.1.0.100", "18.0.1.0.99")
        self.assertFalse(v.ok, msg=f"版號倒退被放行：{v.detail}")
        self.assertEqual(v.reason, "behind")

    def test_normal_bump_is_green(self):
        v = self._verdict("18.0.1.0.116", "18.0.1.0.117")
        self.assertTrue(v.ok)

    def test_equal_but_tests_only_is_green(self):
        v = self._verdict("18.0.1.0.116", "18.0.1.0.116", changed=["src/tests/test_a.py"])
        self.assertTrue(v.ok)
        self.assertEqual(v.reason, "tests-only")

    def test_equal_but_no_src_change_is_green(self):
        v = self._verdict("18.0.1.0.116", "18.0.1.0.116", changed=["docs/a.md"])
        self.assertTrue(v.ok)
        self.assertEqual(v.reason, "out-of-scope")

    def test_bumping_on_an_exempt_pr_is_still_green(self):
        # 豁免是「不強制 bump」，不是「不准 bump」。
        v = self._verdict("18.0.1.0.116", "18.0.1.0.117", changed=["src/tests/test_a.py"])
        self.assertTrue(v.ok)


class TestFailureMessages(unittest.TestCase):
    """兩種失效機制不同，訊息必須分辨得出來（#538 留言 1 第 2 點）。"""

    def test_equal_message_names_the_silent_mechanism(self):
        v = gate.evaluate_bump(
            base_version="18.0.1.0.116",
            head_version="18.0.1.0.116",
            changed_paths=_SRC_CHANGE,
        )
        self.assertIn("靜默", v.detail)
        self.assertIn("衝突", v.detail)

    def test_behind_message_rejects_both_wrong_resolutions(self):
        v = gate.evaluate_bump(
            base_version="18.0.1.0.116",
            head_version="18.0.1.0.113",
            changed_paths=_SRC_CHANGE,
        )
        self.assertIn("保留自己", v.detail)
        self.assertIn("照抄", v.detail)

    def test_both_failures_suggest_a_strictly_greater_value(self):
        for head in ("18.0.1.0.116", "18.0.1.0.113"):
            v = gate.evaluate_bump(
                base_version="18.0.1.0.116", head_version=head, changed_paths=_SRC_CHANGE
            )
            self.assertEqual(v.suggested_version, "18.0.1.0.117")
            self.assertIn("18.0.1.0.117", v.detail)

    def test_suggestion_is_strictly_greater_across_hundred(self):
        v = gate.evaluate_bump(
            base_version="18.0.1.0.99", head_version="18.0.1.0.99", changed_paths=_SRC_CHANGE
        )
        self.assertEqual(v.suggested_version, "18.0.1.0.100")


# --------------------------------------------------------------------------
# 4. 同族的另一半：migrations 目錄版號高於 manifest ⇒ 靜默 no-op
# --------------------------------------------------------------------------
class TestMigrationDirs(unittest.TestCase):
    def test_below_manifest_is_ok(self):
        v = gate.evaluate_migrations("18.0.1.0.116", ["18.0.1.0.87", "18.0.1.0.1"])
        self.assertTrue(v.ok)

    def test_equal_to_manifest_is_ok(self):
        # Odoo 的視窗是 installed < dir <= manifest，等於 manifest 會執行。
        v = gate.evaluate_migrations("18.0.1.0.116", ["18.0.1.0.116"])
        self.assertTrue(v.ok)

    def test_above_manifest_is_red(self):
        v = gate.evaluate_migrations("18.0.1.0.116", ["18.0.1.0.117"])
        self.assertFalse(v.ok)
        self.assertIn("18.0.1.0.117", v.detail)

    def test_above_manifest_across_hundred_is_red(self):
        v = gate.evaluate_migrations("18.0.1.0.99", ["18.0.1.0.100"])
        self.assertFalse(v.ok)

    def test_below_manifest_across_hundred_is_ok(self):
        v = gate.evaluate_migrations("18.0.1.0.100", ["18.0.1.0.99"])
        self.assertTrue(v.ok)

    def test_zero_version_is_special_and_exempt(self):
        # 0.0.0 在 Odoo 是「任何版本變更都跑」的特例，不參與大小比較。
        v = gate.evaluate_migrations("18.0.1.0.116", ["0.0.0"])
        self.assertTrue(v.ok)

    def test_majorless_dir_is_normalised_like_odoo(self):
        # Odoo convert_version：點數 <= 2 就補上 server major（18.0.）。
        self.assertEqual(gate.convert_migration_version("1.0.117"), "18.0.1.0.117")
        self.assertEqual(gate.convert_migration_version("18.0.1.0.117"), "18.0.1.0.117")
        v = gate.evaluate_migrations("18.0.1.0.116", ["1.0.117"])
        self.assertFalse(v.ok)

    def test_dirs_odoo_ignores_are_reported_not_silently_dropped(self):
        # 「跳過幾個」不夠，要說出是哪幾個——否則就是靜默截斷。
        v = gate.evaluate_migrations("18.0.1.0.116", ["18.0.1.0.87", "tests", "README"])
        self.assertTrue(v.ok)
        self.assertEqual(sorted(v.ignored_dirs), ["README", "tests"])
        self.assertIn("README", v.detail)
        self.assertIn("tests", v.detail)

    def test_no_migrations_dir_is_ok(self):
        v = gate.evaluate_migrations("18.0.1.0.116", [])
        self.assertTrue(v.ok)


# --------------------------------------------------------------------------
# 5. 端到端：真 git repo，跑真的腳本
# --------------------------------------------------------------------------
def _git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _manifest(version):
    return '{\n    "name": "Woow PaaS Platform",\n    "version": "%s",\n}\n' % version


class TestEndToEnd(unittest.TestCase):
    """建真的 git repo，用 subprocess 跑真的 gate。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        _git(self.repo.parent, "init", "-b", "develop", "repo")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.10"))
        self._write("src/models/foo.py", "x = 1\n")
        self._write("docs/a.md", "a\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "base")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, content):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def _run(self, cwd=None, base_ref="develop"):
        return subprocess.run(
            [sys.executable, str(_GATE_PATH), "--base-ref", base_ref],
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
        )

    def _advance_develop(self, version):
        """模擬別人先 merge：develop 自己也動了 src/ 並 bump。"""
        _git(self.repo, "checkout", "develop")
        self._write("src/__manifest__.py", _manifest(version))
        self._write("src/models/other.py", "y = 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "someone else merged")

    def test_src_change_without_bump_is_red(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr")
        self._advance_develop("18.0.1.0.11")
        _git(self.repo, "checkout", "pr")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("18.0.1.0.12", r.stdout + r.stderr)

    def test_proper_bump_is_green(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.12"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr")
        self._advance_develop("18.0.1.0.11")
        _git(self.repo, "checkout", "pr")

        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)

    def test_docs_only_pr_is_green_even_when_develop_touched_src(self):
        """🔴 三點 diff 的守線：兩點 diff 會把 develop 自己的 src/ 改動算到 PR 頭上。"""
        _git(self.repo, "checkout", "-b", "pr")
        self._write("docs/a.md", "changed\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "docs only")
        self._advance_develop("18.0.1.0.11")
        _git(self.repo, "checkout", "pr")

        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("out-of-scope", r.stdout)

    def test_tests_only_pr_is_green_without_bump(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/tests/test_x.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "tests only")
        self._advance_develop("18.0.1.0.11")
        _git(self.repo, "checkout", "pr")

        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("tests-only", r.stdout)

    def test_tests_plus_one_impl_file_is_red(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/tests/test_x.py", "pass\n")
        self._write("src/models/foo.py", "x = 3\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "tests + one impl line")
        self._advance_develop("18.0.1.0.11")
        _git(self.repo, "checkout", "pr")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("src/models/foo.py", r.stdout + r.stderr)

    def test_migration_dir_above_manifest_is_red(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        self._write("src/migrations/18.0.1.0.12/pre-x.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "migration ahead of manifest")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("18.0.1.0.12", r.stdout + r.stderr)

    def test_shallow_clone_fails_loud(self):
        """shallow clone 沒有 merge-base ⇒ 必須明講要 fetch-depth: 0，不可靜默算錯。"""
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.12"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr")

        shallow = pathlib.Path(self._tmp.name) / "shallow"
        _git(
            self.repo.parent,
            "clone",
            "--depth=1",
            "--branch=pr",
            "file://%s" % self.repo,
            str(shallow),
        )
        r = self._run(cwd=shallow, base_ref="origin/develop")
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("fetch-depth", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""memory-integrity 的 index-links 純邏輯測試（fixture，不碰真實 memory 目錄）。

全部用合成目錄，所以**不依賴本機 `~/.claude/projects/<slug>/memory/`**，
也因此可以從 worktree／CI runner 跑（受測腳本本身不行——見它的 docstring）。

跑法：
    python3 scripts/audit/test_memory_integrity.py
    # 或 python3 -m pytest scripts/audit/test_memory_integrity.py

本檔的核心是 `TestOrphanNotMaskedByRepoLink`：那條在乎的不是「repo 相對路徑會不會
解析」，而是「解析成功之後**不可以**被算成 memory 檔的入口」。兩者共用同一段程式，
卻是相反方向的需求，只驗前者會讓後者靜默失守。
"""
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_GATE_PATH = pathlib.Path(__file__).with_name("memory-integrity.py")
_spec = importlib.util.spec_from_file_location("memory_integrity", _GATE_PATH)
mi = importlib.util.module_from_spec(_spec)
# 檔名有連字號，只能走 importlib。先登記進 sys.modules 再 exec：@dataclasses.dataclass
# 會用 cls.__module__ 回查 sys.modules，沒登記的話 3.12+ 會噴
# AttributeError: 'NoneType' object has no attribute '__dict__'。
sys.modules[_spec.name] = mi
_spec.loader.exec_module(mi)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class FixtureCase(unittest.TestCase):
    """提供一組 memory 目錄 + 假 repo root。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = self._tmp.name
        self.mem = os.path.join(base, "memory")
        self.root = os.path.join(base, "repo")
        os.makedirs(self.mem)
        os.makedirs(self.root)
        self.addCleanup(self._tmp.cleanup)

    def memory_file(self, name, body="x"):
        _write(os.path.join(self.mem, name), f"---\nname: {name[:-3]}\n---\n{body}\n")

    def repo_file(self, relpath, body="x"):
        _write(os.path.join(self.root, relpath), body + "\n")

    def index(self, *lines):
        _write(os.path.join(self.mem, "MEMORY.md"), "\n".join(lines) + "\n")

    def audit(self):
        return mi.audit_index_links(self.mem, self.root)


class TestRepoRelativeResolution(FixtureCase):
    """情境 1：repo 相對路徑必須以 repo root 為基準解析。"""

    def test_repo_relative_link_resolves(self):
        self.repo_file("docs/tracking/landings/501-global-cap-landing.md")
        self.index("- [x](docs/tracking/landings/501-global-cap-landing.md)")
        # 注入探針（#538 工單 6）：故意改壞，確認 CI 真的會紅；下一個 commit 還原
        self.assertEqual(self.audit().dead, ["PROBE-607-DELIBERATE-FAILURE"])

    def test_memory_relative_link_still_resolves(self):
        # 迴歸：memory 內部互連是原本就有的形態，不能因為新增 repo 基準而壞掉
        self.memory_file("reference_foo.md")
        self.index("- [x](reference_foo.md)")
        self.assertEqual(self.audit().dead, [])

    def test_absolute_link_still_resolves(self):
        self.repo_file("docs/decisions/x.md")
        self.index("- [x](%s)" % os.path.join(self.root, "docs/decisions/x.md"))
        self.assertEqual(self.audit().dead, [])

    def test_memory_base_wins_when_both_exist(self):
        """兩個基準都命中時的優先序必須是確定的（memory 先）。"""
        self.memory_file("both.md")
        self.repo_file("both.md")
        self.index("- [x](both.md)")
        result = self.audit()
        self.assertEqual(result.dead, [])
        # memory 優先 ⇒ 它算 memory 檔的入口 ⇒ 不是孤兒
        self.assertEqual(result.orphan, [])


class TestDeadLinks(FixtureCase):
    """情境 2：兩個基準都找不到 ⇒ 仍必須報斷鏈。"""

    def test_relative_missing_under_both_bases_is_dead(self):
        self.index("- [x](docs/tracking/landings/does-not-exist.md)")
        self.assertEqual(self.audit().dead,
                         ["docs/tracking/landings/does-not-exist.md"])

    def test_bare_name_missing_under_both_bases_is_dead(self):
        self.index("- [x](reference_gone.md)")
        self.assertEqual(self.audit().dead, ["reference_gone.md"])

    def test_absolute_missing_is_dead(self):
        target = os.path.join(self.root, "docs/nope.md")
        self.index("- [x](%s)" % target)
        self.assertEqual(self.audit().dead, [target])

    def test_relative_dir_traversal_does_not_smuggle_a_pass(self):
        """`docs/x.md` 不存在時，不可因為 repo root 之外碰巧有同名檔而放行。"""
        # 只在 memory 的**上一層**放同名檔；兩個基準都不該命中它
        _write(os.path.join(self.mem, "..", "docs", "x.md"), "y\n")
        self.index("- [x](docs/x.md)")
        self.assertEqual(self.audit().dead, ["docs/x.md"])

    def test_dead_result_reports_which_bases_were_tried(self):
        self.index("- [x](docs/gone.md)")
        tried = self.audit().tried["docs/gone.md"]
        self.assertEqual(tried, [os.path.join(self.mem, "docs/gone.md"),
                                 os.path.join(self.root, "docs/gone.md")])


class TestOrphanNotMaskedByRepoLink(FixtureCase):
    """🔴 情境 3：本次最重要的一條——repo 連結不得因 basename 相同而冒充入口。"""

    def test_orphan_still_reported_when_repo_link_shares_basename(self):
        self.memory_file("501-global-cap-landing.md")          # 沒有任何索引行指向它
        self.repo_file("docs/tracking/landings/501-global-cap-landing.md")
        self.index("- [x](docs/tracking/landings/501-global-cap-landing.md)")
        result = self.audit()
        self.assertEqual(result.dead, [], "repo 相對路徑應該要解析得到")
        self.assertEqual(result.orphan, ["501-global-cap-landing.md"],
                         "repo 路徑的 basename 不是 memory 檔的入口")

    def test_memory_link_does_give_an_entry(self):
        """陽性對照：同一個檔，改成 memory 相對連結就**不該**再是孤兒。"""
        self.memory_file("501-global-cap-landing.md")
        self.repo_file("docs/tracking/landings/501-global-cap-landing.md")
        self.index("- [a](docs/tracking/landings/501-global-cap-landing.md)",
                   "- [b](501-global-cap-landing.md)")
        self.assertEqual(self.audit().orphan, [])

    def test_orphan_reported_when_no_link_at_all(self):
        """迴歸：最基本的孤兒仍要抓到。"""
        self.memory_file("lonely.md")
        self.index("（沒有任何連結）")
        self.assertEqual(self.audit().orphan, ["lonely.md"])

    def test_absolute_link_into_memory_dir_counts_as_entry(self):
        """絕對路徑若真的指向 memory 目錄裡的檔，那就是入口——判準是解析結果不是字面。"""
        self.memory_file("abs_linked.md")
        self.index("- [x](%s)" % os.path.join(self.mem, "abs_linked.md"))
        result = self.audit()
        self.assertEqual(result.dead, [])
        self.assertEqual(result.orphan, [])

    def test_file_in_memory_subdir_is_not_a_top_level_entry(self):
        """memory 子目錄下的同名檔不算頂層那個檔的入口。"""
        self.memory_file("nested.md")
        _write(os.path.join(self.mem, "sub", "nested.md"), "z\n")
        self.index("- [x](sub/nested.md)")
        self.assertEqual(self.audit().orphan, ["nested.md"])


class TestResultShape(FixtureCase):
    def test_counts_and_ok_flag(self):
        self.memory_file("a.md")
        self.repo_file("docs/b.md")
        self.index("- [a](a.md)", "- [b](docs/b.md)")
        result = self.audit()
        self.assertEqual(result.total, 2)
        self.assertEqual(result.on_disk, ["a.md"])
        self.assertTrue(result.ok)

    def test_not_ok_when_dead_or_orphan(self):
        self.memory_file("a.md")
        self.index("- [x](docs/gone.md)")
        result = self.audit()
        self.assertFalse(result.ok)
        self.assertEqual(result.dead, ["docs/gone.md"])
        self.assertEqual(result.orphan, ["a.md"])

    def test_dead_is_deduped_and_sorted(self):
        self.index("- [x](z.md)", "- [x](z.md)", "- [y](a.md)")
        self.assertEqual(self.audit().dead, ["a.md", "z.md"])

    def test_memory_md_itself_is_not_an_orphan(self):
        self.index("（空）")
        self.assertEqual(self.audit().orphan, [])


class TestCli(FixtureCase):
    """端到端：真的用 subprocess 跑腳本，驗 exit code 與輸出。"""

    def run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, str(_GATE_PATH), "--links",
             "--memory-dir", self.mem, "--repo-root", self.root, *extra],
            capture_output=True, text=True,
        )

    def test_clean_exits_zero(self):
        self.memory_file("a.md")
        self.repo_file("docs/b.md")
        self.index("- [a](a.md)", "- [b](docs/b.md)")
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("斷鏈 0、孤兒 0", proc.stdout)

    def test_prints_the_repo_root_base_it_used(self):
        """基準是隱性的，不印出來就無法察覺跑錯 checkout。"""
        self.index("（空）")
        self.assertIn(self.root, self.run_cli().stdout)

    def test_dead_and_orphan_exit_one(self):
        self.memory_file("lonely.md")
        self.index("- [x](docs/gone.md)")
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("斷鏈", proc.stdout)
        self.assertIn("docs/gone.md", proc.stdout)
        self.assertIn("孤兒", proc.stdout)
        self.assertIn("lonely.md", proc.stdout)

    def test_repo_root_flag_actually_changes_the_base(self):
        """否定對照：同一份索引，換掉 --repo-root 就必須變成斷鏈。"""
        self.repo_file("docs/b.md")
        self.index("- [b](docs/b.md)")
        self.assertEqual(self.run_cli().returncode, 0)
        other = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(other)
        proc = subprocess.run(
            [sys.executable, str(_GATE_PATH), "--links",
             "--memory-dir", self.mem, "--repo-root", other],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("docs/b.md", proc.stdout)

    def test_missing_memory_dir_fails_closed(self):
        proc = subprocess.run(
            [sys.executable, str(_GATE_PATH), "--links",
             "--memory-dir", os.path.join(self._tmp.name, "nope")],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("找不到 memory 目錄", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

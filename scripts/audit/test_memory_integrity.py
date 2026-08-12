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
import shutil
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
        self.assertEqual(self.audit().dead, [])

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


# ═══════════════════════════════════════════════════════════════════════════
# index-links 的可達性判準（#693：索引 ＝ MEMORY.md ＋ 它直連的那一層）
#
# 🔴 這一組的第一條測試刻意是**注入一個真正無入口的檔、確認仍然報紅**，不是
#    「跑一遍看乾不乾淨」。本次改動把孤兒判定從「只讀 MEMORY.md」放寬成「從
#    MEMORY.md 出發的可達性」，放寬類的改動只有一種災難性失敗模式：放寬到抓不到
#    真孤兒——而那個狀態的輸出（✓ 孤兒 0）與「真的沒有孤兒」完全一樣。
# ═══════════════════════════════════════════════════════════════════════════


class TestIndexReachesThroughTopicFiles(FixtureCase):

    def test_file_with_no_entry_at_all_is_still_an_orphan(self):
        """🔴 陽性對照：從 MEMORY.md 完全到不了的檔，放寬之後仍然是孤兒。

        這是本次改動不可協商的 invariant。它若不紅，下面每一條綠燈都不證明任何事。
        """
        self.memory_file("topic_x.md", "- [有入口](reference_linked.md)")
        self.memory_file("reference_linked.md")
        self.memory_file("reference_unreachable.md")   # 沒有任何人連它
        self.index("- [t](topic_x.md)")
        self.assertEqual(self.audit().orphan, ["reference_unreachable.md"])

    def test_file_linked_only_from_a_topic_file_is_not_an_orphan(self):
        """反向 case（#693 本體）：索引外移到 topic 檔之後不得被判成孤兒。"""
        self.memory_file("topic_x.md", "- [a](reference_a.md)\n- [b](reference_b.md)")
        self.memory_file("reference_a.md")
        self.memory_file("reference_b.md")
        self.index("- [t](topic_x.md)")
        result = self.audit()
        self.assertEqual(result.orphan, [])
        self.assertEqual(result.dead, [])

    def test_entry_depth_records_which_hop_gave_the_entry(self):
        """深度要留在結果裡——不然「索引往更深處搬」這種漂移沒有任何跡象。"""
        self.memory_file("topic_x.md", "- [a](reference_a.md)")
        self.memory_file("reference_a.md")
        self.index("- [t](topic_x.md)")
        depth = self.audit().entry_depth
        self.assertEqual(depth["topic_x.md"], 1)      # MEMORY.md 直連
        self.assertEqual(depth["reference_a.md"], 2)  # 經 topic 檔

    def test_a_non_index_memory_file_does_not_hand_out_entries(self):
        """自己就沒有入口的檔，連出去的東西也不算有入口（否則孤兒可以互相掩護）。"""
        self.memory_file("orphan_hub.md", "- [x](reference_x.md)")
        self.memory_file("reference_x.md")
        self.index("（沒有任何連結）")
        self.assertEqual(self.audit().orphan,
                         ["orphan_hub.md", "reference_x.md"])

    def test_third_level_file_is_still_an_orphan(self):
        """跳數上限 1：MEMORY.md → topic_a → topic_b 之後那一層不算入口。

        這條把「為什麼不做無上限遞迴」釘住。判準是官方機制：MEMORY.md 開場載入，
        topic 檔按需讀取——深度 2 的條目有一條開場就載入的路由行帶過去，深度 3
        的沒有，只有讀過整條鏈才會撞見。
        """
        self.memory_file("topic_a.md", "- [b](topic_b.md)")
        self.memory_file("topic_b.md", "- [deep](reference_deep.md)")
        self.memory_file("reference_deep.md")
        self.index("- [a](topic_a.md)")
        result = self.audit()
        self.assertEqual(result.orphan, ["reference_deep.md"])
        self.assertEqual(result.entry_depth["topic_b.md"], 2)

    def test_cycle_between_index_files_terminates(self):
        """循環引用靠 visited set，不靠跳數上限——上限是可以被調大的。"""
        self.memory_file("topic_a.md", "- [b](topic_b.md)\n- [x](reference_x.md)")
        self.memory_file("topic_b.md", "- [a](topic_a.md)")
        self.memory_file("reference_x.md")
        self.index("- [a](topic_a.md)")
        result = mi.audit_index_links(self.mem, self.root, hops=5)
        self.assertEqual(result.orphan, [])
        visited = [name for name, _ in result.sources]
        self.assertEqual(visited.count("topic_a.md"), 1, "同一個索引檔只該走訪一次")

    def test_dead_link_inside_a_topic_file_is_reported(self):
        """🔴 斷鏈判定必須跟著入口一起搬進 topic 檔，否則等於一起弄丟。"""
        self.memory_file("topic_x.md", "- [沒了](reference_gone.md)")
        self.index("- [t](topic_x.md)")
        result = self.audit()
        self.assertEqual(result.dead, ["reference_gone.md"])
        self.assertEqual(result.dead_sources["reference_gone.md"], ["topic_x.md"])

    def test_dead_link_in_memory_md_still_reported(self):
        """迴歸：MEMORY.md 自己的斷鏈不能因為改了資料源而漏掉。"""
        self.index("- [x](docs/gone.md)")
        self.assertEqual(self.audit().dead, ["docs/gone.md"])

    def test_repo_doc_linked_from_the_index_is_not_read_as_an_index(self):
        """repo doc 是索引的目的地、不是索引的一部分——它內部的連結兩邊都不算。

        它自己的相對連結以**它所在目錄**為基準，而 resolve_link() 只試 memory 與
        repo root ⇒ 讀進來當索引會系統性製造假斷鏈。
        """
        self.memory_file("reference_only_in_doc.md")
        self.repo_file("docs/x.md",
                       "- [a](reference_only_in_doc.md)\n- [b](docs/never-existed.md)")
        self.index("- [doc](docs/x.md)")
        result = self.audit()
        self.assertEqual(result.orphan, ["reference_only_in_doc.md"],
                         "repo doc 連到的 memory 檔不因此取得入口")
        self.assertEqual(result.dead, [], "repo doc 內部的連結不進斷鏈判定")

    def test_cli_reports_the_depth_spread(self):
        self.memory_file("topic_x.md", "- [a](reference_a.md)")
        self.memory_file("reference_a.md")
        self.index("- [t](topic_x.md)")
        proc = subprocess.run(
            [sys.executable, str(_GATE_PATH), "--links",
             "--memory-dir", self.mem, "--repo-root", self.root],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("入口深度", proc.stdout)
        self.assertIn("深度 1 1 檔", proc.stdout)
        self.assertIn("深度 2 1 檔", proc.stdout)


# ═══════════════════════════════════════════════════════════════════════════
# wiki-link 對帳（#693 之 3a）與 MEMORY.md 大小黃燈（3b）
#
# 🔴 兩者都是 **warning-only**。memory-links-guard 看 returncode 決定 block，
#    讓它們改變 exit code 等於自己造一個新的卡死——那正是本 issue 要修的東西。
#    TestWarningsDoNotAffectExitCode 就是釘這一條的。
# ═══════════════════════════════════════════════════════════════════════════


class TestWikiLinkReconciliation(FixtureCase):

    def wiki(self):
        return mi.audit_wiki_links(self.mem, self.root)

    def test_unresolved_wiki_link_is_reported(self):
        """陽性對照：指不到任何載體的 [[name]] 必須被點名。"""
        self.memory_file("reference_a.md", "參考 [[reference_never_written]]")
        self.index("- [a](reference_a.md)")
        result = self.wiki()
        self.assertEqual(sorted(result.unresolved), ["reference_never_written"])
        self.assertEqual(result.unresolved["reference_never_written"],
                         ["reference_a.md"])

    def test_resolved_by_basename(self):
        self.memory_file("reference_a.md", "見 [[reference_b]]")
        self.memory_file("reference_b.md")
        self.index("- [a](reference_a.md)", "- [b](reference_b.md)")
        self.assertEqual(self.wiki().unresolved, {})

    def test_resolved_by_frontmatter_name(self):
        """`name:` 與檔名不一致是本目錄的既有形態（已知限制 1），必須解析得到。"""
        _write(os.path.join(self.mem, "reference_kubectl_woow_k3s.md"),
               "---\nname: kubectl-woow-k3s\n---\n內容\n")
        self.memory_file("reference_a.md", "見 [[kubectl-woow-k3s]]")
        self.index("- [a](reference_a.md)")
        self.assertEqual(self.wiki().unresolved, {})

    def test_resolved_across_kebab_snake_and_prefix_mismatch(self):
        """`[[deploy-stg-gotchas]]` ↔ `reference_deploy_stg_gotchas.md`。"""
        self.memory_file("reference_deploy_stg_gotchas.md")
        self.memory_file("reference_a.md", "見 [[deploy-stg-gotchas]]")
        self.index("- [a](reference_a.md)", "- [d](reference_deploy_stg_gotchas.md)")
        self.assertEqual(self.wiki().unresolved, {})

    def test_trailing_md_suffix_is_tolerated(self):
        self.memory_file("reference_b.md")
        self.memory_file("reference_a.md", "見 [[reference_b.md]]")
        self.index("- [a](reference_a.md)", "- [b](reference_b.md)")
        self.assertEqual(self.wiki().unresolved, {})

    def test_repo_doc_target_resolves(self):
        self.repo_file("docs/guides/testing/x.md")
        self.memory_file("reference_a.md", "見 [[docs/guides/testing/x]]")
        self.index("- [a](reference_a.md)")
        self.assertEqual(self.wiki().unresolved, {})

    def test_shell_test_syntax_in_a_fenced_block_is_not_a_wiki_link(self):
        """🔴 誤報風險最高的一條：`[[ ... ]]` 與 wiki-link 同形。"""
        self.memory_file(
            "reference_a.md",
            "```bash\nif [[ \"${GITHUB_REF_NAME}\" == stg-* ]]; then :; fi\n```")
        self.index("- [a](reference_a.md)")
        result = self.wiki()
        self.assertEqual(result.unresolved, {})
        self.assertEqual(result.targets, [])
        self.assertEqual(result.occurrences, 0, "fenced block 內的整段都該被剝掉")

    def test_posix_class_in_inline_code_is_not_a_wiki_link(self):
        self.memory_file("reference_a.md", "用 `grep -E '[[:space:]]+'` 過濾")
        self.index("- [a](reference_a.md)")
        result = self.wiki()
        self.assertEqual(result.occurrences, 0)
        self.assertEqual(result.unresolved, {})

    def test_placeholder_in_chinese_is_skipped_not_reported(self):
        """示意用的中文佔位符不是壞掉的參照，列進 skipped、不進 unresolved。"""
        self.memory_file("reference_a.md", "改名時把 [[舊檔名]] 一起換掉")
        self.index("- [a](reference_a.md)")
        result = self.wiki()
        self.assertEqual(result.unresolved, {})
        self.assertEqual(result.skipped, ["舊檔名"])

    def test_bare_posix_class_outside_code_is_skipped(self):
        self.memory_file("reference_a.md", "regex 寫 [[:space:]] 就會爆")
        self.index("- [a](reference_a.md)")
        self.assertEqual(self.wiki().unresolved, {})

    def test_wiki_link_does_not_grant_an_index_entry(self):
        """🔴 正文的 [[name]] 是聯想連結、不是索引行——不得讓孤兒免罰。

        規格自己說「link liberally」，把它算成入口，孤兒判定會退化成永遠不會紅。
        """
        self.memory_file("topic_x.md", "只用 wiki 形態提到 [[reference_a]]")
        self.memory_file("reference_a.md")
        self.index("- [t](topic_x.md)")
        self.assertEqual(self.audit().orphan, ["reference_a.md"])


class TestIndexSizeMeasurement(FixtureCase):

    def size(self):
        return mi.index_size(os.path.join(self.mem, "MEMORY.md"))

    def test_limits_are_the_official_constants(self):
        """上限與比例是 Claude Code 自己的常數，不是本機觀測值——改動要有新證據。

        v2.1.228 bundle：`Nse=25000`（bytes cap）、`DJ=200`（line cap）、
        `S9p=0.7`（壓縮目標比例）。官方文件：「first 200 lines … or the first
        25KB, whichever comes first」。
        """
        self.assertEqual(mi.INDEX_BYTE_LIMIT, 25_000)
        self.assertEqual(mi.INDEX_LINE_LIMIT, 200)
        self.assertEqual(mi.INDEX_WARN_RATIO, 0.7)

    def test_frontmatter_is_not_counted(self):
        """官方口徑：frontmatter 先被剝除才計入（v2.1.211+）。"""
        _write(os.path.join(self.mem, "MEMORY.md"),
               "---\ntitle: x\nnote: " + "y" * 500 + "\n---\n短內容\n")
        size = self.size()
        self.assertLess(size.loaded_bytes, 100)
        self.assertGreater(size.raw_bytes, 500)
        self.assertEqual(size.lines, 1)

    def test_block_level_html_comment_is_not_counted(self):
        _write(os.path.join(self.mem, "MEMORY.md"),
               "一行\n<!--\n" + "註解" * 200 + "\n-->\n兩行\n")
        size = self.size()
        self.assertEqual(size.lines, 2)
        self.assertLess(size.loaded_bytes, 100)

    def test_comment_inside_a_code_fence_is_counted(self):
        """保守方向：少剝一點 ⇒ 黃燈提早亮；多剝會讓它晚亮，那才會咬人。"""
        _write(os.path.join(self.mem, "MEMORY.md"),
               "一行\n```html\n<!-- 這是範例 -->\n```\n")
        self.assertEqual(self.size().lines, 4)

    def test_inline_comment_with_trailing_content_is_kept(self):
        _write(os.path.join(self.mem, "MEMORY.md"), "<!-- 註 --> 後面還有正文\n")
        self.assertEqual(self.size().lines, 1)

    def test_warn_fires_at_the_compaction_line_not_at_the_hard_limit(self):
        line = "x" * 499 + "\n"
        _write(os.path.join(self.mem, "MEMORY.md"), line * 40)   # 20,000 bytes
        size = self.size()
        self.assertTrue(size.warn)
        self.assertFalse(size.over)

    def test_under_the_compaction_line_is_quiet(self):
        _write(os.path.join(self.mem, "MEMORY.md"), ("x" * 499 + "\n") * 20)
        size = self.size()
        self.assertFalse(size.warn)
        self.assertFalse(size.over)

    def test_line_count_alone_can_trip_the_warning(self):
        _write(os.path.join(self.mem, "MEMORY.md"), "短\n" * 150)
        size = self.size()
        self.assertTrue(size.warn)
        self.assertLess(size.loaded_bytes, mi.INDEX_BYTE_LIMIT * 0.7)


class TestWarningsDoNotAffectExitCode(FixtureCase):
    """🔴 hook 是看 returncode 決定 block 的。這一組守的是「不要造新的卡死」。"""

    def run_cli(self):
        return subprocess.run(
            [sys.executable, str(_GATE_PATH), "--links",
             "--memory-dir", self.mem, "--repo-root", self.root],
            capture_output=True, text=True)

    def test_unresolved_wiki_link_alone_exits_zero(self):
        self.memory_file("reference_a.md", "見 [[reference_never_written]]")
        self.index("- [a](reference_a.md)")
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("reference_never_written", proc.stdout)
        self.assertIn("warning-only", proc.stdout)

    def test_oversized_index_alone_exits_zero(self):
        _write(os.path.join(self.mem, "MEMORY.md"), ("x" * 499 + "\n") * 60)
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("超過**載入上限**".replace("**", ""), proc.stdout.replace("**", ""))

    def test_yellow_light_alone_exits_zero(self):
        _write(os.path.join(self.mem, "MEMORY.md"), ("x" * 499 + "\n") * 40)
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("建議壓縮線", proc.stdout)

    def test_size_line_is_always_printed_even_when_clean(self):
        """block 文案要引用這一行，所以它不能只有超標才出現。"""
        self.index("（空）")
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("[index-links] MEMORY.md ", proc.stdout)
        self.assertIn("/ 25,000 bytes", proc.stdout)

    def test_a_real_orphan_still_exits_one_with_the_warnings_present(self):
        """陰性對照：警告在場時，真正的 finding 仍然要把 exit code 打紅。"""
        _write(os.path.join(self.mem, "MEMORY.md"), ("x" * 499 + "\n") * 40)
        self.memory_file("reference_lonely.md", "見 [[reference_never_written]]")
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("reference_lonely.md", proc.stdout)


# ═══════════════════════════════════════════════════════════════════════════
# silent-truncation（#683：跨檔搬移扣除 + baseline 豁免）
#
# 🔴 這一組的第一條測試刻意是**注入真消失必須報紅**，不是「跑一遍看乾不乾淨」。
#    降噪類的改動只有一種災難性失敗模式：把判準改到抓不到真問題，而那個狀態的輸出
#    與「真的沒事」完全一樣。所以每一道扣除（跨檔搬移、baseline）都配一組
#    敏感度（該報要報）＋ 特異性（該扣要扣）的對照，缺任一邊都證明不了什麼。
#
# 隱私：以下 fixture 內容全部是為測試現編的假字串，**沒有一行來自真實 memory 檔**，
#      也不觸碰 `~/.claude/projects/<slug>/memory/`（受測程式的路徑全由參數注入）。
# ═══════════════════════════════════════════════════════════════════════════

# 「整段消失」的合成內容。每行都夠長、夠獨特，才會通過 moved_elsewhere() 的辨識度
# 門檻——這是刻意的：短行／樣板行另有專門的測試（TestMoveDeductionGuards）。
BETA = [
    "## 合成章節 Beta：這一節是為了模擬被整份 Write 蓋掉而寫的",
    "Beta 第一段，內容純屬虛構，只是要有足夠長度通過辨識度門檻。",
    "Beta 第二段，講一件不存在的事情，避免與任何真實記錄重疊。",
    "Beta 第三段，收尾用，仍然是虛構內容。",
    "Beta 第四段，這行讓本節達到五行。",
]
GAMMA = [
    "## 合成章節 Gamma：同一個檔後來才發生的**另一次**消失",
    "Gamma 第一段，用來驗證 baseline 不會把整個檔靜音。",
    "Gamma 第二段，它的指紋從來沒有進過 baseline。",
    "Gamma 第三段，所以它必須被重新報出來。",
]
HEADER = ["# 合成記憶檔", "這是一份合成 fixture，不對應任何真實記憶。"]


class TruncationFixtureCase(unittest.TestCase):
    """合成 memory 目錄 ＋ 合成 file-history ＋ 合成 baseline，三者全部由參數注入。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = self._tmp.name
        self.mem = os.path.join(base, "memory")
        self.history = os.path.join(base, "file-history")
        self.baseline = os.path.join(base, "baseline.tsv")
        os.makedirs(self.mem)
        os.makedirs(self.history)
        self.addCleanup(self._tmp.cleanup)

    def _doc(self, name, body):
        return "---\nname: %s\n---\n%s\n" % (name[:-3], "\n".join(body))

    def memory_file(self, name, body):
        _write(os.path.join(self.mem, name), self._doc(name, body))

    def snapshot(self, name, version, body):
        """在合成 file-history 放一份舊版快照；身分靠 frontmatter `name:` 認。"""
        _write(os.path.join(self.history, "session-1", "%s@v%d" % (name[:-3], version)),
               self._doc(name, body))

    def findings(self):
        waivers, _ = mi.read_baseline(self.baseline)
        result, _, _ = mi.compute_truncation(self.mem, self.history, waivers)
        return {f.name: f for f in result}

    def run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, str(_GATE_PATH), "--truncation",
             "--memory-dir", self.mem, "--file-history", self.history,
             "--baseline", self.baseline, *extra],
            capture_output=True, text=True,
        )

    def waive_cli(self, name, reason):
        return self.run_cli("--waive", name, "--reason", reason)

    def lose_beta(self):
        """A 檔的 Beta 一整節被吃掉，且 memory 目錄任何地方都沒有它。"""
        self.memory_file("reference_a.md", HEADER)
        self.snapshot("reference_a.md", 1, HEADER + BETA)


class TestRealLossIsReported(TruncationFixtureCase):
    """🔴 驗收 1（最重要）：注入一次**真的**整段消失 ⇒ 必須報紅。

    降噪改動之後仍抓得到真問題——這條紅不亮，後面三條全綠也沒有意義。
    """

    def test_injected_loss_is_reported_with_exact_lines(self):
        self.lose_beta()
        finding = self.findings()["reference_a.md"]
        self.assertEqual(finding.count, len(BETA))
        self.assertEqual(finding.lost, sorted(BETA))
        self.assertEqual(finding.moved, {}, "沒有別的檔有這些行，不該有任何扣除")
        self.assertEqual(finding.waived, {}, "baseline 不存在，不該有任何豁免")

    def test_cli_exits_one_and_names_the_file(self):
        self.lose_beta()
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reference_a.md 疑似整段消失 5 行", proc.stdout)

    def test_absent_baseline_says_it_silences_nothing(self):
        """baseline 不存在時的行為必須是「全部照常報」，而且要講出來。"""
        self.lose_beta()
        self.assertIn("不存在或無豁免 ⇒ 不靜音任何一筆", self.run_cli().stdout)

    def test_whole_file_deleted_content_is_still_reported(self):
        """極端版：整個檔只剩 frontmatter ⇒ 仍要報（不能被任何扣除吃掉）。"""
        self.memory_file("reference_a.md", [])
        self.snapshot("reference_a.md", 1, HEADER + BETA + GAMMA)
        self.assertEqual(self.findings()["reference_a.md"].count,
                         len(HEADER) + len(BETA) + len(GAMMA))


class TestCrossFileMoveIsNotALoss(TruncationFixtureCase):
    """驗收 2：逐字搬到別的 memory 檔 ⇒ 不報，且扣除本身要出現在輸出裡。"""

    def setUp(self):
        super().setUp()
        self.lose_beta()
        self.memory_file("reference_dest.md", HEADER + BETA)   # 搬過去了

    def test_moved_lines_are_deducted(self):
        finding = self.findings()["reference_a.md"]
        self.assertEqual(finding.count, 0)
        self.assertEqual(sorted(finding.moved), sorted(BETA))
        self.assertEqual(finding.moved[BETA[0]], ["reference_dest.md"])

    def test_cli_exits_zero_and_shows_what_it_deducted(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("跨檔逐字搬移，已扣除", proc.stdout)
        self.assertIn("reference_dest.md", proc.stdout)
        # 🔴 「乾淨」不可以單獨出現——它的前提（扣掉了 5 行）必須同框
        self.assertIn("已扣除：跨檔逐字搬移 5 行", proc.stdout)

    def test_deduction_self_heals_when_the_destination_also_loses_it(self):
        """扣除是每次重算的：目的檔哪天也弄丟了，同一批行必須立刻重新報。

        這是跨檔扣除與 baseline 靜音的關鍵差別，也是它不需要人來覆核的理由。
        """
        self.memory_file("reference_dest.md", HEADER)          # 目的檔也被吃掉
        self.assertEqual(self.findings()["reference_a.md"].count, len(BETA))


class TestMoveDeductionGuards(TruncationFixtureCase):
    """跨檔扣除的兩道辨識度門檻——每道都配敏感度／特異性對照。

    這兩道守的是一種**假扣除**：把樣板行當成搬移扣掉，會讓一筆真遺失的行數被稀釋到
    門檻以下而靜默放行。所以「不扣」和「該扣時真的會扣」兩個方向都要驗。
    """

    SHORT = ["- ok", "- no", "- hm"]
    BOILER = ["樣板行甲：每個合成檔都有一份的結構行",
              "樣板行乙：每個合成檔都有一份的結構行",
              "樣板行丙：每個合成檔都有一份的結構行"]

    def test_short_lines_elsewhere_do_not_count_as_a_move(self):
        self.memory_file("reference_a.md", HEADER)
        self.snapshot("reference_a.md", 1, HEADER + self.SHORT)
        self.memory_file("reference_b.md", HEADER + self.SHORT)
        finding = self.findings()["reference_a.md"]
        self.assertEqual(finding.count, len(self.SHORT), "短行命中不是搬移的證據")
        self.assertEqual(finding.moved, {})

    def test_boilerplate_in_many_files_does_not_count_as_a_move(self):
        self.memory_file("reference_a.md", HEADER)
        self.snapshot("reference_a.md", 1, HEADER + self.BOILER)
        for name in ("reference_b.md", "reference_c.md", "reference_d.md"):
            self.memory_file(name, HEADER + self.BOILER)       # 3 個宿主 > 上限 2
        finding = self.findings()["reference_a.md"]
        self.assertEqual(finding.count, len(self.BOILER))
        self.assertEqual(finding.moved, {})

    def test_same_lines_in_only_two_files_do_count_as_a_move(self):
        """特異性對照：同一批行、只換宿主數量，就必須被扣除。

        沒有這條，上面兩條可以靠「永遠不扣除」通過——那是把降噪整個關掉。
        """
        self.memory_file("reference_a.md", HEADER)
        self.snapshot("reference_a.md", 1, HEADER + self.BOILER)
        for name in ("reference_b.md", "reference_c.md"):
            self.memory_file(name, HEADER + self.BOILER)       # 2 個宿主 = 上限
        self.assertEqual(self.findings()["reference_a.md"].count, 0)


class TestBaselineWaiver(TruncationFixtureCase):
    """驗收 3：baseline 豁免必須只靜音「被判定過的那幾行」，不是整個檔。"""

    def setUp(self):
        super().setUp()
        # 快照有 Beta 也有 Gamma；現況先只掉了 Beta
        self.memory_file("reference_a.md", HEADER + GAMMA)
        self.snapshot("reference_a.md", 1, HEADER + BETA + GAMMA)

    def waive_beta(self):
        proc = self.waive_cli("reference_a.md", "section-rewrite")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_waived_lines_stop_being_reported(self):
        self.assertEqual(self.run_cli().returncode, 1)
        self.waive_beta()
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("baseline 已豁免：reference_a.md section-rewrite×5", proc.stdout)

    def test_new_loss_in_waived_file_is_reported_again(self):
        """🔴 本組最重要的一條：同一個檔發生**新的、不同的**消失 ⇒ 必須重新報。

        baseline 若綁檔名（或整檔靜音），這條會綠得很漂亮——而那正是把一次真實資料
        遺失永久遮蔽的形狀。
        """
        self.waive_beta()
        self.memory_file("reference_a.md", HEADER)             # Gamma 也不見了
        finding = self.findings()["reference_a.md"]
        self.assertEqual(finding.lost, sorted(GAMMA))
        self.assertEqual(finding.waived, {"section-rewrite": len(BETA)})
        for line in BETA:
            self.assertNotIn(line, finding.lost, "已判定過的行不該重複報")
        self.assertEqual(self.run_cli().returncode, 1)

    def test_waiver_is_scoped_to_one_file(self):
        """另一個檔掉了一模一樣的內容 ⇒ 不受這筆豁免影響。"""
        self.memory_file("reference_other.md", HEADER)
        self.snapshot("reference_other.md", 1, HEADER + BETA)
        self.waive_beta()
        self.assertEqual(self.findings()["reference_other.md"].lost, sorted(BETA))

    def test_waive_prints_every_line_before_writing(self):
        """check 只列前 6 行；--waive 不補印全部，就等於讓人對沒看過的內容按靜音。"""
        stdout = self.waive_beta().stdout
        for line in BETA:
            self.assertIn(line, stdout)

    def test_unknown_reason_writes_nothing(self):
        proc = self.waive_cli("reference_a.md", "看起來沒問題")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(self.baseline), "分類無效時不可留下半份 baseline")
        self.assertEqual(self.run_cli().returncode, 1, "原本的發現必須原封不動")

    def test_waive_without_reason_is_rejected(self):
        proc = self.run_cli("--waive", "reference_a.md")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(self.baseline))

    def test_waiving_a_file_with_nothing_pending_fails_loud(self):
        """寫不出東西時不能安靜地成功——否則「我豁免過了」會變成假記憶。"""
        proc = self.waive_cli("reference_不存在.md", "section-rewrite")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("沒有待判定的疑似消失行", proc.stdout)

    def test_malformed_baseline_line_is_ignored_and_announced(self):
        _write(self.baseline, "reference_a.md\tdeadbeef\n沒有分隔的一行\n")
        proc = self.run_cli()
        self.assertIn("格式或分類無效", proc.stdout)
        self.assertEqual(proc.returncode, 1, "壞掉的豁免不生效 ⇒ 方向是變吵不是變綠")


class TestBaselinePrivacy(TruncationFixtureCase):
    """隱私是機械要求，不是自律：baseline 只能有指紋，不能有原文或本機路徑。"""

    def setUp(self):
        super().setUp()
        self.lose_beta()
        proc = self.waive_cli("reference_a.md", "self-correction")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(self.baseline, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_no_memory_content_verbatim(self):
        for line in BETA:
            self.assertNotIn(line, self.text)

    def test_no_local_absolute_paths(self):
        self.assertNotIn(self.mem, self.text)
        self.assertNotIn(self.history, self.text)
        self.assertNotIn(self._tmp.name, self.text)

    def test_fingerprints_are_actually_there(self):
        """陽性對照：上面兩條在「baseline 是空的」時也會通過。"""
        for line in BETA:
            self.assertIn(mi.line_fingerprint(line), self.text)


class TestWorstSnapshotIsChosenByResidual(TruncationFixtureCase):
    """勝出快照要用**扣除後**的殘量挑，不是原始差集大小。

    否則一份「差異大但整份都被跨檔搬移解釋掉」的舊快照會贏過「殘留一筆真消失」的
    另一份快照，於是那筆真消失連出現的機會都沒有——沒有這條，那是一個零訊號假綠。
    """

    def test_snapshot_with_real_residual_wins_over_bigger_explained_one(self):
        moved = ["搬移行第 %d 條，內容夠長也夠獨特可以當搬移證據" % i for i in range(8)]
        self.memory_file("reference_a.md", HEADER)
        self.snapshot("reference_a.md", 1, HEADER + moved)   # 差 8 行，全被搬移解釋
        self.snapshot("reference_a.md", 2, HEADER + GAMMA)   # 差 4 行，真的不見了
        self.memory_file("reference_dest.md", HEADER + moved)
        finding = self.findings()["reference_a.md"]
        self.assertEqual(finding.lost, sorted(GAMMA))
        self.assertEqual(finding.raw, len(GAMMA))


class TestCleanFixtureIsQuiet(TruncationFixtureCase):
    """驗收 4：乾淨的 fixture 不誤報（單獨看沒有鑑別力，配上面三組才有意義）。"""

    def test_identical_snapshot_produces_no_finding(self):
        self.memory_file("reference_a.md", HEADER + BETA)
        self.snapshot("reference_a.md", 1, HEADER + BETA)
        self.assertEqual(self.findings(), {})
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("沒有 ≥3 行的整段消失", proc.stdout)

    def test_reordered_content_is_not_a_loss(self):
        self.memory_file("reference_a.md", HEADER + list(reversed(BETA)))
        self.snapshot("reference_a.md", 1, HEADER + BETA)
        self.assertEqual(self.findings(), {})

    def test_no_snapshots_at_all_says_it_did_not_check(self):
        self.memory_file("reference_a.md", HEADER)
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("這不等於「沒有遺失」，是**沒有檢查**", proc.stdout)


class TestMemoryDirFlagRetargets(TruncationFixtureCase):
    """`--memory-dir` 必須真的換掉**被檢查的目錄**，而不只是換掉一個路徑字串。

    比照 `TestCli::test_repo_root_flag_actually_changes_the_base` 的否定對照寫法：
    同一條命令，只換 `--memory-dir`，結論就必須跟著換——而且要換到**點得出名字**
    的程度（不同的檔名），不是只換 exit code。只驗 exit code 的話，「flag 被靜默
    忽略、兩次都跑同一個目錄」在某些 fixture 下也會剛好給出不同結果。
    """

    def setUp(self):
        super().setUp()
        # 第二組完全獨立的 memory + file-history，代表「另一個語料庫」
        self.mem2 = os.path.join(self._tmp.name, "memory2")
        self.history2 = os.path.join(self._tmp.name, "file-history2")
        os.makedirs(self.mem2)
        os.makedirs(self.history2)
        self.lose_beta()                                   # 語料 1：reference_a.md 掉了 Beta
        _write(os.path.join(self.mem2, "reference_b.md"),
               self._doc("reference_b.md", HEADER))
        _write(os.path.join(self.history2, "session-1", "reference_b@v1"),
               self._doc("reference_b.md", HEADER + GAMMA))  # 語料 2：reference_b.md 掉了 Gamma

    def _run(self, mem, history):
        return subprocess.run(
            [sys.executable, str(_GATE_PATH), "--truncation",
             "--memory-dir", mem, "--file-history", history, "--baseline", self.baseline],
            capture_output=True, text=True)

    def test_flag_selects_which_corpus_is_checked(self):
        first = self._run(self.mem, self.history)
        second = self._run(self.mem2, self.history2)
        self.assertEqual(first.returncode, 1, first.stdout)
        self.assertEqual(second.returncode, 1, second.stdout)
        # 點得出名字：兩次報的是不同的檔，證明目標目錄真的換了
        self.assertIn("reference_a.md 疑似整段消失", first.stdout)
        self.assertNotIn("reference_b.md", first.stdout)
        self.assertIn("reference_b.md 疑似整段消失", second.stdout)
        self.assertNotIn("reference_a.md", second.stdout)

    def test_baseline_defaults_to_the_sibling_of_whichever_dir_was_given(self):
        """baseline 預設位置必須跟著 `--memory-dir` 走，不是跟著本機 HOME 走。

        這條同時是隱私守衛：baseline 若沒跟著走，測試就會寫進**真實**的
        `~/.claude/projects/<slug>/` 底下。
        """
        expected = os.path.join(os.path.dirname(self.mem2),
                                mi.BASELINE_FILENAME)
        self.assertEqual(mi.baseline_path(self.mem2), expected)
        proc = subprocess.run(
            [sys.executable, str(_GATE_PATH), "--memory-dir", self.mem2,
             "--file-history", self.history2,          # 刻意不帶 --baseline
             "--waive", "reference_b.md", "--reason", "section-rewrite"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.isfile(expected), "baseline 沒有落在 --memory-dir 的兄弟位置")
        self.assertTrue(expected.startswith(self._tmp.name),
                        "baseline 落到 fixture 之外＝可能寫進真實 memory 目錄旁")


class TestMissingMemoryDirStaysFailClosed(TruncationFixtureCase):
    """fail-closed 不可以因為新增提示訊息而退化成「印個提示然後繼續跑」。"""

    MODES = (["--links"], ["--truncation"], ["--detail", "x.md"],
             ["--waive", "x.md", "--reason", "section-rewrite"])

    def test_every_mode_exits_one_and_checks_nothing(self):
        missing = os.path.join(self._tmp.name, "nope")
        for mode in self.MODES:
            with self.subTest(mode=" ".join(mode)):
                proc = subprocess.run(
                    [sys.executable, str(_GATE_PATH), *mode, "--memory-dir", missing],
                    capture_output=True, text=True)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("找不到 memory 目錄", proc.stdout)
                # 沒有任何一個 check 的表頭 ⇒ 真的什麼都沒跑，不是印完提示照跑
                self.assertNotIn("[silent-truncation]", proc.stdout)
                self.assertNotIn("[index-links]", proc.stdout)

    def test_explicit_path_does_not_get_the_worktree_lecture(self):
        """使用者自己給了路徑時，講推導機制只會把人帶去查不存在的問題。"""
        proc = subprocess.run(
            [sys.executable, str(_GATE_PATH), "--truncation",
             "--memory-dir", os.path.join(self._tmp.name, "nope")],
            capture_output=True, text=True)
        self.assertIn("--memory-dir 指定的路徑", proc.stdout)
        self.assertNotIn("worktree", proc.stdout)


class TestDerivedPathFailureIsActionable(unittest.TestCase):
    """推導出來的目錄不存在時，訊息必須帶**可照抄的**修正指令。

    這條守的是 2026-08-12 的實遇：有人在 worktree 撞到「找不到 memory 目錄」，
    據此結論「這支工具沒有 --memory-dir」並開單要求新增一個早就存在的 flag。
    警告當時就寫在檔頭 docstring 裡——所以判準不是「有沒有寫」，是「撞牆的人
    在終端上看不看得到」。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 造一棵假 worktree：<tmp>/wt/scripts/audit/<gate>，讓 repo_root() 推導到 <tmp>/wt
        self.tree = os.path.join(self._tmp.name, "wt")
        self.copy = os.path.join(self.tree, "scripts", "audit", _GATE_PATH.name)
        os.makedirs(os.path.dirname(self.copy))
        shutil.copy(str(_GATE_PATH), self.copy)

    def run_bare(self):
        """不帶 --memory-dir ⇒ 走推導路徑（那個 slug 目錄必然不存在）。"""
        return subprocess.run([sys.executable, self.copy, "--truncation"],
                              capture_output=True, text=True)

    def test_worktree_is_detected_and_main_checkout_is_filled_in(self):
        _write(os.path.join(self.tree, ".git"),
               "gitdir: /somewhere/main-repo/.git/worktrees/wt\n")
        proc = self.run_bare()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("--memory-dir", proc.stdout, "沒告訴人有這個 flag")
        self.assertIn("主 checkout ＝ /somewhere/main-repo", proc.stdout)
        # 可照抄＝路徑已經幫你算好填進去了，不是叫你自己推 slug
        self.assertIn(mi.memory_dir("/somewhere/main-repo"), proc.stdout)

    def test_says_repo_root_will_not_fix_it(self):
        """開單者試了三種 --repo-root 組合——訊息要主動擋掉這條岔路。"""
        self.assertIn("--repo-root", self.run_bare().stdout)

    def test_non_worktree_still_gets_the_flag_and_an_example(self):
        os.makedirs(os.path.join(self.tree, ".git"))     # 一般 clone：.git 是目錄
        proc = self.run_bare()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--memory-dir", proc.stdout)
        self.assertNotIn("主 checkout ＝", proc.stdout)

    def test_submodule_gitdir_is_not_mistaken_for_a_worktree(self):
        _write(os.path.join(self.tree, ".git"), "gitdir: ../.git/modules/foo\n")
        self.assertIsNone(mi.main_checkout(self.tree))

    def test_main_checkout_returns_none_without_any_git_marker(self):
        self.assertIsNone(mi.main_checkout(self.tree))


class TestArgvIsStrict(TruncationFixtureCase):
    """打錯的旗標必須炸掉，不可以安靜地跑成「另一個問題的答案」。"""

    def test_unknown_flag_is_rejected(self):
        proc = self.run_cli("--detial", "reference_a.md")     # 手滑打錯 --detail
        self.assertEqual(proc.returncode, 1)
        self.assertIn("不認得的參數", proc.stdout)

    def test_flag_missing_its_value_is_rejected(self):
        proc = subprocess.run(
            [sys.executable, str(_GATE_PATH), "--truncation", "--memory-dir"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("少了值", proc.stdout)

    def test_all_documented_flags_are_accepted(self):
        """陽性對照：嚴格化不可以把正常用法一起擋掉。"""
        self.memory_file("reference_a.md", HEADER)
        self.snapshot("reference_a.md", 1, HEADER)
        proc = self.run_cli("--threshold", "3")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class TestDetailMode(TruncationFixtureCase):
    def test_detail_prints_all_pending_lines(self):
        self.lose_beta()
        proc = self.run_cli("--detail", "reference_a.md")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for line in BETA:
            self.assertIn(line, proc.stdout)

    def test_detail_on_a_clean_file_fails_loud(self):
        self.memory_file("reference_a.md", HEADER)
        self.snapshot("reference_a.md", 1, HEADER)
        self.assertEqual(self.run_cli("--detail", "reference_a.md").returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

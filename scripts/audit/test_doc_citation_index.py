#!/usr/bin/env python3
"""doc_citation_index 與 pattern-guard 引用路徑的測試（#585）。

守的是兩條路徑本身，不是某個數字：
- 檔案**有**逐字引用 → hook 只推那幾份（收斂，本單省 token 的來源）
- 檔案**沒有**引用 → 退回既有的 class 推播（`skill.py` 這類檔的唯一訊號來源）

⚠ 覆蓋邊界：只驗「推了哪幾份」，**不驗 doc 內容是否正確**（#585 §四.2：機制只能
把 doc 推到人面前，判斷仍然是人的）。把本檔綠讀成「被推的 doc 都是對的」，就是
#547 的原型錯誤換一個載體。
"""
import json
import os
import subprocess
import sys
import time
import uuid
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
HOOK = os.path.join(REPO, ".claude", "hooks", "pattern-guard.py")

sys.path.insert(0, HERE)
import doc_citation_index as dci  # noqa: E402


# --------------------------------------------------------------------- #
# 1. 引用形態抽取——五種形態各一條，外加陰性對照                          #
# --------------------------------------------------------------------- #
class TestCitationForms(unittest.TestCase):
    SLUGS = [
        "two-stage-remote-provision-idempotency",
        "cloud-service-unlink-override-cleanup",
        "capability-gate-single-source-field",
        "credential-claim-test-fixture-coupling",
    ]

    def _cited(self, text):
        return set(dci.cited_docs(text, self.SLUGS))

    def test_form1_bare_slug(self):
        """形態 1：docstring／註解裡直接寫 slug（repo 內 54 命中，最大宗）。"""
        text = "    # 見 capability-gate-single-source-field：判準收斂到後端單一欄位\n"
        self.assertEqual(self._cited(text), {"capability-gate-single-source-field"})

    def test_form2_full_path(self):
        """形態 2：註解寫完整路徑 docs/decisions/patterns/xxx.md（30 命中）。"""
        text = "    #   docs/decisions/patterns/cloud-service-unlink-override-cleanup.md\n"
        self.assertEqual(self._cited(text), {"cloud-service-unlink-override-cleanup"})

    def test_form4_wiki_link(self):
        """形態 4：[[wiki-link]]（21 命中）。"""
        text = "  - [[cloud-service-unlink-override-cleanup]] pattern (filter 擴維度)\n"
        self.assertEqual(self._cited(text), {"cloud-service-unlink-override-cleanup"})

    def test_form5_hyphen_wrapped_across_lines(self):
        """形態 5：slug 在連字號處換行續行——**單行 grep 結構上抓不到**。

        repo 內實證 4 處，其中兩處正是 #585 §一 核心案例的那份 doc：
            # of the same teardown contract ([[two-stage-remote-
            # provision-idempotency]]: every failure path keeps ...
        """
        text = (
            "            # of the same True/False teardown contract ([[two-stage-remote-\n"
            "            # provision-idempotency]]: every failure path keeps the stored id\n"
        )
        self.assertEqual(self._cited(text), {"two-stage-remote-provision-idempotency"})

    def test_form5_hyphen_wrapped_without_comment_prefix(self):
        """同形態、續行沒有註解前綴（test_workspace_cascade_cleanup.py 的實際樣子）。"""
        text = (
            "根因 pitfall: cf-ns-orphan-no-account-gc。precedent: cloud-service-unlink-\n"
            "override-cleanup（DB cascade 繞過 Python unlink）。\n"
        )
        self.assertEqual(self._cited(text), {"cloud-service-unlink-override-cleanup"})

    def test_negative_control_unrelated_text(self):
        """陰性對照：沒有任何 slug 的文字必須回空——否則上面每一條都可能是假綠。"""
        text = "def foo():\n    return 42  # 完全無關的註解\n"
        self.assertEqual(self._cited(text), set())

    def test_negative_control_similar_but_not_a_slug(self):
        """長得像但不是 slug 的字串不得命中（zzz 陰性對照的行內版）。"""
        text = "# zzz-not-a-real-slug-qqq 與 two-stage-remote-provision 都不算\n"
        self.assertEqual(self._cited(text), set())

    def test_joining_does_not_fabricate_across_unrelated_lines(self):
        """續行接合不得無中生有：兩行各自無關、接起來也不該湊出 slug。"""
        text = "# 這行結尾提到 capability\n# gate-single-source-field 是另一段的開頭\n"
        # 接合後會變成 "capabilitygate-single-source-field"，不含完整 slug
        self.assertEqual(self._cited(text), set())


# --------------------------------------------------------------------- #
# 2. slug 清單                                                           #
# --------------------------------------------------------------------- #
class TestSlugDiscovery(unittest.TestCase):
    def test_excludes_readme(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "docs", "decisions", "patterns")
            os.makedirs(d)
            for n in ("README.md", "alpha-pattern.md", "beta-pattern.md", "notes.txt"):
                open(os.path.join(d, n), "w").close()
            self.assertEqual(sorted(dci.pattern_doc_slugs(root)),
                             ["alpha-pattern", "beta-pattern"])

    def test_missing_dir_returns_empty(self):
        """docs 目錄不存在（sparse checkout／別的 repo）→ 空清單，不炸。"""
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(dci.pattern_doc_slugs(root), [])


# --------------------------------------------------------------------- #
# 3. hook 的兩條路徑——本單拍板形狀的守線                                  #
# --------------------------------------------------------------------- #
class TestHookRouting(unittest.TestCase):
    """建一個最小 repo fixture，直接跑真的 hook 程序（不 mock）。

    ⚠ session id 必須每次執行都不同：hook 的 marker 寫在 `tempfile.gettempdir()`，
    固定字串會讓第二次跑整個測試檔時 marker 已存在 → hook 靜默放行 → 斷言「必須出聲」
    的那幾支全部假紅。這是實作 GREEN 之後才撞到的，記在這裡免得有人改回固定值。
    """

    def _sid(self, tag):
        return "%s-%s" % (tag, uuid.uuid4().hex[:12])

    def _fixture(self, root, rel, content):
        d = os.path.join(root, "docs", "decisions", "patterns")
        os.makedirs(d, exist_ok=True)
        for slug in ("alpha-pattern", "beta-pattern", "gamma-pattern"):
            open(os.path.join(d, slug + ".md"), "w").close()
        hooks = os.path.join(root, ".claude", "hooks")
        os.makedirs(hooks, exist_ok=True)
        with open(os.path.join(hooks, "pattern-guard-map.json"), "w", encoding="utf-8") as f:
            json.dump({"classes": [{
                "class": "model-write-guard",
                "pattern": r"^src/models/.*\.py$",
                "why": "測試用類別",
                "docs": [
                    "docs/decisions/patterns/alpha-pattern.md — A",
                    "docs/decisions/patterns/beta-pattern.md — B",
                    "docs/decisions/patterns/gamma-pattern.md — G",
                ],
            }]}, f)
        # 真的 hook 與索引模組（複製，讓 fixture 自足）
        import shutil
        shutil.copy(HOOK, os.path.join(hooks, "pattern-guard.py"))
        sd = os.path.join(root, "scripts", "audit")
        os.makedirs(sd, exist_ok=True)
        shutil.copy(os.path.join(HERE, "doc_citation_index.py"),
                    os.path.join(sd, "doc_citation_index.py"))
        target = os.path.join(root, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return os.path.join(hooks, "pattern-guard.py"), target

    def _run(self, root, hook, target, session, env_overrides=None):
        payload = {"session_id": session, "cwd": root,
                   "tool_input": {"file_path": target}}
        env = dict(os.environ, CLAUDE_PROJECT_DIR=root)
        env.pop("CLAUDE_PATTERN_GUARD", None)
        if env_overrides:
            env.update(env_overrides)
        r = subprocess.run([sys.executable, hook], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=10)
        out = r.stdout.strip()
        return (json.loads(out) if out else None), r.stderr

    def _git(self, cwd, *args):
        return subprocess.run(
            ["git", "-C", cwd, *args], check=True, capture_output=True, text=True)

    def _linked_worktree_fixture(self, outer):
        """Create one repo with main, nested, and external linked worktrees."""
        root = os.path.join(outer, "main")
        os.makedirs(root)
        hook, target = self._fixture(
            root, "src/models/thing.py",
            "# 見 docs/decisions/patterns/beta-pattern.md\n")
        helper = os.path.join(root, "src", "utils", "helper.py")
        os.makedirs(os.path.dirname(helper), exist_ok=True)
        with open(helper, "w", encoding="utf-8") as f:
            f.write("# 不在守護路徑\n")
        self._git(root, "init")
        self._git(root, "config", "user.email", "pattern-guard@example.invalid")
        self._git(root, "config", "user.name", "Pattern Guard Test")
        self._git(root, "add", ".")
        self._git(root, "commit", "-m", "fixture")

        nested = os.path.join(root, ".claude", "worktrees", "nested")
        external = os.path.join(outer, "external")
        self._git(root, "worktree", "add", "-b", "nested-test", nested)
        self._git(root, "worktree", "add", "-b", "external-test", external)
        return root, hook, target, nested, external

    def test_cited_file_pushes_only_cited_docs(self):
        """有逐字引用 → 只推那幾份，不推該 class 的其他 doc。"""
        with tempfile.TemporaryDirectory() as root:
            hook, target = self._fixture(
                root, "src/models/thing.py",
                "# 見 docs/decisions/patterns/beta-pattern.md 的判準\n")
            res, _ = self._run(root, hook, target, self._sid("cited"))
            self.assertIsNotNone(res, "有引用時 hook 必須出聲")
            reason = res["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("beta-pattern", reason)
            self.assertNotIn("alpha-pattern", reason,
                             "引用索引命中時不得把整個 class 的 doc 全推出來——"
                             "那正是本單要省掉的閱讀量")
            self.assertNotIn("gamma-pattern", reason)

    def test_uncited_file_falls_back_to_class_push(self):
        """沒有任何引用 → 退回 class 推播（skill.py 這類檔的唯一訊號來源）。"""
        with tempfile.TemporaryDirectory() as root:
            hook, target = self._fixture(
                root, "src/models/thing.py", "# 完全沒引用任何 pattern doc\n")
            res, _ = self._run(root, hook, target, self._sid("uncited"))
            self.assertIsNotNone(res, "無引用時必須退回 class 推播，不得靜默放行")
            reason = res["hookSpecificOutput"]["permissionDecisionReason"]
            for slug in ("alpha-pattern", "beta-pattern", "gamma-pattern"):
                self.assertIn(slug, reason,
                              "退回路徑必須維持原本的整組 class 推播")

    def test_blind_spot_is_announced_on_cited_path(self):
        """收斂時必須明講盲區（#585 §四.1：不得靜默略過未被引用的 doc）。"""
        with tempfile.TemporaryDirectory() as root:
            hook, target = self._fixture(
                root, "src/models/thing.py",
                "# 見 docs/decisions/patterns/beta-pattern.md\n")
            res, _ = self._run(root, hook, target, self._sid("blind"))
            reason = res["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("未被任何", reason,
                          "收斂訊息要說明『沒被引用的 doc 不會出現在這裡』")

    def test_second_edit_same_session_is_silent(self):
        """同 session 重試同一編輯要放行（既有 read-first 語意不變）。"""
        with tempfile.TemporaryDirectory() as root:
            hook, target = self._fixture(
                root, "src/models/thing.py",
                "# 見 docs/decisions/patterns/beta-pattern.md\n")
            sid = self._sid("twice")
            self._run(root, hook, target, sid)
            res, _ = self._run(root, hook, target, sid)
            self.assertIsNone(res, "第二次應放行，否則永遠編輯不了")

    def test_unguarded_path_stays_silent(self):
        """不在守護路徑上的檔即使有引用也不出聲（不擴大 hook 管轄範圍）。"""
        with tempfile.TemporaryDirectory() as root:
            hook, target = self._fixture(
                root, "src/utils/helper.py",
                "# 見 docs/decisions/patterns/beta-pattern.md\n")
            res, _ = self._run(root, hook, target, self._sid("unguarded"))
            self.assertIsNone(res)

    def test_index_failure_signals_instead_of_silent_fallback(self):
        """🔴 新路徑壞掉要有訊號——不沿用既有四種 fail-open 無訊號的習慣。"""
        with tempfile.TemporaryDirectory() as root:
            hook, target = self._fixture(
                root, "src/models/thing.py",
                "# 見 docs/decisions/patterns/beta-pattern.md\n")
            # 把索引模組弄壞
            with open(os.path.join(root, "scripts", "audit", "doc_citation_index.py"),
                      "w", encoding="utf-8") as f:
                f.write("this is not valid python (((\n")
            res, _ = self._run(root, hook, target, self._sid("broken"))
            self.assertIsNotNone(res, "索引壞掉時仍要退回 class 推播，不得靜默放行")
            reason = res["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("引用索引", reason, "必須明講引用索引失效了")
            self.assertIn("alpha-pattern", reason, "且要退回完整 class 推播")

    def test_guarded_paths_in_main_nested_and_external_worktrees(self):
        """所有 linked worktree 都要用各自 repo-relative path 命中守衛。"""
        with tempfile.TemporaryDirectory() as outer:
            root, hook, main_target, nested, external = \
                self._linked_worktree_fixture(outer)
            targets = (
                main_target,
                os.path.join(nested, "src", "models", "thing.py"),
                os.path.join(external, "src", "models", "thing.py"),
            )
            for index, target in enumerate(targets):
                res, stderr = self._run(
                    root, hook, target, self._sid("guarded-%d" % index))
                self.assertEqual(stderr, "")
                self.assertIsNotNone(res, target)
                reason = res["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("src/models/thing.py", reason)

    def test_unguarded_paths_in_main_nested_and_external_worktrees(self):
        """Worktree identity resolution 不得擴張 guard-map 的匹配面。"""
        with tempfile.TemporaryDirectory() as outer:
            root, hook, _, nested, external = self._linked_worktree_fixture(outer)
            for index, checkout in enumerate((root, nested, external)):
                target = os.path.join(checkout, "src", "utils", "helper.py")
                res, stderr = self._run(
                    root, hook, target, self._sid("unguarded-%d" % index))
                self.assertEqual(stderr, "")
                self.assertIsNone(res, target)

    def test_external_worktrees_keep_per_file_citation_markers(self):
        """同一 rel 的不同 worktree 檔案不得共用 citation marker。"""
        with tempfile.TemporaryDirectory() as outer:
            root, hook, _, nested, external = self._linked_worktree_fixture(outer)
            sid = self._sid("marker")
            nested_target = os.path.join(nested, "src", "models", "thing.py")
            external_target = os.path.join(external, "src", "models", "thing.py")
            first, _ = self._run(root, hook, nested_target, sid)
            second, _ = self._run(root, hook, external_target, sid)
            retry, _ = self._run(root, hook, external_target, sid)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second, "不同 worktree 的同 rel 檔案仍須首次提醒")
            self.assertIsNone(retry, "同一 worktree 同一檔案重試才應放行")

    def test_unrelated_git_checkout_is_not_guarded(self):
        """外部路徑必須共享 common git dir，不能只因 rel 長得一樣就命中。"""
        with tempfile.TemporaryDirectory() as outer:
            root, hook, _, _, _ = self._linked_worktree_fixture(outer)
            unrelated = os.path.join(outer, "unrelated")
            _, target = self._fixture(
                unrelated, "src/models/thing.py", "# unrelated repo\n")
            self._git(unrelated, "init")
            res, stderr = self._run(root, hook, target, self._sid("unrelated"))
            self.assertEqual(stderr, "")
            self.assertIsNone(res)

    def test_git_unavailable_keeps_main_fallback_but_not_external_paths(self):
        """找不到 Git 時保留主 checkout 守衛，外部路徑則 fail-open。"""
        with tempfile.TemporaryDirectory() as outer:
            root, hook, main_target, _, external = self._linked_worktree_fixture(outer)
            no_git = os.path.join(outer, "no-git")
            os.makedirs(no_git)
            env = {"PATH": no_git}
            main_res, _ = self._run(
                root, hook, main_target, self._sid("no-git-main"), env)
            external_res, _ = self._run(
                root, hook, os.path.join(external, "src", "models", "thing.py"),
                self._sid("no-git-external"), env)
            self.assertIsNotNone(main_res)
            self.assertIsNone(external_res)

    def test_git_timeout_is_bounded_and_does_not_trust_external_path(self):
        """卡住的 Git 必須在 bounded timeout 後 fail-open，且不可誤攔外部路徑。"""
        with tempfile.TemporaryDirectory() as outer:
            root, hook, _, _, external = self._linked_worktree_fixture(outer)
            fake_bin = os.path.join(outer, "fake-bin")
            os.makedirs(fake_bin)
            fake_git = os.path.join(fake_bin, "git")
            with open(fake_git, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nexec /bin/sleep 5\n")
            os.chmod(fake_git, 0o755)
            target = os.path.join(external, "src", "models", "thing.py")
            started = time.monotonic()
            res, stderr = self._run(
                root, hook, target, self._sid("git-timeout"), {"PATH": fake_bin})
            elapsed = time.monotonic() - started
            self.assertEqual(stderr, "")
            self.assertIsNone(res)
            self.assertLess(elapsed, 3.5, "Git timeout 必須有界，不能卡住 hook")


if __name__ == "__main__":
    unittest.main(verbosity=2)

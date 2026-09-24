#!/usr/bin/env python3
"""manifest 版號 gate 的自測（#538 建立，#691 改制）。

🔴 **這支測試本身就是 gate 的注入式驗證**，不是附屬品。理由（#538 驗收段）：
一道從沒被看過會失敗的檢查，與一道什麼都沒檢查的檢查，輸出長得一模一樣。
所以 CI 每次跑 gate 之前都先跑這支——把「驗過一次」升級成「持續被驗證」。

覆蓋三層：

1. **純比較器**（不碰 git）：數值 tuple 比較，含**跨百位**兩個方向。
   只驗「同值」那組的話，用字串比較的錯誤實作會照樣全綠通過
   （'18.0.1.0.100' > '18.0.1.0.99' 在字串比較下是 False）。
2. **三條規則的裁決**（#691）：一般 PR 不得動版號／帶 migration 才 bump 且目錄要對上／
   release PR 放行。每一格都有「不該出現的結果」那一側，不是只驗通過。
3. **端到端**（真 git repo）：三點 diff（`base...HEAD`）取的是 merge-base 之後
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
# 2. 範圍判準：只管 src/，且目錄邊界不是字串前綴
#
# ⚠ #691 起**沒有 tests-only 豁免**了。舊制的豁免存在的理由是「免除 bump 義務」，
#   而 #691 之後根本沒有 bump 義務可免；剩下的義務是「不得擅自 bump」與「別落後
#   develop」，純測試 PR 沒有比別人多的權利。守線（universal 而非 existence）隨著
#   它守的那條義務一起消失——留著一個沒有指涉對象、卻長得像守線的分支比刪掉更糟。
# --------------------------------------------------------------------------
class TestBumpScope(unittest.TestCase):
    def test_no_src_change_is_out_of_scope(self):
        # extra/paas-operator 是另一條獨立發版線；docs/.gitea/scripts 更無關。
        req = gate.classify_scope(
            [
                "docs/index.md",
                ".gitea/workflows/manifest-version-gate.yml",
                "scripts/ci/manifest-version-gate.py",
                "extra/paas-operator/src/main.py",
            ]
        )
        self.assertFalse(req.in_scope)
        self.assertEqual(req.reason, "out-of-scope")

    def test_tests_only_is_still_in_scope(self):
        # #691：純測試 PR 不再有豁免，一樣要遵守「版號 == develop」。
        req = gate.classify_scope(
            ["src/tests/test_a.py", "src/tests/sub/test_b.py", "docs/x.md"]
        )
        self.assertTrue(req.in_scope)
        self.assertEqual(req.reason, "in-scope")
        self.assertEqual(
            req.src_paths, ["src/tests/sub/test_b.py", "src/tests/test_a.py"]
        )

    def test_src_prefix_is_directory_boundary_not_string_prefix(self):
        # srcfoo/bar.py 不在 src/ 底下——少了目錄邊界，範圍會被檔名拼字繞過。
        req = gate.classify_scope(["srcfoo/bar.py"])
        self.assertFalse(req.in_scope)
        self.assertEqual(req.reason, "out-of-scope")

    def test_src_itself_counts_as_in_scope(self):
        req = gate.classify_scope(["src"])
        self.assertTrue(req.in_scope)

    def test_empty_changeset_is_out_of_scope(self):
        req = gate.classify_scope([])
        self.assertFalse(req.in_scope)


# --------------------------------------------------------------------------
# 3. 本 PR 自己動到的 migration 目錄（規則 B 的輸入）
#
# 🔴 這一格的輸入**必須**是 merge-base 三點 diff 的 changed，不是工作目錄整份 listing。
#    整份 listing 含 41 個既有目錄，拿它當規則 B 的輸入 ⇒ 任何 PR 都「帶 migration」
#    ⇒ 規則 B 恆真、規則 A 的紅燈永遠不會出現（假綠，且輸出與真通過一模一樣）。
#    `evaluate_migrations`（目錄 > manifest ⇒ 紅）才是吃整份 listing 的那一支。
# --------------------------------------------------------------------------
class TestPrMigrationDirs(unittest.TestCase):
    def test_picks_dir_segment_from_changed_paths(self):
        self.assertEqual(
            gate.collect_pr_migration_dirs(
                [
                    "src/migrations/18.0.1.0.176/post-migrate.py",
                    "src/migrations/18.0.1.0.176/pre-migrate.py",
                    "src/models/foo.py",
                ]
            ),
            ["18.0.1.0.176"],
        )

    def test_multiple_dirs_are_all_reported(self):
        self.assertEqual(
            gate.collect_pr_migration_dirs(
                [
                    "src/migrations/18.0.1.0.176/post-migrate.py",
                    "src/migrations/18.0.1.0.122/post-migrate.py",
                ]
            ),
            ["18.0.1.0.122", "18.0.1.0.176"],
        )

    def test_file_directly_under_migrations_is_not_a_dir(self):
        # src/migrations/README.md 沒有目錄段可取，不得被當成一個 migration 目錄。
        self.assertEqual(gate.collect_pr_migration_dirs(["src/migrations/README.md"]), [])

    def test_directory_boundary_not_string_prefix(self):
        # src/migrations_old/... 不在 src/migrations/ 底下。
        self.assertEqual(
            gate.collect_pr_migration_dirs(["src/migrations_old/18.0.1.0.9/x.py"]), []
        )

    def test_no_migration_change_is_empty(self):
        self.assertEqual(gate.collect_pr_migration_dirs(["src/models/foo.py"]), [])


# --------------------------------------------------------------------------
# 4. release 訊號：分支 `release/*`，且**收不到來源時一律當非 release**
#
# 為什麼是分支而不是標題前綴：本 repo 最近 20/20 張 PR 標題都被冠上
# 「⛔ 勿代 merge｜待 Eugene 覆核 — 」前綴 ⇒ `title.startswith("release:")`
# 對真正的 release PR 是**假陰性**；改成「標題含 release:」又讓任何人打幾個字
# 就能繞過規則 A。分支名由 /woow:deploy 產生、不被裝飾，是唯一結構性的訊號。
# --------------------------------------------------------------------------
class TestReleaseSignal(unittest.TestCase):
    def test_release_branch_is_a_release_pr(self):
        sig = gate.detect_release_signal(head_ref="release/18.0.1.0.176")
        self.assertTrue(sig.is_release)
        self.assertEqual(sig.branch, "release/18.0.1.0.176")

    def test_refs_heads_prefix_is_stripped(self):
        sig = gate.detect_release_signal(head_ref="refs/heads/release/18.0.1.0.176")
        self.assertTrue(sig.is_release)
        self.assertEqual(sig.branch, "release/18.0.1.0.176")

    def test_ordinary_branch_is_not_a_release_pr(self):
        sig = gate.detect_release_signal(head_ref="fix/691-manifest-version-rule")
        self.assertFalse(sig.is_release)
        self.assertEqual(sig.branch, "fix/691-manifest-version-rule")

    def test_release_without_slash_is_not_a_release_pr(self):
        # 目錄邊界：`release-notes` / `released` 不得被當成 release 分支。
        for name in ("release-notes", "released", "release"):
            with self.subTest(name=name):
                self.assertFalse(gate.detect_release_signal(head_ref=name).is_release)

    def test_pull_ref_is_not_a_branch_name(self):
        # 🔴 tea api 的 pulls listing 回的是 refs/pull/N/head；event payload 給什麼要實測。
        # 收到這種值時**不是**「非 release 分支」，是「訊號來源不可用」——兩者必須分辨得出來。
        sig = gate.detect_release_signal(head_ref="refs/pull/816/head")
        self.assertFalse(sig.is_release)
        self.assertIsNone(sig.branch)
        self.assertFalse(sig.usable)

    def test_missing_input_degrades_to_non_release_and_says_so(self):
        sig = gate.detect_release_signal(head_ref=None, env_head_ref=None)
        self.assertFalse(sig.is_release)
        self.assertFalse(sig.usable)
        self.assertIn("未收到", sig.detail)

    def test_env_head_ref_is_the_fallback_source(self):
        sig = gate.detect_release_signal(head_ref="", env_head_ref="release/18.0.1.0.176")
        self.assertTrue(sig.is_release)
        self.assertEqual(sig.source, "GITHUB_HEAD_REF")

    def test_raw_inputs_are_echoed_back(self):
        # 🔴 傳參壞掉時的輸出必須與「這不是 release PR」可區分，否則兩者不可辨。
        sig = gate.detect_release_signal(head_ref="refs/pull/816/head", env_head_ref="")
        self.assertEqual(sig.raw_inputs["--head-ref"], "refs/pull/816/head")
        self.assertEqual(sig.raw_inputs["GITHUB_HEAD_REF"], "")


# --------------------------------------------------------------------------
# 3b. 🔴 「本 PR **新增**的」migration 目錄＝動到的 − base 側已存在的
#
# 規則 A／B 判「這是不是 migration PR」用的是這個集合。用「動到的」會把「修既有
# migration 的 bug」整類判紅，而其中一種（`18.0.1.0.37/`，本 repo 實際發生過）**無解**：
# 沒有辦法把既有目錄改名成新版號。
# --------------------------------------------------------------------------
class TestNewMigrationDirs(unittest.TestCase):
    def test_base_side_dirs_are_subtracted(self):
        self.assertEqual(
            gate.select_new_migration_dirs(
                ["18.0.1.0.37", "18.0.1.0.176"], ["18.0.1.0.37", "18.0.1.0.39"]
            ),
            ["18.0.1.0.176"],
        )

    def test_everything_existing_leaves_nothing_new(self):
        self.assertEqual(
            gate.select_new_migration_dirs(["18.0.1.0.37"], ["18.0.1.0.37"]), []
        )

    def test_missing_base_list_degrades_to_treating_all_as_new(self):
        """降級方向必須是 fail-closed：多紅不會多綠。"""
        self.assertEqual(
            gate.select_new_migration_dirs(["18.0.1.0.37"], None), ["18.0.1.0.37"]
        )

    def test_dirs_odoo_never_runs_are_not_a_migration(self):
        """`tests/` 與不符 VERSION_RE 的目錄 Odoo 根本不看 ⇒ 新增它們不產生 bump 義務。"""
        self.assertEqual(
            gate.select_new_migration_dirs(["tests", "README", "18.0.1.0.176"], []),
            ["18.0.1.0.176"],
        )

    def test_zero_version_counts_as_a_migration(self):
        """`0.0.0` 是「任何版本變更都會跑」——沒有版本變更就永遠不跑，所以它算。"""
        self.assertEqual(gate.select_new_migration_dirs(["0.0.0"], []), ["0.0.0"])

    def test_is_odoo_migration_dir_matches_evaluate_migrations_ignores(self):
        """口徑只有一份：這裡認得的，evaluate_migrations 就不會列進 ignored。"""
        names = ["18.0.1.0.176", "1.0.176", "0.0.0", "tests", "README", "18.0.1.0.176x"]
        v = gate.evaluate_migrations("18.0.1.0.200", names)
        for n in names:
            with self.subTest(n=n):
                self.assertEqual(
                    gate.is_odoo_migration_dir(n), n not in v.ignored_dirs
                )


# --------------------------------------------------------------------------
# 5. 🔴 #691 三條規則的裁決（本檔核心）
# --------------------------------------------------------------------------
_SRC_CHANGE = ["src/models/cloud_service.py"]
_RELEASE = gate.ReleaseSignal if hasattr(gate, "ReleaseSignal") else None


def _release_sig(branch="release/18.0.1.0.176"):
    return gate.detect_release_signal(head_ref=branch)


class TestEvaluateBump(unittest.TestCase):
    def _verdict(self, base, head, changed=_SRC_CHANGE, release=None, base_dirs=()):
        return gate.evaluate_bump(
            base_version=base,
            head_version=head,
            changed_paths=changed,
            release=release,
            base_migration_dirs=list(base_dirs),
        )

    # -- 格 1：一般 PR bump 且無 migration ⇒ 紅 ------------------------------
    def test_case1_ordinary_pr_bumping_is_red(self):
        v = self._verdict("18.0.1.0.175", "18.0.1.0.176")
        self.assertFalse(v.ok, msg=f"一般 PR 擅自 bump 被放行：{v.detail}")
        self.assertEqual(v.reason, "unexpected-bump")
        # 建議值是「改回 base 的版號」，不是再往上加一號。
        self.assertEqual(v.suggested_version, "18.0.1.0.175")
        self.assertIn("18.0.1.0.175", v.detail)

    def test_case1b_ordinary_pr_bumping_across_hundred_is_red(self):
        # 跨百位：字串比較的實作在這裡會判成「沒有變大」而放行。
        v = self._verdict("18.0.1.0.99", "18.0.1.0.100")
        self.assertFalse(v.ok, msg=f"跨百位的擅自 bump 被放行：{v.detail}")
        self.assertEqual(v.reason, "unexpected-bump")

    # -- 格 2：migration PR 且目錄 == 新版號 ⇒ 綠 ----------------------------
    def test_case2_migration_pr_with_matching_dir_is_green(self):
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.176",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.176/post-migrate.py"],
        )
        self.assertTrue(v.ok, msg=f"帶對名 migration 的 bump 被誤擋：{v.detail}")
        self.assertEqual(v.reason, "migration-bump")

    def test_case2b_migration_pr_across_hundred_is_green(self):
        # 跨百位正方向：字串比較會把 .100 判成未大於 .99 ⇒ 假紅。
        v = self._verdict(
            "18.0.1.0.99",
            "18.0.1.0.100",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.100/post-migrate.py"],
        )
        self.assertTrue(v.ok, msg=f"跨百位被誤判為未遞增：{v.detail}")
        self.assertEqual(v.reason, "migration-bump")

    def test_case2c_majorless_migration_dir_matches_like_odoo(self):
        # Odoo convert_version：點數 <= 2 補上 server major。`1.0.176` == `18.0.1.0.176`。
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.176",
            changed=_SRC_CHANGE + ["src/migrations/1.0.176/post-migrate.py"],
        )
        self.assertTrue(v.ok, msg=f"Odoo 會執行的目錄名被判成不相符：{v.detail}")
        self.assertEqual(v.reason, "migration-bump")

    # -- 格 3：migration PR 但目錄 != 新版號 ⇒ 紅 ----------------------------
    def test_case3_migration_dir_not_matching_new_version_is_red(self):
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.176",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.175/post-migrate.py"],
        )
        self.assertFalse(v.ok, msg=f"目錄名與版號不符卻放行：{v.detail}")
        self.assertEqual(v.reason, "migration-mismatch")
        self.assertIn("18.0.1.0.175", v.detail)
        self.assertIn("18.0.1.0.176", v.detail)

    def test_case3b_any_one_matching_dir_is_enough(self):
        # 一張 PR 可能同時修舊 migration 的 bug；判準是「至少有一個目錄 == 新版號」。
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.176",
            changed=[
                "src/migrations/18.0.1.0.122/post-migrate.py",
                "src/migrations/18.0.1.0.176/post-migrate.py",
            ],
        )
        self.assertTrue(v.ok)
        self.assertEqual(v.reason, "migration-bump")

    # -- 格 4：release 訊號 ⇒ 綠 --------------------------------------------
    def test_case4_release_branch_bump_is_green(self):
        v = self._verdict("18.0.1.0.175", "18.0.1.0.176", release=_release_sig())
        self.assertTrue(v.ok, msg=f"release PR 的 bump 被誤擋：{v.detail}")
        self.assertEqual(v.reason, "release-bump")

    def test_case4b_release_branch_without_a_bump_is_red(self):
        """🔴 翻轉：舊版本這格是綠（`unchanged`）。

        #691 規則 C 的字面是「release PR 只要求嚴格大於」——「只要求嚴格大於」就是
        「**必須**嚴格大於」，相等不算。這格放行的話，「發版 PR 忘記 bump」剩下的網
        只有 `/woow:deploy` pre-flight 的 script，那是**人跑的**、不是 required check。
        """
        v = self._verdict("18.0.1.0.175", "18.0.1.0.175", release=_release_sig())
        self.assertFalse(v.ok, msg=f"release PR 忘記 bump 被放行：{v.detail}")
        self.assertEqual(v.reason, "release-needs-bump")
        # 建議值是「往上加一號」，不是「改回 base」——release PR 的義務是升上去。
        self.assertEqual(v.suggested_version, "18.0.1.0.176")

    def test_case4c_release_branch_without_a_bump_is_red_even_out_of_scope(self):
        """release 分支不吃 out-of-scope 豁免。

        🔴 「沒動 src/」正是發版 PR 忘了 bump 的樣子（release PR 的唯一產物就是 manifest
        那一行）。豁免留給它 ⇒ 這道紅燈被最可能的失效形狀繞過，等於沒補。
        """
        v = self._verdict(
            "18.0.1.0.175", "18.0.1.0.175", changed=["docs/release-notes.md"],
            release=_release_sig(),
        )
        self.assertFalse(v.ok, msg=f"沒動 src/ 的 release PR 走了 out-of-scope：{v.detail}")
        self.assertEqual(v.reason, "release-needs-bump")

    def test_case4d_ordinary_branch_out_of_scope_is_still_exempt(self):
        """陽性對照：豁免只對 release 分支關閉，一般 PR 的 out-of-scope 短路原樣還在。"""
        v = self._verdict(
            "18.0.1.0.175", "18.0.1.0.175", changed=["docs/release-notes.md"]
        )
        self.assertTrue(v.ok)
        self.assertEqual(v.reason, "out-of-scope")

    # -- 格 6：新增 migration 卻沒 bump ⇒ 紅（#691 原始失效的重演） -----------
    def test_case6_new_migration_without_a_bump_is_red(self):
        """🔴 翻轉：舊版本這格是綠（`unchanged`）——`cmp == 0` 無條件短路。

        版號相等 ⇒ 已升級到該版的環境（prod 一旦發過這個號就是）**永遠不會跑**這個
        migration。Odoo 的執行視窗是 `installed < 目錄 <= manifest`，靜默 no-op。
        """
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.175",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.175/post-migrate.py"],
        )
        self.assertFalse(v.ok, msg=f"新增 migration 卻沒 bump 被放行：{v.detail}")
        self.assertEqual(v.reason, "migration-needs-bump")
        self.assertEqual(v.suggested_version, "18.0.1.0.176")

    def test_case6b_editing_an_existing_migration_without_a_bump_is_green(self):
        """🔴 乙：判準是「目錄在 base 側不存在」，不是「本 PR 動到」。

        本 repo 有 13 個只改既有 migration、完全沒動 manifest 的 commit（`1ec8144c`
        改的是 `18.0.1.0.37/`）。用「動到的目錄」實作規則 A ⇒ 這一整類 PR 全部變紅，
        而 `.37` 那一種**無解**：沒有辦法把它改名成新版號。
        """
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.175",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.37/post-migrate.py"],
            base_dirs=["18.0.1.0.37"],
        )
        self.assertTrue(v.ok, msg=f"修既有 migration 的 PR 被判紅：{v.detail}")
        self.assertEqual(v.reason, "unchanged")
        # 動到的目錄仍要出現在報告裡，不能靜默吞掉。
        self.assertIn("18.0.1.0.37", v.detail)

    def test_case6c_editing_an_existing_migration_with_a_bump_is_not_a_mismatch(self):
        """🔴 乙的另一半：這個假紅在 `>develop` 路徑上**改之前就已經存在**。

        既有目錄改不了名（`18.0.1.0.37/` 變不成新版號），所以 migration-mismatch 給的
        正解對這種 PR 根本做不到。正確裁決是規則 A 的 `unexpected-bump`：把版號改回去。
        """
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.176",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.37/post-migrate.py"],
            base_dirs=["18.0.1.0.37"],
        )
        self.assertNotEqual(
            v.reason, "migration-mismatch",
            msg=f"編輯既有 migration 被當成「帶 migration 的 PR」：{v.detail}",
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "unexpected-bump")
        self.assertEqual(v.suggested_version, "18.0.1.0.175")
        self.assertIn("既有目錄改不了名", v.detail)

    def test_case6d_new_migration_beats_release_when_both_demand_a_bump(self):
        """兩條規則同時要求 bump 時，訊息不能只講一條。

        只講 release ⇒ 讀者以為改掉分支名就沒事；只講 migration ⇒ 反之。
        """
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.175",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.175/post-migrate.py"],
            release=_release_sig(),
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "migration-needs-bump")
        self.assertIn("規則 C", v.detail)

    def test_case6e_new_and_edited_dirs_are_both_reported(self):
        """新增與「動到既有」同時存在時，兩邊都要在報告裡。"""
        v = self._verdict(
            "18.0.1.0.175",
            "18.0.1.0.175",
            changed=[
                "src/migrations/18.0.1.0.175/post-migrate.py",
                "src/migrations/18.0.1.0.37/post-migrate.py",
            ],
            base_dirs=["18.0.1.0.37"],
        )
        self.assertEqual(v.reason, "migration-needs-bump")
        self.assertEqual(v.new_migration_dirs, ["18.0.1.0.175"])
        self.assertIn("18.0.1.0.37", v.detail)

    def test_case6f_editing_an_existing_migration_while_behind_takes_the_ordinary_fix(self):
        """落後 develop 時，只改既有 migration 的 PR 是**一般 PR**。

        正解是照抄 develop 的版號，不是「重新取號」——把它算成會 bump 的那一類，
        建議值會叫人往上加一號，等於教人違反規則 A。
        """
        v = self._verdict(
            "18.0.1.0.176",
            "18.0.1.0.175",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.37/post-migrate.py"],
            base_dirs=["18.0.1.0.37"],
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "behind")
        self.assertEqual(v.suggested_version, "18.0.1.0.176")

    # -- 格 5：版號倒退 ⇒ 紅（release 訊號也不赦免） -------------------------
    def test_case5_behind_is_red(self):
        v = self._verdict("18.0.1.0.176", "18.0.1.0.175")
        self.assertFalse(v.ok, msg=f"版號倒退被放行：{v.detail}")
        self.assertEqual(v.reason, "behind")

    def test_case5b_release_signal_does_not_excuse_going_backwards(self):
        # 🔴 release 是「准你 bump」，不是「准你降版」——把它寫成無條件 pass 就是後門。
        v = self._verdict("18.0.1.0.176", "18.0.1.0.175", release=_release_sig())
        self.assertFalse(v.ok, msg=f"release 訊號赦免了版號倒退：{v.detail}")
        self.assertEqual(v.reason, "behind")

    def test_case5c_migration_dir_does_not_excuse_going_backwards(self):
        v = self._verdict(
            "18.0.1.0.176",
            "18.0.1.0.175",
            changed=_SRC_CHANGE + ["src/migrations/18.0.1.0.175/post-migrate.py"],
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "behind")

    def test_case5d_behind_across_hundred_is_red(self):
        # develop .100 / PR .99 ⇒ 紅。字串比較的實作在這裡會判成假綠。
        v = self._verdict("18.0.1.0.100", "18.0.1.0.99")
        self.assertFalse(v.ok, msg=f"跨百位的版號倒退被放行：{v.detail}")
        self.assertEqual(v.reason, "behind")

    # -- 常態格：一般 PR 不動版號 ⇒ 綠 --------------------------------------
    def test_equal_is_green(self):
        # #691 的核心翻轉：舊制這一格是紅（equal），新制是**一般 PR 的常態**。
        v = self._verdict("18.0.1.0.175", "18.0.1.0.175")
        self.assertTrue(v.ok, msg=f"一般 PR 不動版號被判紅：{v.detail}")
        self.assertEqual(v.reason, "unchanged")

    def test_tests_only_pr_without_bump_is_green(self):
        v = self._verdict(
            "18.0.1.0.175", "18.0.1.0.175", changed=["src/tests/test_a.py"]
        )
        self.assertTrue(v.ok)
        self.assertEqual(v.reason, "unchanged")

    def test_tests_only_pr_bumping_is_red(self):
        # ⚠ 舊制的 test_bumping_on_an_exempt_pr_is_still_green 在這裡翻轉。
        # 豁免的理由（免除 bump 義務）已不存在；純測試 PR 沒有比別人多的 bump 權利。
        v = self._verdict(
            "18.0.1.0.175", "18.0.1.0.176", changed=["src/tests/test_a.py"]
        )
        self.assertFalse(v.ok, msg=f"純測試 PR 擅自 bump 被放行：{v.detail}")
        self.assertEqual(v.reason, "unexpected-bump")

    def test_out_of_scope_pr_is_green_regardless_of_version_relation(self):
        # 沒動 src/ 的 PR 連 manifest 都碰不到（manifest 就在 src/ 底下）⇒
        # 版號關係只可能來自「base 前進了」，那是分支保護 block_on_outdated_branch
        # 的事，不是本 gate 的事。
        for base, head in (
            ("18.0.1.0.175", "18.0.1.0.175"),
            ("18.0.1.0.176", "18.0.1.0.175"),
        ):
            with self.subTest(base=base, head=head):
                v = self._verdict(base, head, changed=["docs/a.md"])
                self.assertTrue(v.ok)
                self.assertEqual(v.reason, "out-of-scope")


class TestFailureMessages(unittest.TestCase):
    """三種失效機制不同，訊息必須分辨得出來（#538 留言 1 第 2 點）。"""

    def test_unexpected_bump_message_names_the_new_rule_and_the_two_exits(self):
        v = gate.evaluate_bump(
            base_version="18.0.1.0.175",
            head_version="18.0.1.0.176",
            changed_paths=_SRC_CHANGE,
        )
        self.assertIn("#691", v.detail)
        # 兩條合法出口都要講，否則帶 migration 的人不知道自己該往哪走。
        self.assertIn("migration", v.detail)
        self.assertIn("release/", v.detail)

    def test_migration_mismatch_message_names_both_sides(self):
        v = gate.evaluate_bump(
            base_version="18.0.1.0.175",
            head_version="18.0.1.0.176",
            changed_paths=["src/migrations/18.0.1.0.175/post-migrate.py"],
        )
        # 兩個修法（改目錄名 / 改版號）都要指出來。
        self.assertIn("目錄", v.detail)
        self.assertIn("18.0.1.0.176", v.detail)

    def test_migration_needs_bump_message_names_the_silent_no_op(self):
        v = gate.evaluate_bump(
            "18.0.1.0.175",
            "18.0.1.0.175",
            ["src/migrations/18.0.1.0.175/post-migrate.py"],
            base_migration_dirs=[],
        )
        # 裁決原話：訊息必須說出「為什麼相等就等於沒做」，不是只說「必須 bump」。
        self.assertIn("版號相等代表已升級環境永遠不會跑這個 migration", v.detail)
        self.assertIn("installed < 目錄 <= manifest", v.detail)
        self.assertIn("18.0.1.0.176", v.detail)  # 正解版號
        # 並且要講明「修既有 migration 不走這一格」，免得有人拿它去 bump 舊目錄。
        self.assertIn("只編輯既有 migration 目錄不會走到這一格", v.detail)

    def test_release_needs_bump_message_names_both_exits(self):
        v = gate.evaluate_bump(
            "18.0.1.0.175", "18.0.1.0.175", _SRC_CHANGE, release=_release_sig()
        )
        self.assertIn("18.0.1.0.176", v.detail)          # 出口一：把版號升上去
        self.assertIn("release/", v.detail)              # 出口二：分支改名
        # required status check 與「人跑的 script」的差別是這格存在的理由，要寫出來。
        self.assertIn("required status check", v.detail)

    def test_behind_message_rejects_both_wrong_resolutions(self):
        v = gate.evaluate_bump(
            base_version="18.0.1.0.176",
            head_version="18.0.1.0.173",
            changed_paths=_SRC_CHANGE,
        )
        self.assertIn("保留自己", v.detail)
        self.assertIn("照抄", v.detail)

    def test_behind_suggests_the_base_value_for_an_ordinary_pr(self):
        # 一般 PR 的正解是「把 develop 併進來」＝版號回到 develop 的值，不是再 +1。
        v = gate.evaluate_bump(
            base_version="18.0.1.0.176",
            head_version="18.0.1.0.173",
            changed_paths=_SRC_CHANGE,
        )
        self.assertEqual(v.suggested_version, "18.0.1.0.176")

    def test_behind_suggests_a_strictly_greater_value_for_a_bumping_pr(self):
        # 帶 migration 的 PR 被 release PR 追過時，正解是重 bump ＋ 改目錄名（#691）。
        v = gate.evaluate_bump(
            base_version="18.0.1.0.176",
            head_version="18.0.1.0.175",
            changed_paths=["src/migrations/18.0.1.0.175/post-migrate.py"],
        )
        self.assertEqual(v.suggested_version, "18.0.1.0.177")
        self.assertIn("18.0.1.0.177", v.detail)

    def test_suggestion_is_strictly_greater_across_hundred(self):
        v = gate.evaluate_bump(
            base_version="18.0.1.0.99",
            head_version="18.0.1.0.98",
            changed_paths=["src/migrations/18.0.1.0.98/post-migrate.py"],
        )
        self.assertEqual(v.suggested_version, "18.0.1.0.100")

    def test_silent_same_value_mechanism_is_still_documented_for_bumping_prs(self):
        # 🔴 「兩邊 bump 到同值時 git 不報衝突」對 migration PR 與 release PR 仍然成立，
        #    這段機制說明不得隨著 equal 轉綠一起被刪掉。
        v = gate.evaluate_bump(
            base_version="18.0.1.0.176",
            head_version="18.0.1.0.175",
            changed_paths=["src/migrations/18.0.1.0.175/post-migrate.py"],
        )
        self.assertIn("靜默", v.detail)
        self.assertIn("衝突", v.detail)


# --------------------------------------------------------------------------
# 6. 同族的另一半：migrations 目錄版號高於 manifest ⇒ 靜默 no-op
#
# ⚠ 這一支吃的是**工作目錄整份 listing**（所有既有目錄），與規則 B 的輸入
#   （本 PR 動到的目錄）刻意不同——#691 明寫保留、行為不變。
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
# 7. 端到端：真 git repo，跑真的腳本
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

    def _run(self, cwd=None, base_ref="develop", head_ref=None, env=None):
        argv = [sys.executable, str(_GATE_PATH), "--base-ref", base_ref]
        if head_ref is not None:
            argv += ["--head-ref", head_ref]
        return subprocess.run(
            argv,
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
            # GITHUB_HEAD_REF 預設清掉：CI 在 release/* 的 PR 上會把它設成該分支名，
            # 繼承進來的話**每個 fixture 都會被判成 release PR**，期望「非 release
            # 行為」的格全數誤紅（2026-09-24：release/18.0.1.0.178 上 95 支倒 11 支，
            # required check 掛在自測、根本沒跑到真正的判定 ⇒ 發版流程整條卡死）。
            # fixture 要測 env 來源時自己在 env= 明寫，後面的展開會蓋過這個預設。
            env={**os.environ, "GITHUB_HEAD_REF": "", **(env or {})},
        )

    def _advance_develop(self, version):
        """模擬別人先 merge：develop 自己也動了 src/ 並 bump。"""
        _git(self.repo, "checkout", "develop")
        self._write("src/__manifest__.py", _manifest(version))
        self._write("src/models/other.py", "y = 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "someone else merged")

    # -- 格 1（端到端）：一般 PR 不 bump ⇒ 綠 --------------------------------
    def test_src_change_without_bump_is_green(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr")

        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("unchanged", r.stdout)

    def test_ambient_github_head_ref_does_not_leak_into_fixtures(self):
        """自測本身必須對 GITHUB_HEAD_REF 免疫——CI 跑在 release/* 的 PR 上時它是有值的。

        沒有這一格，整個端到端區塊在發版 PR 上會集體誤紅（見 _run 的註解）。
        """
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr")

        saved = os.environ.get("GITHUB_HEAD_REF")
        os.environ["GITHUB_HEAD_REF"] = "release/18.0.1.0.999"
        try:
            r = self._run()
        finally:
            if saved is None:
                os.environ.pop("GITHUB_HEAD_REF", None)
            else:
                os.environ["GITHUB_HEAD_REF"] = saved
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("unchanged", r.stdout)

    # -- 格 2（端到端）：一般 PR bump 無 migration ⇒ 紅 ----------------------
    def test_bump_without_migration_is_red(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr bumps without migration")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("unexpected-bump", r.stdout)
        self.assertIn("#691", r.stdout)

    # -- 格 3（端到端）：bump ＋ 目錄 == 新版號 ⇒ 綠 -------------------------
    def test_bump_with_matching_migration_dir_is_green(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        self._write("src/migrations/18.0.1.0.11/post-migrate.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "migration pr")

        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("migration-bump", r.stdout)

    def test_bump_with_mismatching_migration_dir_is_red(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        self._write("src/migrations/18.0.1.0.10/post-migrate.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "migration dir does not match")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("migration-mismatch", r.stdout)

    def test_existing_migration_dirs_do_not_count_as_this_prs_migration(self):
        """🔴 規則 B 的輸入必須是本 PR 的 changed，不是工作目錄整份 listing。

        base 已經有一個 migration 目錄；本 PR 只 bump、沒帶新 migration ⇒ 必須紅。
        吃整份 listing 的實作在這裡會判成 migration-bump（假綠）。
        """
        _git(self.repo, "checkout", "develop")
        self._write("src/migrations/18.0.1.0.10/post-migrate.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "existing migration on base")

        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "bump only")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("unexpected-bump", r.stdout)

    # -- 格 6（端到端）：新增 migration 卻沒 bump ⇒ 紅 -----------------------
    def test_new_migration_without_bump_is_red(self):
        """🔴 這一格同時釘住「base 側目錄清單讀的是 base commit，不是工作目錄」。

        目錄名刻意取 == manifest，讓第二道檢查（目錄 > manifest）不會叫——紅燈只能
        來自規則 A。若差集的右項誤讀工作目錄，本 PR 新加的目錄會把自己抵消掉 ⇒ 綠。
        """
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/migrations/18.0.1.0.10/post-migrate.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "migration without a bump")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("migration-needs-bump", r.stdout)
        self.assertIn("永遠不會跑", r.stdout)

    def test_editing_an_existing_migration_without_bump_is_green(self):
        """🔴 乙：base 已存在的目錄不算「本 PR 新增」⇒ 不產生 bump 義務。"""
        _git(self.repo, "checkout", "develop")
        self._write("src/migrations/18.0.1.0.5/post-migrate.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "existing migration on base")

        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/migrations/18.0.1.0.5/post-migrate.py", "pass  # bugfix\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "fix a bug in an old migration")

        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("unchanged", r.stdout)

    def test_editing_an_existing_migration_with_bump_is_not_a_mismatch(self):
        """既有目錄改不了名 ⇒ migration-mismatch 給的正解做不到，裁決必須是規則 A。"""
        _git(self.repo, "checkout", "develop")
        self._write("src/migrations/18.0.1.0.5/post-migrate.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "existing migration on base")

        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/migrations/18.0.1.0.5/post-migrate.py", "pass  # bugfix\n")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "fix an old migration and bump")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertNotIn("migration-mismatch", r.stdout)
        self.assertIn("unexpected-bump", r.stdout)

    def test_same_migration_dir_reused_after_base_merged_it_stays_green(self):
        """⚠ 已知殘留（leader1 覆核殘留 #2，本 PR **未**收掉）：兩張 migration PR 撞同一個
        目錄名時，後合的那張把新檔案加進 base 已存在的目錄 ⇒ 目錄不算新增 ⇒ 綠。

        釘成測試是為了讓這條界線可見：哪天有人把判準從「目錄」換成 `git diff` 的 `A`，
        這一格會翻成紅——那時要一併重估「往既有目錄加檔案」的假紅風險，別無聲改掉。
        """
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        self._write("src/migrations/18.0.1.0.11/pr2.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr2 picks .11")

        _git(self.repo, "checkout", "develop")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        self._write("src/migrations/18.0.1.0.11/pr1.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr1 merged first with the same number")
        _git(self.repo, "checkout", "pr")

        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("unchanged", r.stdout)

    # -- 格 4（端到端）：release 分支訊號 ⇒ 綠 -------------------------------
    def test_release_branch_without_bump_is_red(self):
        _git(self.repo, "checkout", "-b", "release/18.0.1.0.11")
        self._write("src/models/foo.py", "x = 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "release pr forgot to bump")

        r = self._run(head_ref="release/18.0.1.0.11")
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("release-needs-bump", r.stdout)

    def test_release_branch_without_bump_is_red_even_without_src_changes(self):
        """release PR 忘記 bump 最可能的樣子就是「src/ 一個檔都沒動」。"""
        _git(self.repo, "checkout", "-b", "release/18.0.1.0.11")
        self._write("docs/a.md", "release notes\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "release pr touched docs only")

        r = self._run(head_ref="release/18.0.1.0.11")
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("release-needs-bump", r.stdout)

    def test_release_branch_bump_is_green(self):
        _git(self.repo, "checkout", "-b", "release/18.0.1.0.11")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "release: 18.0.1.0.11")

        r = self._run(head_ref="release/18.0.1.0.11")
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("release-bump", r.stdout)

    def test_release_signal_absent_degrades_to_ordinary_pr(self):
        """🔴 傳參壞掉時必須安全降級成「非 release」，且輸出要說出它收到了什麼。

        沒有這個回印的話，「傳參壞了」與「這不是 release PR」在輸出上不可區分。
        """
        _git(self.repo, "checkout", "-b", "release/18.0.1.0.11")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "release without the signal wired")

        r = self._run(env={"GITHUB_HEAD_REF": ""})
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("unexpected-bump", r.stdout)
        self.assertIn("未收到", r.stdout)

    def test_pull_ref_input_is_reported_as_unusable_not_as_non_release(self):
        _git(self.repo, "checkout", "-b", "release/18.0.1.0.11")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "release with a pull ref")

        r = self._run(head_ref="refs/pull/816/head", env={"GITHUB_HEAD_REF": ""})
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("refs/pull/816/head", r.stdout)
        self.assertIn("不是分支名", r.stdout)

    # -- 格 5（端到端）：版號倒退 ⇒ 紅 --------------------------------------
    def test_behind_is_red(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/models/foo.py", "x = 2\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "pr")
        self._advance_develop("18.0.1.0.11")
        _git(self.repo, "checkout", "pr")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("behind", r.stdout)
        self.assertIn("18.0.1.0.11", r.stdout + r.stderr)

    # -- 既有守線：三點 diff / 範圍 / migration 目錄 / shallow ---------------
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

    def test_tests_only_pr_without_bump_is_green(self):
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/tests/test_x.py", "pass\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "tests only")

        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("unchanged", r.stdout)

    def test_tests_only_pr_that_bumps_is_red(self):
        # #691：純測試 PR 不再享有「怎麼動版號都行」的豁免。
        _git(self.repo, "checkout", "-b", "pr")
        self._write("src/tests/test_x.py", "pass\n")
        self._write("src/__manifest__.py", _manifest("18.0.1.0.11"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "tests only but bumps")

        r = self._run()
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("unexpected-bump", r.stdout)

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

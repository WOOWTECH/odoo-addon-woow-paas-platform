#!/usr/bin/env python3
"""repo-lint 第 36 條 `indentation` 的注入測試：先證明它會紅、紅在對的檔、說得出寬度。

判準寫「性質」不寫「哪個檔」：每個案例都在臨時 git repo 裡從零造出 base 分支與新增檔，
所以真實 repo 的 codebase 分裂狀態（既有檔 2/4 混雜）不會讓測試變紅；反過來，量法本身
（tokenize INDENT、註解剝除、增量眾數）壞掉時會在這裡先紅。

跑法：
    python3 scripts/audit/test_indentation_gate.py
    # 或 python3 -m pytest scripts/audit/test_indentation_gate.py

契約（.gitea/workflows/repo-lint.yml 的 scripts/ 自我測試步驟）：不得有外部依賴——
不碰網路、不依賴 cwd；只用本機 git（gate 本身就靠它）。
"""
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")

_spec = importlib.util.spec_from_file_location("repo_lint", _GATE_PATH)
repo_lint = importlib.util.module_from_spec(_spec)
sys.modules["repo_lint"] = repo_lint
_spec.loader.exec_module(repo_lint)

EDITORCONFIG = (_REPO_ROOT / ".editorconfig").read_text(encoding="utf-8")

XML_4 = """<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="view_x_form" model="ir.ui.view">
        <field name="name">x.form</field>
        <field name="arch" type="xml">
            <form/>
        </field>
    </record>
</odoo>
"""
XML_2 = XML_4.replace("    ", "  ")
JS_2 = """/** @odoo-module **/

import { Component } from "@odoo/owl";

/**
 * JSDoc 續行的 ` * ` 只有 1 個空格，剝掉註解後不該被當縮排。
 * @param {Object} props
 */
export class X extends Component {
  static template = "woow_paas_platform.X";
  setup() {
    // 行註解也在 code 縮排上
    this.a = 1;
  }
}
"""
JS_4 = """/** @odoo-module **/

import { Component } from "@odoo/owl";

export class X extends Component {
    static template = "woow_paas_platform.X";
    setup() {
        this.a = 1;
    }
}
"""
PY_4 = '''"""Module docstring 的條列只有 2 個空格：
  - 這不是 INDENT，tokenize 看不到它
  - 所以不會把 4-space 檔判成 2
"""


class X:
    def a(self):
        return (1,
             2)
'''
PY_2 = "class X:\n  def a(self):\n    return 1\n"
XML_TAB = "<odoo>\n\t<data/>\n</odoo>\n"
# Odoo data 檔的典型形狀：每筆 record 的 eval 陣列換行對齊（col 19／26，增量 7），record 一多
# 天真的相鄰行眾數會被 7 反超——結構性取樣只看以 < 起頭的行。
XML_HANGING = "<odoo>\n    <data>\n" + "".join(
    "        <record id=\"link_%d\" model=\"x.y\">\n"
    "            <field name=\"tmpl_ids\"\n"
    "                   eval=\"[(4, ref('a')),\n"
    "                          (4, ref('b'))]\"/>\n"
    "        </record>\n" % i for i in range(5)) + "    </data>\n</odoo>\n"
JS_FAKE_COMMENT_IN_STRING = """/** @odoo-module **/
export class X extends Component {
  static template = "/* not a comment";
  setup() {
        this.a = 1;
        if (this.a) {
            this.b = 2;
        }
  }
}
/* end */
"""
JS_MIXED_TIE = """export class X extends Component {
    setup() {
        this.rows = [
          1,
        ];
        this.cols = [
          2,
        ];
    }
}
"""
JS_HANGING_ARGS = """export function f() {
  callSomething(aVeryLongArgument,
                anotherArgument);
  return {
    a: 1,
  };
}
"""


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})


def _write(root, rel, text):
    path = pathlib.Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


class _TmpRepo(unittest.TestCase):
    """臨時 git repo：base 分支含 .editorconfig、三個探針檔（合規）與一個既有 2-space XML，
    `origin/develop` 指到 base；每個測試再往上加新檔。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="indent-gate-")
        self.addCleanup(shutil.rmtree, self.root, True)
        _git(self.root, "init", "-q", "-b", "develop")
        _write(self.root, ".editorconfig", EDITORCONFIG)
        for probe, width in repo_lint._INDENT_PROBE_FILES:
            kind = probe.rsplit(".", 1)[-1]
            body = {"py": PY_4, "js": JS_2, "xml": XML_4}[kind]
            _write(self.root, probe, body)
        _write(self.root, "src/views/legacy_2space.xml", XML_2)  # 既有檔：不歸本 gate 管
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        _git(self.root, "update-ref", "refs/remotes/origin/develop", "HEAD")
        _git(self.root, "checkout", "-q", "-b", "feature")

    def _all(self):
        return repo_lint.check_indentation(self.root)


class TestRed(_TmpRepo):
    """該紅的要紅，而且點名是哪個檔、量到幾格、規定幾格。"""

    def test_new_backend_xml_with_two_spaces_is_flagged(self):
        _write(self.root, "src/views/new_2space.xml", XML_2)
        _git(self.root, "add", "-A")
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn("src/views/new_2space.xml", v[0])
        self.assertIn("量到 2 空格", v[0])
        self.assertIn("規定 4", v[0])

    def test_new_frontend_js_with_four_spaces_is_flagged(self):
        _write(self.root, "src/static/src/paas/components/x/X.js", JS_4)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "add")   # 已 commit 的新增也要抓（base..HEAD）
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn("X.js", v[0])
        self.assertIn("規定 2", v[0])

    def test_new_python_with_two_spaces_is_flagged(self):
        _write(self.root, "scripts/audit/new_tool.py", PY_2)  # untracked 也要抓
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn("new_tool.py", v[0])

    def test_tab_indent_is_flagged(self):
        _write(self.root, "src/data/tabbed.xml", XML_TAB)
        v = self._all()
        self.assertTrue(any("tab 縮排" in x for x in v), v)

    def test_string_with_fake_comment_marker_does_not_hide_violation(self):
        # 審查漏網案：字串裡的 /* 讓天真 regex 吃掉整段 4-space 違規；_strip_js_noise 認得字串邊界。
        _write(self.root, "src/static/src/paas/components/x/Fake.js", JS_FAKE_COMMENT_IN_STRING)
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn("規定 2", v[0])

    def test_mixed_units_tie_is_flagged_as_mixed(self):
        # 審查漏網案：4-space class 裡塞 2-space 陣列讓眾數並列——並列＝混用，紅。
        _write(self.root, "src/static/src/paas/components/x/Tie.js", JS_MIXED_TIE)
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn("並列", v[0])

    def test_lib_dir_outside_static_is_not_skipped(self):
        _write(self.root, "src/models/lib/helper.py", PY_2)
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn("src/models/lib/helper.py", v[0])

    def test_renamed_and_edited_file_counts_as_added(self):
        # git 預設相似度偵測會把「改名＋小改」判成 R 而從 --diff-filter=A 消失；--no-renames 擋這個。
        big = XML_4.replace("</odoo>", "".join(
            "    <record id=\"r%d\" model=\"ir.ui.view\">\n        <field name=\"name\">r%d</field>\n    </record>\n" % (i, i)
            for i in range(20)) + "</odoo>")
        _write(self.root, "src/views/big.xml", big)
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-q", "-m", "big")
        _git(self.root, "update-ref", "refs/remotes/origin/develop", "HEAD")
        _git(self.root, "mv", "src/views/big.xml", "src/views/big_renamed.xml")
        text = (pathlib.Path(self.root) / "src/views/big_renamed.xml").read_text(encoding="utf-8")
        text = text.replace("    <record id=\"r19\"", "  <record id=\"r19\"").replace(
            "        <field name=\"name\">r19</field>", "    <field name=\"name\">r19</field>")
        _write(self.root, "src/views/big_renamed.xml", text)
        _git(self.root, "add", "-A"); _git(self.root, "commit", "-q", "-m", "rename+edit")
        # 性質＝它必須被列進「新增檔」（git 自己的 --name-status 對這個形狀回 R）；
        # 整檔仍以 4-space 為眾數，所以不會產生違規——這裡驗的是候選集合，不是判定。
        status = subprocess.run(["git", "diff", "--name-status", "origin/develop", "HEAD"],
                                cwd=self.root, capture_output=True, text=True).stdout
        self.assertIn("R", status.split()[0], status)
        files, err = repo_lint._indent_new_files(self.root)
        self.assertIsNone(err)
        self.assertIn("src/views/big_renamed.xml", files)

    def test_missing_editorconfig_is_loud(self):
        os.remove(os.path.join(self.root, ".editorconfig"))
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn(".editorconfig", v[0])

    def test_editorconfig_without_indent_size_is_loud(self):
        _write(self.root, ".editorconfig", "root = true\n[*.py]\nindent_style = space\n")
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn("indent_size", v[0])

    def test_broken_probe_section_is_loud(self):
        # glob 被改壞（後端 XML section 拿掉）⇒ menu.xml 探針算不出 4 ⇒ 整條紅，而不是沉默。
        broken = EDITORCONFIG.replace("[src/{views,data,security}/**.xml]", "[nowhere/**.xml]")
        _write(self.root, ".editorconfig", broken)
        v = self._all()
        self.assertTrue(any("src/views/menu.xml" in x and "預期 4" in x for x in v), v)

    def test_missing_base_ref_is_loud(self):
        _git(self.root, "update-ref", "-d", "refs/remotes/origin/develop")
        v = self._all()
        self.assertEqual(len(v), 1, v)
        self.assertIn("base ref", v[0])


class TestGreen(_TmpRepo):
    """不該紅的不紅——每一條都是「報告範例版」量法會誤擋的形狀。"""

    def test_no_new_files_is_clean(self):
        self.assertEqual(self._all(), [])

    def test_pre_existing_two_space_file_is_not_judged(self):
        # base 裡的 2-space XML 存在但不是本 PR 新增 ⇒ 不判（「既有檔跟現況」）。
        _write(self.root, "src/views/new_ok.xml", XML_4)
        _git(self.root, "add", "-A")
        self.assertEqual(self._all(), [])

    def test_jsdoc_and_line_comments_do_not_pollute(self):
        _write(self.root, "src/static/src/paas/components/y/Y.js", JS_2)
        self.assertEqual(self._all(), [])

    def test_docstring_bullets_do_not_pollute_python(self):
        _write(self.root, "src/models/new_model.py", PY_4)
        self.assertEqual(self._all(), [])

    def test_odoo_data_xml_with_hanging_eval_attributes_is_clean(self):
        # 審查誤殺案：5 筆 record、每筆兩行 eval 續行——天真眾數會量到 7。
        _write(self.root, "src/data/links.xml", XML_HANGING)
        self.assertEqual(self._all(), [])

    def test_js_hanging_args_do_not_pollute(self):
        _write(self.root, "src/static/src/paas/components/h/H.js", JS_HANGING_ARGS)
        self.assertEqual(self._all(), [])

    def test_vendored_lib_is_skipped(self):
        _write(self.root, "src/static/src/paas/lib/vendor.js", JS_4)
        _write(self.root, "src/static/src/paas/x.min.js", JS_4)
        self.assertEqual(self._all(), [])

    def test_file_without_indentation_has_no_evidence(self):
        _write(self.root, "src/data/flat.xml", "<odoo/>\n")
        self.assertEqual(self._all(), [])

    def test_non_git_mirror_root_is_silent_in_all_mode(self):
        # copytree 鏡像（無 .git）沒有「本 PR 新增」的概念：不判、也不當探針壞掉。
        plain = tempfile.mkdtemp(prefix="indent-gate-mirror-")
        self.addCleanup(shutil.rmtree, plain, True)
        _write(plain, ".editorconfig", EDITORCONFIG)
        for probe, _ in repo_lint._INDENT_PROBE_FILES:
            _write(plain, probe, "x\n")
        _write(plain, "src/views/x.xml", XML_2)
        self.assertEqual(repo_lint.check_indentation(plain), [])

    def test_xml_comment_indent_does_not_pollute(self):
        body = XML_4.replace("    <record", "  <!-- 註解縮 2 格 -->\n    <record")
        _write(self.root, "src/views/commented.xml", body)
        self.assertEqual(self._all(), [])


class TestHookMode(_TmpRepo):
    """hook 模式（only_file）：只驗 untracked，不算 base。"""

    def test_untracked_new_file_is_judged(self):
        path = _write(self.root, "src/views/hook_2space.xml", XML_2)
        v = repo_lint.check_indentation(self.root, only_file=path)
        self.assertEqual(len(v), 1, v)

    def test_tracked_file_is_skipped_even_if_noncompliant(self):
        path = os.path.join(self.root, "src/views/legacy_2space.xml")
        self.assertEqual(repo_lint.check_indentation(self.root, only_file=path), [])

    def test_missing_editorconfig_is_silent_in_hook_mode(self):
        # hook 不擋開發：同一個 root 的 --all 會紅（TestRed.test_missing_editorconfig_is_loud）。
        os.remove(os.path.join(self.root, ".editorconfig"))
        path = _write(self.root, "src/views/hook_2space.xml", XML_2)
        self.assertEqual(repo_lint.check_indentation(self.root, only_file=path), [])

    def test_non_git_root_is_silent_in_hook_mode(self):
        plain = tempfile.mkdtemp(prefix="indent-gate-nogit-")
        self.addCleanup(shutil.rmtree, plain, True)
        _write(plain, ".editorconfig", EDITORCONFIG)
        path = _write(plain, "src/views/x.xml", XML_2)
        self.assertEqual(repo_lint.check_indentation(plain, only_file=path), [])

    def test_nested_worktree_file_uses_its_own_root(self):
        # #776 同型：hook 收到 .claude/worktrees/<x>/ 下的檔，root 若用主 checkout，rel 帶前綴
        # 對不上 .editorconfig 的 src/ 錨 ⇒ 靜默。改以該檔自己的 toplevel 為 root。
        _git(self.root, "worktree", "add", "-q", "-b", "inner", ".claude/worktrees/inner", "develop")
        inner = os.path.join(self.root, ".claude/worktrees/inner")
        path = _write(inner, "src/views/inner_2space.xml", XML_2)
        v = repo_lint.check_indentation(self.root, only_file=path)
        self.assertEqual(len(v), 1, v)
        self.assertIn("inner_2space.xml", v[0])

    def test_out_of_scope_extension_is_skipped(self):
        path = _write(self.root, "docs/x.md", "  - two spaces\n")
        self.assertEqual(repo_lint.check_indentation(self.root, only_file=path), [])


class TestGlobTranslation(unittest.TestCase):
    """editorconfig glob 子集的翻譯：這是探針對照真正在守的東西。"""

    def test_real_editorconfig_maps_probe_paths(self):
        sections = repo_lint._parse_editorconfig(EDITORCONFIG)
        for probe, width in repo_lint._INDENT_PROBE_FILES:
            self.assertEqual(repo_lint._indent_rule_for(probe, sections)[0], width, probe)
        self.assertEqual(repo_lint._indent_rule_for("src/static/src/paas/a/b.scss", sections)[0], 2)
        self.assertEqual(repo_lint._indent_rule_for("src/data/x.xml", sections)[0], 4)
        self.assertEqual(repo_lint._indent_rule_for("src/security/ir_rules.xml", sections)[0], 4)
        self.assertEqual(repo_lint._indent_rule_for("extra/paas-operator/src/main.py", sections)[0], 4)
        self.assertIsNone(repo_lint._indent_rule_for("docs/x.md", sections)[0])

    def test_base_ref_keeps_slashes_and_strips_refs_heads(self):
        saved = {k: os.environ.get(k) for k in ("REPO_LINT_INDENT_BASE", "GITHUB_BASE_REF")}
        try:
            os.environ.pop("REPO_LINT_INDENT_BASE", None)
            os.environ["GITHUB_BASE_REF"] = "release/18.0.1.0.177"
            self.assertEqual(repo_lint._indent_base_ref(), "origin/release/18.0.1.0.177")
            os.environ["GITHUB_BASE_REF"] = "refs/heads/develop"
            self.assertEqual(repo_lint._indent_base_ref(), "origin/develop")
            os.environ["REPO_LINT_INDENT_BASE"] = "origin/main"
            self.assertEqual(repo_lint._indent_base_ref(), "origin/main")
        finally:
            for k, val in saved.items():
                if val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = val

    def test_unanchored_pattern_matches_any_directory(self):
        rx = repo_lint._editorconfig_glob_re("*.py")
        self.assertTrue(rx.search("a/b/c.py"))
        self.assertTrue(rx.search("c.py"))
        self.assertFalse(rx.search("a/b/c.pyx"))

    def test_double_star_crosses_directories_and_braces_alternate(self):
        rx = repo_lint._editorconfig_glob_re("src/static/src/**.{js,scss}")
        self.assertTrue(rx.search("src/static/src/paas/x/y.js"))
        self.assertTrue(rx.search("src/static/src/z.scss"))
        self.assertFalse(rx.search("src/views/y.js"))


if __name__ == "__main__":
    unittest.main(verbosity=1)

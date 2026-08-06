#!/usr/bin/env python3
"""doc-symbol-anchor（repo-lint 第 19 條）的 A/B 注入測試（#626）。

被測的 check 問一件事：**hook 必讀件裡 `path:symbol` 錨點的符號，今天還在不在**。
這支測試問的是被測 check 的同一個問題的上一層：**改壞它，會不會紅、紅得對不對、
有沒有點名是哪一個**——「現在是綠的」從來就不是驗收（#596 那條 check 通過了 CI，
卻在 hook 模式整條休眠，綠色與「根本沒跑到」長得一模一樣）。

四類覆蓋，缺一不可：

  A. **doc 側注入** —— doc 寫一個不存在的符號 ⇒ 必紅。
  B. **code 側注入** —— 動 code 不動 doc（把被引用的符號改名）⇒ 必紅。
     這條才證明它讀的是 source，不是讀自己剛寫的散文自我印證。
  C. **特異性** —— 正常新增一條合規錨點 ⇒ **必須維持綠**。過度敏感的 gate 會擋住
     正當工作，而那種失效在「只驗改壞」的測試集裡是**隱形的**。
  D. **三條處理規則各自的守衛** —— 圍欄內不驗 ／ repo 外的檔跳過 ／ basename 歧義。

🔴 注入的錨點一律**從真相源推導**（掃真實 corpus 找一個今天活著的錨點，再從它衍生
   出「保證不存在」的變體），不寫死當下值。寫死的注入器會在對方把 doc 改好的那天
   靜默不匹配 ⇒ 那幾支測試改為驗證一份**未突變**的內容，看起來全綠。實證：PR #620
   在尾端新增 check 時，寫死錨點的注入器三處同時失效，是絆線擋住才沒變假綠。
   每一處注入都配**陽性對照**：重新解析注入後的內容確認注入真的生效，而不是比對
   「我打算寫什麼」。

跑法：
    python3 scripts/audit/test_doc_symbol_anchor.py
    # 或 python3 -m pytest scripts/audit/test_doc_symbol_anchor.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部依賴
——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo，也不依賴 cwd。實測過 `env -i`、
空 HOME、`cwd=/` 皆綠且不寫 HOME（暫存一律走 tempfile，即 TMPDIR／`/tmp`）。
"""
import collections
import glob
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")

# 檔名有連字號，只能走 importlib（與 test_repo_lint_docstring_registry.py 同）。
_spec = importlib.util.spec_from_file_location("repo_lint", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)

_REAL_GATE_SRC = _GATE_PATH.read_text(encoding="utf-8")

Anchor = collections.namedtuple("Anchor", "doc line path symbol target")


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _write_text(path, text):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------- 真相源推導

def _live_anchors(root):
    """掃 root 的 scope docs，回今天**解析得到且符號存在**的錨點。

    這是所有注入的素材來源。刻意用被測模組自己的 helper 推導，不寫死任何一組
    (doc, path, symbol)：doc 被合理重寫時，注入器要嘛跟著動、要嘛在下面的 assert
    大聲失敗，不能靜默變成「驗證一份沒被改過的內容」。
    """
    files = rl._repo_code_files(root)
    top_dirs = {n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))}
    out = []
    for rel_dir in rl.DOC_SYMBOL_ANCHOR_DIRS:
        for doc_path in sorted(glob.glob(os.path.join(root, rel_dir, "**", "*.md"),
                                         recursive=True)):
            rel_doc = os.path.relpath(doc_path, root).replace(os.sep, "/")
            text = _read(doc_path)
            for line, path, symbol in rl._doc_symbol_anchors(text):
                target, status, _cands = rl._resolve_anchor_file(path, files, top_dirs)
                if status not in ("exact", "unique"):
                    continue
                names, _err = rl._anchor_file_symbols(
                    target, os.path.join(root, target))
                if names and all(seg in names for seg in symbol.split(".")):
                    out.append(Anchor(rel_doc, line, path, symbol, target))
    return out


_LIVE = _live_anchors(str(_REPO_ROOT))
assert _LIVE, (
    "真實 corpus 一個活著的錨點都推導不出來——**注入工具失效，不是測試通過**。"
    "所有 A/B 注入的素材都從這裡來，空清單會讓下面每一條測試變成驗證空集合。"
    "若 scope 內的 doc 真的一條 `path:symbol` 錨點都不剩了，這支測試要重新設計，"
    "不是把這行 assert 拿掉")

# code 側注入需要能在 Python 原始碼裡改名，所以只從 .py 目標推導。
_PY_LIVE = [a for a in _LIVE if a.target.endswith(".py")]
assert _PY_LIVE, "推導不出任何指向 .py 的活錨點——code 側注入器失效，不是測試通過"


def _dead_symbol_for(root, target, seed):
    """從真相源衍生一個**保證不存在於該檔**的符號名（帶陽性對照）。"""
    names, err = rl._anchor_file_symbols(target, os.path.join(root, target))
    assert names is not None, "%s 抽不出符號表（%s）——注入工具失效" % (target, err)
    dead = seed + "_zzz_gate_probe"
    assert dead not in names, (
        "衍生出來的「不存在符號」%r 竟然真的在 %s 裡——注入工具失效，不是測試通過"
        % (dead, target))
    return dead


# --------------------------------------------------------- 真實 repo 的鏡像

_MIRROR = None


def setUpModule():
    """把真實 repo 複製一份出來注入。

    不在原地改：測試若中途炸掉，工作目錄會被留在被注入的狀態，而那個狀態長得像
    「有人真的把 doc 寫壞了」。複製一次約 0.9s，全模組共用。
    """
    global _MIRROR
    _MIRROR = tempfile.mkdtemp(prefix="doc-symbol-anchor-")
    shutil.copytree(
        str(_REPO_ROOT), os.path.join(_MIRROR, "repo"),
        ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules",
                                      "*.pyc", ".venv", "venv"))


def tearDownModule():
    if _MIRROR:
        shutil.rmtree(_MIRROR, ignore_errors=True)


def _mirror_root():
    return os.path.join(_MIRROR, "repo")


class MirrorCase(unittest.TestCase):
    """在鏡像上注入、跑 check、自動還原的共用底座。"""

    def setUp(self):
        self.root = _mirror_root()
        # 🔴 每一支測試都先確認基準是綠的。少了這行，「注入後是紅的」可以只是因為
        #    基準本來就紅（對照 0：改動前必綠、注入後必紅，兩端都要有斷言）。
        self.assertEqual(
            rl.check_doc_symbol_anchor(self.root), [],
            "鏡像在注入之前就不是綠的——後面每一條「注入後變紅」都失去意義")

    def _rewrite(self, rel, new_text):
        """改寫鏡像裡的一個檔，測試結束自動還原。"""
        path = os.path.join(self.root, rel)
        original = _read(path)
        self.addCleanup(lambda: _write_text(path, original))
        _write_text(path, new_text)
        return original

    def _append_to_doc(self, rel, extra):
        original = _read(os.path.join(self.root, rel))
        self._rewrite(rel, original + extra)

    def _run(self):
        return rl.check_doc_symbol_anchor(self.root)


# --------------------------------------------------------- A：doc 側注入

class TestDocSideInjection(MirrorCase):
    """doc 寫一個不存在的符號 ⇒ 必紅，且要點名是哪一份 doc、哪個符號。"""

    def test_dead_symbol_in_doc_is_caught(self):
        live = _PY_LIVE[0]
        dead = _dead_symbol_for(self.root, live.target, live.symbol)
        payload = "\n\n- 注入探針：`%s:%s`\n" % (live.target, dead)
        self._append_to_doc(live.doc, payload)

        # 陽性對照：**重新解析注入後的內容**，確認 gate 真的看得到這個錨點——
        # 而不是比對「我打算寫什麼」。
        text = _read(os.path.join(self.root, live.doc))
        parsed = rl._doc_symbol_anchors(text)
        self.assertIn((live.target, dead), [(p, s) for _l, p, s in parsed],
                      "注入的錨點重新解析後抽不到——注入工具失效，不是測試通過")

        violations = self._run()
        self.assertTrue(violations, "doc 寫了不存在的符號，gate 完全沉默")
        hit = [v for v in violations if live.doc in v and dead in v]
        self.assertTrue(hit, "紅了但沒點名是哪一份 doc／哪個符號：%r" % violations)
        self.assertIn("已經不存在", hit[0])

    def test_dead_symbol_reported_once_per_occurrence(self):
        """同一份 doc 兩處壞錨點要各報一次（帶行號），不是合併成一句。"""
        live = _PY_LIVE[0]
        dead = _dead_symbol_for(self.root, live.target, live.symbol)
        self._append_to_doc(live.doc, "\n\nX `%s:%s`\n\nY `%s:%s`\n"
                            % (live.target, dead, live.target, dead))
        hits = [v for v in self._run() if dead in v]
        self.assertEqual(len(hits), 2, "兩處壞錨點只報了 %d 次：%r" % (len(hits), hits))
        self.assertNotEqual(hits[0], hits[1], "兩則訊息一字不差 ⇒ 行號沒帶上，無法定位")


# --------------------------------------------------------- B：code 側注入

class TestCodeSideInjection(MirrorCase):
    """動 code 不動 doc ⇒ 必紅。這條才證明它讀的是 source。"""

    def _rename_first_renamable(self):
        """把某個被 doc 引用的符號在 code 裡改名，回 (anchor, 新名字)。

        逐個試而不是寫死挑第一個：`def X` 這個形態不一定命中（符號也可能是欄位
        宣告或 class），試不到就是注入工具失效，要大聲說。
        """
        for live in _PY_LIVE:
            path = os.path.join(self.root, live.target)
            src = _read(path)
            pattern = re.compile(r"\bdef %s\b" % re.escape(live.symbol))
            if not pattern.search(src):
                continue
            renamed = live.symbol + "_renamed_by_probe"
            self._rewrite(live.target, pattern.sub("def " + renamed, src))
            names, err = rl._anchor_file_symbols(
                live.target, os.path.join(self.root, live.target))
            self.assertIsNotNone(names, "改名後抽不出符號表（%s）" % err)
            if live.symbol in names:
                continue  # 同檔還有別的地方宣告同名，換一個目標
            self.assertIn(renamed, names, "改名沒生效——注入工具失效")
            return live, renamed
        self.fail("找不到任何可用 `def X` 改名注入的活錨點——注入工具失效，不是測試通過")

    def test_renamed_symbol_in_code_is_caught(self):
        live, renamed = self._rename_first_renamable()
        violations = self._run()
        self.assertTrue(violations,
                        "被 doc 引用的符號在 code 裡改名了，gate 完全沉默 ⇒ 它讀的"
                        "不是 source")
        hit = [v for v in violations if live.doc in v and live.symbol in v]
        self.assertTrue(hit, "紅了但沒點名是哪一份 doc／哪個符號：%r" % violations)
        self.assertIn(live.target, hit[0], "訊息沒說是哪個檔的符號不見了")
        # 反向陽性對照：新名字**不該**被當成錨點的一部分報出來（訊息在講舊名字）。
        self.assertNotIn(renamed + "`", hit[0])

    def test_deleting_the_symbol_is_caught(self):
        """整段刪掉（不是改名）一樣要紅——改名只是消失的其中一種形態。"""
        live = _PY_LIVE[0]
        path = os.path.join(self.root, live.target)
        src = _read(path)
        names_before, _ = rl._anchor_file_symbols(live.target, path)
        self.assertIn(live.symbol, names_before)
        # 把所有出現一律換名（含呼叫點）＝該符號在這個檔徹底消失。
        self._rewrite(live.target,
                      re.sub(r"\b%s\b" % re.escape(live.symbol), "gone_probe", src))
        names_after, _ = rl._anchor_file_symbols(
            live.target, os.path.join(self.root, live.target))
        self.assertNotIn(live.symbol, names_after, "注入沒生效——符號還在")
        self.assertTrue([v for v in self._run() if live.symbol in v])


# --------------------------------------------------------- C：特異性

class TestSpecificity(MirrorCase):
    """正常寫法必須維持綠。過度敏感的 gate 會擋住正當工作，而那種失效在「只驗改壞」
    的測試集裡是隱形的。"""

    def test_new_valid_anchor_stays_green(self):
        live = _PY_LIVE[0]
        payload = "\n\n- 新增的合規錨點：`%s:%s`\n" % (live.target, live.symbol)
        self._append_to_doc(live.doc, payload)
        # 陽性對照：這條新錨點確實被解析到了（不是因為「沒抽到」才綠）。
        text = _read(os.path.join(self.root, live.doc))
        self.assertIn((live.target, live.symbol),
                      [(p, s) for _l, p, s in rl._doc_symbol_anchors(text)],
                      "新增的合規錨點根本沒被抽到——這條測試在驗空集合")
        self.assertEqual(self._run(), [], "正常新增一條合規錨點竟然變紅")

    def test_line_number_pointers_stay_green(self):
        """帶行號的舊式指標不歸本 gate 管——全 docs/ 有 2473 處，收進來會一次紅爆。"""
        live = _PY_LIVE[0]
        self._append_to_doc(
            live.doc,
            "\n\n- `%s:581`、`%s:43-61`、`%s:1375, 1454-1471`、`%s:81 %s`\n"
            % (live.target, live.target, live.target, live.target, live.symbol))
        self.assertEqual(self._run(), [], "帶行號的指標被誤抓成符號錨點")

    def test_unique_basename_without_directory_stays_green(self):
        """嚴格解析不等於「只准寫全路徑」——basename 唯一時就該解析得到。"""
        live = next((a for a in _PY_LIVE
                     if len({f for f in rl._repo_code_files(self.root)
                             if os.path.basename(f) == os.path.basename(a.target)}) == 1),
                    None)
        self.assertIsNotNone(live, "找不到 basename 唯一的活錨點——推導失效")
        self._append_to_doc(live.doc, "\n\n- `%s:%s`\n"
                            % (os.path.basename(live.target), live.symbol))
        self.assertEqual(self._run(), [], "basename 唯一卻被判成解析不到")


# --------------------------------------------------------- 合成 repo 的底座

def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_text(path, text)


class SyntheticCase(unittest.TestCase):
    """自建最小 repo：要造「兩個同名檔」「repo 外的檔」這些形態，真實 repo 造不出來。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doc-symbol-anchor-syn-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        # 兩個宣告目錄各先放一份無錨點的 doc：check 對「宣告了卻掃不到 .md」有 floor。
        for rel_dir in rl.DOC_SYMBOL_ANCHOR_DIRS:
            _write(self.root, rel_dir + "/README.md", "# 佔位\n")

    def doc(self, text, name="probe.md"):
        _write(self.root, rl.DOC_SYMBOL_ANCHOR_DIRS[0] + "/" + name, text)

    def run_check(self):
        return rl.check_doc_symbol_anchor(self.root)


# --------------------------------------------------------- D1：``` 圍欄內不驗

class TestFenceRule(SyntheticCase):
    """規則 1：圍欄內的指標是**舉證**不是導航，過期是正常的、甚至是必要的。"""

    def setUp(self):
        super().setUp()
        _write(self.root, "src/models/thing.py", "def real_symbol():\n    pass\n")

    def test_broken_anchor_inside_fence_is_ignored(self):
        self.doc("# t\n\n```bash\n$ grep -rn foo\n"
                 "`src/models/thing.py:vanished_symbol`\n```\n")
        self.assertEqual(self.run_check(), [])

    def test_same_payload_outside_fence_is_caught(self):
        """對照組：同一段 payload 移出圍欄就必須紅——證明上一條的綠不是因為 payload
        本身沒效（不然「圍欄豁免」與「錨點根本沒被抽到」分不出來）。"""
        self.doc("# t\n\n`src/models/thing.py:vanished_symbol`\n")
        self.assertTrue(self.run_check())

    def test_tilde_fence_and_unclosed_fence(self):
        self.doc("# t\n\n~~~\n`src/models/thing.py:vanished_symbol`\n~~~\n", "a.md")
        self.assertEqual(self.run_check(), [])
        self.doc("# t\n\n```\n`src/models/thing.py:vanished_symbol`\n", "b.md")
        self.assertEqual(self.run_check(), [], "未閉合的圍欄應該當開到檔尾")

    def test_fence_blanking_preserves_line_numbers(self):
        blanked = rl._blank_md_fences("a\n```\nxx\n```\nb `x.py:y`\n")
        self.assertEqual(blanked.count("\n"), 5)
        self.assertEqual([n for n, _p, _s in rl._doc_symbol_anchors(
            "a\n```\nxx\n```\nb `x.py:y`\n")], [5])

    def test_fence_rule_is_load_bearing_on_the_real_corpus(self):
        """🔴 這條規則在**今天的真實 corpus** 上就有作用，不是預想的防禦。

        判準不是「圍欄裡有沒有東西」，而是「拿掉這條規則會不會多出**會讓 gate 變紅**
        的錨點」——只多出幾個照樣通過的錨點，那這條規則就只是省事而已。
        """
        root = str(_REPO_ROOT)
        files = rl._repo_code_files(root)
        top_dirs = {n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))}
        would_break = []
        for rel_dir in rl.DOC_SYMBOL_ANCHOR_DIRS:
            for doc_path in sorted(glob.glob(
                    os.path.join(root, rel_dir, "**", "*.md"), recursive=True)):
                text = _read(doc_path)
                kept = {(p, s) for _l, p, s in rl._doc_symbol_anchors(text)}
                for m in rl._DOC_SYMBOL_ANCHOR_RE.finditer(text):
                    pair = (m.group(1), m.group(2))
                    if pair in kept:
                        continue
                    target, status, _c = rl._resolve_anchor_file(pair[0], files, top_dirs)
                    if status in ("ambiguous", "dead"):
                        would_break.append((doc_path, pair, status))
                        continue
                    if status == "outside":
                        continue
                    names, _e = rl._anchor_file_symbols(
                        target, os.path.join(root, target))
                    if names is None or any(seg not in names
                                            for seg in pair[1].split(".")):
                        would_break.append((doc_path, pair, "missing"))
        self.assertTrue(
            would_break,
            "拿掉圍欄排除後，今天的 corpus 一個違規都不會多——這條規則在真實資料上"
            "沒有作用了。若圍欄裡的歷史重現真的都被清掉了，請重新評估這條規則要不要"
            "留，而不是把這個斷言刪掉")


# --------------------------------------------------------- D2：repo 外的檔跳過

class TestOutsideRepoRule(SyntheticCase):
    """規則 2：「檔不存在（跳過）」與「檔在、符號不在（fail）」必須分開判。"""

    def setUp(self):
        super().setUp()
        _write(self.root, "src/models/thing.py", "def real_symbol():\n    pass\n")

    def test_bare_foreign_basename_is_skipped(self):
        """`binary.py:content_common` 的形態：Odoo 核心的檔，不在本 repo。"""
        self.doc("# t\n\n`binary.py:content_common`\n")
        self.assertEqual(self.run_check(), [])

    def test_foreign_rooted_path_is_skipped(self):
        self.doc("# t\n\n`addons/base/models/ir_rule.py:_compute_domain`\n")
        self.assertEqual(self.run_check(), [])

    def test_existing_file_missing_symbol_fails(self):
        """對照組：檔在就一定要判——「跳過」不能被當成萬用後門。"""
        self.doc("# t\n\n`src/models/thing.py:no_such_symbol`\n")
        violations = self.run_check()
        self.assertTrue(violations)
        self.assertIn("已經不存在", violations[0])

    def test_repo_rooted_missing_file_fails(self):
        """路徑宣稱自己是 repo 相對路徑卻找不到 ⇒ 指標已死，不是「repo 外」。

        沒有這條分界，把路徑打錯一個字就能讓錨點永久靜默。
        """
        self.doc("# t\n\n`src/models/typo_thing.py:real_symbol`\n")
        violations = self.run_check()
        self.assertTrue(violations, "repo 相對路徑指到不存在的檔，gate 完全沉默")
        self.assertIn("指標已死", violations[0])
        self.assertNotIn("已經不存在", violations[0],
                         "「檔不見了」與「符號不見了」要給出不同的訊息")

    def test_broken_python_is_not_a_pass(self):
        """探針壞掉不算通過：parse 不了就報，不是靜默放行。"""
        _write(self.root, "src/models/broken.py", "def oops(:\n")
        self.doc("# t\n\n`src/models/broken.py:oops`\n")
        violations = self.run_check()
        self.assertTrue(violations)
        self.assertIn("探針壞掉不算通過", violations[0])


# --------------------------------------------------------- D3：basename 歧義

class TestBasenameAmbiguityRule(SyntheticCase):
    """規則 3：錨點必須唯一解析到一個檔。

    #626 的可行性評估就是栽在這裡：腳本用 basename glob 取第一個命中，命中了沒有該
    符號的那一個，於是報「符號找不到」——**一個純粹由解析策略製造的假陽性**。
    """

    def setUp(self):
        super().setUp()
        _write(self.root, "src/models/dup.py", "def only_in_models():\n    pass\n")
        _write(self.root, "src/controllers/dup.py",
               "def only_in_controllers():\n    pass\n")
        _write(self.root, "src/models/solo.py", "def solo_symbol():\n    pass\n")

    def test_ambiguous_basename_fails(self):
        self.doc("# t\n\n`dup.py:only_in_controllers`\n")
        violations = self.run_check()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("2 個候選", violations[0])
        self.assertIn("src/controllers/dup.py", violations[0])
        self.assertIn("src/models/dup.py", violations[0])

    def test_ambiguity_is_not_reported_as_missing_symbol(self):
        """🔴 #626 那個假陽性的回歸守衛。

        「取第一個命中」的策略會在這個 fixture 上報「符號找不到」——把解析問題誤診
        成 doc 寫錯。訊息說錯了病因，讀者就會去改一個根本沒壞的東西。
        """
        self.doc("# t\n\n`dup.py:only_in_controllers`\n")
        self.assertNotIn("已經不存在", self.run_check()[0])

    def test_qualified_path_resolves(self):
        self.doc("# t\n\n`src/controllers/dup.py:only_in_controllers`\n")
        self.assertEqual(self.run_check(), [])

    def test_partial_path_suffix_resolves(self):
        """後綴唯一就夠——不必逼所有人寫 repo 根起算的全路徑。"""
        self.doc("# t\n\n`controllers/dup.py:only_in_controllers`\n")
        self.assertEqual(self.run_check(), [])

    def test_unique_basename_resolves(self):
        self.doc("# t\n\n`solo.py:solo_symbol`\n")
        self.assertEqual(self.run_check(), [])

    def test_worktree_copies_do_not_poison_the_index(self):
        """主 checkout 上的 `.claude/worktrees/` 是整個 repo 的副本。不剪掉的話
        **每一個**錨點都會同時變成「歧義」——一整片純由掃描範圍製造的假陽性。"""
        _write(self.root, ".claude/worktrees/wt/src/models/solo.py",
               "def solo_symbol():\n    pass\n")
        self.doc("# t\n\n`solo.py:solo_symbol`\n")
        self.assertEqual(self.run_check(), [])


# --------------------------------------------------------- 宣告清單的 floor

class TestDeclaredScopeFloor(SyntheticCase):
    def test_missing_declared_dir_is_a_violation(self):
        shutil.rmtree(os.path.join(self.root, rl.DOC_SYMBOL_ANCHOR_DIRS[1]))
        violations = self.run_check()
        self.assertTrue(violations, "宣告的 doc 目錄整個不見了，gate 完全沉默 ⇒ 那個"
                                    "目錄的錨點整批休眠，輸出卻與「全部通過」相同")
        self.assertIn(rl.DOC_SYMBOL_ANCHOR_DIRS[1], violations[0])

    def test_both_declared_dirs_missing_but_docs_present_is_a_violation(self):
        """兩個都不見、`docs/` 還在 ⇒ 仍然要紅（漂移，不是「不在這棵樹上」）。"""
        for rel_dir in rl.DOC_SYMBOL_ANCHOR_DIRS:
            shutil.rmtree(os.path.join(self.root, rel_dir))
        _write(self.root, "docs/index.md", "# 佔位\n")
        self.assertEqual(len(self.run_check()), len(rl.DOC_SYMBOL_ANCHOR_DIRS))

    def test_root_without_the_docs_tree_is_silent(self):
        """🔴 刻意留的洞，寫成測試免得下次有人以為它是 bug。

        hook 的 root 由 CLAUDE_PROJECT_DIR 決定，可以指到任何地方；對「連 docs/
        都沒有」的樹報違規，會讓 hook 對每一個 .py 編輯都吐 block。代價是：整個
        `docs/` 被刪掉時本 check 沉默（那個情境由 pattern-doc-registry 大聲紅）。
        """
        shutil.rmtree(os.path.join(self.root, "docs"))
        self.assertEqual(self.run_check(), [])


# --------------------------------------------------------- 符號抽取

class TestSymbolExtraction(SyntheticCase):
    def _syms(self, rel, text):
        _write(self.root, rel, text)
        names, err = rl._anchor_file_symbols(rel, os.path.join(self.root, rel))
        self.assertIsNone(err, err)
        return names

    def test_python_forms(self):
        names = self._syms("src/models/x.py",
                           "from a.b import imported_name\n"
                           "import pkg.mod\n"
                           "MODULE_CONST = 1\n\n"
                           "class Thing:\n"
                           "    field_name = fields.Char()\n"
                           "    async def amethod(self):\n"
                           "        self.attr_name = 1\n"
                           "        def nested():\n"
                           "            pass\n")
        for want in ("imported_name", "pkg", "MODULE_CONST", "Thing",
                     "field_name", "amethod", "attr_name", "nested"):
            self.assertIn(want, names, want)
        self.assertNotIn("fields", names, "呼叫的對象不是這個檔宣告的符號")

    def test_python_syntax_error_is_reported_not_swallowed(self):
        _write(self.root, "src/models/bad.py", "def (:\n")
        names, err = rl._anchor_file_symbols(
            "src/models/bad.py", os.path.join(self.root, "src/models/bad.py"))
        self.assertIsNone(names)
        self.assertIn("語法錯誤", err)

    def test_js_ignores_comments_and_strings(self):
        names = self._syms(
            "src/static/x.js",
            "// mentionedInComment\n"
            "const label = \"mentionedInString\";\n"
            "export class C {\n    realMethod() { return 1; }\n}\n")
        self.assertIn("realMethod", names)
        self.assertIn("label", names)
        self.assertNotIn("mentionedInComment", names)
        self.assertNotIn("mentionedInString", names)

    def test_js_anchor_end_to_end(self):
        _write(self.root, "src/static/x.js",
               "export class C {\n    saveThing() { return 1; }\n}\n")
        self.doc("# t\n\n`src/static/x.js:saveThing`\n", "js_ok.md")
        self.assertEqual(self.run_check(), [])
        self.doc("# t\n\n`src/static/x.js:vanishedThing`\n", "js_bad.md")
        self.assertTrue(self.run_check())


# --------------------------------------------------------- 錨點抽取的邊界

class TestAnchorParsing(unittest.TestCase):
    def _pairs(self, text):
        return [(p, s) for _l, p, s in rl._doc_symbol_anchors(text)]

    def test_line_number_pointers_are_not_anchors(self):
        for text in ("`a/b.py:581`", "`a/b.py:43-61`", "`a/b.py:1375, 1454`",
                     "`a/b.py:81 _ensure_namespace`", "`helm.sh/resource-policy: keep`",
                     "`x.yml:14,26`", "`x.md:2061`"):
            self.assertEqual(self._pairs(text), [], text)

    def test_symbol_forms(self):
        self.assertEqual(self._pairs("`a/b.py:foo`"), [("a/b.py", "foo")])
        self.assertEqual(self._pairs("`b.js:onClick`"), [("b.js", "onClick")])
        self.assertEqual(self._pairs("`t.py:Cls.method`"), [("t.py", "Cls.method")])
        # 尾隨的 () 不屬於符號名
        self.assertEqual(self._pairs("`a.py:foo()`"), [("a.py", "foo")])

    def test_backticks_are_convention_not_trigger(self):
        """拿掉 backtick 不該讓 gate 靜默——backtick 只是排版。"""
        self.assertEqual(self._pairs("見 a/b.py:foo 這個位置"), [("a/b.py", "foo")])

    def test_line_numbers_are_one_based_and_after_fences(self):
        text = "l1\n```\nl3 `x.py:a`\n```\nl5 `y.py:b`\n"
        self.assertEqual(rl._doc_symbol_anchors(text), [(5, "y.py", "b")])


# --------------------------------------------------------- hook 分派（#596 形態）

class TestHookDispatch(MirrorCase):
    """漏掛 run_hook 分派＝CI 會抓、hook 完全沉默，而兩者的輸出一模一樣。

    三類輸入都要接住：scope 內的 doc、src 的 .py、以及**不匹配任何既有分支**的
    extra/paas-operator/**（doc-symbol-anchor 之前那條 fallback 只認 ladder／
    availability）。
    """

    def _hook(self, rel):
        env = {"CLAUDE_PROJECT_DIR": self.root, "PYTHONIOENCODING": "utf-8"}
        if os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        proc = subprocess.run(
            [sys.executable, os.path.join(self.root, "scripts/audit/repo-lint.py"),
             "--hook"],
            input=json.dumps({"tool_input": {
                "file_path": os.path.join(self.root, rel)}}),
            capture_output=True, text=True, env=env, cwd=self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)  # hook 永遠 exit 0
        return proc.stdout.strip()

    def _break_a_doc(self):
        live = _PY_LIVE[0]
        dead = _dead_symbol_for(self.root, live.target, live.symbol)
        self._append_to_doc(live.doc, "\n\n`%s:%s`\n" % (live.target, dead))
        return live, dead

    def test_clean_repo_does_not_block(self):
        self.assertEqual(self._hook(_PY_LIVE[0].doc), "")

    def test_editing_the_broken_doc_blocks(self):
        live, dead = self._break_a_doc()
        payload = json.loads(self._hook(live.doc) or "{}")
        self.assertEqual(payload.get("decision"), "block",
                         "編輯了寫壞的 doc，hook 完全沉默")
        self.assertIn(dead, payload["reason"])

    def test_editing_referenced_python_blocks(self):
        """改 code 也要接住——壞掉的是**另一個檔**（doc），不是你剛編輯的這一個。"""
        live, dead = self._break_a_doc()
        payload = json.loads(self._hook(live.target) or "{}")
        self.assertEqual(payload.get("decision"), "block")
        self.assertIn(dead, payload["reason"])

    def test_editing_pitfalls_doc_blocks(self):
        """docs/tracking/pitfalls/** 底下**沒有**任何既有分支會接到它。"""
        live, dead = self._break_a_doc()
        pitfall = next(a.doc for a in _LIVE
                       if a.doc.startswith("docs/tracking/pitfalls/"))
        payload = json.loads(self._hook(pitfall) or "{}")
        self.assertEqual(payload.get("decision"), "block")
        self.assertIn(dead, payload["reason"])

    def test_editing_operator_python_blocks(self):
        """extra/paas-operator/** 同樣不匹配任何分支，靠 fallback 接住。"""
        live, dead = self._break_a_doc()
        operator_file = "extra/paas-operator/src/services/helm.py"
        self.assertTrue(os.path.isfile(os.path.join(self.root, operator_file)))
        payload = json.loads(self._hook(operator_file) or "{}")
        self.assertEqual(payload.get("decision"), "block")
        self.assertIn(dead, payload["reason"])


# --------------------------------------------------------- 鑑別力自檢

class TestGateIsTheSourceOfRed(MirrorCase):
    """把 check 函式體換成 `return []`，同一份被注入的 repo 必須變綠。

    這是 mutation 自檢的常駐版：證明上面那些「注入後變紅」的紅色**確實來自這條
    check**，而不是碰巧被別條 check 一起抓到。不變綠就代表這些測試沒有鑑別力。
    """

    _DEF_RE = re.compile(r"^def check_doc_symbol_anchor\([^)]*\):\n", re.M)

    def _gate_with_neutered_check(self):
        m = self._DEF_RE.search(_REAL_GATE_SRC)
        self.assertIsNotNone(m, "推導不出 check_doc_symbol_anchor 的 def 行——突變"
                                "工具失效，不是測試通過")
        return (_REAL_GATE_SRC[:m.end()] + "    return []  # MUTATED\n"
                + _REAL_GATE_SRC[m.end():])

    def _run_all(self, gate_src=None):
        path = os.path.join(self.root, "scripts/audit/repo-lint.py")
        if gate_src is not None:
            original = _read(path)
            self.addCleanup(lambda: _write_text(path, original))
            _write_text(path, gate_src)
        env = {"CLAUDE_PROJECT_DIR": self.root, "PYTHONIOENCODING": "utf-8"}
        if os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        return subprocess.run([sys.executable, path, "--all"],
                              capture_output=True, text=True, env=env, cwd=self.root)

    def test_neutering_the_check_turns_the_injected_repo_green(self):
        live = _PY_LIVE[0]
        dead = _dead_symbol_for(self.root, live.target, live.symbol)
        self._append_to_doc(live.doc, "\n\n`%s:%s`\n" % (live.target, dead))

        armed = self._run_all()
        self.assertEqual(armed.returncode, 1, armed.stdout)
        self.assertIn(dead, armed.stdout)

        neutered = self._run_all(self._gate_with_neutered_check())
        self.assertEqual(neutered.returncode, 0,
                         "拔掉 check_doc_symbol_anchor 之後仍然是紅的 ⇒ 上面那些紅"
                         "不是這條 check 造成的，測試沒有鑑別力：\n" + neutered.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""repo-lint 規則登記表的**不可下降 floor** 測試（#613）。

`check_repo_lint_docstring_registry` 守的是**一致性**，不是**存在性**：把某條 check 的
四處（檔頭條目／`REPO_LINT_RULES`／`run_all()`／`run_hook()` 分派）**一起刪掉**，四處
仍然完全一致 ⇒ `--all` 全綠，守線就這樣消失，沒有任何訊號。連整條
`check_repo_lint_docstring_registry` 自己刪掉也是綠的。

實測過（develop 0c64397c，未加 floor 之前）：

    刪掉 `conceal-instances` 的全部四處            → --all exit=0（通過訊息少一個名字）
    刪掉 `repo-lint-docstring-registry` 的全部四處 → --all exit=0

floor 的形狀（比照 `baselines/seed-topology.txt`）：baseline 檔登記**規則名與 check
函式名**，現況必須是它的超集。少了任何一個就紅、而且點名是哪一個；要調降必須顯式跑
`--update-baseline`，那個動作會出現在 diff 裡被覆核。

🔴 **為什麼記名字而不是記數量**：issue 只要求「數量低於 baseline 即 fail」，但純數量
的 floor 對「刪掉 A、同時新增 B」完全沉默（總數不變）。記名字是嚴格更強的判準，而且
違規時說得出**是哪一條不見了**——「差幾個」是一個沒法行動的訊號
（memory: `feedback_completeness_check_output_which_not_how_many`）。

🔴 **為什麼 floor 直接寫在 `run_all()`、而不是做成第 21 條 check**：#613 的驗收之一
是「把 `check_repo_lint_docstring_registry` 自己的四處一起刪掉必須紅」。任何寫在
`check_*` 裡的 floor 都會跟著那條 check 一起被刪掉，然後靜默恆綠——那是本 issue 要修的
形態換一個載體重演。`run_all()` 是這支工具的入口，刪掉它工具就不存在了。
反向的保護（有人只刪 floor）由 `check_repo_lint_docstring_registry` 的第 (6) 條負責，
兩者互相守著對方。

跑法：
    python3 scripts/audit/test_repo_lint_registry_floor.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部依賴
——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo，也不依賴 cwd。
"""
import ast
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_SELF_REL = "scripts/audit/repo-lint.py"
_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")

_spec = importlib.util.spec_from_file_location("repo_lint_floor_ut", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)

_REAL_SRC = _GATE_PATH.read_text(encoding="utf-8")
_DOC_END = '\n"""\n'


# ------------------------------------------------------------ 原始碼切片

def _line_starts(data):
    starts, off = [0], 0
    for line in data.split(b"\n"):
        off += len(line) + 1
        starts.append(off)
    return starts


def _replace_spans(src, items):
    """把 [(ast 節點, 新文字)] 逐一置換回原始碼（`col_offset` 是 utf-8 byte offset）。"""
    data = src.encode("utf-8")
    starts = _line_starts(data)
    spans = [(starts[n.lineno - 1] + n.col_offset,
              starts[n.end_lineno - 1] + n.end_col_offset, t) for n, t in items]
    for lo, hi, text in sorted(spans, reverse=True):
        data = data[:lo] + text.encode("utf-8") + data[hi:]
    return data.decode("utf-8")


def _func(tree, name):
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, "找不到 %s()——**注入工具失效，不是測試通過**" % name
    return fn


# ------------------------------------------------------------ 注入工具

class InjectionFailed(AssertionError):
    """注入沒有真的發生。**這是失敗，不是「測試通過」。**"""


def drop_rule(src, rule):
    """把一條規則的**全部四處**一起刪掉（＝#613 描述的那個假綠場景）。

    四處：檔頭編號條目（含續行，並把後面重新編號）／`REPO_LINT_RULES` 的該項／
    `run_all()` 的呼叫／`run_hook()` 的分派；順帶刪掉函式定義本身，否則會撞到既有的
    (4a)「定義了卻沒進 run_all」而紅在別的理由上。

    🔴 錨點全部從 AST／被測模組自己的 regex 推導，不寫死任何當下值。呼叫／分派一律
    以 **AST 節點 span** 置換成 `[]`，不用行為基礎的 `str.replace`：`anchors =
    (check_doc_symbol_anchor(root)\\n  if ... else [])` 這種跨行三元式，行為基礎的
    regex 會靜默改不到（找不到就原樣回傳 ⇒ 注入沒發生而測試照樣「通過」）。
    """
    # 🔴 規則→函式的對照從**正在被突變的 src** 讀，不從已載入的模組讀：後者是本 repo
    #    當下的樣子，對「先注入一條新規則、再把它刪掉」這種疊加突變會查無此鍵而誤報
    #    「注入工具失效」。錨點一律取自被突變的那份真相源。
    rules = rl._repo_lint_module_literal(ast.parse(src), "REPO_LINT_RULES")
    if not isinstance(rules, dict):
        raise InjectionFailed("讀不到被突變原始碼的 REPO_LINT_RULES——注入工具失效")
    fns = rules.get(rule)
    if not fns:
        raise InjectionFailed("REPO_LINT_RULES 裡沒有規則 %r——注入工具失效" % rule)

    # (1) 檔頭條目：整條刪掉（含續行），再把剩下的重新編號 1..M-1。
    head, sep, tail = src.partition(_DOC_END)
    if not sep:
        raise InjectionFailed("找不到 module docstring 的結尾——注入工具失效")
    entries = list(rl._REPO_LINT_ENTRY_RE.finditer(head))
    index = next((i for i, m in enumerate(entries) if m.group(2) == rule), None)
    if index is None:
        raise InjectionFailed("檔頭找不到條目 %r——注入工具失效" % rule)
    if index + 1 < len(entries):
        end = entries[index + 1].start()
    else:
        tail_marker = head.find("\n用法：")
        if tail_marker < 0:
            raise InjectionFailed("檔頭找不到「用法：」段——尾條目的邊界推導不出來")
        end = tail_marker + 1
    head = head[:entries[index].start()] + head[end:]
    counter = [0]

    def _renumber(match):
        counter[0] += 1
        return "%d.%s" % (counter[0], match.group(2))

    head = rl._REPO_LINT_ENTRY_RE.sub(
        lambda m: _renumber(re.match(r"^(\d+)\.([ \t]+[a-z0-9][a-z0-9-]*)", m.group(0))),
        head)

    # (2) 兩個計數宣告各自扣掉。
    def _dec(regex, delta):
        match = regex.search(head)
        if not match:
            raise InjectionFailed("檔頭找不到計數宣告（%s）——注入工具失效" % regex.pattern)
        value = rl._cn_int(match.group(1))
        if value is None:
            raise InjectionFailed("計數宣告看不懂——注入工具失效")
        return head[:match.start(1)] + str(value - delta) + head[match.end(1):]

    head = _dec(rl._REPO_LINT_RULE_COUNT_RE, 1)
    head = _dec(rl._REPO_LINT_FUNC_COUNT_RE, len(fns))
    src = head + sep + tail

    # (3) REPO_LINT_RULES 的該項：把鍵改成哨兵、再整行刪掉。
    tree = ast.parse(src)
    mapping = next((n.value for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == "REPO_LINT_RULES"
                            for t in n.targets)), None)
    if not isinstance(mapping, ast.Dict):
        raise InjectionFailed("讀不到 REPO_LINT_RULES 的 dict literal——注入工具失效")
    pair = next(((k, v) for k, v in zip(mapping.keys, mapping.values)
                 if getattr(k, "value", None) == rule), None)
    if pair is None:
        raise InjectionFailed("REPO_LINT_RULES 的 literal 裡沒有 %r——注入工具失效" % rule)
    src = _replace_spans(src, [(pair[0], '"__drop__"'), (pair[1], "()")])
    src = "".join(line for line in src.splitlines(True)
                  if '"__drop__": ()' not in line)

    # (4) run_all()／run_hook() 的呼叫：AST span → `[]`（語意上等同拿掉那一項，
    #     而且對 `x = (f(root) if cond else [])` 這種形狀一樣改得動）。
    for holder in ("run_all", "run_hook"):
        tree = ast.parse(src)
        calls = [n for n in ast.walk(_func(tree, holder))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id in fns]
        if not calls:
            raise InjectionFailed("%s() 裡找不到 %s 的呼叫——注入工具失效"
                                  % (holder, "／".join(fns)))
        src = _replace_spans(src, [(c, "[]") for c in calls])

    # (5) 函式定義本身（由後往前刪，行號才不會位移）。
    tree = ast.parse(src)
    defs = sorted((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name in fns),
                  key=lambda n: n.lineno, reverse=True)
    if len(defs) != len(fns):
        raise InjectionFailed("module 層只找到 %d/%d 個要刪的函式定義——注入工具失效"
                              % (len(defs), len(fns)))
    lines = src.splitlines(True)
    for node in defs:
        del lines[node.lineno - 1:node.end_lineno]
    src = "".join(lines)

    _assert_rule_is_gone(src, rule, fns)
    return src


def _assert_rule_is_gone(src, rule, fns):
    """🔴 注入的語意自檢：四處**都**真的不見了，而且剩下的四處仍然彼此一致。

    少了這一層，任何一處沒改到都會讓「注入沒發生」偽裝成「floor 正確」。最後一句
    （`check_repo_lint_docstring_registry` 必須是綠的）是整組測試的**陽性對照**：
    它證明注入產生的正是 #613 描述的場景——**四處一致、整條消失**——而不是一份改壞的
    原始碼。少了它，floor 的紅可能只是「檔頭與實作對不上」，那條線本來就守得住。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise InjectionFailed("注入產生了語法錯誤：%s" % exc)
    rules = rl._repo_lint_module_literal(tree, "REPO_LINT_RULES")
    if not isinstance(rules, dict) or rule in rules:
        raise InjectionFailed("REPO_LINT_RULES 裡還有 %r——注入沒生效" % rule)
    doc = ast.get_docstring(tree) or ""
    if rule in [name for _, name in rl._REPO_LINT_ENTRY_RE.findall(doc)]:
        raise InjectionFailed("檔頭還有 %r 的條目——注入沒生效" % rule)
    for holder in ("run_all", "run_hook"):
        left = set(rl._repo_lint_check_calls(_func(tree, holder))) & set(fns)
        if left:
            raise InjectionFailed("%s() 裡還留著 %s——注入沒生效"
                                  % (holder, "／".join(sorted(left))))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if defined & set(fns):
        raise InjectionFailed("module 層還定義著 %s——注入沒生效"
                              % "／".join(sorted(defined & set(fns))))
    with _TmpRoot(src) as root:
        residual = rl.check_repo_lint_docstring_registry(root)
    if residual:
        raise InjectionFailed(
            "注入後的原始碼被既有的檔頭對帳 gate 判為不一致——那不是 #613 的場景"
            "（#613 是**四處一致而整條消失**），floor 的紅會歸因錯誤：\n%s"
            % "\n".join("  ✗ " + v for v in residual))


def _insert_before_main_guard(src, text):
    """把新的 module 層定義插在 `if __name__ == "__main__":` 之前。

    🔴 直接 `src += ...` 會把定義接在那道 guard **之後**——AST 層的檢查全部看得到它
    （所以 in-process 的測試會綠），但腳本真的被執行時 `main()` 已經跑完了 ⇒
    `NameError`。實測撞到：`--all` 子行程對這種 mutant 回 exit=1，而那個紅與「floor
    誤擋新 check」在 exit code 上完全相同——特異性的結論會反過來。
    錨點從 AST 推導（找 `__name__` 那道 guard），並在插完之後自檢順序。
    """
    tree = ast.parse(src)
    guard = next((n for n in tree.body if isinstance(n, ast.If)
                  and any(isinstance(x, ast.Name) and x.id == "__name__"
                          for x in ast.walk(n.test))), None)
    if guard is None:
        raise InjectionFailed('找不到 `if __name__ == "__main__":` guard——注入工具失效')
    lines = src.splitlines(True)
    out = "".join(lines[:guard.lineno - 1]) + text + "".join(lines[guard.lineno - 1:])
    after = ast.parse(out)
    new_guard = next(n for n in after.body if isinstance(n, ast.If)
                     and any(isinstance(x, ast.Name) and x.id == "__name__"
                             for x in ast.walk(n.test)))
    added = [n for n in after.body if isinstance(n, ast.FunctionDef)
             and n.lineno >= guard.lineno]
    if not added or min(n.lineno for n in added) > new_guard.lineno:
        raise InjectionFailed("新定義沒有插在 __main__ guard 之前——mutant 不是可執行"
                              "的程式，特異性的結論會反過來")
    return out


def add_rule(src, rule, fn):
    """在四處都正確登記一條新 check（特異性用：floor 不得誤擋）。"""
    tree = ast.parse(src)
    run_all = _func(tree, "run_all")
    calls = sorted((n for n in ast.walk(run_all)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id.startswith("check_")),
                   key=lambda n: (n.lineno, n.col_offset))
    if not calls:
        raise InjectionFailed("run_all() 裡一個 check 呼叫都沒有——注入工具失效")
    last = calls[-1]
    out = _replace_spans(src, [(last, "%s\n                  + %s(root)"
                                % (ast.get_source_segment(src, last), fn))])
    if out == src:
        raise InjectionFailed("run_all 的尾呼叫沒被改動——注入工具失效")

    tree = ast.parse(out)
    mapping = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", None) == "REPO_LINT_RULES" for t in n.targets))
    before = out
    out = _replace_spans(out, [(mapping, "%s\n" % ast.get_source_segment(out, mapping)[:-1]
                                + '    "%s": ("%s",),\n}' % (rule, fn))])
    if out == before:
        raise InjectionFailed("REPO_LINT_RULES 沒被改動——注入工具失效")

    tree = ast.parse(out)
    chain = next(n for n in ast.walk(_func(tree, "run_hook")) if isinstance(n, ast.If)
                 and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                         and c.func.id.startswith("check_") for c in ast.walk(n)))
    before = out
    out = _replace_spans(out, [(chain, 'if re.match(r"^src/models/.*\\.py$", rel):\n'
                                "            violations = %s(root)\n        el" % fn
                                + ast.get_source_segment(out, chain))])
    if out == before:
        raise InjectionFailed("run_hook 的分派鏈頭沒被改動——注入工具失效")

    out = _insert_before_main_guard(
        out, "\ndef %s(root, only_file=None):\n    return []\n\n\n" % fn)

    # 檔頭：補條目（編號接續）＋兩個計數各 +1。
    head, sep, tail = out.partition(_DOC_END)
    entries = list(rl._REPO_LINT_ENTRY_RE.finditer(head))
    marker = head.find("\n用法：")
    head = (head[:marker + 1] + "%d. %s  測試用的新條目。\n"
            % (int(entries[-1].group(1)) + 1, rule) + head[marker + 1:])
    for regex in (rl._REPO_LINT_RULE_COUNT_RE, rl._REPO_LINT_FUNC_COUNT_RE):
        match = regex.search(head)
        head = head[:match.start(1)] + str(rl._cn_int(match.group(1)) + 1) + head[match.end(1):]
    out = head + sep + tail

    with _TmpRoot(out) as root:
        residual = rl.check_repo_lint_docstring_registry(root)
    if residual:
        raise InjectionFailed(
            "新增一條 check 的注入本身就不一致——特異性測試的前提壞掉了：\n%s"
            % "\n".join("  ✗ " + v for v in residual))
    return out


# ------------------------------------------------------------ tmp root

class _TmpRoot(object):
    """把（可能突變過的）repo-lint.py ＋ 真實 baseline 檔擺成一個最小 root。"""

    def __init__(self, src, baseline=None):
        self.src = src
        self.baseline = baseline

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        os.makedirs(os.path.join(root, "scripts", "audit", "baselines"))
        with open(os.path.join(root, _SELF_REL), "w", encoding="utf-8") as f:
            f.write(self.src)
        real = os.path.join(_REPO_ROOT, rl.REPO_LINT_REGISTRY_BASELINE_REL)
        target = os.path.join(root, rl.REPO_LINT_REGISTRY_BASELINE_REL)
        if self.baseline is None:
            shutil.copy(real, target)
        elif self.baseline is not False:
            with open(target, "w", encoding="utf-8") as f:
                f.write(self.baseline)
        return root

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


def floor(src, baseline=None):
    with _TmpRoot(src, baseline) as root:
        return rl.repo_lint_registry_floor(root)


def mentions(violations, *needles):
    return [v for v in violations if all(n in v for n in needles)]


# ------------------------------------------------------------ 陰性對照

class TestBaselineIsClean(unittest.TestCase):
    """未注入時綠——而且綠得有前提。"""

    def test_real_repo_is_green(self):
        self.assertEqual(rl.repo_lint_registry_floor(_REPO_ROOT), [])

    def test_unmutated_copy_is_green(self):
        # 比對實驗要固定 recipe：同一份內容複製到別的 root 也必須綠，否則下面每個
        # 「注入後變紅」都可能是複製本身弄紅的。
        self.assertEqual(floor(_REAL_SRC), [])

    def test_baseline_file_is_not_empty(self):
        # 「零違規」若來自空的 baseline 就是假綠——那正是這條 floor 要防的形態。
        recorded = rl.read_baseline_pairs(
            os.path.join(_REPO_ROOT, rl.REPO_LINT_REGISTRY_BASELINE_REL))
        self.assertGreaterEqual(len(recorded), 20, "baseline 內容太少——探針失去鑑別力")
        self.assertTrue(any(k.startswith("rule:") for k in recorded))
        self.assertTrue(any(k.startswith("fn:") for k in recorded))

    def test_baseline_is_a_subset_of_reality(self):
        # baseline 裡的每一個名字今天都必須真的存在（floor 的判準是 ⊆，所以 baseline
        # 若登記了不存在的東西，會恆紅到有人把它刪掉為止——這裡先把它釘住）。
        names = rl.repo_lint_registry_names(_REPO_ROOT)
        self.assertIsNotNone(names, "讀不到現況登記表——探針失效")
        rules, fns = names
        current = {"rule:" + r for r in rules} | {"fn:" + f for f in fns}
        recorded = set(rl.read_baseline_pairs(
            os.path.join(_REPO_ROOT, rl.REPO_LINT_REGISTRY_BASELINE_REL)))
        self.assertEqual(sorted(recorded - current), [])

    def test_update_baseline_reproduces_the_committed_file(self):
        # baseline 必須是 `--update-baseline` 的產物，不是手抄的。手抄版的下場見
        # run_all() 通過訊息那段註解（同一支工具三個數字）。
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(os.path.join(_REPO_ROOT, "scripts", "audit"),
                            os.path.join(tmp, "scripts", "audit"))
            for name in ("src", "docs", ".claude"):
                os.makedirs(os.path.join(tmp, name), exist_ok=True)
            proc = subprocess.run(
                [sys.executable, os.path.join(tmp, _SELF_REL), "--update-baseline"],
                capture_output=True, text=True, cwd=tmp,
                env={**os.environ, "CLAUDE_PROJECT_DIR": tmp})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            regenerated = pathlib.Path(
                tmp, rl.REPO_LINT_REGISTRY_BASELINE_REL).read_text(encoding="utf-8")
        committed = pathlib.Path(
            _REPO_ROOT, rl.REPO_LINT_REGISTRY_BASELINE_REL).read_text(encoding="utf-8")
        self.assertEqual(regenerated, committed,
                         "committed 的 baseline 與 --update-baseline 重產的不同——"
                         "它被手改過，或產生器與讀取器對格式的認知已經分岔")


# ------------------------------------------------------------ 敏感度（#613 驗收 1／2）

class TestDroppingAllFourPlacesIsRed(unittest.TestCase):
    """把一條 check 的四處一起刪掉 ⇒ 必須紅，而且點名是哪一條。"""

    def test_every_rule_is_floored(self):
        # 對**每一條**規則各跑一次，不是只挑一條示範——假綠洞從來不會挑在被示範的
        # 那一條上。
        rules = list(rl.REPO_LINT_RULES)
        self.assertGreaterEqual(len(rules), 10, "規則太少——探針失效")
        for rule in rules:
            with self.subTest(rule=rule):
                violations = floor(drop_rule(_REAL_SRC, rule))   # 自帶語意自檢
                self.assertTrue(
                    mentions(violations, "rule", rule),
                    "刪掉 `%s` 的全部四處之後 floor 仍然是綠的——守線靜靜消失了。"
                    "實際違規：%s" % (rule, violations))
                for fn in rl.REPO_LINT_RULES[rule]:
                    self.assertTrue(mentions(violations, fn),
                                    "違規沒有點名消失的 check 函式 %s" % fn)

    def test_dropping_the_docstring_gate_itself_is_red(self):
        """#613 驗收 2：連 `check_repo_lint_docstring_registry` 自己都守得住。

        這是 floor 為什麼**不能**寫在某條 check 裡的證明：寫在裡面的話，這一刀會把
        floor 一起刪掉，然後靜默恆綠。
        """
        rule = "repo-lint-docstring-registry"
        violations = floor(drop_rule(_REAL_SRC, rule))
        self.assertTrue(mentions(violations, rule),
                        "刪掉檔頭對帳 gate 自己的四處之後 floor 沒紅：%s" % violations)
        self.assertTrue(mentions(violations, "check_repo_lint_docstring_registry"))

    def test_the_dropped_source_passes_the_consistency_gate(self):
        """陽性對照（把注入的性質釘死）：被注入的原始碼**四處是一致的**。

        `drop_rule` 內部已經自檢過一次，這裡把它提到測試層明講：#613 的洞是
        「一致但不存在」，若注入產生的是「不一致」，那條線本來就守得住，floor 的紅
        就歸因錯誤、而且證明不了任何事。
        """
        src = drop_rule(_REAL_SRC, "owl-template")
        with _TmpRoot(src) as root:
            self.assertEqual(rl.check_repo_lint_docstring_registry(root), [],
                             "注入後的原始碼並非「四處一致」——對照組不成立")
            self.assertNotEqual(rl.repo_lint_registry_floor(root), [],
                                "同一份原始碼：一致性 gate 綠、floor 也綠 ＝ #613 的洞還在")


# ------------------------------------------------------------ 特異性（#613 驗收 3）

class TestSpecificity(unittest.TestCase):
    def test_adding_a_new_rule_stays_green(self):
        """正常新增一條 check（四處都登記）⇒ 必須維持綠，floor 不得誤擋。

        floor 的判準刻意是 **baseline ⊆ 現況**（不是相等）：相等會讓每一次合法新增都
        被擋到有人跑 `--update-baseline` 為止，而一條會擋住正當工作的 gate 活不久。
        代價寫在下一支測試裡（新增的東西在 baseline 更新前不受 floor 保護）。
        """
        src = add_rule(_REAL_SRC, "brand-new-thing", "check_brand_new_thing")
        self.assertEqual(floor(src), [], "四處都登記好的新 check 被 floor 誤擋")

    def test_the_known_gap_of_a_subset_floor(self):
        """把「⊆ 判準的已知缺口」寫成可執行的斷言，而不是散文。

        新增但還沒進 baseline 的規則，刪掉時 floor 不會紅（它不在 baseline 裡）。
        這是 ⊆ 判準的必然代價，不是 bug——但它必須是**被記錄下來的**已知行為，否則
        下一個人會把這個缺口當成 floor 的保證。補法是新增 check 後跑
        `--update-baseline`（產生的 diff 會被覆核）。
        """
        added = add_rule(_REAL_SRC, "brand-new-thing", "check_brand_new_thing")
        self.assertEqual(floor(added), [])
        # 同一份 baseline 下把它再刪掉——floor 沉默，因為 baseline 沒登記過它。
        self.assertEqual(floor(drop_rule(added, "brand-new-thing")), [])
        # 但同一份原始碼裡刪掉一條**baseline 有登記**的規則，floor 立刻紅。
        # 這一句是肯定性的：它證明上面的「沉默」來自 baseline 沒登記，
        # 而不是 floor 對這份原始碼整個失效。
        self.assertTrue(floor(drop_rule(added, "owl-template")))


# ------------------------------------------------------------ fail-loud

class TestFailLoud(unittest.TestCase):
    """探針壞掉必須是紅的。floor 寫成 fail-open ＝永久沉默且看起來永遠是綠的。"""

    def test_missing_baseline_file(self):
        violations = floor(_REAL_SRC, baseline=False)
        self.assertTrue(mentions(violations, "讀不到"), violations)

    def test_empty_baseline_file(self):
        violations = floor(_REAL_SRC, baseline="# 只有註解，沒有任何登記\n")
        self.assertTrue(mentions(violations, "沒有任何登記"), violations)

    def test_unparseable_source(self):
        violations = floor("def broken(:\n    pass\n")
        self.assertTrue(mentions(violations, "讀不到或 parse 失敗"), violations)

    def test_mapping_table_renamed(self):
        src = _REAL_SRC.replace("REPO_LINT_RULES = {", "REPO_LINT_RULES_OLD = {", 1)
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效——映射表的宣告變了")
        self.assertTrue(mentions(floor(src), "讀不到或 parse 失敗"), floor(src))

    def test_missing_source_file(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "scripts", "audit", "baselines"))
            shutil.copy(os.path.join(_REPO_ROOT, rl.REPO_LINT_REGISTRY_BASELINE_REL),
                        os.path.join(root, rl.REPO_LINT_REGISTRY_BASELINE_REL))
            self.assertTrue(mentions(rl.repo_lint_registry_floor(root), "讀不到"))


# --------------------------------------------------- floor 自己的保護（互相守著）

class TestFloorIsItselfGuarded(unittest.TestCase):
    """有人只把 floor 拿掉時，`check_repo_lint_docstring_registry` 必須紅。

    floor 守 docstring gate 的存在、docstring gate 守 floor 的存在——刪掉任何一個
    都會紅。兩個一起刪才會綠，而那是一個大到瞞不住的 diff。這條互相保護是本設計的
    不動點，寫下來是為了讓下一個人知道它是刻意的，不是巧合。
    """

    def _drop_floor_call(self, holder):
        tree = ast.parse(_REAL_SRC)
        calls = [n for n in ast.walk(_func(tree, holder))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == rl.REPO_LINT_FLOOR_FN]
        if not calls:
            raise InjectionFailed("%s() 裡找不到 %s 的呼叫——注入工具失效"
                                  % (holder, rl.REPO_LINT_FLOOR_FN))
        src = _replace_spans(_REAL_SRC, [(c, "[]") for c in calls])
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效")
        return src

    def test_removing_the_floor_from_run_all_is_red(self):
        with _TmpRoot(self._drop_floor_call("run_all")) as root:
            violations = rl.check_repo_lint_docstring_registry(root)
        self.assertTrue(mentions(violations, rl.REPO_LINT_FLOOR_FN, "run_all"),
                        "把 floor 從 run_all() 拿掉之後沒有任何東西會紅：%s" % violations)

    def test_removing_the_floor_from_run_hook_is_red(self):
        with _TmpRoot(self._drop_floor_call("run_hook")) as root:
            violations = rl.check_repo_lint_docstring_registry(root)
        self.assertTrue(mentions(violations, rl.REPO_LINT_FLOOR_FN, "run_hook"),
                        "把 floor 從 run_hook() 拿掉之後沒有任何東西會紅：%s" % violations)

    def test_floor_is_not_a_check_function(self):
        # floor 刻意不叫 `check_*`：叫了就會被拉進檔頭↔映射表↔run_all 的雙射，
        # 變成「四處一起刪就靜默消失」的第 21 個受害者——那正是它要修的形態。
        self.assertFalse(rl.REPO_LINT_FLOOR_FN.startswith("check_"))
        self.assertNotIn(rl.REPO_LINT_FLOOR_FN,
                         [fn for fns in rl.REPO_LINT_RULES.values() for fn in fns])


# ------------------------------------------------------------ hook 模式實走

class TestHookMode(unittest.TestCase):
    """不 grep 原始碼，直接餵 hook payload 給複製過去的那份腳本跑。

    「gate 寫好了但 hook 分派根本到不了它」就是 #596 本體。floor 掛在 hook 上的那條
    分派，只有端對端跑一次才知道接不接得到。
    """

    def _hook(self, src, rel):
        with _TmpRoot(src) as root:
            path = os.path.join(root, rel)
            env = {"CLAUDE_PROJECT_DIR": root, "PYTHONIOENCODING": "utf-8"}
            if os.environ.get("PATH"):
                env["PATH"] = os.environ["PATH"]
            proc = subprocess.run(
                [sys.executable, os.path.join(root, _SELF_REL), "--hook"],
                input=json.dumps({"tool_input": {"file_path": path}}),
                capture_output=True, text=True, env=env, cwd=root)
            self.assertEqual(proc.returncode, 0, proc.stderr)   # hook 永遠 exit 0
            return proc.stdout.strip()

    def test_clean_source_does_not_block(self):
        self.assertEqual(self._hook(_REAL_SRC, _SELF_REL), "")

    def test_dropped_rule_blocks_when_editing_the_script(self):
        out = self._hook(drop_rule(_REAL_SRC, "reuse-values"), _SELF_REL)
        self.assertTrue(out, "hook 對「整條 check 消失」完全沉默——分派沒接到 %s"
                        % _SELF_REL)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("reuse-values", payload["reason"])

    def test_dropped_rule_blocks_when_editing_the_baseline(self):
        # baseline 檔自己也是 floor 的輸入：有人「調降」它時 hook 一樣要接得到。
        out = self._hook(drop_rule(_REAL_SRC, "reuse-values"),
                         rl.REPO_LINT_REGISTRY_BASELINE_REL)
        self.assertTrue(out, "編輯 baseline 檔時 hook 完全沉默——分派沒接到它")
        self.assertEqual(json.loads(out)["decision"], "block")


# ------------------------------------------------------------ 注入工具自身

class TestInjectionToolTripwires(unittest.TestCase):
    """🔴 注入工具的絆線本身要有測試——這是整份測試最容易做假的一環。"""

    def test_unknown_rule_raises_instead_of_noop(self):
        with self.assertRaises(InjectionFailed):
            drop_rule(_REAL_SRC, "no-such-rule")

    def test_partial_drop_is_rejected(self):
        """只刪掉四處中的三處（留下 run_hook 分派）必須被語意自檢擋下。

        這證明 `_assert_rule_is_gone` 是活的，而不是一段沒被執行過的註解。
        """
        rule = "owl-template"
        src = drop_rule(_REAL_SRC, rule)
        fns = rl.REPO_LINT_RULES[rule]
        # 把 run_hook 的分派「還原」回去——四處只刪了三處。
        tree = ast.parse(src)
        chain = next(n for n in ast.walk(_func(tree, "run_hook")) if isinstance(n, ast.If))
        revived = _replace_spans(src, [(chain, 'if %s(root):\n            pass\n        el'
                                        % fns[0] + ast.get_source_segment(src, chain))])
        revived += "\n\ndef %s(root, only_file=None):\n    return []\n" % fns[0]
        with self.assertRaises(InjectionFailed):
            _assert_rule_is_gone(revived, rule, fns)

    def test_semantic_guard_rejects_an_inconsistent_drop(self):
        """只刪掉檔頭條目（其餘三處不動）不是 #613 的場景，必須被擋下。"""
        head, sep, tail = _REAL_SRC.partition(_DOC_END)
        entries = list(rl._REPO_LINT_ENTRY_RE.finditer(head))
        broken = (head[:entries[2].start()] + head[entries[3].start():]) + sep + tail
        self.assertNotEqual(broken, _REAL_SRC, "注入工具失效")
        with self.assertRaises(InjectionFailed):
            _assert_rule_is_gone(broken, entries[2].group(2),
                                 rl.REPO_LINT_RULES[entries[2].group(2)])


if __name__ == "__main__":
    unittest.main(verbosity=2)

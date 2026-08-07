#!/usr/bin/env python3
"""repo-lint hook 分派的**可達性**測試（#612）。

`check_repo_lint_docstring_registry` 的 4b 只問「這個函式名有沒有出現在 `run_hook()`
裡」——那是 ast 層的**存在性**。把某條 check 搬進一個 regex 永遠不匹配的分支（打錯的
path pattern、或被後來新增的分支完全遮蔽），名字照樣在，4b 照樣綠，而 hook 對它已經
死了。這正是 #596 的形態換一個載體：gate 寫好了、看起來掛上了、實際跑不到，而且輸出
與「跑了而且沒事」完全相同。

## 判準（不變式層，不是形態層）

本檔刻意**不**斷言「run_hook 裡有幾個 elif」「分派長什麼樣」——那種判準在對方把
if/elif 改寫成 dict 分派的當天就失效，而失效的方式是靜默的。這裡斷言兩條不變式：

  **不變式 1（每條 check 都到得了）**：`run_all()` 呼叫的每個 `check_*`（扣除
  `REPO_LINT_HOOK_EXEMPT` 具名豁免者），都必須存在**至少一條真實的 repo 檔案路徑**，
  餵進 `run_hook()` 時真的會呼叫到它。

  **不變式 2（沒有死分支）**：`run_hook()` 裡每個通往 check 呼叫的分派條件，都必須
  被至少一條真實 repo 路徑選中。這條比不變式 1 嚴格——`check_slug_capability_gate`
  掛在 .xml 與 .js 兩個分支，遮蔽掉其中一個時它仍然「可達」，不變式 1 抓不到，而
  hook 對整類 .js 編輯已經沉默了。issue 明寫的「某條 pattern 被後來新增的分支完全
  遮蔽」就是這一類。

## 為什麼用真實 repo 路徑（路 B）而不是造 fixture（路 A）

issue 建議每條 check 造一個「會觸發它」的最小 fixture 檔。實查後改走真實路徑：

  * **實查數據**：全 repo 1426 個檔（排除 .git／__pycache__／node_modules／
    .claude/worktrees）掃過去，22 條 check **每一條都被真實檔案觸發到**，16 個分派
    分支**每一個都被選中**。issue 擔心的「某條 check 守的路徑類型在 repo 裡沒有檔案
    ⇒ 誤判成不可達」在本 repo 不存在，所以路 B 沒有誤報成本。
  * **fixture 會與真實佈局脫鉤**：造 `src/controllers/x.py` 只證明 regex 在自己捏的
    形狀上能動；真實路徑會在有人重組目錄（例如把 controllers 搬走）時把測試一起弄紅
    ——那正是該紅的時候。
  * **對「遮蔽」天然敏感**：fixture 是一條路徑對一條 check 的點測，遮蔽是**分支之間**
    的關係，要靠掃全集才看得出哪個分支從頭到尾沒被選中。

  ⚠ 代價（寫下來，不要讓它是隱含的）：**新增一條 check 卻沒有任何真實檔案能觸發它**
  時，本檔會紅。那是刻意的——沒有真實輸入的分派與死分派在 hook 模式下無從分辨。真的
  需要時，把它加進 `REPO_LINT_HOOK_EXEMPT` 並寫下理由（那是具名的、會出現在 diff 裡的
  動作）。

## 探針自身的鑑別力

「壞掉的探針最愛回答『零／空／找不到』」。本檔的探針壞掉時症狀是**全部 check 都不可達**
⇒ 不變式 1 立刻紅，不會偽裝成通過。反向（探針謊報可達）在構造上不可能：recorder 只在
函式**真的被呼叫**時才記錄。除此之外還釘了三道：

  1. `test_instrumentation_preserves_dispatch`：插樁版與原版的可達集合必須逐字相同。
  2. `test_hook_never_swallows_an_exception`：`run_hook()` 有全域 `except: return 0`
     的 fail-open，例外被吞掉時外觀與「分派沒接到」一模一樣。這支把 fail-open 換成
     `raise` 再掃一次全 repo——有任何真實路徑會炸就大聲說出來。
  3. 敏感度與特異性成對：mutation 測試證明每條 check／每個分支被殺掉時本檔會紅，
     `TestBaselineIsClean` 證明未注入時綠。一條恆紅的測試通不過後者，一條恆綠的
     通不過前者。

跑法：
    python3 scripts/audit/test_repo_lint_hook_dispatch.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_hook_dispatch.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部依賴
——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo、git 執行檔，也不依賴 cwd 與
`CLAUDE_PROJECT_DIR`（`repo_root()` 一律被覆寫成本檔所在的 checkout，否則環境變數
指到另一份 repo 時整批結果會靜默錯位）。
"""
import ast
import collections
import importlib.util
import io
import json
import os
import pathlib
import sys
import unittest

_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")
_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
_REAL_SRC = _GATE_PATH.read_text(encoding="utf-8")

# 掃描時排除的目錄。`.claude/worktrees/` 是本 repo 的**複本**——不排掉會把同一份佈局
# 算兩次（而且 rel 前綴是 .claude/…，分派結果與本尊不同），本機與 CI 的結果也會分岔。
_SKIP_DIRS = {".git", "__pycache__", "node_modules"}
_SKIP_PREFIXES = (".claude/worktrees",)

# 掃到的檔案數下限。掉到這個數字以下代表**探針沒走到 repo**（路徑算錯、被排除規則吃
# 掉），那時「每條 check 都不可達」是探針壞了而不是 gate 壞了——先在這裡大聲失敗，
# 免得下游的紅指向錯的方向。刻意寫成遠低於實測值的鬆下限：它要偵測的是量級崩掉
# （0 或個位數），不是正常增減。
_MIN_FILES = 200

# 注入用的「永不匹配」條件，取自 issue #612 的字面建議。
_DEAD_TEST = 're.match(r"^src/nonexistent-xyz/.*\\.py$", rel)'


# ------------------------------------------------------------ 原始碼切片

def _line_starts(data):
    starts, off = [0], 0
    for line in data.split(b"\n"):
        off += len(line) + 1
        starts.append(off)
    return starts


def _replace_spans(src, items):
    """把 [(ast 節點, 新文字)] 逐一置換回原始碼。

    🔴 `col_offset` 是 **utf-8 byte offset**，不是字元 offset。本檔滿是中文註解，用
    字元索引切片會在任何前面有中文的行上錯位——而錯位的結果是語法錯誤或改到別的東西，
    兩者都會被下游的 tripwire 擋下，但那時訊息已經指向錯的地方。所以一律在 bytes 上切。
    由後往前置換，前面的 span 才不會位移。
    """
    data = src.encode("utf-8")
    starts = _line_starts(data)
    spans = []
    for node, text in items:
        lo = starts[node.lineno - 1] + node.col_offset
        hi = starts[node.end_lineno - 1] + node.end_col_offset
        spans.append((lo, hi, text))
    for lo, hi, text in sorted(spans, reverse=True):
        data = data[:lo] + text.encode("utf-8") + data[hi:]
    return data.decode("utf-8")


def _func(tree, name):
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, "找不到 %s()——**探針失效，不是測試通過**" % name
    return fn


def _check_calls(node, name=None):
    """node 底下所有 `check_*(...)` 呼叫節點，**依原始碼順序**。

    `ast.walk` 是 BFS、不保證順序，而下游（尾呼叫錨點、分支去重順序）都依賴順序穩定
    ——不排序的話同一份原始碼在不同 Python 版本上可能給出不同結果。
    """
    calls = [n for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id.startswith("check_")
             and (name is None or n.func.id == name)]
    calls.sort(key=lambda n: (n.lineno, n.col_offset))
    return calls


def _is_early_out(node):
    """分支的 body 恰好只有 `return 0`＝畸形 payload 的早退守衛，不是路徑分派。

    這是不變式 2 的唯一豁免，而且是**結構判定**的（不是寫死行號或條件字面值）：
    `if not file_path: return 0` 與 `if rel.startswith(".."): return 0` 只有在餵進
    畸形／repo 外的路徑時才成立，掃真實檔案永遠不會選中它們。豁免寫成「body 恰為
    return 0」而不是「豁免這兩行」——有人再加一個同型早退時自動涵蓋，改寫成別的形狀
    時則會回到必須被選中的行列。
    """
    body = getattr(node, "body", None)
    if not isinstance(body, list) or len(body) != 1:
        return False
    stmt = body[0]
    return (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant)
            and stmt.value.value == 0)


def _dispatch_branches(src):
    """`run_hook()` 裡所有「通往 check 呼叫」的分派條件，依原始碼順序。

    回傳 [(節點, 人看得懂的標籤)]。`ast.If` 收 if/elif 鏈，`ast.IfExp` 收
    `x = (check_y(root) if <條件> else [])` 這種跨檔不變式的三元守衛——後者一樣是
    分派，漏收等於對整類 check 沒有判準。
    """
    tree = ast.parse(src)
    out = []
    for node in ast.walk(_func(tree, "run_hook")):
        if isinstance(node, (ast.If, ast.IfExp)) and not _is_early_out(node):
            label = "L%d: %s" % (node.lineno, ast.get_source_segment(src, node.test))
            out.append((node, label))
    out.sort(key=lambda item: (item[0].lineno, item[0].col_offset))
    return out


def _dispatch_chain_head(src):
    """路徑分派 if/elif 鏈的**鏈頭**（第一個非早退的 `ast.If` 語句）。

    從 AST 推導而非寫死行號／條件字面值：有人在鏈頭插一條新分支或改寫既有條件時，
    這裡自動跟著走，不會靜默指到別的地方。
    """
    heads = [n for n, _ in _dispatch_branches(src) if isinstance(n, ast.If)]
    assert heads, "run_hook() 裡找不到任何 if/elif 路徑分派——探針失效"
    return heads[0]


# ------------------------------------------------------------ 注入工具

class InjectionFailed(AssertionError):
    """注入沒有真的發生。**這是失敗，不是「測試通過」。**

    2026-08-06／08-07 兩次實證（見 test_repo_lint_docstring_registry.py 的長註解）：
    寫死錨點的注入工具在別人改排版的當天靜默不匹配，`str.replace()` 找不到就原樣
    回傳 ⇒ 測試改為驗證一份未突變的原始碼，而且看起來完全正常。所以本檔的注入一律
    走 AST（錨點從真相源推導，排版怎麼寫都找得到），並在注入後**自檢語意**。
    """


def _branch_of(call, parent):
    """往上找 call 所在的最內層分派條件；找不到回 None（＝無條件執行）。"""
    cur = call
    while cur in parent:
        node = parent[cur]
        if isinstance(node, ast.If) and _in(cur, node.body):
            return node
        if isinstance(node, ast.IfExp) and cur is node.body:
            return node
        cur = node
    return None


def _in(node, body):
    """node 是否在 body 這串語句底下（用 ast.walk 而非身分比對，才吃得到巢狀）。"""
    return any(node is n or any(node is m for m in ast.walk(n)) for n in body)


def _parent_map(root):
    parent = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _governing_branches(src, fn):
    """`run_hook()` 裡每個 `fn(...)` 呼叫點所屬的最內層分派條件（去重、依序）。"""
    tree = ast.parse(src)
    run_hook = _func(tree, "run_hook")
    parent = _parent_map(run_hook)
    calls = _check_calls(run_hook, fn)
    if not calls:
        raise InjectionFailed(
            "run_hook() 裡找不到 %s(...) 的呼叫點——注入工具失效，不是測試通過" % fn)
    branches, seen = [], set()
    for call in calls:
        branch = _branch_of(call, parent)
        if branch is None:
            raise InjectionFailed(
                "%s 在 run_hook() 裡是**無條件**呼叫（不在任何分支底下），"
                "沒有分派條件可以殺——注入工具的假設不成立" % fn)
        key = (branch.lineno, branch.col_offset)
        if key not in seen:
            seen.add(key)
            branches.append(branch)
    return branches


def _kill_branches(src, branches, what):
    """把指定分派條件換成永不匹配的 regex，並自檢注入真的生效。"""
    mutated = _replace_spans(src, [(b.test, _DEAD_TEST) for b in branches])
    if mutated == src:
        raise InjectionFailed("置換後原始碼一字未變（%s）——注入工具失效" % what)
    try:
        ast.parse(mutated)
    except SyntaxError as exc:
        raise InjectionFailed("注入產生了語法錯誤（%s）：%s" % (what, exc))
    return mutated


def kill_dispatch(src, fn):
    """殺掉 `fn` 在 `run_hook()` 的**全部**分派條件。

    自檢（tripwire）分三層，缺一層都會讓「注入沒發生」偽裝成「gate 正確」：

      1. 找不到呼叫點 ／ 呼叫點不在任何分支底下 → `InjectionFailed`（在 `_governing_
         branches` 裡）。
      2. 置換後原始碼未變、或變成語法錯誤 → `InjectionFailed`。
      3. 🔴 **語意自檢**：重新 parse 突變後的原始碼，`fn` 的**每一個**呼叫點所屬的
         分派條件都必須已經是那句永不匹配的 regex。少殺一處時 `fn` 仍然可達，而
         「注入無效」與「gate 正確」在輸出上完全相同——這一層就是專門擋那個。
         （`check_slug_capability_gate` 這類掛在兩個分支的 check 最容易踩到。）
    """
    branches = _governing_branches(src, fn)
    mutated = _kill_branches(src, branches, "殺掉 %s 的分派" % fn)
    dead = ast.dump(ast.parse(_DEAD_TEST, mode="eval").body)
    for branch in _governing_branches(mutated, fn):
        if ast.dump(branch.test) != dead:
            raise InjectionFailed(
                "%s 仍有分派條件沒被殺掉（L%d: %s）——注入沒生效，測試會假綠"
                % (fn, branch.lineno, ast.get_source_segment(mutated, branch.test)))
    return mutated


def kill_one_branch(src, index):
    """只殺第 index 個分派條件（模擬「某條 pattern 被後來的分支完全遮蔽」）。

    🔴 分支的身分一律用**索引**，不用標籤文字：注入之後那個分支的條件就變成永不匹配
    的 regex，標籤自然跟著變，拿突變前的標籤去比對突變後的清單永遠找不到——那會讓
    「gate 抓到了」被誤判成「gate 沒抓到」。索引可以當身分是因為 `_dispatch_branches`
    依原始碼位置排序、而下面的 tripwire 保證注入前後的分支**數量與順序**都沒變
    （只換條件、不動結構）。
    """
    branches = _dispatch_branches(src)
    if not 0 <= index < len(branches):
        raise InjectionFailed("分支索引 %d 超出範圍（共 %d 個）" % (index, len(branches)))
    node, label = branches[index]
    mutated = _kill_branches(src, [node], "殺掉分支 %s" % label)
    after = _dispatch_branches(mutated)
    if len(after) != len(branches):
        raise InjectionFailed(
            "注入前後的分支數不同（%d → %d）——置換改到了結構，不是只改條件"
            % (len(branches), len(after)))
    if ast.dump(after[index][0].test) != ast.dump(ast.parse(_DEAD_TEST, mode="eval").body):
        raise InjectionFailed(
            "第 %d 個分支注入後不是那句永不匹配的 regex（實際：%s）——索引錯位，"
            "殺到的不是預期的那一個" % (index, after[index][1]))
    return mutated, label


def insert_before_main_guard(src, text):
    """把新的 module 層定義插在 `if __name__ == "__main__":` 之前。

    🔴 直接 `src += ...` 會把定義接在那道 guard **之後**。本檔的 mutant 只經
    `_load()` 執行（`__name__` 不是 "__main__"，`main()` 不會跑），所以接在後面也
    「剛好」能動——但那是一個沒被寫下來的隱含假設，換一種驗收方式（例如子行程跑
    `--all`）就會炸成 `NameError`，而那個紅與「gate 誤擋」在 exit code 上完全相同。
    姊妹檔 test_repo_lint_registry_floor.py 實際踩過，這裡一併修掉。
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
                              "的程式")
    return out


def strict_hook(src):
    """把 `run_hook()` 的全域 fail-open（`except Exception: return 0`）換成 `raise`。

    fail-open 在 hook 模式是對的（絕不擋開發），但它讓「例外被吞掉」與「分派沒接到」
    在探針眼裡完全相同。本變體只給測試用，用來把前者揪出來。
    """
    tree = ast.parse(src)
    run_hook = _func(tree, "run_hook")
    handlers = [h for node in ast.walk(run_hook) if isinstance(node, ast.Try)
                for h in node.handlers]
    if not handlers:
        raise InjectionFailed("run_hook() 裡找不到 except 區塊——注入工具失效")
    items = []
    for handler in handlers:
        first, last = handler.body[0], handler.body[-1]
        items.append((_Span(first, last), "raise"))
    mutated = _replace_spans(src, items)
    if mutated == src:
        raise InjectionFailed("fail-open 置換後原始碼一字未變——注入工具失效")
    for handler in [h for node in ast.walk(_func(ast.parse(mutated), "run_hook"))
                    if isinstance(node, ast.Try) for h in node.handlers]:
        if not all(isinstance(s, ast.Raise) for s in handler.body):
            raise InjectionFailed("still-swallowing except 區塊沒被換掉——注入沒生效")
    return mutated


class _Span(object):
    """把「第一句的起點～最後一句的終點」包成 `_replace_spans` 吃的形狀。"""

    def __init__(self, first, last):
        self.lineno = first.lineno
        self.col_offset = first.col_offset
        self.end_lineno = last.end_lineno
        self.end_col_offset = last.end_col_offset


# ------------------------------------------------------------ 掃描探針

def _repo_files():
    files = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, _REPO_ROOT).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        if any(rel_dir.startswith(p) for p in _SKIP_PREFIXES):
            dirnames[:] = []
            continue
        for name in filenames:
            files.append("%s/%s" % (rel_dir, name) if rel_dir else name)
    files.sort()
    assert len(files) >= _MIN_FILES, (
        "只掃到 %d 個檔（下限 %d）——**探針沒走到 repo，不是 repo 變小了**。"
        "這時任何「不可達」的結論都不可信（root=%s）"
        % (len(files), _MIN_FILES, _REPO_ROOT))
    return files


_FILES = _repo_files()


def _load(src, name):
    """把（可能被突變過的）原始碼載成獨立模組。

    檔名有連字號只能走 importlib；用 `spec_from_file_location` 讓模組的 `__file__`
    指向真實位置（`SCRIPT_DIR` / baseline 路徑都由它推導），但 code object 用傳進來
    的 src 編譯——這樣突變版與本尊看到的 repo 完全相同，差別只有被注入的那幾行。
    """
    spec = importlib.util.spec_from_file_location(name, _GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    exec(compile(src, str(_GATE_PATH), "exec"), mod.__dict__)
    # 🔴 不讓 CLAUDE_PROJECT_DIR 決定 root：它在開發機上常指向另一份 checkout，
    #    那時 rel 全部算錯而結果看起來只是「比較少東西可達」。
    mod.repo_root = lambda: _REPO_ROOT
    return mod


Sweep = collections.namedtuple("Sweep", "reach taken labels raised")


def sweep(src, instrument=True, strict=False, files=None):
    """餵每一條真實 repo 路徑進 `run_hook()`，記錄誰被呼叫到、哪些分支被選中。

    check 一律換成**回傳非空**的 recorder：`run_hook()` 尾端的
    `elif ladder or availability or poll_facts or anchors:` 分支是看 check 的**回傳值**
    決定走不走的，stub 回 `[]` 的話那個分支永遠不會被選中 ⇒ 不變式 2 會誤判它是死的。
    """
    tree = ast.parse(src)
    called = sorted({c.func.id for c in _check_calls(_func(tree, "run_all"))})
    labels = []
    if instrument:
        items = []
        for node, label in _dispatch_branches(src):
            items.append((node.test, "_probe_branch(%d, (%s))"
                          % (len(labels), ast.get_source_segment(src, node.test))))
            labels.append(label)
        src = _replace_spans(src, items)
    if strict:
        src = strict_hook(src)

    mod = _load(src, "repo_lint_probe_%d" % (len(sys.modules),))
    taken, seen, raised = set(), [], []
    mod._probe_branch = lambda i, value: (taken.add(i) if value else None) or value
    for name in called:
        setattr(mod, name, (lambda n: lambda *a, **k: (seen.append(n), ["<probe>"])[1])(name))

    reach = collections.defaultdict(list)
    for rel in (files if files is not None else _FILES):
        del seen[:]
        payload = {"tool_input": {"file_path": os.path.join(_REPO_ROOT, rel)}}
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
        try:
            mod.run_hook()
        except Exception as exc:                      # 只有 strict 變體會走到這裡
            raised.append((rel, "%s: %s" % (type(exc).__name__, exc)))
        finally:
            sys.stdin, sys.stdout = stdin, stdout
        for name in set(seen):
            reach[name].append(rel)
    return Sweep(dict(reach), taken, labels, raised)


def unreachable(src, **kwargs):
    """不變式 1 的判定結果：哪些 check 沒有任何真實路徑到得了（已扣除具名豁免）。"""
    tree = ast.parse(src)
    called = {c.func.id for c in _check_calls(_func(tree, "run_all"))}
    exempt = set(_load(src, "repo_lint_exempt_%d" % len(sys.modules))
                 .REPO_LINT_HOOK_EXEMPT or {})
    result = sweep(src, **kwargs)
    return sorted(fn for fn in called - exempt if not result.reach.get(fn)), result


def dead_branch_indices(result):
    """不變式 2 的判定結果（索引形式）——注入測試的比對身分。"""
    return [i for i in range(len(result.labels)) if i not in result.taken]


def dead_branches(result):
    """同上，換成人看得懂的標籤（給失敗訊息與陰性對照用）。"""
    return [result.labels[i] for i in dead_branch_indices(result)]


# ------------------------------------------------------------ 陰性對照

class TestBaselineIsClean(unittest.TestCase):
    """未注入時綠——而且綠得有前提（探針真的掃到東西、真的呼叫到東西）。"""

    @classmethod
    def setUpClass(cls):
        cls.missing, cls.result = unreachable(_REAL_SRC)

    def test_every_check_is_reachable(self):
        """不變式 1。"""
        self.assertEqual(
            self.missing, [],
            "以下 check 進了 run_all() 卻**沒有任何真實 repo 路徑**能讓 run_hook() "
            "呼叫到它——ast 層看得見、執行時到不了，CI 會抓而 hook 完全沉默（#596／"
            "#612）。真的不需要 hook 就加進 REPO_LINT_HOOK_EXEMPT 並寫下理由。")

    def test_every_dispatch_branch_is_live(self):
        """不變式 2。"""
        self.assertEqual(
            dead_branches(self.result), [],
            "以下分派條件沒有被任何真實 repo 路徑選中——打錯的 path pattern，或被"
            "前面的分支完全遮蔽。掛在它底下的 check 對那類編輯已經沉默了（#612）。")

    def test_probe_actually_swept_the_repo(self):
        # 「零違規」若來自空集合就是假綠：釘住前提——掃了夠多檔、有夠多 check 被
        # 呼叫到、有夠多分支被選中。三個下限都刻意遠低於實測值（1426／22／16），
        # 它們要偵測的是量級崩掉，不是正常增減。
        self.assertGreaterEqual(len(_FILES), _MIN_FILES)
        self.assertGreaterEqual(len(self.result.reach), 10)
        self.assertGreaterEqual(len(self.result.taken), 10)
        self.assertGreaterEqual(len(self.result.labels), 10)

    def test_instrumentation_preserves_dispatch(self):
        # 插樁把每個分派條件包進 `_probe_branch(i, (原條件))`。它回傳原值、不改真值性，
        # 所以可達集合必須逐字相同。不同就代表**探針改變了被測物**，那時上面兩支的
        # 綠或紅都不能信。
        plain = sweep(_REAL_SRC, instrument=False)
        both = {fn: (sorted(self.result.reach.get(fn, [])), sorted(plain.reach.get(fn, [])))
                for fn in set(self.result.reach) | set(plain.reach)}
        self.assertEqual([fn for fn, (a, b) in both.items() if a != b], [],
                         "插樁版與原版的可達集合不同——探針改變了被測物")

    def test_hook_never_swallows_an_exception(self):
        # run_hook() 的 `except Exception: return 0` 是刻意的 fail-open，但它讓
        # 「例外被吞掉」長得跟「分派沒接到」一模一樣。把 fail-open 換成 raise 再掃
        # 一次：任何真實路徑會炸都在這裡大聲說出來，而不是變成一句「不可達」。
        result = sweep(strict_hook(_REAL_SRC))
        self.assertEqual(result.raised, [])


# ------------------------------------------------------------ 敏感度（mutation）

class TestKillingAnyDispatchIsDetected(unittest.TestCase):
    """A/B 注入：把分派條件改成永不匹配的 regex ⇒ 必須紅、且點名是哪一個。

    對 `run_all()` 裡的**每一條** check 各跑一次，不是只挑一條示範。挑一條的版本
    只證明「至少有一條守得住」，而假綠洞從來不會挑在被示範的那一條上。
    """

    def test_kills_are_detected_for_every_check(self):
        tree = ast.parse(_REAL_SRC)
        called = sorted({c.func.id for c in _check_calls(_func(tree, "run_all"))})
        exempt = set(_load(_REAL_SRC, "repo_lint_exempt_base").REPO_LINT_HOOK_EXEMPT or {})
        targets = [fn for fn in called if fn not in exempt]
        self.assertGreaterEqual(len(targets), 10, "受測 check 太少——探針失效")
        for fn in targets:
            with self.subTest(check=fn):
                mutated = kill_dispatch(_REAL_SRC, fn)      # 自帶三層 tripwire
                missing, _ = unreachable(mutated)
                self.assertIn(
                    fn, missing,
                    "殺掉 %s 的 hook 分派之後，不變式 1 仍然是綠的——本測試對它沒有"
                    "鑑別力（實際不可達清單：%s）" % (fn, missing))

    def test_positive_control_injection_really_took_effect(self):
        """🔴 陽性對照：確認注入真的生效，而不是「注入了但其實沒注入」。

        取一條 check，先記下**未注入時哪些真實路徑會呼叫到它**（必須非空，否則整個
        對照是空集合上的恆真），注入後那批路徑必須一條都不再呼叫到它。這一句是肯定性
        的：注入若沒發生，它會紅。
        """
        fn = "check_config_parameter_uninstall"
        before = sweep(_REAL_SRC).reach.get(fn, [])
        self.assertTrue(before, "未注入時 %s 就沒有任何路徑到得了——對照組本身壞掉" % fn)
        after = sweep(kill_dispatch(_REAL_SRC, fn), files=before).reach.get(fn, [])
        self.assertEqual(after, [],
                         "注入後這些路徑仍然呼叫得到 %s ⇒ 注入沒生效，"
                         "上面那批 mutation 測試全部是假的：%s" % (fn, after))

    def test_restoring_the_source_is_green_again(self):
        """還原後綠——比對實驗要固定 recipe，不能只固定基準。"""
        missing, result = unreachable(_REAL_SRC)
        self.assertEqual(missing, [])
        self.assertEqual(dead_branches(result), [])


class TestKillingOneBranchIsDetected(unittest.TestCase):
    """遮蔽單一分支：不變式 1 可能仍綠，不變式 2 必須紅。

    這一組就是不變式 2 存在的理由。`check_slug_capability_gate` 之流掛在 .xml 與 .js
    兩個分支，遮蔽掉 .js 那個之後它依然「可達」——只驗不變式 1 的測試集會全綠，而
    hook 對整類 .js 編輯已經沉默。
    """

    def test_every_branch_kill_is_detected(self):
        branches = _dispatch_branches(_REAL_SRC)
        self.assertGreaterEqual(len(branches), 10, "分派分支太少——探針失效")
        for index in range(len(branches)):
            with self.subTest(branch=branches[index][1]):
                mutated, label = kill_one_branch(_REAL_SRC, index)
                _, result = unreachable(mutated)
                self.assertIn(
                    index, dead_branch_indices(result),
                    "殺掉分支 %s（第 %d 個）之後不變式 2 仍然是綠的——本測試對它沒有"
                    "鑑別力" % (label, index))

    def test_a_shadowed_branch_can_hide_from_invariant_1(self):
        """🔴 存在性證明：確實有「不變式 1 綠、不變式 2 紅」的分支。

        沒有這一支的話，不變式 2 可能只是不變式 1 的複本（memory:
        `feedback_assert_the_tests_own_independence`）。挑選條件從真相源推導——
        「這個分支底下直接呼叫的 check，每一個都還有別的分支也呼叫」——不寫死是哪個
        分支，重排 run_hook 也不會讓它靜默失效。
        """
        tree = ast.parse(_REAL_SRC)
        run_hook = _func(tree, "run_hook")
        parent = _parent_map(run_hook)
        owners = collections.defaultdict(set)
        for call in _check_calls(run_hook):
            branch = _branch_of(call, parent)
            if branch is not None:
                owners[call.func.id].add((branch.lineno, branch.col_offset))

        candidates = []
        for index, (node, label) in enumerate(_dispatch_branches(_REAL_SRC)):
            key = (node.lineno, node.col_offset)
            mine = [fn for fn, keys in owners.items() if key in keys]
            if mine and all(len(owners[fn]) > 1 for fn in mine):
                candidates.append((index, label))
        self.assertTrue(
            candidates,
            "找不到「底下每條 check 都另有分支」的分派分支——存在性證明的前提不成立，"
            "不變式 2 此刻可能只是不變式 1 的複本")

        index, label = candidates[0]
        mutated, killed = kill_one_branch(_REAL_SRC, index)
        missing, result = unreachable(mutated)
        self.assertEqual(missing, [],
                         "挑選條件說這個分支底下的 check 都另有去路，不變式 1 卻紅了"
                         "——挑選邏輯與實況不符：%s" % missing)
        self.assertIn(index, dead_branch_indices(result),
                      "不變式 2 沒抓到被遮蔽的分支 %s" % killed)


# ------------------------------------------------------------ 特異性

class TestSpecificity(unittest.TestCase):
    """合法改動不得被誤擋。

    只驗「改壞→紅」的測試集裡，一條恆紅的 gate 看起來完美——直到它擋住正當工作然後
    被人整條拿掉。敏感度與特異性要成對。
    """

    def test_named_exemption_silences_only_that_check(self):
        # 具名豁免必須是**逐項扣除**，不是「EXEMPT 非空就跳過整條不變式」的全域開關。
        # 所以殺掉兩條 check 的分派、只豁免其中一條：被豁免的消音、另一條仍被點名。
        # 後者是肯定性斷言——不變式 1 整條 fail-open 時它會紅。
        exempted, still_red = "check_owl_template", "check_seed_topology"
        src = kill_dispatch(kill_dispatch(_REAL_SRC, exempted), still_red)
        patched = src.replace("REPO_LINT_HOOK_EXEMPT = {}",
                              "REPO_LINT_HOOK_EXEMPT = {'%s': '理由'}" % exempted)
        self.assertNotEqual(patched, src, "注入工具失效——豁免清單的宣告變了")
        missing, _ = unreachable(patched)
        self.assertNotIn(exempted, missing, "具名豁免沒有被扣除")
        self.assertIn(still_red, missing, "未豁免的 check 沒有被點名")

    def test_adding_a_new_reachable_check_stays_green(self):
        """正常新增一條 check（run_all ＋ run_hook 分派都掛上、且真的有檔案命中）
        不得被誤擋。

        新分支插在 if/elif 鏈的**最前面**，條件挑 `^src/models/.*\\.py$`——那是 repo
        裡確定有檔案的形狀。錨點（鏈頭那個 `ast.If`）從 AST 推導，不寫死行號或條件
        字面值。
        """
        fn = "check_brand_new_thing"
        last = _check_calls(_func(ast.parse(_REAL_SRC), "run_all"))[-1]
        src = _replace_spans(_REAL_SRC, [
            (last, "%s\n                  + %s(root)"
             % (ast.get_source_segment(_REAL_SRC, last), fn))])
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效——run_all 的尾呼叫變了")

        chain = _dispatch_chain_head(src)
        before = src
        src = _replace_spans(src, [(chain, (
            'if re.match(r"^src/models/.*\\.py$", rel):\n'
            "            violations = %s(root)\n"
            "        el" % fn) + ast.get_source_segment(src, chain))])
        self.assertNotEqual(src, before, "注入工具失效——run_hook 的鏈頭變了")
        src = insert_before_main_guard(
            src, "\ndef %s(root, only_file=None):\n    return []\n\n\n" % fn)

        missing, result = unreachable(src)
        self.assertEqual(missing, [], "四處都掛好的新 check 被誤判成不可達")
        self.assertEqual(dead_branches(result), [], "新增的分派分支被誤判成死分支")
        # 肯定性斷言：新分支真的被真實路徑選中了（否則上面兩句可能只是「注入沒發生」）。
        self.assertTrue(sweep(src).reach.get(fn), "新增的 check 一條路徑都沒命中——"
                        "注入沒生效，這支的綠是假的")


# ------------------------------------------------------------ 注入工具自身

class TestInjectionToolTripwires(unittest.TestCase):
    """🔴 注入工具的絆線本身要有測試——這是整份測試最容易做假的一環。

    絆線壞掉時的症狀是「注入靜默沒發生」，而那與「gate 正確」在輸出上完全相同。
    """

    def test_unknown_check_raises_instead_of_noop(self):
        with self.assertRaises(InjectionFailed):
            kill_dispatch(_REAL_SRC, "check_this_does_not_exist")

    def test_out_of_range_branch_raises(self):
        with self.assertRaises(InjectionFailed):
            kill_one_branch(_REAL_SRC, len(_dispatch_branches(_REAL_SRC)))

    def test_partial_kill_is_rejected(self):
        """語意自檢那一層：只殺掉多分支 check 的其中一處必須被擋下。

        構造方式是刻意繞過 `kill_dispatch` 的多分支處理（只殺第一處），再把結果餵回
        自檢邏輯。這證明第 3 層 tripwire 是活的，而不是一句沒被執行過的註解。
        """
        multi = [fn for fn in {c.func.id for c
                               in _check_calls(_func(ast.parse(_REAL_SRC), "run_hook"))}
                 if len(_governing_branches(_REAL_SRC, fn)) > 1]
        self.assertTrue(multi, "沒有掛在多個分支的 check——本測試的前提不成立")
        fn = sorted(multi)[0]
        partial = _kill_branches(_REAL_SRC, _governing_branches(_REAL_SRC, fn)[:1],
                                 "只殺一處")
        dead = ast.dump(ast.parse(_DEAD_TEST, mode="eval").body)
        alive = [b for b in _governing_branches(partial, fn)
                 if ast.dump(b.test) != dead]
        self.assertTrue(alive, "只殺一處之後居然沒有存活的分派條件——探針失效")
        # kill_dispatch 走完整流程時必須把它們全殺光。
        full = kill_dispatch(_REAL_SRC, fn)
        self.assertEqual(
            [b for b in _governing_branches(full, fn) if ast.dump(b.test) != dead], [])

    def test_strict_hook_really_removes_fail_open(self):
        strict = strict_hook(_REAL_SRC)
        self.assertNotEqual(strict, _REAL_SRC)
        handlers = [h for node in ast.walk(_func(ast.parse(strict), "run_hook"))
                    if isinstance(node, ast.Try) for h in node.handlers]
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertTrue(all(isinstance(s, ast.Raise) for s in handler.body))

    def test_strict_hook_actually_propagates(self):
        # 陽性對照：strict 變體真的會把例外傳出來，而不是換了個寫法照樣吞掉。
        # 故意讓 json.load 拿到壞輸入。
        mod = _load(strict_hook(_REAL_SRC), "repo_lint_strict_probe")
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO("not json at all"), io.StringIO()
        try:
            with self.assertRaises(Exception):
                mod.run_hook()
        finally:
            sys.stdin, sys.stdout = stdin, stdout


if __name__ == "__main__":
    unittest.main(verbosity=2)

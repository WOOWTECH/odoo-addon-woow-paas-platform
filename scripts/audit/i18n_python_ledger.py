#!/usr/bin/env python3
"""#107 對帳台帳：Python 側使用者可見訊息是否都包了 `_()`，並與抽取器對帳。

**為什麼需要這支**：`.pot` 的驗收不能看「總條數」——那對這類改動零鑑別力。要看的是
**帶 `#. odoo-python` 註解的條目**，而那批條目完全由 babel 的 python extractor 決定
（`odoo/tools/translate.py:1378` `_babel_extract_terms(extract_method='python',
extract_keywords={'_': None, '_lt': None})`）。該路徑**不碰 DB**，所以本機就能用同一支
抽取器重現，不必跑起 Odoo。

**兩把尺，且第二把是產生答案的東西本人**：
  尺 A（本檔的 AST 掃描）＝「哪些位置**應該**被翻譯」
  尺 B（babel extractor）＝「哪些字串**實際**會進 .pot」
兩把尺的取得路徑完全獨立（一個走 ast，一個走 babel 的 tokenizer），所以 A ⊆ B 這件事
才是證據。⚠ f-string 是唯一會讓 A 命中而 B 靜默漏掉的形態（babel 對 ast.JoinedStr 不產
任何 msgid，沒有任何警告）——這正是本檔要抓的東西。

⚠ **尺 B 缺席不是通過**（#107 第五批修）：babel 沒裝時本檔**回非 0**並說明怎麼裝，
而不是印一行「已跳過」然後 exit 0。兩把尺的整個價值就在「尺 B 是產生 `.pot` 的那個
東西本人」，只跑尺 A 等於只有一把尺，而它與自己永遠相符。真的要在無 babel 環境跑
（例如只想看尺 A 的清單），必須明講 `--skip-ruler-b`——把「探針不在」變成一個要打字
才成立的決定，而不是靜默的預設。

掃描範圍與桶（#107 第五批擴充）：
  `src/models` / `src/controllers` / `src/wizard` / `src/migrations`
    → `raise <UserError|ValidationError|…>(msg)` 的訊息
  `src/controllers`
    → 直送前端的 JSON 訊息鍵（`MSG_KEYS`）與 helper 的文字引數（`HELPER_ARG`）
  `src/models`
    → 寫入 `error_message` 欄位的位置。sink 已實證：`ServiceDetailPage.xml:154-156`
      的租戶紅色故障 banner 用 `t-esc` 直接吐這個欄位 ⇒ 會上畫面 ⇒ 要翻。
      值是同 scope 區域變數／模組常數時**回溯一階**取其字面（否則
      `self.write({'error_message': message})` 這種形態整條看不見）。

用法：
    python3 scripts/audit/i18n_python_ledger.py            # 對帳，有問題 exit 1
    python3 scripts/audit/i18n_python_ledger.py --list     # 印出所有應翻位置
    python3 scripts/audit/i18n_python_ledger.py --skip-ruler-b   # ⛔ 只在明知的情況下用
需要 babel（Odoo 本身的相依）。裝法：`uv run --with babel python3 <本檔>`，或
`pip install babel`。
"""
from __future__ import annotations
import ast, os, re, sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCAN_DIRS = ('src/models', 'src/controllers', 'src/wizard', 'src/migrations')
#: `error_message` 欄位只長在這些目錄的 model 上（`fields.Text` 定義處：
#: cloud_service.py / security_access.py / smart_home.py）。
ERROR_FIELD_DIRS = ('src/models',)
#: 被寫入就會出現在租戶故障 banner 上的欄位。
ERROR_FIELDS = {'error_message'}

# 面向使用者的例外類別（Odoo 會把這些轉成前端錯誤對話框；其餘轉通用 500）
USER_EXC = {'UserError', 'ValidationError', 'AccessError', 'MissingError',
            'RedirectWarning', 'AccessDenied', 'Warning', 'UserWarning'}
# controller 直送前端的 JSON 訊息鍵
MSG_KEYS = {'error', 'message', 'detail', 'error_message', 'msg', 'warning',
            'reason', 'hint', 'error_description'}
# helper → 使用者可見文字的引數索引。⚠ 逐支查過定義，不可憑名字猜：
#   _sse_error_response(self, error, error_code) → 第 0 個是文字，第 1 個是機器碼
#   _json_error(error, description, status)      → 第 0 個是 RFC 6749 機器碼，第 1 個才是文字
#   _provision_failure_message(base, outcome, created) → 第 0 個是文字（其餘兩個是
#     列舉值）；helper 自己回傳的骨架見下方 EXEMPT_TOKENS 的說明。
HELPER_ARG = {'_sse_error_response': 0, '_json_error': 1, 'error_response': 1}
#: 在 models 也要展開的 helper（controllers 之外）。
MODEL_HELPER_ARG = {'_provision_failure_message': 0}

CJK = re.compile(r'[一-鿿]')
#: `%s` / `%(name)s` / `{}` / `{name}` 這類佔位符。
PLACEHOLDER = re.compile(r'%\([^)]*\)[sdifr]|%[sdifr]|\{[^{}]*\}')
LETTER = re.compile(r'[^\W\d_]', re.UNICODE)

#: ⛔ 明列排除項（**非默契**）。判準是那條通則：「這個值除了顯示給人看，還有沒有被
#: 程式拿去比對？」有就是排除項——翻譯它會打破那個比對，而比對端不會有任何警告。
#: key = (相對路徑, 該位置原始碼行內必然出現的 token)。用 token 而不是行號：行號會漂。
EXEMPT_TOKENS = {
    ('src/models/cloud_service.py', '{_UNCONFIRMED_WORKLOAD_MARKER_497}'):
        '#497 prod rollout checklist 拿去 search 的可搜尋字面（Eugene 2026-08-26 '
        '裁決明列排除）。骨架上唯一的字面是全形冒號，本來也零可翻內容。',
}


# ⛔ 刻意不翻的機器碼（API 契約值，翻了會打破呼叫端）。
# 判準：字面**無空白且無 CJK**。中文訊息沒有 ASCII 空白，所以 CJK 條款不能省。
def is_machine_code(lit: str) -> bool:
    return not re.search(r'\s', lit) and not CJK.search(lit)


def is_format_only(lit: str) -> bool:
    """純格式串：拿掉佔位符後不剩任何字母／CJK ⇒ 沒有可翻的內容。

    例：``'%s — %s'``（兩端各自已經翻過了）、``'%s：%s'``。把這種包起來只會產出
    一個沒有語意的 msgid，讓譯者面對一串標點符號——而且它會**稀釋**真正需要翻譯
    的條目。⚠ 判準刻意是「不剩字母」而不是「很短」：``'%s [%s: name=%s]'`` 剩下
    ``name`` ⇒ 不算純格式串（那是給 grep 用的標記格式，屬 EXEMPT 的範疇）。
    """
    return not LETTER.search(PLACEHOLDER.sub('', lit))


def _is_gettext(n) -> bool:
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in ('_', '_lt'))


def _gettext_on_skeleton(n, _depth=0) -> bool:
    """骨架上有沒有 _()。⛔ 不用 ast.walk：巢狀呼叫**引數**裡的 _() 不算數
    （那是別人的翻譯，這一站仍然是裸的），否則會假綠。"""
    if _depth > 12:
        return False
    if _is_gettext(n):
        return True
    if isinstance(n, ast.BinOp):
        return _gettext_on_skeleton(n.left, _depth + 1) or _gettext_on_skeleton(n.right, _depth + 1)
    if isinstance(n, ast.BoolOp):
        return all(_gettext_on_skeleton(v, _depth + 1) or not _literals(v)
                   for v in n.values)
    if isinstance(n, ast.IfExp):
        return ((_gettext_on_skeleton(n.body, _depth + 1) or not _literals(n.body))
                and (_gettext_on_skeleton(n.orelse, _depth + 1) or not _literals(n.orelse)))
    if isinstance(n, ast.Call):
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr in ('format', 'join'):
            return _gettext_on_skeleton(f.value, _depth + 1)
    return False


def _exc_name(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else '?')


def _literals(n, _depth=0):
    """**訊息骨架**上的字面字串（含 f-string 的字面片段）。

    ⛔ 刻意不用 ast.walk：那會把巢狀呼叫的引數也收進來，於是
    ``raise ValidationError(self._msg(self.env['woow.model'].browse(x)))``
    會因為模型名 ``'woow.model'`` 被誤判成「這裡有沒包的訊息字面」。
    只沿著「這個表達式**自己**產生字串」的路徑走：字面／f-string 片段／
    ``%`` 與 ``+`` 的兩側／``or``、三元的各分支／``_()`` 與 ``.format()`` 的接收端。
    一般函式呼叫的回傳值視為 runtime 值（它的字面歸它自己那一站管）。
    """
    if _depth > 12:
        return []
    if isinstance(n, ast.Constant):
        return [n.value] if isinstance(n.value, str) and n.value.strip() else []
    if isinstance(n, ast.JoinedStr):
        return [v.value for v in n.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value.strip()]
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
        return _literals(n.left, _depth + 1)
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
        return _literals(n.left, _depth + 1) + _literals(n.right, _depth + 1)
    if isinstance(n, ast.BoolOp):
        return [s for v in n.values for s in _literals(v, _depth + 1)]
    if isinstance(n, ast.IfExp):
        return _literals(n.body, _depth + 1) + _literals(n.orelse, _depth + 1)
    if isinstance(n, ast.Call):
        f = n.func
        if isinstance(f, ast.Name) and f.id in ('_', '_lt'):
            return _literals(n.args[0], _depth + 1) if n.args else []
        if isinstance(f, ast.Attribute) and f.attr in ('format', 'join'):
            return _literals(f.value, _depth + 1)
    return []


def _has_fstring(n, _depth=0):
    """骨架上有沒有 f-string（同 _literals 的路徑規則）。"""
    if _depth > 12:
        return False
    if isinstance(n, ast.JoinedStr):
        return True
    if isinstance(n, ast.BinOp):
        return _has_fstring(n.left, _depth + 1) or _has_fstring(n.right, _depth + 1)
    if isinstance(n, ast.BoolOp):
        return any(_has_fstring(v, _depth + 1) for v in n.values)
    if isinstance(n, ast.IfExp):
        return _has_fstring(n.body, _depth + 1) or _has_fstring(n.orelse, _depth + 1)
    if isinstance(n, ast.Call):
        f = n.func
        if isinstance(f, ast.Name) and f.id in ('_', '_lt'):
            return _has_fstring(n.args[0], _depth + 1) if n.args else False
        if isinstance(f, ast.Attribute) and f.attr in ('format', 'join'):
            return _has_fstring(f.value, _depth + 1)
    return False


# ---------------------------------------------------------------------- #
#  一階回溯（#107 第五批新增）                                           #
# ---------------------------------------------------------------------- #
#
# `self.write({'error_message': message})` 的骨架上沒有任何字面——字面在同一個
# function 裡的 `message = "..."`。不回溯的話這一整條在尺 A 下是隱形的，而它正是
# 會上租戶 banner 的那個字串。⚠ 只回溯**一階**：再往下就會追進別人的站，把「那一
# 站自己該負責的字面」算到這裡，重演 `_literals` 刻意不用 ast.walk 要避開的誤判。

SCOPE_NODES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
               ast.ClassDef)


def _link_parents(tree):
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._ledger_parent = parent


def _enclosing_scope(node):
    cur = getattr(node, '_ledger_parent', None)
    while cur is not None and not isinstance(cur, SCOPE_NODES):
        cur = getattr(cur, '_ledger_parent', None)
    return cur


def _scope_assignments(scope):
    """該 scope（不含巢狀 function）內 `name = <expr>` 的所有 value 節點。"""
    out = {}
    stack = [scope]
    while stack:
        cur = stack.pop()
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not scope:
                continue          # 巢狀 function 有自己的 scope
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    if isinstance(t, ast.Name):
                        out.setdefault(t.id, []).append(child.value)
            stack.append(child)
    return out


def _resolve_one_hop(node):
    """值是 Name 時展開成該名字在同 scope／module scope 的所有賦值。找不到回 []。"""
    if not isinstance(node, ast.Name):
        return []
    scope = _enclosing_scope(node)
    seen = []
    while scope is not None:
        for v in _scope_assignments(scope).get(node.id, []):
            if v is not node:
                seen.append(v)
        if seen or isinstance(scope, ast.Module):
            break
        scope = _enclosing_scope(scope)
    return seen


# ---------------------------------------------------------------------- #
#  尺 A                                                                   #
# ---------------------------------------------------------------------- #

def positions(src: str, rp: str):
    """尺 A：回傳所有『使用者可見訊息位置』的 (kind, node)。"""
    in_controllers = 'controllers' in rp
    in_models = any(rp.startswith(d) for d in ERROR_FIELD_DIRS)
    tree = ast.parse(src)
    _link_parents(tree)
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                and n.exc.args and _exc_name(n.exc) in USER_EXC):
            out.append(('raise', n.exc.args[0]))
        if in_controllers:
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value in MSG_KEYS:
                        out.append((f'dict[{k.value}]', v))
            if isinstance(n, ast.Call):
                fn = (n.func.attr if isinstance(n.func, ast.Attribute)
                      else n.func.id if isinstance(n.func, ast.Name) else '')
                i = HELPER_ARG.get(fn)
                if i is not None and len(n.args) > i:
                    out.append((f'{fn}#arg{i}', n.args[i]))
        if in_models:
            out.extend(_error_field_positions(n))
    return out


def _error_field_positions(n):
    """`error_message` 欄位的**寫入**位置。資源側枚舉：問「誰會寫到那個位置」，
    而不是「誰呼叫了某個入口」——後者會漏掉繞過入口的路徑。

    五種形態：write/create 的 dict key、`x.error_message = v`、
    `vals['error_message'] = v`、`error_message=v` 具名引數、`setattr`。
    ⚠ `'error_message': self.error_message or ''` 這種是**讀出**（`to_dict()` 的
    JSON payload），骨架上沒有字面 ⇒ 自然落到「純 runtime 值」被跳過，不需特判。
    """
    out = []
    if isinstance(n, ast.Dict):
        for k, v in zip(n.keys, n.values):
            if isinstance(k, ast.Constant) and k.value in ERROR_FIELDS:
                out.append((f'field[{k.value}]', v))
    if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
        for t in tgts:
            if isinstance(t, ast.Attribute) and t.attr in ERROR_FIELDS:
                out.append((f'attr[{t.attr}]', n.value))
            if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                    and t.slice.value in ERROR_FIELDS):
                out.append((f'item[{t.slice.value}]', n.value))
    if isinstance(n, ast.Call):
        for kw in n.keywords:
            if kw.arg in ERROR_FIELDS:
                out.append((f'kwarg[{kw.arg}]', kw.value))
        fn = (n.func.attr if isinstance(n.func, ast.Attribute)
              else n.func.id if isinstance(n.func, ast.Name) else '')
        i = MODEL_HELPER_ARG.get(fn)
        if i is not None and len(n.args) > i:
            out.append((f'{fn}#arg{i}', n.args[i]))
        if (isinstance(n.func, ast.Name) and n.func.id == 'setattr'
                and len(n.args) == 3 and isinstance(n.args[1], ast.Constant)
                and n.args[1].value in ERROR_FIELDS):
            out.append((f'setattr[{n.args[1].value}]', n.args[2]))
    return out


# ---------------------------------------------------------------------- #
#  尺 B                                                                   #
# ---------------------------------------------------------------------- #

def babel_msgids(root):
    """尺 B：Odoo 用的同一支抽取器。回傳 {(relpath, lineno)} 與 msgid 集合。"""
    try:
        from babel.messages import extract
    except ImportError:
        return None, None
    sites, ids = set(), set()
    for d in SCAN_DIRS:
        for dp, _dn, fns in os.walk(os.path.join(root, d)):
            for fn in sorted(fns):
                if not fn.endswith('.py'):
                    continue
                p = os.path.join(dp, fn)
                with open(p, 'rb') as fh:
                    for ex in extract.extract('python', fh,
                                              keywords={'_': None, '_lt': None},
                                              options={'encoding': 'UTF-8'}):
                        lineno, message = ex[0], ex[1]
                        sites.add((os.path.relpath(p, root), lineno))
                        ids.add(message if isinstance(message, str) else message[0])
    return sites, ids


def main(argv):
    root = REPO_ROOT
    want_list = '--list' in argv
    skip_b = '--skip-ruler-b' in argv
    unwrapped, fstrings, machine, wrapped, exempt = [], [], [], [], []
    #: 尺 A 認定「已翻譯」的字面 → 拿去和尺 B 的 msgid 集合對帳。
    wrapped_lits = {}

    for d in SCAN_DIRS:
        for dp, _dn, fns in os.walk(os.path.join(root, d)):
            for fn in sorted(fns):
                if not fn.endswith('.py'):
                    continue
                p = os.path.join(dp, fn)
                rp = os.path.relpath(p, root)
                src = open(p, encoding='utf-8').read()
                lines = src.split('\n')
                try:
                    pos = positions(src, rp)
                except SyntaxError as e:
                    print(f'!! {rp}: {e}')
                    return 2
                # 一階回溯：值是同 scope 的變數時，把它的賦值也當成位置。
                expanded = []
                for kind, v in pos:
                    expanded.append((kind, v))
                    for hop in _resolve_one_hop(v):
                        expanded.append((f'{kind}→{v.id}', hop))
                for kind, v in expanded:
                    lits = _literals(v)
                    if not lits:
                        continue          # 純 runtime 值，沒有可翻的字面
                    line = lines[v.lineno - 1].strip()[:110]
                    row = (rp, v.lineno, kind, line)
                    # 0) 明列排除項先出場。⛔ 不可讓它排在機器碼／純格式串之後：
                    #    那樣「裁決排除的那一條」會被順帶跳過，排除項退化成死碼
                    #    （零命中，沒人知道它還有沒有在保護什麼）。放最前面，它的
                    #    命中數與理由就一直在 --list 的輸出裡。
                    if (hit := _exempt_reason(rp, lines, v)):
                        exempt.append(row + (hit,))
                        continue
                    # ⚠ 以下順序有意義，每一格都是踩出來的：
                    # 1) 骨架上「沒有任何可翻內容」的先出場。機器碼（API 契約值）
                    #    與純格式串（`'%s — %s'`）都屬此類，f-string 形態也算——
                    #    babel 抽不到它，但它本來就沒有東西可抽，不是缺口。
                    #    ⛔ 這一格若排在 f-string 之後，`f'{a} — {b}'` 會被誤報成
                    #    「必須改 %(name)s」的紅燈（實測：ai_assistant.py:641）。
                    if all(is_machine_code(l) or is_format_only(l) for l in lits):
                        machine.append(row + (lits[0],))
                        continue
                    # 2) f-string：有可翻字面但 babel 靜默漏掉 ⇒ 包了 _() 也抽不到。
                    if _has_fstring(v):
                        fstrings.append(row)
                        continue
                    # 3) 已包 _() ⇒ 達標。⛔ 這一格必須在機器碼之後、未包之前：
                    #    `_("%s: %r") % (err, x)` 的骨架字面是純格式串但它**確實**
                    #    包了，計進 machine 桶會讓 A ⊆ B 對帳少驗一條。
                    if _gettext_on_skeleton(v):
                        wrapped.append(row)
                        wrapped_lits.setdefault(''.join(lits), []).append(row)
                        continue
                    # 4) 剩下的就是缺口。
                    unwrapped.append(row)

    sites_b, ids_b = babel_msgids(root)

    print('=== 尺 A：AST 掃出的使用者可見訊息位置 ===')
    print(f'  掃描目錄        : {", ".join(SCAN_DIRS)}')
    print(f'  已包 _()        : {len(wrapped)}')
    print(f'  ⛔ 未包 _()      : {len(unwrapped)}')
    print(f'  ⛔ f-string      : {len(fstrings)}   （包了也抽不到，必須改 %(name)s）')
    print(f'  刻意跳過的機器碼／純格式串: {len(machine)}')
    print(f'  明列排除項      : {len(exempt)}')

    missing_in_b = []
    if sites_b is None:
        if skip_b:
            print('\n=== 尺 B：babel 未安裝，⚠ 已依 --skip-ruler-b 跳過（不等於通過）===')
        else:
            print('\n🔴 尺 B（babel）不可用 —— 這**不是**通過。')
            print('   兩把尺的整個價值就在尺 B 是產生 .pot 的那個東西本人；只跑尺 A')
            print('   等於拿同一把尺量兩次。裝法（擇一）：')
            print('     uv run --with babel python3 scripts/audit/i18n_python_ledger.py')
            print('     pip install babel')
            print('   真的要在無 babel 的環境只看尺 A：明講 --skip-ruler-b。')
            return 3
    else:
        print('\n=== 尺 B：babel（Odoo 的 #. odoo-python 抽取器）===')
        print(f'  抽到 (檔,行) : {len(sites_b)}')
        print(f'  去重 msgid   : {len(ids_b)}')
        # A ⊆ B：尺 A 認定已翻譯的每個字面，都必須真的出現在 babel 的 msgid 集合裡。
        # ⛔ 只印兩個數字不算對帳——兩個獨立集合「大小相近」不是「相等」。
        for lit, rows in sorted(wrapped_lits.items()):
            if lit not in ids_b:
                missing_in_b.append((lit, rows))
        print(f'  A ⊆ B 對帳   : 尺 A 的 {len(wrapped_lits)} 個去重字面中，'
              f'{len(wrapped_lits) - len(missing_in_b)} 個在 B 裡找得到')

    if want_list:
        for tag, rows in (('未包', unwrapped), ('f-string', fstrings),
                          ('機器碼／純格式串', machine), ('明列排除', exempt)):
            if rows:
                print(f'\n--- {tag} ---')
                for r in rows:
                    extra = f'   → {r[4]}' if len(r) > 4 else ''
                    print(f'  {r[0]}:{r[1]}  [{r[2]}]  {r[3]}{extra}')

    bad = 0
    if unwrapped:
        bad += len(unwrapped)
        print(f'\n🔴 {len(unwrapped)} 個使用者可見訊息位置沒有包 _()：')
        for r in unwrapped:
            print(f'   {r[0]}:{r[1]}  [{r[2]}]  {r[3]}')
    if fstrings:
        bad += len(fstrings)
        print(f'\n🔴 {len(fstrings)} 個位置用了 f-string —— babel 不會產生任何 msgid，'
              f'且不會有任何警告。必須改成 _("… %(name)s …", name=expr)：')
        for r in fstrings:
            print(f'   {r[0]}:{r[1]}  [{r[2]}]  {r[3]}')
    if missing_in_b:
        bad += len(missing_in_b)
        print(f'\n🔴 {len(missing_in_b)} 個字面尺 A 認為已翻譯，但 babel 抽不到 '
              f'—— 尺 A 假綠：')
        for lit, rows in missing_in_b:
            print(f'   {lit[:90]!r}')
            for r in rows:
                print(f'      ← {r[0]}:{r[1]}  [{r[2]}]')
    if bad:
        print(f'\nFAIL: {bad} 個位置未達成。')
        return 1
    print('\nOK: 所有使用者可見訊息位置都已包 _()、無 f-string 漏抽，'
          '且尺 A 的字面在 babel 抽取結果中逐條找得到。')
    return 0


def _exempt_reason(rp, lines, node):
    """該位置是否落在明列排除項上。比對「節點所跨的原始碼行」而非行號。"""
    lo = node.lineno - 1
    hi = getattr(node, 'end_lineno', node.lineno)
    body = '\n'.join(lines[lo:hi])
    for (path, token), why in EXEMPT_TOKENS.items():
        if rp == path and token in body:
            return why
    return None


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

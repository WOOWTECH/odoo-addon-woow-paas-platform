#!/usr/bin/env python3
"""#107 段一/段二對帳台帳：Python 側使用者可見訊息是否都包了 `_()`，並與抽取器對帳。

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

用法：
    python3 scripts/audit/i18n_python_ledger.py            # 對帳，有問題 exit 1
    python3 scripts/audit/i18n_python_ledger.py --list     # 印出所有應翻位置
需要 babel（Odoo 本身的相依）。找不到 babel 時只跑尺 A 並在輸出明示尺 B 已跳過
（⛔ 不會因此變綠——「探針不在」與「檢查通過」必須可分辨）。
"""
from __future__ import annotations
import ast, os, re, sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCAN_DIRS = ('src/models', 'src/controllers')

# 面向使用者的例外類別（Odoo 會把這些轉成前端錯誤對話框；其餘轉通用 500）
USER_EXC = {'UserError', 'ValidationError', 'AccessError', 'MissingError',
            'RedirectWarning', 'AccessDenied', 'Warning', 'UserWarning'}
# controller 直送前端的 JSON 訊息鍵
MSG_KEYS = {'error', 'message', 'detail', 'error_message', 'msg', 'warning',
            'reason', 'hint', 'error_description'}
# helper → 使用者可見文字的引數索引。⚠ 逐支查過定義，不可憑名字猜：
#   _sse_error_response(self, error, error_code) → 第 0 個是文字，第 1 個是機器碼
#   _json_error(error, description, status)      → 第 0 個是 RFC 6749 機器碼，第 1 個才是文字
HELPER_ARG = {'_sse_error_response': 0, '_json_error': 1, 'error_response': 1}

CJK = re.compile(r'[一-鿿]')

# ⛔ 刻意不翻的機器碼（API 契約值，翻了會打破呼叫端）。
# 判準：字面**無空白且無 CJK**。中文訊息沒有 ASCII 空白，所以 CJK 條款不能省。
def is_machine_code(lit: str) -> bool:
    return not re.search(r'\s', lit) and not CJK.search(lit)


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


def positions(src: str, in_controllers: bool):
    """尺 A：回傳所有『使用者可見訊息位置』的節點。"""
    tree = ast.parse(src)
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
    return out


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
    unwrapped, fstrings, machine, wrapped = [], [], [], []

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
                    pos = positions(src, 'controllers' in rp)
                except SyntaxError as e:
                    print(f'!! {rp}: {e}')
                    return 2
                for kind, v in pos:
                    lits = _literals(v)
                    if not lits:
                        continue          # 純 runtime 值，沒有可翻的字面
                    row = (rp, v.lineno, kind, lines[v.lineno - 1].strip()[:110])
                    # f-string：babel 靜默漏掉 ⇒ 就算包了 _() 也抽不到
                    if _has_fstring(v):
                        fstrings.append(row)
                        continue
                    if len(lits) == 1 and is_machine_code(lits[0]):
                        machine.append(row + (lits[0],))
                        continue
                    (wrapped if _gettext_on_skeleton(v) else unwrapped).append(row)

    sites_b, ids_b = babel_msgids(root)

    print('=== 尺 A：AST 掃出的使用者可見訊息位置 ===')
    print(f'  已包 _()        : {len(wrapped)}')
    print(f'  ⛔ 未包 _()      : {len(unwrapped)}')
    print(f'  ⛔ f-string      : {len(fstrings)}   （包了也抽不到，必須改 %(name)s）')
    print(f'  刻意跳過的機器碼: {len(machine)}')
    if sites_b is None:
        print('\n=== 尺 B：babel 未安裝，⚠ 已跳過（不等於通過）===')
    else:
        print(f'\n=== 尺 B：babel（Odoo 的 #. odoo-python 抽取器）===')
        print(f'  抽到 (檔,行) : {len(sites_b)}')
        print(f'  去重 msgid   : {len(ids_b)}')

    if want_list:
        for tag, rows in (('未包', unwrapped), ('f-string', fstrings), ('機器碼', machine)):
            if rows:
                print(f'\n--- {tag} ---')
                for r in rows:
                    print(f'  {r[0]}:{r[1]}  [{r[2]}]  {r[3]}')

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
    if bad:
        print(f'\nFAIL: {bad} 個位置未達成。')
        return 1
    print('\nOK: 所有使用者可見訊息位置都已包 _()，且無 f-string 漏抽。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

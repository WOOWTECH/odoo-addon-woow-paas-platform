#!/usr/bin/env python3
"""驗 cron XML 的接線：`code` 裡的 method 名與 `model_id` 的 ref，都要對得上實體。

## 為什麼這一格非驗不可

method 名拼錯時，`ir.cron` 每次到點都會**靜默失敗**（框架吞例外、只寫 log），
而下面這三件在拼錯的情況下**全部照樣成立**：

  * 模組 `-u` 安裝成功（data 檔 XML 格式沒問題）
  * cron 記錄真的建進 DB（`model_id` 對、`code` 是任意字串，Odoo 不檢查）
  * 單元測試全綠（測試是**直呼** method，根本沒走 ir.cron 那條路）

⇒ 三個綠燈都看不見這一格。而症狀是「cron 看起來裝好了、卻永遠沒有效果」。

## 判準

⛔ 不用 `grep -c`（數量對不代表是哪一個對）。兩邊都**列名**：
  * XML → `xml.etree` 解析出 `code` 與 `model_id/@ref`
  * Python → `ast` 解析出 class 的 `_name` 與所有 `def` 名
再做名字層級的比對，輸出「是哪一個」不是「差幾個」。

## 兩種身分：CI gate 的**唯一實作** ＋ 手動跑的 standalone utility

本檔是 `scripts/audit/repo-lint.py` 第 35 條 `cron-wiring` 的**唯一實作**（#960）：
repo-lint 用 `importlib` 從 sibling 位置載入本檔、呼叫 `check(root=…, verbose=False)`，
⛔ **不複製**下面那段 AST／XML 邏輯過去——兩份實作必然漂移，而漂移的那天兩邊都是綠的。
於是 CI（`.gitea/workflows/repo-lint.yml`）與 PostToolUse hook 都走得到這一格。

本檔仍可獨立執行（下面的用法不變）。🔴 `python_models()` / `cron_records()` /
`check()` 的 `root` 參數預設 `None` ＝ 沿用模組層的 `REPO_ROOT`，所以獨立跑的行為與
輸出**與加入 root 參數之前逐字相同**；只有 repo-lint 會傳 root 進來——它必須能對合成
的 fixture 樹跑，那正是 meta-test 注入壞樣本的方式。

## 用法

    python3 scripts/audit/verify-cron-wiring.py                   # 全 repo 的 ir.cron
    python3 scripts/audit/verify-cron-wiring.py <某個.xml 檔名>    # 只驗一個檔

exit 0 ＝ 全部接得上；exit 1 ＝ 有問題（逐條列名）。

## 涵蓋範圍與已知邊界

* 只驗 `src/data/*.xml` 裡 `model="ir.cron"` 的 record。
* `model_id` 的 ref **帶模組前綴**者（如 `project.model_project_project`）**略過並列名**
  ——那是跨模組 ref，本檔只讀 `src/models/`，看不到別的 addon 的 `_name`。
* 只認 `code` 是 `model.<method>()` 形狀的；其它形狀會被列為「無法驗」而不是靜默放過。
* AST 只看 class 自己 body 裡的 `def`——**繼承來的 method 看不到**。今天 repo 內的
  cron method 都定義在同一個 class 上；若日後有人把 `_cron_*` 放進 mixin，這裡會誤報。
  修法是往 `_inherit` 追，屆時再說。

## 出處

#803（Access 保護的週期性收斂 cron）開發期間寫的一次性檢查器，promote 進 repo 是因為
它守的性質與 #803 無關、對全 repo 的 ir.cron 都適用。當時對自己那筆 cron 做過三格
注入式陽性對照（method 名拼錯／ref 指向不存在的 model／ref 指向**另一個存在的** model），
三格都如期 exit 1 並列出是哪一個錯。詳見
`docs/tracking/landings/803-access-resync-cron-handoff.md`。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

# 🔴 路徑相對**本檔**解析，不寫死絕對路徑：本檔住 `scripts/audit/`，repo 根＝往上兩層。
# 寫死路徑的版本在別的 worktree／別人的機器上會靜默掃到 0 筆 ir.cron，而
# 「0 筆全部接得上」與「真的全部接得上」在輸出上完全一樣。
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / 'src'
CODE_CALL = re.compile(r'^\s*model\.(?P<meth>[A-Za-z_]\w*)\s*\(')


def _src_dir(root=None) -> pathlib.Path:
    """`src/` 的位置。

    `root=None` ＝維持既有行為（相對**本檔**往上兩層的 `REPO_ROOT`）；傳 root 進來
    時改看那棵樹——repo-lint 與 meta-test 的合成 fixture 樹都走這條。
    """
    return SRC if root is None else pathlib.Path(root) / 'src'


def python_models(root=None) -> dict[str, dict]:
    """{_name: {'file':…, 'methods': {…}, 'xmlid': model_…}}，用 AST 不用 grep。"""
    out = {}
    for py in sorted((_src_dir(root) / 'models').glob('*.py')):
        try:
            tree = ast.parse(py.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            name = None
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign)
                        and any(getattr(t, 'id', None) == '_name'
                                for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)):
                    name = stmt.value.value
            if not name:
                continue
            meths = {s.name for s in node.body
                     if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}
            rec = out.setdefault(name, {'file': py.name, 'methods': set(),
                                        'xmlid': 'model_' + name.replace('.', '_')})
            rec['methods'] |= meths
    return out


def cron_records(only: str | None = None, root=None):
    """[(檔名, xmlid, model_ref, code 字串)]，用 XML 解析不用 grep。"""
    out = []
    for xml in sorted((_src_dir(root) / 'data').glob('*.xml')):
        if only and xml.name != only:
            continue
        for rec in ET.parse(xml).iter('record'):
            if rec.get('model') != 'ir.cron':
                continue
            ref = code = None
            for f in rec.findall('field'):
                if f.get('name') == 'model_id':
                    ref = f.get('ref')
                elif f.get('name') == 'code':
                    code = (f.text or '').strip()
            out.append((xml.name, rec.get('id'), ref, code))
    return out


def check(only=None, verbose=True, root=None):
    """回 `(掃到幾筆 ir.cron, 問題訊息 list)`。

    `root=None` ＝ `REPO_ROOT`（獨立跑）；`verbose=False` 關掉 print 副作用
    （repo-lint 走這條——gate 的輸出格式由 repo-lint 決定，不由本檔 print）。
    """
    models = python_models(root)
    by_xmlid = {v['xmlid']: (k, v) for k, v in models.items()}
    problems = []
    checked = 0
    for fname, xmlid, ref, code in cron_records(only, root):
        checked += 1
        label = '%s :: %s' % (fname, xmlid)
        if ref and '.' in ref:          # 跨模組 ref（如 project.model_project_project）
            if verbose:
                print('  ⏭  %-56s 跨模組 ref=%s（本檢查只驗同模組）' % (label, ref))
            continue
        if ref not in by_xmlid:
            problems.append('%s：model_id ref=%r 在 src/models/ 找不到對應的 _name'
                            % (label, ref))
            continue
        mname, info = by_xmlid[ref]
        m = CODE_CALL.match(code or '')
        if not m:
            problems.append('%s：code=%r 不是 `model.<method>()` 形狀，無法驗'
                            % (label, code))
            continue
        meth = m.group('meth')
        if meth not in info['methods']:
            near = sorted(x for x in info['methods'] if x.startswith('_cron'))
            problems.append(
                '%s：code 呼叫 `%s()`，但 %s（%s）上**沒有**這個 method。'
                '該 model 的 _cron* 有：%s'
                % (label, meth, mname, info['file'], ', '.join(near) or '（無）'))
            continue
        if verbose:
            print('  ✅ %-56s ref→%s  code→%s()（%s）'
                  % (label, mname, meth, info['file']))
    return checked, problems


if __name__ == '__main__':
    only = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else None
    print('=== cron 接線檢查（%s）===' % (only or '全 repo 的 ir.cron'))
    n, probs = check(only)
    print('\n檢查了 %d 筆 ir.cron 記錄' % n)
    if probs:
        print('\n⛔ 問題（列名，不是數量）：')
        for p in probs:
            print('  - ' + p)
        sys.exit(1)
    print('✅ 全部接得上')

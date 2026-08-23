#!/usr/bin/env python3
"""解完 merge 衝突後，驗「import 集合 == 兩側聯集」。

⚠ **standalone utility，不是 CI gate。** 沒有任何 workflow 或 hook 會呼叫它；
需要時由人手動跑（用法見下）。**已知盲區列在本 docstring 末尾，用之前先讀。**

## 要解決什麼

`src/tests/__init__.py` 是本 repo 最常見的衝突點（檔尾 append 型）。解衝突時
最自然的驗法是 `grep <我的模組名> src/tests/__init__.py` —— **那個判準在
「我的 import 在、但對方的被我吃掉」時照樣綠**，因為它只問「我的在不在」。

## 判準形狀

不問「我的在不在」，問 **「解完的集合是否恰好等於兩側聯集」**，而且輸出要
**列名**（少了誰、多了誰），不是報數字差。三格判定，不是兩格：
  - 聯集有而解完沒有 → 列名（有人被吃掉）
  - 解完有而兩側都沒有 → 列名（憑空多出來，通常是手打錯字）
  - 重複 import → 列名（兩邊都保留時最容易發生）

## 用法

    git merge origin/develop        # 先 merge，讓它報衝突
    # ...解衝突...
    python3 verify-import-union.py <ours-ref> <theirs-ref> [path]

`<ours-ref>` / `<theirs-ref>` 是 merge 的兩個 parent（例：`60ff2a7a`、`origin/develop`）。

## 已知盲區

- 只認 **`^from . import <name>`** 這個式樣。用 `import x as y`、括號跨行、
  或縮排在 try 區塊裡的 import 一律看不到 ⇒ 換 repo 前先確認式樣還成立。
  **抽取式回 0 就是探針壞了**（本檔會 fail-loud）。
- 只驗 import 的**集合**，不驗**順序**，也不驗每個 import 附帶的註解區塊有沒有被吃掉。
  註解那一半目前沒有機械判準（實務上靠 diff 目視）。
- 不驗 import 指到的模組檔是否真的存在（那是 `--check-baseline` 的工作）。
"""
import re, subprocess, sys

PAT = re.compile(r'^from \. import ([A-Za-z_0-9]+)', re.M)


def from_ref(ref, path):
    r = subprocess.run(['git', 'show', f'{ref}:{path}'], capture_output=True, text=True)
    if r.returncode != 0:                     # 🔴 不檢查 rc 會把「執行失敗」吞成「空集合」
        sys.exit(f'🔴 探針失效：git show {ref}:{path} rc={r.returncode} {r.stderr.strip()}')
    return _parse(r.stdout, f'ref {ref}')


def from_file(path):
    with open(path, encoding='utf-8') as fh:
        return _parse(fh.read(), f'工作樹 {path}')


def _parse(text, who):
    names = PAT.findall(text)
    if not names:                             # 空集合不算通過——這正是探針自毀的形狀
        sys.exit(f'🔴 探針失效：{who} 抽出 0 個 import（式樣可能不再成立）')
    return names


def main():
    ours_ref, theirs_ref = sys.argv[1], sys.argv[2]
    path = sys.argv[3] if len(sys.argv) > 3 else 'src/tests/__init__.py'
    ours, theirs = set(from_ref(ours_ref, path)), set(from_ref(theirs_ref, path))
    resolved_list = from_file(path)
    resolved, union = set(resolved_list), ours | theirs

    print(f'ours({ours_ref})={len(ours)} theirs({theirs_ref})={len(theirs)} '
          f'union={len(union)} resolved={len(resolved)}')
    print('  我方獨有:', sorted(ours - theirs) or 'NONE')
    print('  對方獨有:', sorted(theirs - ours) or 'NONE')

    missing = sorted(union - resolved)
    extra = sorted(resolved - union)
    dups = sorted({n for n in resolved_list if resolved_list.count(n) > 1})
    print('🔴 聯集有而解完沒有:', missing or 'NONE')
    print('🔴 解完多出來（不在任一側）:', extra or 'NONE')
    print('🔴 重複 import:', dups or 'NONE')

    ok = not (missing or extra or dups)
    print('==>', 'PASS 集合相等' if ok else '🔴 FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""驗 baseline 檔「只增不減」——對**多個**參照側各驗一次。

⚠ **standalone utility，不是 CI gate。** 沒有任何 workflow 或 hook 會呼叫它；
需要時由人手動跑（用法見下）。**已知盲區列在本 docstring 末尾，用之前先讀。**

## 要解決什麼

`scripts/audit/baselines/test-collection.txt` 是排序後的清單，兩邊都純新增時
**git 會自動合併成功、`git status` 乾乾淨淨** —— 但那不代表沒人被吃掉：
`--update-baseline` 重產、rebase、或手動編輯都可能靜默移除別人的列。
`git status` 判準對這件事**零鑑別力**。

## 判準形狀

對**每一個**參照側（merge-base、我方 parent、對方 parent）各問兩件事，**列名**：
  - 該側有而現在沒有 → 丟失（列名）
  - 兩側都有但條數變小 → 調降（列名）

多側都要驗的理由：只驗 merge-base 會漏掉「對方 parent 在 merge-base 之後新增、
而我把它吃掉了」那一格 —— 那正是 merge 最容易出事的方向。

🔴 **判定要有第三格**：除了「通過／不通過」，還要有「探針壞了」。
本檔的作法是**任一側解析出 0 筆就 fail-loud**：
對空集合做「有沒有丟失」的比對**恆真**，看起來與通過**完全一樣**。

## 用法

    python3 verify-baseline-monotonic.py <ref1> [<ref2> ...] -- [path]

例（#36 PR #879 解衝突當時）：

    python3 verify-baseline-monotonic.py d559aaa4 60ff2a7a origin/develop

## 已知盲區

- 只驗「不減」，**不驗「該增的有沒有增」** —— 那是
  `test_collection_audit.py --check-baseline`（比對原始碼）與 `--check-executed`
  （比對真實執行 log）的工作，本檔不重複。
- 條數相等但**內容語意改變**（同名 class 換了測試方法）看不出來。
- 只認 `<count>\t<name>` 這個格式；換格式會讓解析回 0 筆 ⇒ 會 fail-loud（刻意）。

## 這支的由來

2026-08-23（#36）第一版寫成 inline 而且**沒有檢查 `subprocess` 的 returncode**，
配上一個組錯的 ref（shell 的 `$( ...; ... )` 讓變數變成兩行 sha），
`git show` 失敗 → stdout 空 → 解析出 0 筆 → 輸出「丟失:NONE 條數調降:NONE」。
**看起來完全像通過。** 抓到它的不是 exit code，是
「其他三個數字都合理、唯獨 merge-base 那側是 0」。
"""
import io, subprocess, sys

DEFAULT_PATH = 'scripts/audit/baselines/test-collection.txt'


def _parse(text, who):
    d = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        count, name = line.split('\t', 1)
        d[name.strip()] = int(count)
    if not d:                                  # 🔴 空集合不算通過
        sys.exit(f'🔴 探針失效：{who} 解析出 0 筆（ref 錯？格式變了？）')
    return d


def from_ref(ref, path):
    r = subprocess.run(['git', 'show', f'{ref}:{path}'], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'🔴 探針失效：git show {ref}:{path} rc={r.returncode} {r.stderr.strip()}')
    return _parse(r.stdout, f'ref {ref}')


def main():
    argv = sys.argv[1:]
    path = DEFAULT_PATH
    if '--' in argv:
        i = argv.index('--')
        if argv[i + 1:]:
            path = argv[i + 1]
        argv = argv[:i]
    if not argv:
        sys.exit('用法：verify-baseline-monotonic.py <ref1> [<ref2> ...] -- [path]')

    now = _parse(io.open(path, encoding='utf-8').read(), f'工作樹 {path}')
    print(f'現在（工作樹）= {len(now)} 筆   path={path}')

    failed = False
    for ref in argv:
        side = from_ref(ref, path)
        lost = sorted(set(side) - set(now))
        low = sorted(k for k in set(side) & set(now) if now[k] < side[k])
        gained = sorted(set(now) - set(side))
        print(f'  vs {ref:<20} ({len(side)} 筆)  丟失:{lost or "NONE"}  條數調降:{low or "NONE"}')
        print(f'     新增（列名）: {gained or "NONE"}')
        if lost or low:
            failed = True

    print('==>', '🔴 FAIL' if failed else 'PASS 三側皆只增不減')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

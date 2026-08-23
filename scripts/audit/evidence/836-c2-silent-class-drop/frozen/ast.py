"""🔴 執行守衛（issue #836）—— 這不是 stdlib 的 `ast`，是**故意**的同名遮蔽。

## 為什麼會有這個檔

本目錄的四個 `*.py.frozen` 是**凍結件**：它們是產出 `../evidence/*.txt` 的那一版
原始碼，內容一個位元組都不能改（改了就把「當時量到什麼」這件事一起改掉了）。

問題是它們有兩個寫死的絕對路徑：

    ROOT = pathlib.Path("/Users/eugene/.../woow-addons/wt-836")     # 三支都有
    OUT  = pathlib.Path("/private/tmp/claude-501/.../256a4fa3-.../scratchpad/836")

⇒ 它們**不量自己所在的那棵樹**，量的是 `wt-836` 當下的內容；而 `t1_p9_p10` 還會
**寫**回那個舊 session 的 scratchpad。其中最危險的不是會炸的那支——是
`t1_universe`：它 `exit 0`、印出一份看起來完全正常的數字，而那份數字來自別棵樹。

（2026-08-23 實例：有人為了覆核跑了 `t1_p9_p10.py`，它覆寫了舊 session 的
`p9_output.txt` / `p10_output.txt`。內容還原得回去，**mtime 還不回去**。）

## 它怎麼運作

`python3 <某檔>` 會把**該檔所在目錄**放進 `sys.path[0]`，而 `ast` 不在直譯器啟動時
預載（實測 `'ast' in sys.modules` 為 `False`）⇒ 從本目錄執行任何腳本時，`import ast`
會先撿到這個檔，於是在**第一行 import 就大聲死掉**，走不到任何寫入點。
本目錄三支 `t1_*` 的第一個 import 都是 `ast`，所以三支全被涵蓋。

## 它的失效模式（必須寫出來，否則它就是下一個假綠）

- `python3 -P` 或 `PYTHONSAFEPATH=1` 會關掉 `sys.path[0]` 注入 ⇒ **本守衛不會觸發**。
- 未來的 Python 若把 `ast` 改成啟動預載，也一樣不會觸發。

⇒ 所以它**不是唯一防線**：主要防線是「本目錄沒有任何 `*.py` 檔（除了本檔）」，
凍結件一律 `*.py.frozen`，讓習慣性的 `python3 <檔>.py` 直接 `No such file or directory`。
本守衛擋的是「刻意去跑 `.frozen`」與「把它改名回 `.py`」那兩條路。

兩層都由 `scripts/audit/test_836_frozen_evidence_not_runnable.py` 逐條把關（CI 每次跑）。

## 要重跑這些量測怎麼辦

**不要改這裡的檔**。複製一份到別處，改掉 `ROOT` / `OUT` 再跑。
README 的「逐檔實測」表寫著每一支現在的實際行為。
"""
import sys

_MSG = """\
FROZEN-EVIDENCE-GUARD-836: 這個目錄裡的檔是凍結件，不可直接執行。

  你剛才執行的腳本寫死了絕對路徑（ROOT 指向 wt-836，OUT 指向某個舊 session 的
  scratchpad），所以它量的不是你所在的那棵樹，而且其中一支會**寫**回那個舊路徑。

  要重跑：複製一份到別處，改掉 ROOT / OUT 再跑。
  每一支現在的實際行為見 ../README.md 的「逐檔實測」表。
"""
print(_MSG, file=sys.stderr)
raise SystemExit(97)

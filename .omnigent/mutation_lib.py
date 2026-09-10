"""突變測試的共用套用函式：**在改檔之前**斷言定位字串仍然有效。

⚠ 檔名用底線（不是 repo 在 `.omnigent/` 慣用的連字號）因為它**要被 import**——
`mutation-lib.py` 這種名字 `import` 不進來。

## 為什麼這道斷言要存在（三次實際失效）

突變測試是所有 CEL 與 dispatcher 守線的守線；而突變本身靠「在目標檔案裡找一段字串再改掉」
定位。那個定位字串會隨著被測程式重構而失效，**而失效的症狀是突變靜默變成 no-op**
——測試照樣顯示綠，但它其實什麼都沒驗。本 repo 已經踩過三次：

| 輪次 | 突變 | 失效方式 |
| --- | --- | --- |
| FIX ROUND 5 | `G` | `sed` 的目標字串被改掉 ⇒ no-op 卻顯示 PASS |
| FIX ROUND 7 | `Y` | 比對的是行尾註解的層號字母（`# C`），重編層號後 ⇒ no-op |
| FIX ROUND 8 | `Z1` | 字串變成兩處，`replace(…, 1)` 改到了**錯的函式** |

三次都是靠人工稽核或下游指紋斷言**碰巧**接住。依 `CLAUDE.md`
「可機械判定 → 加 check，gate 比散文強」，這條升成 gate。

## 斷言內容（套用前，fail loud）

1. 定位字串命中次數 **≥ 1**——命中 0 ＝ 它已經失效了。
2. 命中 **> 1 次**時**必須**同時給錨點（`anchor=`，通常是所屬函式的 `def` 行）；
   只給字串不給錨點 ⇒ 報錯。這正是 `Z1` 那次事故：兩處都中，`replace(…, 1)` 改了前面那個。
3. 給了錨點時，錨點本身也必須命中，且要在錨點**之後**才開始找定位字串。

⛔ 這些一律 `raise`（不是 warning）。`mutation-tests.sh` 會把這個例外當成該 case 的
硬失敗並把訊息原樣印出來——⛔ 不可以降級成「no-op 警告」，那就退回成我們要修的那個問題。

⚠ 這是**加強**而不是取代 `mutation-tests.sh` 裡的 `_tree_hash` no-op 守衛：那一層抓的是
「改完內容或權限卻沒有實際差異」（例如突變 N 只 `chmod`），兩層管不同的事。
"""

from __future__ import annotations

import os
from pathlib import Path


class MutationLocatorError(RuntimeError):
    """定位字串已失效，或命中多處卻沒給錨點。"""


def _mut() -> str:
    """目前在跑哪個突變（`mutation-tests.sh` 以 `MUT_CODE` 傳進來）。"""
    return os.environ.get("MUT_CODE") or "<未知突變>"


def _excerpt(needle: str, limit: int = 90) -> str:
    """把（可能多行的）定位字串壓成一行，方便放進錯誤訊息。"""
    flat = " ⏎ ".join(line.strip() for line in needle.splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _locate(text: str, needle: str, anchor: str | None, *, what: str,
            path: str = "") -> int:
    """回傳 `needle` 在 `text` 裡的起始位置，套用前先跑完三條斷言。"""
    where = f"（{path}）" if path else ""
    if not needle:
        raise MutationLocatorError(f"突變 {_mut()}{where}：{what}是空字串")
    hits = text.count(needle)
    if hits == 0:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：{what}在目標檔案裡**命中 0 次**（已失效）\n"
            f"    字串: {_excerpt(needle)}\n"
            "    ⇒ 被測程式重構後這個突變已經變成 no-op，它宣稱要驗的那道守線其實沒被驗到。"
            "請更新定位字串。"
        )
    if hits > 1 and anchor is None:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：{what}**命中 {hits} 次**卻沒給錨點\n"
            f"    字串: {_excerpt(needle)}\n"
            "    ⇒ 這樣會改到其中任意一處（很可能是錯的那一處）。"
            "請加 `anchor=` 指定所屬函式的 def 行，或把字串改成唯一。"
        )
    if anchor is None:
        return text.index(needle)
    if anchor not in text:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：錨點在目標檔案裡命中 0 次（已失效）\n"
            f"    錨點: {_excerpt(anchor)}"
        )
    start = text.index(anchor)
    try:
        return text.index(needle, start)
    except ValueError:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：{what}在錨點之後找不到\n"
            f"    錨點: {_excerpt(anchor)}\n"
            f"    字串: {_excerpt(needle)}"
        ) from None


def edit(path: str | Path, *, old: str, new: str, anchor: str | None = None) -> None:
    """把 `old` 的**第一個有效命中**換成 `new`，套用前跑完定位斷言。

    :param anchor: 當 `old` 命中多處時**必填**——通常是所屬函式的 `def …(` 那一行。
    """
    target = Path(path)
    text = target.read_text()
    i = _locate(text, old, anchor, what="定位字串", path=str(target))
    target.write_text(text[:i] + new + text[i + len(old):])


def edit_span(path: str | Path, *, start: str, through: str, new: str,
              anchor: str | None = None) -> None:
    """把「`start` 開頭、到 `through` 結尾」這一整段換成 `new`。

    用於那種「要連同後面一段可變內容一起換掉」的突變（例如把一個長 regex 的
    alternation 整段砍短）。`through` 在 `start` **之後**才開始找。
    """
    target = Path(path)
    text = target.read_text()
    i = _locate(text, start, anchor, what="區間起點", path=str(target))
    try:
        j = text.index(through, i) + len(through)
    except ValueError:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：區間終點在起點之後找不到\n"
            f"    起點: {_excerpt(start)}\n"
            f"    終點: {_excerpt(through)}"
        ) from None
    target.write_text(text[:i] + new + text[j:])

"""突變測試的共用套用函式：**在改檔之前**斷言定位字串仍然有效。

規則只有一條：**定位字串必須在目標檔案裡全域命中恰好 1 次。** 0 次或 >1 次一律 fail loud。

## 為什麼這道斷言要存在（三次實際失效）

突變測試是所有 CEL 與 dispatcher 守線的守線；而突變本身靠「在目標檔案裡找一段字串再改掉」
定位。那個定位字串會隨著被測程式重構而失效，**而失效的症狀是突變靜默變成 no-op 或改到錯的
位置**——測試照樣顯示綠，但它其實什麼都沒驗。本 repo 已經踩過三次：

| 輪次 | 突變 | 失效方式 |
| --- | --- | --- |
| FIX ROUND 5 | `G` | `sed` 的目標字串被改掉 ⇒ no-op 卻顯示 PASS |
| FIX ROUND 7 | `Y` | 比對的是行尾註解的層號字母（`# C`），重編層號後 ⇒ no-op |
| FIX ROUND 8 | `Z1` | 字串變成兩處，`replace(…, 1)` 改到了**錯的函式** |

依 `CLAUDE.md`「可機械判定 → 加 check，gate 比散文強」，這條升成 gate。

## ⛔ 為什麼**不**提供 `anchor=`（縮範圍）機制——連補三輪的教訓

ROUND 9 加了 `anchor=`：定位字串命中多處時，給一個錨點（通常是所屬函式的 `def` 行）把搜尋
範圍縮到「錨點所屬區塊」。區塊邊界用縮排判斷。然後：

| 輪次 | 補了什麼 | 立刻冒出的新洞 |
| --- | --- | --- |
| ROUND 9 | `anchor=` ＋ 縮排區塊 | 錨點**自己**不唯一；定位字串落在區塊**外**也會被改 |
| ROUND 10 | 錨點須唯一 ＋ 定位字串須區塊內唯一 | 函式體內的**頂格註解**、**shell heredoc** 讓區塊提早截斷 |
| ROUND 11 | 跳過註解行 ＋「縮排不可信就拒絕」 | **註解裡的假三引號**把奇偶判斷帶歪；heredoc 內容裡**縮排的假結束符**被誤認 |

⚠ 每補一次就冒一個新洞，**那是抽象錯了的訊號，不是實作不夠仔細**。根因是：想用「不解析
語法的啟發式」去判斷「語法上的區塊邊界」，這件事本身不可能 sound——而且截斷的後果**不是
漏報，是把該報的錯變成靜默改錯**（本該報「區塊內歧義」的情況只看到截斷點前面那一個命中）。

⇒ ROUND 12 **整套拆掉**。改用「全域命中恰好 1 次」這個**sound 但略寬**的規則：
全域唯一的字串只可能對到一個位置，**不需要知道任何語法**——沒有邊界要猜，就沒有猜錯的洞。

**代價**：定位字串會長一點（要把足夠的上下文包進去才唯一）。實測只有 2 個突變受影響
（`Z1`／`AC`），兩者加長後解析到的行號與之前完全相同。為了那 2 個去養一個永遠補不完的
啟發式，不划算。

⛔ 所以：**不要再加 `anchor=` 或任何「縮範圍」參數。** 需要定到特定位置就把定位字串加長到
全域唯一；需要連同後面一段可變內容一起換掉就用 `edit_span()`。

## 斷言內容（套用前，fail loud）

1. 定位字串**命中 0 次** ⇒ 它已經失效了（被測程式重構過）。
2. 定位字串**命中 >1 次** ⇒ 會改到其中任意一處，很可能是錯的那一處。

⛔ 這些一律 `raise`（不是 warning）。`mutation-tests.sh` 會把這個例外當成該 case 的
硬失敗並把訊息原樣印出來——⛔ 不可以降級成「no-op 警告」，那就退回成我們要修的那個問題。

⚠ 這是**加強**而不是取代 `mutation-tests.sh` 裡的 `_tree_hash` no-op 守衛：那一層抓的是
「改完內容或權限卻沒有實際差異」（例如突變 `N` 只 `chmod`），兩層管不同的事。

⚠ 檔名用底線（不是 repo 在 `.omnigent/` 慣用的連字號）因為它**要被 import**。
"""

from __future__ import annotations

import os
from pathlib import Path


class MutationLocatorError(RuntimeError):
    """定位字串已失效（命中 0 次），或不唯一（命中 >1 次）。"""


def _mut() -> str:
    """目前在跑哪個突變（`mutation-tests.sh` 以 `MUT_CODE` 傳進來）。"""
    return os.environ.get("MUT_CODE") or "<未知突變>"


def _excerpt(needle: str, limit: int = 90) -> str:
    """把（可能多行的）定位字串壓成一行，方便放進錯誤訊息。"""
    flat = " ⏎ ".join(line.strip() for line in needle.splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _locate(text: str, needle: str, *, what: str, path: str = "") -> int:
    """回傳 `needle` 在 `text` 裡的位置；**全域命中恰好 1 次**才算有效。"""
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
    if hits > 1:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：{what}**命中 {hits} 次**（不唯一）\n"
            f"    字串: {_excerpt(needle)}\n"
            "    ⇒ 這樣會改到其中任意一處（很可能是錯的那一處）。"
            "請把定位字串**加長到全域唯一**（把上下文包進來，多行也可以）。\n"
            "      ⛔ 不要改用「縮範圍」的錨點機制——那條路連補三輪每次都冒新洞，"
            "已於 FIX ROUND 12 拆除，理由見本檔檔頭。"
        )
    return text.index(needle)


def edit(path: str | Path, *, old: str, new: str) -> None:
    """把 `old` 換成 `new`，套用前先斷言 `old` 在目標檔案裡全域唯一。

    :param old: 定位字串，**必須全域唯一**。不唯一就把上下文包進來加長它（多行可以）。
    """
    target = Path(path)
    text = target.read_text()
    i = _locate(text, old, what="定位字串", path=str(target))
    target.write_text(text[:i] + new + text[i + len(old):])


def edit_span(path: str | Path, *, start: str, through: str, new: str) -> None:
    """把「`start` 開頭、到 `through` 結尾」這一整段換成 `new`。

    用於那種「要連同後面一段可變內容一起換掉」的突變（例如把一個長 regex 的 alternation
    整段砍短）。`start` 必須全域唯一；`through` 在 `start` **之後**才開始找。
    """
    target = Path(path)
    text = target.read_text()
    i = _locate(text, start, what="區間起點", path=str(target))
    try:
        j = text.index(through, i) + len(through)
    except ValueError:
        raise MutationLocatorError(
            f"突變 {_mut()}（{target}）：區間終點在起點之後找不到\n"
            f"    起點: {_excerpt(start)}\n"
            f"    終點: {_excerpt(through)}"
        ) from None
    target.write_text(text[:i] + new + text[j:])

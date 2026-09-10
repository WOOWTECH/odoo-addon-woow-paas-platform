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
3. **錨點自己也必須唯一**（它也是定位字串）。
4. 定位字串必須落在錨點所屬**區塊內**（縮排判定，見 `_block_bounds`）**且區塊內唯一**——
   `anchor=` 的語意是「縮範圍」，⛔ 不是「從這裡往後找第一個」。

⚠ 第 3、4 條是 FIX ROUND 10 補的：ROUND 9 只做 `anchor in text` ＋ `index(needle, start)`，
於是 anchor 不唯一時會靜默改到第一個區塊，而 needle 只要在 anchor **後面任何地方**就會被改。
那等於把「定位字串會靜默失效」往上推一層（改靠 anchor，但 anchor 自己沒人守），不是解決。

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


def _indent_of(line: str) -> int:
    """行首空白的寬度（tab 當 8 欄，與 Python／YAML 的慣例一致）。"""
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 8 - (width % 8)
        else:
            break
    return width


def _block_bounds(text: str, anchor_idx: int) -> tuple[int, int]:
    """回傳 anchor 所屬區塊的 `[起, 迄)` 字元範圍。

    規則：**從 anchor 那一行開始，到下一個「縮排 ≤ anchor 行縮排」的非空行為止**（或 EOF）。
    空行跳過（它不代表區塊結束）。

    ⚠ 刻意選這個通用規則而不是語言特定的語法解析：本專案的突變目標檔有 Python、YAML 與
    shell，三者都是縮排表達層級 ⇒ 同一條規則就夠，而且不需要為每種語言養一個 parser。
    """
    line_start = text.rfind("\n", 0, anchor_idx) + 1
    lines = text[line_start:].splitlines(keepends=True)
    if not lines:
        return line_start, len(text)
    base = _indent_of(lines[0])
    pos = line_start + len(lines[0])
    for line in lines[1:]:
        stripped = line.strip()
        # 空行與「整行只有註解」的行在 Python／YAML／shell **都不終止區塊** ⇒ 跳過。
        # ⚠ 不跳過的話，函式體內一個頂格註解就會把區塊提早截斷（實測：覆核者的
        #   dedented-comment fixture 會因此靜默改到截斷點前面那一個）。
        if not stripped or stripped.startswith("#"):
            pos += len(line)
            continue
        if _indent_of(line) <= base:
            return line_start, pos
        pos += len(line)
    return line_start, len(text)


#: 讓「用縮排判斷區塊邊界」不再可信的訊號。出現任一個就**拒絕使用 anchor 機制**。
#: ⚠ 刻意不為 Python／YAML／shell 各養一個語法解析器——那是又一個會漂的啟發式。
#:    原則是「**不確定就 fail loud，不要猜**」：能可靠判斷的情境用縮排規則，不能的退回到
#:    更嚴的「定位字串必須全域唯一」。


def _unclosed_in(block: str, token: str) -> bool:
    """區塊裡這個多行字串起訖符**有沒有成對收掉**。

    奇數次 ⇒ 有一個開了沒關 ⇒ 區塊邊界很可能就截在那個字串中間 ⇒ 縮排不可信。
    偶數次 ⇒ 字串完整含在區塊內，它外面的縮排仍然可信。
    """
    return block.count(token) % 2 == 1


def _heredoc_unclosed(block: str) -> bool:
    """區塊裡有 heredoc 起始、但**結束符不在區塊內**。

    結束符不在區塊內 ⇒ 邊界截在 heredoc 內容中間（內容常在第 0 欄）⇒ 縮排不可信。
    """
    import re as _re

    for m in _re.finditer(r"<<-?\s*([\"\']?)([A-Za-z_][A-Za-z0-9_]*)\1", block):
        delim = m.group(2)
        rest = block[m.end():]
        if not any(line.strip() == delim for line in rest.splitlines()):
            return True
    return False


def _indent_untrustworthy(block: str) -> str | None:
    """區塊裡有沒有讓縮排判斷失效的東西？回傳原因，或 `None`。

    三種訊號（都實測過會讓縮排規則算錯邊界）：

      - **前導空白混用 tab 與空格**：同一個縮排深度會算出兩種數字 ⇒ 無條件不可信。
      - **heredoc 起始而結束符不在區塊內**：內容常在第 0 欄，邊界會截在內容中間。
      - **多行字串起訖符在區塊內出現奇數次**：有一個開了沒關 ⇒ 邊界截在字串中間。

    ⚠ 後兩者刻意用「**有沒有收掉**」而不是「**有沒有出現**」來判斷（FIX ROUND 11）：
    只要出現就拒絕的話，任何帶 docstring 的函式都無法用 anchor——實測會退回 `Z1`／`AC`
    這兩個**已經驗證過定位正確**的突變。而「收掉了」本身就證明邊界沒截在裡面，
    那時縮排仍然可信 ⇒ 不需要退回。⛔ 這不是語法解析，只是數有沒有成對。
    """
    for line in block.splitlines():
        lead = line[: len(line) - len(line.lstrip())]
        if "\t" in lead and " " in lead:
            return "有一行的前導空白混用 tab 與空格"
    if _heredoc_unclosed(block):
        return "區塊內有 heredoc 起始（`<<`）而結束符不在區塊內"
    for token, name in (('"""', "三個雙引號"), ("'''", "三個單引號")):
        if _unclosed_in(block, token):
            return "區塊內的多行字串起訖符（" + name + "）出現奇數次（開了沒關）"
    return None


def _locate(text: str, needle: str, anchor: str | None, *, what: str,
            path: str = "") -> int:
    """回傳 `needle` 在 `text` 裡的起始位置，套用前先跑完全部斷言。

    ⚠ FIX ROUND 10 補了 anchor 自己的兩個漏。ROUND 9 的版本只做 `anchor in text` 然後
    `text.index(needle, start)`，於是：

      - **anchor 自己不唯一**時，靜默改到第一個 anchor 之後的命中；
      - needle 只要出現在 anchor **後面任何地方**就會被改——`anchor=` 從「縮範圍」
        變成「往後找第一個」，一個遙遠的同名字串就會被默默改掉。

    ⇒ 那等於把「定位字串會靜默失效」這個問題**往上推一層**（needle 不可靠就改靠 anchor，
    但 anchor 本身也是定位字串、而且沒人守），不是解決。現在 anchor 必須唯一，且 needle
    必須落在 anchor 所屬**區塊內**並在區塊內唯一。
    """
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

    # ── anchor 自己也是定位字串，所以也要守 ───────────────────────────────────
    anchor_hits = text.count(anchor)
    if anchor_hits == 0:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：錨點在目標檔案裡命中 0 次（已失效）\n"
            f"    錨點: {_excerpt(anchor)}"
        )
    if anchor_hits > 1:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：錨點**命中 {anchor_hits} 次**（不唯一）\n"
            f"    錨點: {_excerpt(anchor)}\n"
            "    ⇒ 錨點不唯一的話會縮到第一個區塊，可能不是你要的那個。"
            "請把錨點改成唯一（例如帶上完整的 def 簽名）。"
        )

    # ── needle 必須落在 anchor 所屬區塊內，且區塊內唯一 ───────────────────────
    bs, be = _block_bounds(text, text.index(anchor))
    block = text[bs:be]

    # ── 縮排不可信時**拒絕 anchor 機制**，改要求定位字串全域唯一 ───────────────
    # ⚠ 為什麼不是「盡力猜一個邊界」：算錯邊界的症狀是把「區塊內歧義」這個**該報的錯**
    #   變成**靜默改到截斷點前面那一個**（實測：覆核者的 dedented-comment 與 heredoc
    #   fixture 都默默改到第 2 行）。那正是這道 gate 要消除的性質。
    reason = _indent_untrustworthy(block)
    if reason is not None:
        if hits == 1:
            return text.index(needle)   # 全域唯一 ⇒ 不靠 anchor 也能唯一定位
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：縮排無法可靠判斷區塊邊界，**不能用 anchor 縮範圍**\n"
            f"    原因: {reason}\n"
            f"    錨點: {_excerpt(anchor)}\n"
            f"    字串: {_excerpt(needle)}（全域命中 {hits} 次）\n"
            "    ⇒ 這種內容下用縮排算出的邊界可能提早截斷，而截斷的後果是「本該報"
            "『區塊內歧義』的情況變成靜默改到前面那一個」。\n"
            "      請把定位字串加長到**全域唯一**（不靠 anchor），或改用 edit_span()。"
        )
    in_block = block.count(needle)
    if in_block == 0:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：{what}**不在錨點所屬區塊內**\n"
            f"    錨點: {_excerpt(anchor)}\n"
            f"    字串: {_excerpt(needle)}\n"
            "    ⇒ 它在檔案別處有命中，但不在這個區塊裡。`anchor=` 的語意是**縮範圍**，"
            "不是「從這裡往後找第一個」⇒ 請確認錨點選對了，或改用唯一的定位字串。"
        )
    if in_block > 1:
        raise MutationLocatorError(
            f"突變 {_mut()}{where}：{what}在錨點所屬區塊內**命中 {in_block} 次**（仍歧義）\n"
            f"    錨點: {_excerpt(anchor)}\n"
            f"    字串: {_excerpt(needle)}\n"
            "    ⇒ 縮到這個區塊之後還是不唯一。請把定位字串加長到唯一。"
        )
    return bs + block.index(needle)


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

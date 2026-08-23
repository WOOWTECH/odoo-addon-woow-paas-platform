#!/usr/bin/env python3
"""守衛：`evidence/836-c2-silent-class-drop/` 的凍結件不得被執行成功（issue #836）。

## 它在守什麼

那個目錄的凍結件寫死了兩個絕對路徑（`ROOT` 指向 `wt-836`、`OUT` 指向某個舊 session
的 scratchpad）⇒ 它們**不量自己所在那棵樹**，而且其中一支會**寫**回舊路徑。

🔴 最危險的不是會炸的那支，是 `t1_universe`：修法前它 `exit 0`、印出一份看起來完全
   正常的數字，而那份數字來自別棵樹。**rc=0 與「量對了」不可區分**——這與「拿到零／空
   先假設探針壞了」是同一個簽名的另一面：**拿到 rc=0 也要問它量的是哪棵樹**。

（2026-08-23 實例：有人為了覆核跑了 `t1_p9_p10.py`，覆寫了舊 session 的
`p9_output.txt` / `p10_output.txt`。內容還原得回去，**mtime 還不回去**。）

## 兩層防線，本檔逐層驗

1. **改名**：凍結件一律 `*.py.frozen`，目錄裡除了守衛本身沒有任何 `*.py`
   ⇒ 習慣性的 `python3 <檔>.py` 直接 `No such file or directory`（rc=2）。
2. **同名遮蔽守衛** `frozen/ast.py`：`python3 <某檔>` 會把該檔所在目錄放進
   `sys.path[0]`，而 `ast` 不在啟動時預載 ⇒ 從該目錄執行任何腳本時 `import ast`
   先撿到守衛，在第一行 import 就 `SystemExit(97)`，走不到任何寫入點。

⚠ 第 2 層有明確的失效模式（`python3 -P`／`PYTHONSAFEPATH=1`，或未來 Python 預載 `ast`）。
  所以本檔的 (C) 是**活體 canary**：真的去執行一支**靜態證明沒有寫入點**的凍結件，
  並要求輸出裡出現守衛的標記字串。只看 rc 不看標記會假綠——在 CI 上 `ROOT` 那個路徑
  根本不存在，凍結件會因 `FileNotFoundError` 自己非零，**與守衛有沒有觸發不可區分**。

## 四格

    (A) 沒有 `*.py`（守衛本身除外）—— 第 1 層
    (B) 凍結件內容沒被動過（sha256 釘死）—— 「凍結」這個詞的機械定義
    (C) 活體 canary：跑 `t1_universe.py.frozen`，必須非零**且**輸出含守衛標記 —— 第 2 層
    (D) 陽性對照：合成一個會寫檔的 victim，有守衛時不得寫成、移走守衛後必須寫成
        —— 沒有這格，(C) 的綠燈可能只代表「那支檔剛好會壞」

⛔ 本檔**不執行** `t1_p9_p10.py.frozen`：它 `:63`／`:81` 會寫死路徑。改用靜態斷言
   「它的第一個 import 是 `ast`」來確認守衛涵蓋得到它。

Refs #836
"""
from __future__ import annotations

import ast as _ast          # 本檔不在受守衛目錄下，這裡拿到的是 stdlib 的 ast
import hashlib
import pathlib
import subprocess
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
FROZEN_DIR = _HERE / "evidence" / "836-c2-silent-class-drop" / "frozen"
GUARD = FROZEN_DIR / "ast.py"
GUARD_MARKER = "FROZEN-EVIDENCE-GUARD-836"
GUARD_RC = 97

#: 凍結件的內容指紋。**這就是「凍結」的機械定義**——散文說「刻意保留原件不改」
#: 攔不住任何人，這一格攔得住。任何一支被動過，本檔紅。
FROZEN_SHA256 = {
    "red_836_silent_class_drop.py.frozen":
        "7bd322ac298a404db547d82952059065d0db12640a4e7a3f2a1d895c82f60a85",
    "t1_p9_p10.py.frozen":
        "2dd409811c1222ebf7c4f5e24497ea0381a857dc4e4a28cb141fbdc429999c71",
    "t1_partition.py.frozen":
        "1b6ba601bafd02322e68b03bc68a9a7babbf8602d75868a63d350f7e8c44d614",
    "t1_universe.py.frozen":
        "13fc21b97030cb07fb2924e5d7057cdd789148c751bbe7f430c59204097c279d",
}

#: (C) 的 canary 只能挑**靜態證明沒有任何寫入點**的那一支。挑錯就是拿 CI 去寫別人的檔。
CANARY = "t1_universe.py.frozen"
#: 有寫入點、⛔ 一律不執行的那些
NEVER_RUN = ("t1_p9_p10.py.frozen",)

_WRITE_ATTRS = {"write_text", "write_bytes", "touch", "mkdir", "rename", "unlink",
                "writelines", "symlink_to", "rmdir"}

VICTIM = """\
import ast, sys, pathlib
pathlib.Path(sys.argv[1]).write_text("victim wrote this\\n")
print("VICTIM RAN")
"""


def _run(path: pathlib.Path, *args: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(path), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def _write_calls(src: str) -> list[int]:
    """回傳寫入類呼叫的行號（AST，不是 grep —— `grep -c` 對這種查詢兩個方向都會錯）。"""
    tree = _ast.parse(src)
    out = []
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute) \
                and n.func.attr in _WRITE_ATTRS:
            out.append(n.lineno)
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id == "open":
            mode = ""
            if len(n.args) > 1 and isinstance(n.args[1], _ast.Constant):
                mode = str(n.args[1].value)
            for kw in n.keywords:
                if kw.arg == "mode" and isinstance(kw.value, _ast.Constant):
                    mode = str(kw.value.value)
            if any(c in mode for c in "wax+"):
                out.append(n.lineno)
    return sorted(out)


def main() -> int:  # noqa: C901
    failures: list[str] = []
    print("=" * 74)
    print("凍結證據件不可執行守衛（#836）")
    print("=" * 74)

    if not FROZEN_DIR.is_dir():
        print("FATAL: 找不到 %s —— 被守護的東西不在，空跑不算通過" % FROZEN_DIR)
        return 2
    if not GUARD.is_file():
        print("FATAL: 守衛 %s 不存在" % GUARD)
        return 2

    # ── (A) 目錄裡不得有 *.py（守衛本身除外）─────────────────────────────
    print("\n(A) 第 1 層：目錄裡有沒有 `*.py`（習慣路徑）")
    stray = sorted(p.name for p in FROZEN_DIR.glob("*.py") if p.name != GUARD.name)
    print("    守衛以外的 .py : %s" % (stray or "（無）"))
    if stray:
        failures.append(
            "(A) `%s/` 底下出現 `*.py`：%s —— 習慣性的 `python3 <檔>.py` 又打得到了。"
            "凍結件一律要 `*.py.frozen`" % (FROZEN_DIR.name, stray))

    # ── (B) 內容沒被動過 ────────────────────────────────────────────────
    print("\n(B) 凍結件內容指紋")
    on_disk = sorted(p.name for p in FROZEN_DIR.glob("*.frozen"))
    if on_disk != sorted(FROZEN_SHA256):
        failures.append("(B) 凍結件清單與釘死的名單不符：磁碟=%s 名單=%s"
                        % (on_disk, sorted(FROZEN_SHA256)))
    for name, want in sorted(FROZEN_SHA256.items()):
        p = FROZEN_DIR / name
        if not p.is_file():
            failures.append("(B) 凍結件不見了：%s" % name)
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        print("    %-38s %s" % (name, "OK" if got == want else "🔴 %s" % got[:16]))
        if got != want:
            failures.append("(B) %s 內容被動過（sha256 %s ≠ 釘死的 %s）"
                            "—— 凍結件一改，`evidence/*.txt` 就不再是它產出的了"
                            % (name, got[:16], want[:16]))

    # ── ⛔ 會寫檔的那些：靜態確認守衛涵蓋得到，不執行 ────────────────────
    print("\n(⛔) 有寫入點、一律不執行的凍結件")
    for name in NEVER_RUN:
        p = FROZEN_DIR / name
        if not p.is_file():
            failures.append("(⛔) %s 不見了" % name)
            continue
        src = p.read_text(encoding="utf-8")
        wl = _write_calls(src)
        imports = [n for n in _ast.parse(src).body
                   if isinstance(n, (_ast.Import, _ast.ImportFrom))]
        first = sorted(a.name for a in imports[0].names)[0] if imports else None
        print("    %-24s 寫入點行號=%s  第一個 import=%r" % (name, wl, first))
        if first != "ast":
            failures.append(
                "(⛔) %s 的第一個 import 是 %r，不是 `ast` ⇒ `frozen/ast.py` 這層守衛"
                "**涵蓋不到它**，而它有寫入點（行 %s）。本檔刻意不執行它，所以這裡是"
                "唯一會發現這件事的地方" % (name, first, wl))
        if not wl:
            failures.append(
                "(⛔) %s 靜態上找不到寫入點 —— 它被列進 NEVER_RUN 的前提消失了，"
                "要嘛清單過期、要嘛偵測器壞了；兩種都不該靜靜放過" % name)

    # ── (C) 活體 canary ─────────────────────────────────────────────────
    print("\n(C) 第 2 層活體 canary：真的去執行 %s" % CANARY)
    canary = FROZEN_DIR / CANARY
    if not canary.is_file():
        failures.append("(C) canary %s 不見了" % CANARY)
    else:
        wl = _write_calls(canary.read_text(encoding="utf-8"))
        print("    先確認它靜態上沒有寫入點 : %s" % (wl or "（無）"))
        if wl:
            failures.append(
                "(C) canary %s 出現寫入點（行 %s）—— 不可再拿它當 canary，"
                "否則這支 gate 每次跑都在寫別人的檔" % (CANARY, wl))
        else:
            rc, out = _run(canary)
            marked = GUARD_MARKER in out
            print("    rc = %d（非 0 為必要條件）" % rc)
            print("    輸出含守衛標記 %r : %s" % (GUARD_MARKER, marked))
            if rc == 0:
                failures.append(
                    "(C) %s 執行成功（rc=0）—— 它會印出一份看起來正常、實際來自 "
                    "`wt-836` 的數字，這正是本 gate 要擋的那一格" % CANARY)
            if not marked:
                failures.append(
                    "(C) %s 非零了，但輸出裡**沒有**守衛標記 ⇒ 擋住它的不是守衛，"
                    "是別的原因（例如 CI 上 `ROOT` 那個路徑不存在）。rc 單獨看沒有"
                    "鑑別力，這一格就是為此存在" % CANARY)

    # ── (D) 陽性對照 ────────────────────────────────────────────────────
    # 沒有這格，(C) 的綠燈可能只代表「那支檔剛好會壞」。這裡合成一個**一定會寫檔**
    # 的 victim，先證明守衛擋得住它，再移走守衛證明沒有守衛時它真的寫得成。
    print("\n(D) 陽性對照：合成 victim（一定會寫檔）")
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / "ast.py").write_text(GUARD.read_text(encoding="utf-8"), encoding="utf-8")
        (d / "victim.py").write_text(VICTIM, encoding="utf-8")
        target = d / "victim-wrote-here.txt"
        rc1, out1 = _run(d / "victim.py", str(target))
        wrote1 = target.exists()
        print("    有守衛  : rc=%-3d 寫成功=%s 含標記=%s"
              % (rc1, wrote1, GUARD_MARKER in out1))
        if rc1 != GUARD_RC or wrote1 or GUARD_MARKER not in out1:
            failures.append(
                "(D) 守衛沒擋住合成 victim：rc=%d（期望 %d）／寫成功=%s／含標記=%s"
                % (rc1, GUARD_RC, wrote1, GUARD_MARKER in out1))
        (d / "ast.py").unlink()
        rc2, out2 = _run(d / "victim.py", str(target))
        wrote2 = target.exists()
        print("    移走守衛: rc=%-3d 寫成功=%s" % (rc2, wrote2))
        if rc2 != 0 or not wrote2:
            failures.append(
                "(D) 陰性對照失敗：把守衛移走之後 victim 竟然還是沒跑成（rc=%d／"
                "寫成功=%s）⇒ 上面那格證明的不是「守衛擋住了」，是 victim 自己壞了"
                % (rc2, wrote2))

    print("\n" + "=" * 74)
    if failures:
        print("🔴 FAIL（%d 格）：" % len(failures))
        for f in failures:
            print("  ✗ " + f)
        print("=" * 74)
        return 1
    print("🟢 PASS —— 凍結件擋在兩層之後：沒有 `*.py` 習慣路徑、內容指紋相符、")
    print("           刻意執行會撞上守衛（活體驗證＋陽性/陰性對照）。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""共同祖先 workspace 的 hook dispatcher。

## 它補的洞

跨 repo 統籌需要 workspace 抬到六個 repo 的共同祖先（`/home/elmo/woow-repos`）——
因為 top-level session 之間不能互相 `sys_session_send`，只有同一棵樹能派工。
代價是（實測）**子 repo 的 `.claude/` 三項全失**：

| 失去的 | 症狀 |
| --- | --- |
| hooks | worker `cd` 進 repo 後，repo 自己註冊的 PreToolUse / PostToolUse **不觸發** |
| CLAUDE.md | 不載入（on-demand 注入**只綁 `Read` 工具** ⇒ 用 `cat` / `grep` 連那點補償都沒有） |
| skills | 不註冊 |

本 hook 把 **hooks** 那一項拿回來：claude CLI 只載入 workspace 根那一份 settings，
所以這支 hook 讀 PreToolUse / PostToolUse 的 payload，判斷指令落在哪些子 repo，
**轉呼叫那些 repo 自己註冊的 hook command**，原樣傳遞 stdout / stderr / exit code。

## ⛔ 為什麼不改寫成 CEL policy

把 `kubectl-ns-guard` 的 namespace 白名單搬進 CEL，它會變成**第四份副本**，而且不在
repo-lint `kubectl-ns-whitelist-sync` 的對帳範圍內 ⇒ 必然漂移。dispatcher 跑的是 repo 裡
**原封不動**那一支，真相只有一個來源。

## 判定採聯集（寧可多跑一次檢查、不可漏守）

  A. payload 的 `cwd` 落在某個 repo 底下（agent `cd` 進去後的常態）
  B. **path-token 解析**：`shlex.split` 抽出路徑型 token，以 cwd 與 workspace 根兩個基準
     各 `normpath` 一次，等於某個 repo 或落在它底下就命中。補住 `cd ../repoA`、
     `git -C <path>`、`pushd`、`env -C`、`bash -c "cd … && …"`、絕對路徑等寫法。

⛔ **沒有「對整條 command 做裸字串比對」這一層**（FIX ROUND 7 移除）。那層把 `repo.name`
當裸字串比對 ⇒ `git commit -m "fix repoA bug"` 誤命中，而誤命中與下面的 fail-closed
**相乘**會擋掉一條完全合法的 commit。唯一例外是 `shlex` 解析失敗時的 fallback。

## repo hook 的回傳：三種情況，三種處置

  - **exit 2**＝repo hook 刻意 block ⇒ **原樣透傳**（它的 stderr 就是給使用者看的理由，
    ⛔ 不可被 dispatcher 的診斷訊息蓋掉）。
  - **stdout JSON 判 deny**（`kubectl-ns-guard.py` 走這條，exit code 是 **0**）⇒ 偵測到就
    **短路**，否則後面的 hook 會再吐一份 JSON，兩份混在 stdout 上 claude 解析不了。
  - **其餘任何非 0**＝hook 自己掛了、**沒有**給出判斷 ⇒ 見下。

## PreToolUse 的失敗一律 fail-closed（判準是「有沒有拿到判斷」）

⚠ hook 協定裡**只有 exit 2 代表 block**，其餘非 0 是「非阻斷性錯誤」——原樣回傳等於
**放行**。所以 PreToolUse 下這些全部正規化成 exit 2：timeout、非 0 且非 2 的 exit code
（crash／exception／exit 1／exit 7）、hook 檔不存在或不可執行、呼叫本身拋例外、以及
**命中的 repo 有 `settings.json` 但讀不到／解不開**。

PostToolUse 一律 warning-only（工具已執行完，擋也改變不了結果）。

⛔ 分界點：命中的 repo **根本沒有** `settings.json` ⇒ 正常情況，**不擋**。只有「宣稱有守線
却載不起來」才擋。

## dispatcher **自己**拿不到判斷時也 fail-closed（更危險的一層）

上面講的是「repo hook 拿不到判斷」。但 dispatcher 自己壞掉更糟：repo hook 失敗只影響那一個
repo，**dispatcher 自己壞掉會讓所有 repo 的守線一起靜默消失**。所以 PreToolUse 下這些也是
exit 2：stdin 不是合法 JSON／讀不到、缺 `tool_name`、缺 `cwd` 或 `cwd` 不存在、
`CLAUDE_PROJECT_DIR` 未設或不存在、以及 dispatcher 本身拋出的**未預期例外**。

⛔ 唯一不擋的是「`WS_ROOT` 存在但底下沒有任何 repo」——那代表這個 workspace 本來就沒有守線
要轉呼叫。PostToolUse 一律 warning-only。

## 緊急逆轉門 `DISPATCH_HOOK=off`

照 repo 既有慣例（`kubectl-ns-guard.py` 的 `KUBECTL_NS_GUARD=off`）：指令前綴
`DISPATCH_HOOK=off <指令>`（一次性）或 `export DISPATCH_HOOK=off`（整個 session）。
⛔ **緊急用，不是日常用法**——開啟它等於關掉所有 repo 的所有守線。每則拒絕訊息都會印出
逆轉方式，人被擋住時不必翻文件。

## ⚠ 不要靠 shebang

bundle 打包會**掉 exec bit**（755 → 644），所以 `hooks.json` 的 command 一律寫
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/dispatch-hook.py"`。突變測試 T 實證這件事。

## 環境變數

  - `${CLAUDE_PLUGIN_ROOT}` = materialized bundle 路徑（claude 展開，hooks.json 用）
  - `$CLAUDE_PROJECT_DIR`   = workspace 根。⚠ 轉呼叫時**必須覆寫成該 repo 根**——
    repo 的 hook command 幾乎都寫成 `"$CLAUDE_PROJECT_DIR/.claude/hooks/x.py"`，
    不覆寫就找不到檔。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import traceback
from pathlib import Path

#: workspace 根的**唯一**來源。claude CLI 自己注入（實測：等於啟動 claude 時的 cwd）；
#: omnigent 不設它。
#: ⛔ **無法從 hook 檔位置推導**——`CLAUDE_PLUGIN_ROOT` 指向 materialize 後的 bundle，
#:    與 workspace **無包含關係**（實測兩者是互不包含的路徑）⇒ 這個變數沒有替代來源。
#:    所以它缺失時只能 fail-closed，見 `_workspace_problem()`。
WS_ROOT_RAW = os.environ.get("CLAUDE_PROJECT_DIR")
#: ⚠ 這裡仍保留 `"."` 的退路，只為了讓下面的 helper 在 `main()` 驗過之前不會炸；
#:    **真正的判斷在 `main()` 開頭**——沒驗過就不會走到任何用到它的地方。
WS_ROOT = Path(WS_ROOT_RAW or ".").resolve()
#: 從 workspace 根往下找 repo 的深度上限（本機佈局最深是 `WOOW/<repo>` = 2）。
MAX_DEPTH = 4
#: 轉呼叫單一 hook 的逾時上限（秒）。repo 的 repo-lint --hook 可能偏慢。
DEFAULT_TIMEOUT = 60
#: 掃 repo 時要跳過的目錄名（避免走進 worktree／快取／產生物）。
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".omnigent-runtime"}
#: 這些 token 的**下一個** token 是目錄（`cd x`／`pushd x`／`git -C x`／`env -C x`）。
DIR_ARG_TOKENS = {"cd", "pushd", "-C", "--directory"}
#: PreToolUse 的 hook 失敗時改為放行的逃生門（⛔ 只在 repo hook 自己壞掉時臨時用）。
#: 🔴 **緊急逆轉門**，照 repo 既有慣例（`.claude/hooks/kubectl-ns-guard.py` 的
#:    `KUBECTL_NS_GUARD=off`）：`export DISPATCH_HOOK=off`（整個 session）或指令前綴
#:    `DISPATCH_HOOK=off <指令>`（一次性）。
#: ⛔ **這不是日常用法**：關掉它等於關掉**所有 repo 的所有守線**（kubectl namespace 白名單、
#:    pattern-guard、repo-lint…）。只在「dispatcher 或某支 repo hook 自己壞掉、擋住了正常
#:    工作」時當逃生用，修好就關掉。
BYPASS_ENV = "DISPATCH_HOOK"
BYPASS_VALUE = "off"
#: 舊名（FIX ROUND 6 引入）保留為別名，避免已經寫進筆記的人踩空。
FAIL_OPEN_ENV = "OMNIGENT_DISPATCH_FAIL_OPEN"
#: `shlex` 不吃 shell 的語法字元，會把它們黏在 token 邊緣（`(cd`、`group/repoA;`）。
#: 取路徑前先剝掉，否則 `(cd group/repoA; …)` 這種子 shell 寫法會因為多一個 `;` 而漏判。
SHELL_PUNCT = "();&|,"
#: hook 協定裡**唯一**代表 block 的 exit code。其餘非 0 是「非阻斷性錯誤」，
#: Claude Code 會照跑工具 ⇒ 不可原樣回傳（見 _hook_failure）。
BLOCK_EXIT_CODE = 2
#: 會觸發「零命中告警」的工具（這些是真的會改動或執行東西的）。
WARN_TOOLS = {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"}


def guarded_repos() -> list[Path]:
    """列出 workspace 底下所有自帶 `.claude/settings.json` 的 repo。

    不含 workspace 根自己——根那一份（若有）claude 本來就會載入，再轉呼叫會重複執行。
    """
    out: list[Path] = []
    for depth in range(1, MAX_DEPTH + 1):
        pattern = "/".join(["*"] * depth) + "/.claude/settings.json"
        for s in WS_ROOT.glob(pattern):
            repo = s.parent.parent
            if repo == WS_ROOT:
                continue
            if SKIP_DIRS & set(repo.relative_to(WS_ROOT).parts):
                continue
            out.append(repo)
    return sorted(set(out))


def _command_of(payload: dict) -> str:
    """取出指令文字。Bash 用 `command`；Edit／Write 類用檔案路徑當判定依據。"""
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return ""
    parts = [ti.get("command"), ti.get("file_path"), ti.get("path"), ti.get("notebook_path")]
    return " ".join(str(p) for p in parts if p)


def _path_tokens(text: str, _depth: int = 0) -> list[str] | None:
    """從指令文字抽出「看起來是路徑」的 token。回傳 `None` 代表**解析失敗**。

    三種來源：
      1. token 本身含 `/` 或以 `.` 開頭（`../repoA`、`group/repoA`、`./x`）
      2. `cd` / `pushd` / `git -C` / `env -C` 的**下一個** token（即使它不含 `/`，
         例如 `cd repoA`）
      3. `-c` 類 flag 後面的 shell payload，遞迴抽一層（`bash -c "cd ../repoA && …"`）

    ⚠ **回傳 `None` 與回傳 `[]` 是兩件不同的事**：
      - `[]`＝解析成功、這條指令裡沒有任何路徑 token（⇒ 可以放心不命中）
      - `None`＝`shlex` 解析不出來（引號不對稱、未終止字串…）⇒ **無法證明安全**，
        呼叫端必須倒向保守：fallback 回寬鬆的字串比對（寧可多命中）。
    ⛔ 不可把解析失敗當成 `[]`——那會把「看不懂」誤判成「沒有路徑」而漏守。
    """
    try:
        tokens = shlex.split(text, comments=False, posix=True)
    except ValueError:
        return None
    out: list[str] = []
    expect_dir = False
    expect_payload = False
    for raw_tok in tokens:
        tok = _strip_shell_punct(raw_tok)
        if expect_dir:
            out.append(tok)
            expect_dir = False
        if expect_payload:
            # payload 本身是一條指令 ⇒ 遞迴抽一層。
            # ⚠ 深度限 1：再深就該懷疑是刻意混淆，而本 hook 防的是手滑不是攻擊。
            if _depth < 1:
                inner = _path_tokens(raw_tok, _depth + 1)
                if inner is None:
                    return None          # 內層看不懂 ⇒ 整條倒向保守
                out.extend(inner)
            expect_payload = False
        if _is_shell_payload_flag(tok):
            expect_payload = True
            continue
        if tok in DIR_ARG_TOKENS:
            expect_dir = True
            continue
        # `git -C<path>` / `--directory=<path>` 這種黏在一起的寫法
        for flag in ("-C", "--directory="):
            if tok.startswith(flag) and len(tok) > len(flag):
                out.append(tok[len(flag):].lstrip("="))
        if "/" in tok or tok.startswith("."):
            out.append(tok)
    return out


def _strip_shell_punct(tok: str) -> str:
    """剝掉 token 邊緣的 shell 語法字元。

    `shlex.split("(cd group/repoA; echo hi)")` 會給出 `['(cd', 'group/repoA;', …]`——
    語法字元黏在 token 上。不剝的話子 shell 寫法會漏判（FIX ROUND 7 實測：改用
    path-token 後 `(cd group/repoA; echo hi)` 零命中，而它是合理的 shell 寫法）。

    ⚠ 剝掉之後仍然要通過「像路徑」的判斷（含 `/`、以 `.` 開頭，或跟在 `cd` 之類後面）
    才會被當候選 ⇒ **不會**把 `git commit -m "fix repoA bug"` 那種誤命中帶回來。
    """
    return tok.strip(SHELL_PUNCT)


def _is_shell_payload_flag(tok: str) -> bool:
    """這個 token 後面是不是一段 shell payload？

    認 `-c` 與 `sh -lc` / `bash -ec` 這種把 `c` 併進短 flag 串的寫法
    （`-lc`／`-ec`／`-xc` 都是實務上會看到的）。
    """
    if tok == "-c":
        return True
    return (
        len(tok) > 1
        and tok[0] == "-"
        and tok[1] != "-"
        and tok.endswith("c")
        and tok[1:].isalpha()
    )


def _resolved_candidates(payload_cwd: Path, text: str) -> tuple[set[Path], bool]:
    """把 path token 以 **cwd 與 WS_ROOT 兩個基準**各 resolve 一次。

    :returns: `(候選路徑集合, 解析是否失敗)`。失敗時集合為空、旗標為 `True`，
        呼叫端要改用寬鬆比對。

    兩個基準都要試，因為 agent 可能寫相對於當前位置的路徑（`cd ../repoA`），也可能寫
    相對於 workspace 根的路徑（`cd group/repoA`）——光靠單一基準會漏。

    `os.path.normpath` 順帶解掉連續斜線（`../repoA//x`）、尾斜線與 `..`，不碰檔案系統
    （⛔ 不用 `Path.resolve()`：它會跟隨 symlink 並在路徑不存在時行為依版本而異，
    而我們只要語意上的正規化）。
    """
    tokens = _path_tokens(text)
    if tokens is None:
        return set(), True
    out: set[Path] = set()
    for tok in tokens:
        if not tok or tok.startswith("-"):
            continue
        for base in (payload_cwd, WS_ROOT):
            raw = tok if os.path.isabs(tok) else os.path.join(str(base), tok)
            out.add(Path(os.path.normpath(raw)))
    return out, False


def _loose_match(repo: Path, text: str) -> bool:
    """寬鬆的字串比對——**只在 `shlex` 解析失敗時**當 fallback 用。

    ⛔ 這層會誤命中（`git commit -m "fix repoA bug"` 會中 `repo.name`），所以它
    **不再是常態判定**（FIX ROUND 7 把它從 targets() 的常態路徑移除）。留著是因為
    「解析不出來」屬於「無法證明安全」，那時寧可多命中也不能漏守。
    """
    rel = repo.relative_to(WS_ROOT).as_posix()
    for needle in (rel, str(repo), repo.name):
        if needle and re.search(r"(?<![\w/.-])" + re.escape(needle) + r"(?![\w-])", text):
            return True
    return False


def targets(payload: dict) -> list[Path]:
    """判斷這道呼叫可能作用到哪些 repo。

    兩層聯集（**寧可多跑一次檢查、不可漏守**）：

      A. payload 的 `cwd` 落在某個 repo 底下（agent `cd` 進去後的常態）
      B. **path-token 解析**：把 token 當路徑、以 cwd 與 WS_ROOT 兩個基準各
         `normpath` 一次，等於某個 repo 或落在它底下就命中

    ⚠ **FIX ROUND 7 移除了原本的第三層「對整條 command 做裸字串 regex」**。
    那層會把 `repo.name` 當裸字串比對 ⇒ `git commit -m "fix repoA bug"` 誤命中
    （覆核實測）。而誤命中與同輪的 fail-closed 修正**相乘**：誤命中一個不相干的
    repo、那個 repo 的 hook 又因任何理由失敗 ⇒ 一條完全合法的 `git commit` 被擋。
    commit message／PR body 提到 repo 名是極常見的寫法，所以這裡的誤擋不是「便宜的
    保守選擇」，而是會阻斷日常工作。

    ⛔ 唯一例外：`shlex` 解析失敗時仍退回寬鬆比對（見 `_loose_match`）——
    「看不懂」必須倒向保守。
    """
    try:
        cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    except OSError:
        cwd = WS_ROOT
    text = _command_of(payload)
    candidates, degraded = _resolved_candidates(cwd, text)
    hits: list[Path] = []
    for repo in guarded_repos():
        if cwd == repo or repo in cwd.parents:                        # A
            hits.append(repo)
            continue
        if any(c == repo or repo in c.parents for c in candidates):    # B
            hits.append(repo)
            continue
        if degraded and _loose_match(repo, text):                      # fallback
            hits.append(repo)
    return hits


def matches(matcher: str, tool_name: str) -> bool:
    """比對 settings.json 的 matcher（claude 的 matcher 是 regex，支援 `A|B` 形式）。"""
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, tool_name) is not None
    except re.error:
        return matcher == tool_name


def _is_blocking_stdout(text: str) -> bool:
    """stdout JSON 是否表示 deny／block（kubectl-ns-guard 走這條，exit code 為 0）。"""
    if "permissionDecision" not in text and "decision" not in text:
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    specific = data.get("hookSpecificOutput")
    if isinstance(specific, dict) and specific.get("permissionDecision") == "deny":
        return True
    return data.get("decision") == "block"


def _bypassed(text: str = "") -> bool:
    """緊急逆轉門是否開啟。兩種形式，照 `kubectl-ns-guard.py` 的既有慣例：

      1. `export DISPATCH_HOOK=off`（整個 session）
      2. 指令前綴 `DISPATCH_HOOK=off <指令>`（一次性）

    ⛔ 開啟它等於**關掉所有 repo 的所有守線**，不是日常用法。
    """
    for name in (BYPASS_ENV, FAIL_OPEN_ENV):
        value = os.environ.get(name, "").strip().lower()
        if name == BYPASS_ENV and value == BYPASS_VALUE:
            return True
        # 舊別名當初是「設任何 truthy 值就放行」，維持原語意
        if name == FAIL_OPEN_ENV and value not in ("", "0", "false"):
            return True
    return bool(text) and bool(
        re.search(rf"(?:^|[\s;]){re.escape(BYPASS_ENV)}={BYPASS_VALUE}\b", text))


def _untrusted(event: str, reason: str, detail: str, *, text: str = "") -> int:
    """**dispatcher 自己**拿不到判斷時的處置。回傳要用的 exit code。

    🔴 這一層與 `_hook_failure`（repo hook 拿不到判斷）是**兩件不同的事**，而且這層更危險：
    repo hook 失敗只影響那一個 repo，**dispatcher 自己壞掉會讓所有 repo 的守線一起靜默
    消失**。最典型的是 `CLAUDE_PROJECT_DIR` 沒設 ⇒ `targets()` 回空 ⇒ 每一道指令都暢行，
    而唯一的徵兆是一個「零命中告警」——而那個告警**區分不了**「真的無關指令」與「守線全掉」。

    涵蓋（PreToolUse 一律 exit 2）：
      - stdin 不是合法 JSON／讀不到
      - 缺 `tool_name`
      - 缺 `cwd`／`cwd` 指向不存在的目錄
      - `CLAUDE_PROJECT_DIR` 未設／指向不存在的目錄
      - dispatcher 本身拋出未預期例外（traceback 進 stderr 供診斷）

    ⛔ 不在此列（正常情況，**不擋**）：`WS_ROOT` 存在但底下沒有任何帶 `.claude/settings.json`
    的 repo——那代表這個 workspace 本來就沒有要轉呼叫的守線。

    PostToolUse 一律 warning-only（工具已執行完，擋也改變不了結果）。
    """
    if _bypassed(text):
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {BYPASS_ENV}={BYPASS_VALUE} 已開，放行一個**完全未經守線檢查**"
            f"的指令（{reason}）。⛔ 這等於關掉所有 repo 的守線，修好就關掉。\n")
        return 0
    if event != "PreToolUse":
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {event}：dispatcher 自己拿不到判斷（{reason}），"
            f"但工具已執行完、不擋。\n  {detail}\n")
        return 0
    sys.stderr.write(
        "[dispatch-hook] ⛔ 拒絕執行：**dispatcher 自己**拿不到判斷，無法確認這道指令安全。\n"
        f"  原因      : {reason}\n"
        f"  細節      : {detail}\n"
        "  為什麼擋 : dispatcher 是所有 repo 守線在共同祖先 workspace 下的唯一通路。\n"
        "             它壞掉而放行 ⇒ 每一個 repo 的 kubectl 白名單／pattern-guard／repo-lint\n"
        "             全部一起靜默消失，而唯一徵兆是一個分辨不出差別的「零命中」告警。\n"
        f"  怎麼逆轉 : 指令前綴 `{BYPASS_ENV}={BYPASS_VALUE} <指令>`（一次性），或\n"
        f"             `export {BYPASS_ENV}={BYPASS_VALUE}`（整個 session）。\n"
        "             ⛔ 那是**緊急逆轉門、不是日常用法**——等於關掉所有 repo 的守線。\n")
    return 2


def _workspace_problem() -> str | None:
    """`CLAUDE_PROJECT_DIR` 不可信的理由，或 `None`。

    ⚠ 實測過才敢 fail-closed：claude CLI 自己注入這個變數（omnigent 不設），值等於啟動
    claude 時的 cwd；plugin hook 路徑與 sub-agent 路徑都有設。
    ⛔ 而且它**沒有替代來源**——`CLAUDE_PLUGIN_ROOT` 指向 materialize 後的 bundle，與
    workspace 無包含關係，推導不出 workspace 根。
    """
    if not WS_ROOT_RAW or not WS_ROOT_RAW.strip():
        return ("CLAUDE_PROJECT_DIR 未設；dispatcher 無從得知 workspace 根在哪"
                "（這個變數沒有替代來源：CLAUDE_PLUGIN_ROOT 指向 bundle，不是 workspace）")
    if not Path(WS_ROOT_RAW).is_dir():
        return f"CLAUDE_PROJECT_DIR 指向不存在的目錄：{WS_ROOT_RAW!r}"
    return None


#: 各種失敗原因對應的處置建議（拒絕訊息要能直接照著做，不是只說「失敗了」）。
_FAILURE_HINTS = {
    "timeout": "調高該 repo `.claude/settings.json` 裡那支 hook 的 `timeout`，或修它為什麼這麼慢",
    "非預期的 exit code": "那支 hook 自己 crash 了（exception／exit 1／exit 7…）——看它的 stderr 修它。"
                          "⚠ hook 協定裡**只有 exit 2 代表 block**，其餘非 0 是「非阻斷性錯誤」",
    "無法執行": "確認 hook 檔存在、路徑對、且 `python3` 跑得起來",
    "settings.json 載不起來": "修該 repo 的 `.claude/settings.json`（JSON 語法／檔案權限）",
}


def _hook_failure(event: str, repo: Path, subject: str, kind: str, detail: str) -> int:
    """repo hook 無法給出判斷時的處置。回傳要用的 exit code。

    🔴 **PreToolUse 一律 fail-closed（exit 2）**，涵蓋**所有**「無法確認這道指令安全」
    的理由，不只 timeout：

      | 失敗形態 | 為什麼也要擋 |
      | --- | --- |
      | `timeout` | hook 沒跑完，沒有判斷 |
      | 非 0 且**非 2** 的 exit code | hook crash／exception／exit 1／exit 7。⚠ hook 協定裡**只有 exit 2 代表 block**，其餘非 0 是「非阻斷性錯誤」⇒ 原樣回傳等於放行 |
      | hook 檔不存在／不可執行 | 同上，沒有判斷 |
      | 呼叫本身拋例外（OSError…） | 同上 |
      | 命中的 repo 有 `settings.json` 但讀不到／解不開 | **宣稱有守線却載不起來** |

    ⚠ FIX ROUND 6 只補了 timeout 那一條，其餘三條仍 fail-open（覆核實跑：
    `Pre hook exits 7 → rc=7`、`Pre invalid settings JSON → rc=0`）。
    ⛔ 判準是「**有沒有拿到判斷**」，不是「失敗長什麼樣」——任何新的失敗形態都走這裡。

    **PostToolUse 一律 warning-only（exit 0）**：工具已經執行完了，擋也改變不了結果。
    （FIX ROUND 7 之前「非 0 exit code」這條路徑會原樣回傳 rc=7，與文件敘述不一致。）

    ⛔ **exit 2 不走這裡**——那是 repo hook 刻意的 block，要原樣透傳，它的 stderr 就是
    給使用者看的理由，不可被本函式的訊息蓋掉。

    ⚠ fail-closed 有誤擋風險：repo hook 自己有 bug 的話，**該 repo 的所有操作都會被擋**。
    所以訊息必須可診斷（哪個 repo、哪個對象、什麼原因、怎麼逆轉），並留明確的逃生門。
    """
    if event != "PreToolUse":
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {event} hook {kind}（工具已執行完，不擋）："
            f"{repo.name} / {subject}\n  {detail}\n")
        return 0
    if _bypassed():
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {BYPASS_ENV}={BYPASS_VALUE} 已開，放行一個**未經檢查**的"
            f"指令：{repo.name} / {subject}（{kind}）\n")
        return 0
    sys.stderr.write(
        "[dispatch-hook] ⛔ 拒絕執行：轉呼叫 repo 的守線失敗，無法確認這道指令安全。\n"
        f"  repo      : {repo}\n"
        f"  對象      : {subject}\n"
        f"  事件／原因: {event} / {kind}\n"
        f"  細節      : {detail}\n"
        "  為什麼擋 : PreToolUse 是安全 gate；沒拿到判斷就放行，等於那道守線這次不存在。\n"
        f"  怎麼處理 : (1) {_FAILURE_HINTS.get(kind, '修上面那個對象')}；\n"
        f"             (2) 真的要硬過：指令前綴 `{BYPASS_ENV}={BYPASS_VALUE} <指令>`"
        "（⛔ 緊急逆轉門，等於關掉所有 repo 的守線）。\n")
    return 2


def _load_settings(event: str, repo: Path) -> tuple[dict | None, int]:
    """讀某個 repo 的 `.claude/settings.json`。回傳 `(settings, 要用的 exit code)`。

    ⚠ 分界點是「**宣稱有守線却載不起來**」vs「**本來就沒有守線**」：

      - 檔案存在但讀不到／解不開 ⇒ PreToolUse **fail-closed**（BLOCKING 3）。
      - 檔案**根本不存在** ⇒ 正常情況，**絕不可擋**。`guarded_repos()` 只會回傳有
        `settings.json` 的 repo，所以這條理論上走不到；留著是因為 glob 與 read 之間
        檔案可能被刪（TOCTOU），而那時「不存在」仍然是「本來就沒有守線」。
    """
    path = repo / ".claude" / "settings.json"
    if not path.exists():
        return None, 0          # 本來就沒有守線 ⇒ 不擋
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, _hook_failure(event, repo, str(path),
                                   "settings.json 載不起來", str(exc))
    if not isinstance(data, dict):
        return None, _hook_failure(event, repo, str(path),
                                   "settings.json 載不起來",
                                   f"最外層不是 JSON object（是 {type(data).__name__}）")
    return data, 0


def main(raw: str) -> int:  # noqa: PLR0911, PLR0912
    # ── dispatcher 自己可不可信？不可信就不要假裝有判斷（見 _untrusted）──────────
    # ⚠ FIX ROUND 7 之前這裡是 `return 0`，註解還寫著「dispatcher 自己絕不該變成故障點」
    #    ——方向剛好相反：它**是**所有 repo 守線的唯一通路，壞掉時靜默放行才是最糟的。
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _untrusted("PreToolUse", "stdin 不是合法 JSON", str(exc), text=raw)
    if not isinstance(payload, dict):
        return _untrusted("PreToolUse", "stdin 的最外層不是 JSON object",
                          f"是 {type(payload).__name__}", text=raw)
    event = payload.get("hook_event_name") or "PreToolUse"
    text = _command_of(payload) or raw

    problem = _workspace_problem()
    if problem:
        return _untrusted(event, "workspace 根不可信", problem, text=text)

    tool_name = payload.get("tool_name")
    if not tool_name or not isinstance(tool_name, str):
        return _untrusted(event, "payload 缺 tool_name",
                          "沒有工具名就無法比對 repo hook 的 matcher ⇒ 等於略過所有守線",
                          text=text)

    raw_cwd = payload.get("cwd")
    if not raw_cwd or not isinstance(raw_cwd, str):
        return _untrusted(event, "payload 缺 cwd",
                          "cwd 是判斷「這道指令作用到哪個 repo」的主要依據", text=text)
    if not Path(raw_cwd).is_dir():
        return _untrusted(event, "payload 的 cwd 指向不存在的目錄", repr(raw_cwd), text=text)

    matched_repos = targets(payload)
    executed = 0

    for repo_root in matched_repos:
        settings, rc = _load_settings(event, repo_root)
        if rc:
            return rc
        if settings is None:
            continue
        groups = (settings.get("hooks") or {}).get(event) or []
        for group in groups:
            if not isinstance(group, dict) or not matches(group.get("matcher", ""), tool_name):
                continue
            for h in group.get("hooks") or []:
                if not isinstance(h, dict) or h.get("type") != "command":
                    continue
                command = h.get("command")
                if not command:
                    continue
                env = dict(os.environ)
                # repo 的 hook command 寫 "$CLAUDE_PROJECT_DIR/.claude/hooks/x.py"，
                # 而 claude 給的值是 workspace 根 ⇒ 必須改寫成該 repo 根。
                env["CLAUDE_PROJECT_DIR"] = str(repo_root)
                try:
                    proc = subprocess.run(
                        command, shell=True, input=raw, text=True, capture_output=True,
                        cwd=str(repo_root), env=env,
                        timeout=h.get("timeout", DEFAULT_TIMEOUT),
                    )
                except subprocess.TimeoutExpired as exc:
                    rc = _hook_failure(event, repo_root, command, "timeout",
                                       f"逾時 {exc.timeout} 秒未結束")
                    if rc:
                        return rc
                    continue
                except OSError as exc:
                    rc = _hook_failure(event, repo_root, command, "無法執行", str(exc))
                    if rc:
                        return rc
                    continue
                executed += 1
                if proc.stdout:
                    sys.stdout.write(proc.stdout)
                if proc.stderr:
                    sys.stderr.write(proc.stderr)
                # ⛔ exit 2 ＝ repo hook 刻意的 block ⇒ **原樣透傳**。它的 stderr 就是
                #    給使用者看的理由，不可被我們的診斷訊息蓋掉。
                if proc.returncode == BLOCK_EXIT_CODE:
                    return BLOCK_EXIT_CODE
                # 其餘非 0 ＝ hook 自己掛了，**沒有**給出判斷。原樣回傳的話 Claude Code
                # 視為「非阻斷性錯誤」而照跑工具 ⇒ fail-open（BLOCKING 2）。
                if proc.returncode != 0:
                    rc = _hook_failure(
                        event, repo_root, command, "非預期的 exit code",
                        f"exit {proc.returncode}"
                        + (f"；stderr: {proc.stderr.strip()[:200]}" if proc.stderr else ""))
                    if rc:
                        return rc
                    continue
                # stdout JSON 判 deny（kubectl-ns-guard 走這條，exit code 是 0）⇒ 短路，
                # 否則後面的 hook 會再吐一份 JSON，兩份混在 stdout 上 claude 解析不了。
                if _is_blocking_stdout(proc.stdout or ""):
                    return 0

    # ── 零命中告警 ────────────────────────────────────────────────────────────
    # 命中了 repo 卻沒有任何 hook 真的跑起來 ⇒ 接線可能壞了（matcher 改了…）。
    # 只對真的會改動／執行東西的工具告警，避免噪音。
    #
    # ⚠ **這個告警抓不到「targets() 回空」那一類漏守**（FIX ROUND 6 的 BLOCKING 1 正是
    #   那一類：`cd ../repoA` 讓 targets() 回空）。它是 path-token 解析的**輔助**，
    #   ⛔ 不是替代品。真正的守線在 targets() 的兩層聯集。
    if executed == 0 and matched_repos and tool_name in WARN_TOOLS:
        names = ", ".join(r.name for r in matched_repos)
        sys.stderr.write(
            f"[dispatch-hook] ⚠ 命中 repo（{names}）但沒有任何 {event} hook 跑起來"
            f"（tool={tool_name}）——接線可能壞了，請確認該 repo 的 .claude/settings.json。\n")
    return 0


def _entry() -> int:
    """進入點：把 stdin 讀取與**任何未預期例外**也納入 fail-closed。

    ⚠ 前面每一條都是「我們想到的失敗形態」。這一層接的是想不到的那些——dispatcher 自己
    拋出 `AttributeError`／`KeyError`／權限錯誤時，若讓 Python 以非 2 的 exit code 收場，
    Claude Code 會視為「非阻斷性錯誤」而照跑工具 ⇒ 又是一次靜默放行。
    """
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # noqa: BLE001
        return _untrusted("PreToolUse", "讀不到 stdin", f"{type(exc).__name__}: {exc}")
    # 例外處理用得到 event，先盡力解析一次（解不出來就當 PreToolUse：往保守倒）
    event = "PreToolUse"
    try:
        guess = json.loads(raw)
        if isinstance(guess, dict):
            event = guess.get("hook_event_name") or "PreToolUse"
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        return main(raw)
    except Exception:  # noqa: BLE001
        return _untrusted(event, "dispatcher 自己拋出未預期例外",
                          traceback.format_exc().strip(), text=raw)


if __name__ == "__main__":
    sys.exit(_entry())

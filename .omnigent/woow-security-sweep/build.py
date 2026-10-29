#!/usr/bin/env python3
"""產生 woow-security-sweep 的 config.yaml 並驗收。

用法::

    python3 .omnigent/woow-security-sweep/build.py           # 重產 config.yaml
    python3 .omnigent/woow-security-sweep/build.py --check   # 只驗收，不寫檔

驗收項目（--check）：

    0. config.yaml 與本檔同步（drift）
    1. omnigent.spec parse() ＋ validate()
    2. harness × model 家族相容（借 generate-worker.py 的防線 1）
    3. 本 bundle 的 CEL policy 雙向測試向量（deny／allow）
    4. skill frontmatter name 合法且不與 woow-issue-leader 撞名（first-wins 會吞掉後者）
    5. AGENTS.md「你身上的機器守線」表列的每條守線都真的掛在 config
    6. bundle 內沒有 symlink（打包器不解 symlink，整批 skills 會靜默消失）

1／3 需要 omnigent（本機 uv tool 的 site-packages），CI 沒有 omnigent ⇒ 本檔只在本機跑；
腳本的單元測試由 CI 的 security-sweep-tests workflow 跑。

policy 片段一律從 woow-issue-leader/generate-worker.py 取（單一真相）；本檔只定義 sweep
專屬的三條 CEL：P-secret（不准讀 API key）、P-forge（不准對 GitHub 寫入）、
P-gitea（Gitea issue 只能經 issue_sync.py）。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

SWEEP_DIR = Path(__file__).resolve().parent
OMNIGENT_DIR = SWEEP_DIR.parent
LEADER_DIR = OMNIGENT_DIR / "woow-issue-leader"
CONFIG = SWEEP_DIR / "config.yaml"

GENERATED_HEADER = (
    "# ⚠ 由 .omnigent/woow-security-sweep/build.py 產生——⛔ 不要手改。\n"
    "#   改 build.py 再重跑：python3 .omnigent/woow-security-sweep/build.py\n"
)


def _load_generator():
    """載入 generate-worker.py（檔名有 `-`，不能直接 import）。"""
    path = LEADER_DIR / "generate-worker.py"
    spec = importlib.util.spec_from_file_location("_omnigent_generate_worker", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法載入 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = _load_generator()

#: leader 的 harness 選 claude-native：使用者的 Omnigent UI Inbox 只有 claude-native 的
#: AskUserQuestion 實測會出現（見 README §Inbox）。排程用 agent_id 啟動，不經 UI picker，
#: 所以 issue-leader「頂層不可 *-native」那條限制（UI picker 會過濾）在這裡不適用。
HARNESS = "claude-native"
MODEL = "claude-sonnet-5-5[1m]"
EFFORT = "high"

# ── sweep 專屬 CEL ────────────────────────────────────────────────────────────

#: API key 與 TypeSafe 相關路徑。jev_gate.py 自己讀 key，agent 完全不需要碰。
SECRET_RE = "(?i)(typesafe|api_key)"
# dict.fromkeys 去重保序：GEN.WRITE_TOOLS 已含 NotebookEdit（#1922），這裡仍列出以防產生器日後移除
FILE_TOOLS = list(dict.fromkeys(GEN.WRITE_TOOLS + ["Read", "sys_os_read", "read", "NotebookRead", "NotebookEdit",
                                                   "Grep", "Glob", "LS"]))

CEL_SECRET = f"""event.type == "tool_call" && (
  (event.data.name in {GEN._cel_list(GEN.SHELL_TOOLS)}
   && has(event.data.arguments.command)
   && event.data.arguments.command.matches("{SECRET_RE}"))
  || (event.data.name in {GEN._cel_list(FILE_TOOLS)} && (
       (has(event.data.arguments.path) && event.data.arguments.path.matches("{SECRET_RE}"))
    || (has(event.data.arguments.file_path) && event.data.arguments.file_path.matches("{SECRET_RE}"))
    || (has(event.data.arguments.notebook_path) && event.data.arguments.notebook_path.matches("{SECRET_RE}"))
    || (has(event.data.arguments.pattern) && event.data.arguments.pattern.matches("{SECRET_RE}"))))
) ? {{"result": "DENY", "reason": "TypeSafe API key 只由 jev_gate.py 讀取，agent 不讀、不搜、不印。要呼叫 Jev 就跑 jev_gate.py；不要直接打 API。"}} : {{"result": "ALLOW"}}"""

#: 目標 repo 是 public：leader 對 GitHub 的任何寫入都可能提前公開漏洞細節。
FORGE_WRITE_RE = (
    "(\\\\bgh\\\\s+(issue|pr|release|gist|label)\\\\s+"
    "(create|comment|edit|close|reopen|merge|review|delete|lock|transfer|develop)\\\\b"
    "|\\\\bgh\\\\s+api\\\\b[\\\\s\\\\S]*(-X\\\\s*(POST|PATCH|PUT|DELETE)|--method\\\\s+(POST|PATCH|PUT|DELETE)"
    "|\\\\s(-f|-F|--field|--raw-field|--input)\\\\s)"
    "|\\\\bgit\\\\b[\\\\s\\\\S]*\\\\bpush\\\\b"
    "|\\\\bcurl\\\\b[\\\\s\\\\S]*api\\\\.github\\\\.com)"
)

CEL_FORGE = f"""event.type == "tool_call"
&& event.data.name in {GEN._cel_list(GEN.SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("{FORGE_WRITE_RE}")
? {{"result": "DENY", "reason": "woow-security-sweep 的 leader 不對 GitHub 寫入（issue／PR／留言／push）：目標 repo 是 public，寫出去就公開。結果寫進本機報告與 Inbox；修正由 fix worker 在 worktree 裡做，PR 由人核准後才開。"}} : {{"result": "ALLOW"}}"""

#: Gitea issue 只能由 issue_sync.py 開（#1957）：它寫死 owner、遮蔽秘密值、去重。
#: leader 直接 tea／curl 寫入就繞過這些檢查。tea 的讀取（GET）不擋。
GITEA_WRITE_RE = (
    "(\\\\btea\\\\s+api\\\\b[\\\\s\\\\S]*(-X\\\\s*(POST|PATCH|PUT|DELETE)|--method\\\\s+(POST|PATCH|PUT|DELETE)"
    "|\\\\s(-d|--data|-f|-F|--field)\\\\s)"
    "|\\\\btea\\\\s+(issues?|i|pulls?|pr|labels?|milestones?|ms|releases?|r)\\\\s+"
    "(create|c|edit|e|close|reopen|open|delete|rm|merge|m|approve|reject)\\\\b"
    "|\\\\btea\\\\s+(comment|c)\\\\s"
    "|\\\\bcurl\\\\b[\\\\s\\\\S]*git-prod\\\\.woowtech\\\\.io/api)"
)

CEL_GITEA = f"""event.type == "tool_call"
&& event.data.name in {GEN._cel_list(GEN.SHELL_TOOLS)}
&& has(event.data.arguments.command)
&& event.data.arguments.command.matches("{GITEA_WRITE_RE}")
? {{"result": "DENY", "reason": "Gitea issue 一律用 sec-issue skill 的 issue_sync.py 開（只寫非 public 的 repo、遮蔽秘密值、同一 fingerprint 只開一張）。leader 不直接用 tea／curl 寫 Gitea。"}} : {{"result": "ALLOW"}}"""

P_SECRET = GEN._cel_yaml("p_secret_no_api_key_read", CEL_SECRET)
P_FORGE = GEN._cel_yaml("p_forge_no_public_write", CEL_FORGE)
P_GITEA = GEN._cel_yaml("p_gitea_issue_via_script", CEL_GITEA)

#: 花費：每天一個新 session ⇒ per-session 上限即每日上限。⛔ 不設 ask：無人值守時 ASK 等於停機。
#: 排程另設 max_cost_usd 當第二道上限。
P_COST_SWEEP = GEN._fn_policy(
    "cost_budget", "omnigent.policies.builtins.cost.cost_budget", {"max_cost_usd": 60.0},
    note="sweep 專用：每輪（＝每天一個 session）硬上限 60 USD，不設 ask（無人值守時 ASK 等於停機）。")


def policies() -> list[str]:
    return (GEN.P_LEADER_ORCH + [GEN.P_BOARD, GEN.P_MERGE, GEN.P_APPROVE, GEN.P_TAG, GEN.P_ESCAPE,
                                 P_SECRET, P_FORGE, P_GITEA] + GEN.P_SAFETY + [P_COST_SWEEP])


def config_text() -> str:
    pol = ["guardrails:", f"  ask_timeout: {GEN.ASK_TIMEOUT}", "  policies:"]
    return "\n".join([
        GENERATED_HEADER,
        "spec_version: 1",
        "name: woow_security_sweep",
        "description: 每日安全掃描與修正：依排程 prompt 的 targets 掃 repo，Jev 當判斷閘門，"
        "結果送 Inbox；Phase 1 為 shadow mode，所有動作由人決定。",
        "",
        "executor:",
        "  type: omnigent",
        f"  reasoning_effort: {EFFORT}",
        f"  model: {MODEL}",
        "  config:",
        f"    harness: {HARNESS}",
        "    # 排程無人值守，沒人能回答 ApprovalCard；auto 在 managed settings 下合法。",
        "    permission_mode: auto",
        "",
        "# spawn: true：deep review／fix／review worker 由本層在執行期用",
        "# woow-issue-leader/generate-worker.py 寫出 config，再 sys_session_create(config_path=…)。",
        "spawn: true",
        "",
        "# prompt 由同目錄的 AGENTS.md 承載。⛔ 不要改寫成 `instructions: <相對路徑>`：",
        "#    containment 檢查失敗時整條路徑字串會被當成 system prompt，而且不報錯。",
        "",
        "# ⛔ 一律 `all`：`none` 會發出 --setting-sources \"\"，pi 下名單模式還會關掉 host skill。",
        "skills: all",
        GEN.OS_ENV_BLOCK.rstrip(),
        "",
        "\n".join(pol + policies()),
    ]) + "\n"


# ── 驗收 ──────────────────────────────────────────────────────────────────────

def _shell(cmd: str, name: str = "Bash") -> dict:
    return {"type": "tool_call", "target": name, "data": {"name": name, "arguments": {"command": cmd}}}


def _file(name: str, **args: str) -> dict:
    return {"type": "tool_call", "target": name, "data": {"name": name, "arguments": args}}


J = "python3 /home/elmo/woow-repos/paas/woow_paas_platform/.omnigent/woow-security-sweep/skills/sec-gate/scripts/jev_gate.py"
I = ("python3.12 /data/pi-agent/home/woow-repos/paas/woow_paas_platform/.omnigent/woow-security-sweep"
     "/skills/sec-issue/scripts/issue_sync.py")

CEL_VECTORS: dict[str, dict[str, list[dict]]] = {
    "p_secret_no_api_key_read": {
        "deny": [
            _shell("cat ~/.config/typesafe/api_key"),
            _shell("cat /home/elmo/.config/TypeSafe/api_key"),
            _shell("curl -H \"Authorization: Bearer $(cat k)\" https://api.typesafe.ai/v1/models"),
            _shell("grep -r api_key /home/elmo/.config", name="sys_os_shell"),
            _file("Read", file_path="/home/elmo/.config/typesafe/api_key"),
            _file("sys_os_read", path="/home/elmo/.config/typesafe/api_key"),
            _file("Grep", pattern="api_key", path="/home/elmo/.config"),
            _file("Glob", pattern="**/typesafe/*"),
        ],
        "allow": [
            _shell(f"{J} triage --run-dir /tmp/run/woow_line_base"),
            _shell(f"{J} models"),
            _shell("git -C /home/elmo/woow-repos/WOOW/woow_line_base log --oneline -5"),
            _file("Read", file_path="/home/elmo/woow-repos/WOOW/woow_line_base/models/line_api_service.py"),
        ],
    },
    "p_forge_no_public_write": {
        "deny": [
            _shell("gh issue create --repo WOOWTECH/woow_line_base --title x --body y"),
            _shell("gh pr comment 8 --repo WOOWTECH/woow_line_base --body x"),
            _shell("gh api -X POST repos/WOOWTECH/woow_line_base/issues -f title=x"),
            _shell("gh api repos/WOOWTECH/woow_line_base/issues -f title=x"),
            _shell("git -C /home/elmo/woow-repos/WOOW/wt-x push origin fix/sec-1"),
            _shell("curl -X POST https://api.github.com/repos/WOOWTECH/x/issues"),
        ],
        "allow": [
            _shell("gh pr view 8 --repo WOOWTECH/woow_line_base --json state"),
            _shell("gh api repos/WOOWTECH/woow_line_base/pulls/8"),
            _shell("gh run list --repo WOOWTECH/woow_line_base --limit 5"),
            _shell("git -C /home/elmo/woow-repos/WOOW/woow_line_base fetch origin"),
        ],
    },
    "p_gitea_issue_via_script": {
        "deny": [
            _shell("tea api -X POST -d @/tmp/i.json repos/mirror-internal/woow_line_base/issues"),
            _shell("tea api --login git-prod --method PATCH repos/mirror-internal/x/issues/1 -f state=closed"),
            _shell("tea api -d @b.json repos/mirror-internal/x/issues"),
            _shell("tea issues create --repo mirror-internal/x --title t"),
            _shell("tea issue close 3 --repo mirror-internal/x"),
            _shell("tea comment 3 hello", name="sys_os_shell"),
            _shell("tea labels create --name x"),
            _shell("curl -s https://git-prod.woowtech.io/api/v1/repos/mirror-internal/x/issues"),
        ],
        "allow": [
            _shell(f"{I} --state-dir /tmp/s sync --run-dir /tmp/run/woow_line_base"),
            _shell(f"{I} --state-dir /tmp/s sync --run-dir /tmp/run/woow_line_base --dry-run"),
            _shell("tea api --login git-prod repos/mirror-internal/woow_line_base/issues?state=open"),
            _shell("tea issues list --repo mirror-internal/woow_line_base"),
        ],
    },
}


def _import_omnigent():
    sp = os.environ.get("OMNIGENT_SITE_PACKAGES", GEN.DEFAULT_SITE_PACKAGES)
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)
    import omnigent.spec as spec_mod  # noqa: PLC0415
    from omnigent.policies.builtins.cel import cel_policy  # noqa: PLC0415
    return spec_mod, cel_policy


def _frontmatter_name(skill_md: Path) -> str | None:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    front = text.split("---", 2)[1]
    for line in front.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip()
    return None


def run_checks() -> int:
    results: list[tuple[str, bool, str]] = []

    def item(name: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 —— 一項壞掉不能中斷其餘驗收
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, ok, detail))

    def c0():
        same = CONFIG.exists() and CONFIG.read_text(encoding="utf-8") == config_text()
        return same, "已同步" if same else "config.yaml 與 build.py 不同步：重跑 build.py"

    def c1():
        spec_mod, _ = _import_omnigent()
        spec = spec_mod.parse(SWEEP_DIR, expand_env=False)
        res = spec_mod.validate(spec)
        return not res.errors, "errors=[]" if not res.errors else "; ".join(
            f"{e.path}: {e.message}" for e in res.errors[:6])

    def c2():
        GEN._check_model_family(HARNESS, MODEL)
        return True, f"{HARNESS} × {MODEL}"

    def c3():
        _, cel_policy = _import_omnigent()
        bad, n = [], 0
        for name, expr in (("p_secret_no_api_key_read", CEL_SECRET), ("p_forge_no_public_write", CEL_FORGE),
                           ("p_gitea_issue_via_script", CEL_GITEA)):
            evaluate = cel_policy(expression=expr, reason="policy")
            for want, events in (("DENY", CEL_VECTORS[name]["deny"]), ("ALLOW", CEL_VECTORS[name]["allow"])):
                for ev in events:
                    n += 1
                    got = (evaluate(ev) or {}).get("result", "ALLOW")
                    if got != want:
                        bad.append(f"{name}: 期望 {want} 得到 {got} :: {ev['data']['arguments']}")
        return not bad, f"{n} 個事件符合預期" if not bad else "; ".join(bad[:5])

    def c4():
        names = {}
        for md in sorted((SWEEP_DIR / "skills").glob("*/SKILL.md")):
            names[md.parent.name] = _frontmatter_name(md)
        leader = {_frontmatter_name(md) for md in (LEADER_DIR / "skills").glob("*/SKILL.md")}
        bad = [f"{d}: {n!r}" for d, n in names.items() if not n or not re.fullmatch(r"[a-z0-9-]+", n)]
        bad += [f"{n} 與 woow-issue-leader 撞名" for n in names.values() if n in leader]
        return not bad, f"{sorted(names.values())}" if not bad else "; ".join(bad)

    def c5():
        md = (SWEEP_DIR / "AGENTS.md").read_text(encoding="utf-8")
        section = md.split("## 你身上的機器守線", 1)[1]
        claimed = set(re.findall(r"^\| `([a-z_]+)`", section, flags=re.M))
        cfg = CONFIG.read_text(encoding="utf-8")
        missing = sorted(n for n in claimed if f"\n    {n}:\n" not in cfg)
        return not missing and bool(claimed), (f"{len(claimed)} 條都在 config" if not missing
                                                else f"表上有、config 沒有：{missing}")

    def c6():
        links = [str(p.relative_to(SWEEP_DIR)) for p in SWEEP_DIR.rglob("*") if p.is_symlink()]
        return not links, "0 條 symlink" if not links else f"{links[:3]}"

    item("0. config.yaml 與 build.py 同步", c0)
    item("1. parse() ＋ validate()", c1)
    item("2. harness × model 家族相容", c2)
    item("3. CEL 雙向測試向量", c3)
    item("4. skill name 合法且不撞名", c4)
    item("5. AGENTS.md 守線表 ⊆ config", c5)
    item("6. bundle 內沒有 symlink", c6)
    for name, ok, detail in results:
        print(f"{'✅' if ok else '❌'} {name} — {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n總計：{len(results) - failed}/{len(results)} 通過")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只驗收，不寫檔")
    args = ap.parse_args()
    if args.check:
        return run_checks()
    CONFIG.write_text(config_text(), encoding="utf-8")
    print(f"已寫入 {CONFIG.relative_to(OMNIGENT_DIR.parent)}")
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())

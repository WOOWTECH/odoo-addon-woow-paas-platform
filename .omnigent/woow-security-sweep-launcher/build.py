#!/usr/bin/env python3
"""產生 woow-security-sweep-launcher（前台 agent）的 config.yaml 與 skill 副本，並驗收。

用法::

    python3 .omnigent/woow-security-sweep-launcher/build.py           # 重產 config.yaml ＋ 複製 skill
    python3 .omnigent/woow-security-sweep-launcher/build.py --check   # 只驗收，不寫檔

驗收項目（--check）：

    0. config.yaml 與本檔同步（drift）
    1. omnigent.spec parse() ＋ validate()
    2. harness × model 家族相容（借 generate-worker.py 的防線 1）
    3. CEL 雙向測試向量：sweep 共用的三條（直接取 sweep build.py 的向量）＋前台專屬兩條
    4. skill 副本與 .agents/skills/ 的正本逐字相同，bundle 裡沒有其他 skill
    5. bundle 內沒有 symlink（打包器不解 symlink，整批 skills 會靜默消失）
    6. 頂層 harness 不是 *-native（UI picker 會濾掉，見 .omnigent/README.md §UI picker）
    7. AGENTS.md「你身上的機器守線」表列的每條守線都真的掛在 config

1／3 需要 omnigent（本機 uv tool 的 site-packages）⇒ 本檔只在本機跑；其餘項目另有
unittest（sweep 的 tests/test_launcher_build.py）在 CI 跑。

為什麼是獨立 bundle：sweep 必須 claude-native 才能把判斷題送進 Inbox，但 UI picker 會濾掉
頂層 *-native。前台用 claude-sdk 進 picker，不 spawn，跑一次／建排程一律經過
woow-security-sweep-launch skill 的 launch.py。

policy 一律從兩個地方取（單一真相）：通用守線取 woow-issue-leader/generate-worker.py，
sweep 專屬的 P-secret／P-forge／P-gitea 直接 import sweep 的 build.py。本檔只定義前台專屬兩條：
P-schedule（不直接用排程的 MCP 工具）、P-state（不直接寫 sweep 的 state 目錄）。
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

LAUNCHER_DIR = Path(__file__).resolve().parent
OMNIGENT_DIR = LAUNCHER_DIR.parent
REPO = OMNIGENT_DIR.parent
SWEEP_DIR = OMNIGENT_DIR / "woow-security-sweep"
CONFIG = LAUNCHER_DIR / "config.yaml"
SKILL_NAME = "woow-security-sweep-launch"
SKILL_SOURCE = REPO / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
SKILL_COPY = LAUNCHER_DIR / "skills" / SKILL_NAME / "SKILL.md"

GENERATED_HEADER = (
    "# ⚠ 由 .omnigent/woow-security-sweep-launcher/build.py 產生——⛔ 不要手改。\n"
    "#   改 build.py 再重跑：python3 .omnigent/woow-security-sweep-launcher/build.py\n"
)


def _load_sweep_build():
    """sweep 的 build.py（共用 CEL 與測試向量；它也已載入 generate-worker.py）。"""
    path = SWEEP_DIR / "build.py"
    spec = importlib.util.spec_from_file_location("_sweep_build", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法載入 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SB = _load_sweep_build()
GEN = SB.GEN

#: ⛔ 頂層不可 *-native：UI picker 會把它濾掉（驗收第 6 項）。
HARNESS = "claude-sdk"
MODEL = "claude-sonnet-5-5"
EFFORT = "medium"

# ── 前台專屬 CEL ──────────────────────────────────────────────────────────────

#: P-schedule：排程的 MCP 工具（含 MCP 前綴的變形）一律擋；shell 直打 REST 也擋。
#: launch.py 的子命令（`launch.py schedule`／`run`）不含 `scheduled-tasks` 字樣，不會被擋。
SCHEDULE_TOOL_RE = r"sys_scheduled_task_"
SCHEDULE_REST_RE = r"(\\bcurl\\b|\\bwget\\b|\\bhttpx?\\b|\\brequests\\b|\\burllib\\b)[\\s\\S]*scheduled-tasks"

CEL_SCHEDULE = f"""event.type == "tool_call" && (
  event.data.name.matches("{SCHEDULE_TOOL_RE}")
  || (event.data.name in {GEN._cel_list(GEN.SHELL_TOOLS)}
      && has(event.data.arguments.command)
      && event.data.arguments.command.matches("{SCHEDULE_REST_RE}"))
) ? {{"result": "DENY", "reason": "排程一律經過 woow-security-sweep-launch 的 launch.py（run／schedule／status）：它會做 targets 重疊檢查、補設 max_cost_usd、讀回逐欄核對。不直接用排程的 MCP 工具，也不直接打 /v1/scheduled-tasks。"}} : {{"result": "ALLOW"}}"""

#: P-state：sweep 的 state（<WS>/.omnigent-runtime/woow-security-sweep/）只由 sweep 與腳本寫。
#: 唯讀白名單與串接判斷沿用 P-board（同一組 regex）。
STATE_DIR_RE = r"\\.omnigent-runtime/woow-security-sweep"

CEL_STATE = f"""event.type == "tool_call" && (
  (event.data.name in {GEN._cel_list(GEN.WRITE_TOOLS)} && (
    (has(event.data.arguments.path) && event.data.arguments.path.matches("{STATE_DIR_RE}"))
    || (has(event.data.arguments.file_path) && event.data.arguments.file_path.matches("{STATE_DIR_RE}"))
    || (has(event.data.arguments.notebook_path) && event.data.arguments.notebook_path.matches("{STATE_DIR_RE}"))
  ))
  || (event.data.name in {GEN._cel_list(GEN.SHELL_TOOLS)}
      && has(event.data.arguments.command)
      && event.data.arguments.command.matches("{STATE_DIR_RE}")
      && !(event.data.arguments.command.matches("{GEN.BOARD_READ_ONLY_RE}")
           && !event.data.arguments.command.matches("{GEN.SHELL_CHAIN_RE}")))
) ? {{"result": "DENY", "reason": "sweep 的 state 目錄只由 sweep 與 launch.py／setup.py 寫。preset 用 launch.py preset 建立；唯讀請用單一不串接的 cat / less / head / tail / grep / rg / ls / wc / diff。"}} : {{"result": "ALLOW"}}"""

P_SCHEDULE = GEN._cel_yaml("p_schedule_via_script", CEL_SCHEDULE)
P_STATE = GEN._cel_yaml("p_state_via_script", CEL_STATE)

#: 前台是互動 session，人就在對話框前；每次使用開新 session ⇒ per-session 上限。
#: ⛔ 不設 ask：照 sweep 的取捨（ASK 是 fail-closed）。sweep 本身的花費由它自己的 config 與排程上限管。
P_COST_LAUNCHER = GEN._fn_policy(
    "cost_budget", "omnigent.policies.builtins.cost.cost_budget", {"max_cost_usd": 20.0},
    note="前台專用：每個 session 硬上限 20 USD，不設 ask。sweep 那一輪的花費不算在這裡。")


def policies() -> list[str]:
    return ([GEN.P_MERGE, GEN.P_APPROVE, GEN.P_TAG, GEN.P_ESCAPE,
             SB.P_SECRET, SB.P_FORGE, SB.P_GITEA, P_SCHEDULE, P_STATE] + GEN.P_SAFETY + [P_COST_LAUNCHER])


def config_text() -> str:
    pol = ["guardrails:", f"  ask_timeout: {GEN.ASK_TIMEOUT}", "  policies:"]
    return "\n".join([
        GENERATED_HEADER,
        "spec_version: 1",
        "name: woow_security_sweep_launcher",
        "description: woow-security-sweep 的前台：從 UI 叫起，檢查環境、跑一次（觸發手動排程）、"
        "建排程、查狀態；判斷題由 sweep 本體送進 Inbox。",
        "",
        "executor:",
        "  type: omnigent",
        f"  reasoning_effort: {EFFORT}",
        f"  model: {MODEL}",
        "  config:",
        "    # ⛔ 頂層不可 *-native：UI picker 會把它濾掉（.omnigent/README.md §UI picker）。",
        f"    harness: {HARNESS}",
        "    permission_mode: auto",
        "",
        "# 不 spawn：跑一次是觸發 sweep 的手動排程（launch.py run），不是自己派 sub-agent。",
        "spawn: false",
        "",
        "# prompt 由同目錄的 AGENTS.md 承載。⛔ 不要改寫成 `instructions: <相對路徑>`：",
        "#    containment 檢查失敗時整條路徑字串會被當成 system prompt，而且不報錯。",
        "",
        "# ⛔ 一律 `all`：`none` 會發出 --setting-sources \"\"。",
        "# skills/ 底下是 .agents/skills/woow-security-sweep-launch/SKILL.md 的副本（build.py 複製）。",
        "skills: all",
        GEN.OS_ENV_BLOCK.rstrip(),
        "",
        "\n".join(pol + policies()),
    ]) + "\n"


# ── 驗收 ──────────────────────────────────────────────────────────────────────

_shell, _file = SB._shell, SB._file


def _tool(tool: str, **args: object) -> dict:
    return {"type": "tool_call", "target": tool, "data": {"name": tool, "arguments": args}}


WS = "/data/pi-agent/home/woow-repos"
L = f"python3.12 {WS}/paas/woow_paas_platform/.agents/skills/woow-security-sweep-launch/scripts/launch.py --workspace {WS}"
STATE = f"{WS}/.omnigent-runtime/woow-security-sweep"

CEL_VECTORS: dict[str, dict[str, list[dict]]] = {
    "p_schedule_via_script": {
        "deny": [
            _tool("sys_scheduled_task_create", name="x", prompt="preset: line-daily", rrule="FREQ=DAILY"),
            _tool("sys_scheduled_task_update", scheduled_task_id="t1", state="active"),
            _tool("sys_scheduled_task_list"),
            _tool("sys_scheduled_task_delete", scheduled_task_id="t1"),
            _tool("mcp__omnigent__sys_scheduled_task_create", name="x"),
            _shell("curl -X POST https://omnigent.example/v1/scheduled-tasks/t1/run"),
            _shell("python3 -c 'import httpx; httpx.post(\"/v1/scheduled-tasks\")'"),
        ],
        "allow": [
            _shell(f"{L} run --preset line-daily"),
            _shell(f"{L} schedule --preset line-weekly --rrule \"FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=30\""),
            _shell(f"{L} status"),
            _tool("sys_session_get_history", session_id="a" * 32),
        ],
    },
    "p_state_via_script": {
        "deny": [
            _file("Write", file_path=f"{STATE}/presets/line-daily.yaml", content="targets: []"),
            _file("Edit", file_path=f"{STATE}/agent.json"),
            _file("sys_os_write", path=".omnigent-runtime/woow-security-sweep/repos/woow_line_base.json"),
            _shell(f"rm -rf {STATE}/runs"),
            _shell(f"cat {STATE}/agent.json && rm {STATE}/agent.json"),
            _shell(f"echo x > {STATE}/presets/a.yaml"),
        ],
        "allow": [
            _shell(f"{L} preset --name line-daily --targets {WS}/WOOW/woow_line_base"),
            _shell(f"cat {STATE}/runs/20261025-0830/report.md"),
            _shell(f"ls {STATE}/runs"),
            _file("Read", file_path=f"{STATE}/runs/20261025-0830/report.md"),
        ],
    },
}

#: sweep 共用的三條：運算式與向量都取自 sweep build.py
SHARED_CEL = {"p_secret_no_api_key_read": SB.CEL_SECRET, "p_forge_no_public_write": SB.CEL_FORGE,
              "p_gitea_issue_via_script": SB.CEL_GITEA}
OWN_CEL = {"p_schedule_via_script": CEL_SCHEDULE, "p_state_via_script": CEL_STATE}


def skill_copy_problems() -> list[str]:
    """副本必須與正本逐字相同；bundle 的 skills/ 只能有 launch skill 這一份。"""
    bad = []
    if not SKILL_SOURCE.is_file():
        return [f"找不到正本 {SKILL_SOURCE.relative_to(REPO)}"]
    if not SKILL_COPY.is_file():
        bad.append(f"缺副本 {SKILL_COPY.relative_to(REPO)}：重跑 build.py")
    elif SKILL_COPY.read_bytes() != SKILL_SOURCE.read_bytes():
        bad.append("副本與正本不同：重跑 build.py（⛔ 不要手改副本）")
    extra = sorted(str(p.relative_to(LAUNCHER_DIR)) for p in (LAUNCHER_DIR / "skills").rglob("*")
                   if p.is_file() and p != SKILL_COPY)
    if extra:
        bad.append(f"skills/ 有正本以外的檔案：{extra[:3]}")
    return bad


def symlinks() -> list[str]:
    return [str(p.relative_to(LAUNCHER_DIR)) for p in LAUNCHER_DIR.rglob("*") if p.is_symlink()]


def claimed_guards() -> set[str]:
    md = (LAUNCHER_DIR / "AGENTS.md").read_text(encoding="utf-8")
    section = md.split("## 你身上的機器守線", 1)[1]
    return set(re.findall(r"^\| `([a-z_]+)`", section, flags=re.M))


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
        spec_mod, _ = SB._import_omnigent()
        spec = spec_mod.parse(LAUNCHER_DIR, expand_env=False)
        res = spec_mod.validate(spec)
        return not res.errors, "errors=[]" if not res.errors else "; ".join(
            f"{e.path}: {e.message}" for e in res.errors[:6])

    def c2():
        GEN._check_model_family(HARNESS, MODEL)
        return True, f"{HARNESS} × {MODEL}"

    def c3():
        _, cel_policy = SB._import_omnigent()
        bad, n = [], 0
        cases = [(name, expr, SB.CEL_VECTORS[name]) for name, expr in SHARED_CEL.items()]
        cases += [(name, expr, CEL_VECTORS[name]) for name, expr in OWN_CEL.items()]
        for name, expr, vectors in cases:
            evaluate = cel_policy(expression=expr, reason="policy")
            for want, events in (("DENY", vectors["deny"]), ("ALLOW", vectors["allow"])):
                for ev in events:
                    n += 1
                    got = (evaluate(ev) or {}).get("result", "ALLOW")
                    if got != want:
                        bad.append(f"{name}: 期望 {want} 得到 {got} :: {ev['data']['name']} {ev['data']['arguments']}")
        return not bad, f"{len(cases)} 條／{n} 個事件符合預期" if not bad else "; ".join(bad[:5])

    def c4():
        bad = skill_copy_problems()
        name = SB._frontmatter_name(SKILL_SOURCE) if SKILL_SOURCE.is_file() else None
        if name != SKILL_NAME:
            bad.append(f"正本 frontmatter name 是 {name!r}，不是 {SKILL_NAME}")
        return not bad, f"{SKILL_NAME} 副本與正本一致" if not bad else "; ".join(bad)

    def c5():
        links = symlinks()
        return not links, "0 條 symlink" if not links else f"{links[:3]}"

    def c6():
        cfg = CONFIG.read_text(encoding="utf-8")
        m = re.search(r"^\s+harness:\s*(\S+)", cfg, flags=re.M)
        harness = m.group(1) if m else None
        ok = harness is not None and not harness.endswith("-native")
        return ok, f"harness={harness}" + ("" if ok else "（UI picker 會濾掉 *-native）")

    def c7():
        claimed = claimed_guards()
        cfg = CONFIG.read_text(encoding="utf-8")
        missing = sorted(n for n in claimed if f"\n    {n}:\n" not in cfg)
        return not missing and bool(claimed), (f"{len(claimed)} 條都在 config" if not missing
                                                else f"表上有、config 沒有：{missing}")

    item("0. config.yaml 與 build.py 同步", c0)
    item("1. parse() ＋ validate()", c1)
    item("2. harness × model 家族相容", c2)
    item("3. CEL 雙向測試向量（sweep 共用 ＋ 前台專屬）", c3)
    item("4. skill 副本與正本一致", c4)
    item("5. bundle 內沒有 symlink", c5)
    item("6. 頂層 harness 不是 *-native", c6)
    item("7. AGENTS.md 守線表 ⊆ config", c7)
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
    SKILL_COPY.parent.mkdir(parents=True, exist_ok=True)
    SKILL_COPY.write_bytes(SKILL_SOURCE.read_bytes())
    print(f"已寫入 {CONFIG.relative_to(REPO)}、{SKILL_COPY.relative_to(REPO)}")
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())

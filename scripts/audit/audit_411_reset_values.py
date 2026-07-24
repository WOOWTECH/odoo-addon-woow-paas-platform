#!/usr/bin/env python3
"""#411 前置盤點：stored release values vs 平台可重算 values 差異盤點。

**第一階段：收集端 ＋ 報告骨架**（本檔）。比對引擎（可重算 values）留 stub，
待 W2 `dev-411-pipeline` 全量組裝 helper 接上（第二階段，經 odoo shell dry-run）。

背景
----
平台 redeploy 走 `helm upgrade --reuse-values`，render 時用「舊 release 的 coalesced
values」，導致 chart 升版新增的預設鍵在升級路徑永不生效（僅 fresh install 有）。#411 要
翻掉 reuse 語意、改顯式 `-f` 全量升級；翻之前須先盤點「平台現在會算出、但 stored 顯式
values 裡沒有」的鍵並 backfill，否則翻語意當下升級行為會突變。

本腳本收集每個租戶 release 的 **stored 顯式 values**（`helm get values`，不帶 --all）
與 chart 版本，輸出遮罩後的 markdown 報告骨架。差異（diff）欄位待第二階段接 helper。

鐵律（CLAUDE.md）
----------------
- **唯讀**：只 `kubectl get/list` 與 `helm list/get values`，零寫入。
- **namespace 白名單**：只 iterate 租戶 ns —— `paas-ws-*`（prod）/ `paas-ws-stg-*`（stg）。
  system/cloudflare ns 不在盤點對象。跨 ns 的 `get ns` 僅供歸屬分流（唯讀 metadata）。
- **secret 不外洩**：raw values（含明文 secret）只寫 `--raw-dir`（預設系統 tmp、repo 外），
  **絕不進 git**；進 git 的 markdown 報告一律遮罩。

比對引擎單一真相源
------------------
`compute_platform_values()` 是 **stub**。可重算 values 的唯一真相源＝W2 helper；第一階段
**禁止自寫 template defaults merge 邏輯**（會產生假 diff）。

helm 版本註記
------------
release 由 operator（helm 3.13.3）部署；本機若為 helm 4，`get values -o json` 會多插一個
sentinel top-level key `USER-SUPPLIED VALUES`（helm 3 無），本腳本會剝除。get values/list
為唯讀，helm 4 讀 helm 3 release 相容。

用法
----
    # 第一階段真連 stg 收集（唯讀）
    python3 scripts/audit/audit_411_reset_values.py --env stg --context woow-k3s \\
        --report docs/tracking/reports/411-reset-values-audit.md

    # prod（目前可能 0 租戶 ns）／兩者
    python3 scripts/audit/audit_411_reset_values.py --env both
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONTEXT = "woow-k3s"
CMD_TIMEOUT = 60  # 秒；kubectl/helm 單次呼叫上限（macOS 無 GNU timeout，用 subprocess timeout）

# 租戶 ns 白名單（鐵律）。prod = paas-ws-* 但排除 paas-ws-stg-*（後者也 match 前綴）。
_PROD_NS_RE = re.compile(r"^paas-ws-(?!stg-)")
_STG_NS_RE = re.compile(r"^paas-ws-stg-")

# 遮罩規則：對齊 operator `src/services/helm.py::_sanitize_release_values`
#   —— key == "secret" 或路徑尾端為 ("config", "sensitive") 的整棵子樹遮罩
# 再加鍵名 heuristic（fail-safe：非標準命名的敏感鍵也遮，寧多遮不少漏）。
#   涵蓋 pass / token / secret / credential / key（裸 key 抓 currentKey / privateKey /
#   apiKey 等 —— agent-runtime `auth.currentKey` 是明文 64-hex 認證 key，operator 規則
#   與早期 heuristic 都抓不到，實測漏網，故補裸 key）。
# 已知限制：命名不含上列字樣的 secret（如 dsn / connectionString）heuristic 抓不到；
#   最終防線＝raw dump 不進 git（見 render_report 鐵律段）。
_SECRET_KEY_HEURISTIC_RE = re.compile(
    r"pass|token|secret|credential|key", re.I
)

# helm 4 `get values -o json` 插入的 sentinel top-level key（helm 3 無），須剝除。
_HELM4_SENTINEL_KEY = "USER-SUPPLIED VALUES"


def _run(cmd: list[str]) -> tuple[int, str, str]:
    """執行唯讀外部指令，回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"逾時 {CMD_TIMEOUT}s: {' '.join(cmd)}"
    except FileNotFoundError as exc:
        return 127, "", f"指令不存在: {exc}"


def list_target_namespaces(context: str, env: str) -> list[tuple[str, str]]:
    """回 [(env_tag, ns), ...]，僅含租戶 ns（鐵律白名單）。"""
    rc, out, err = _run(
        ["kubectl", "--context", context, "--request-timeout=30s", "get", "ns", "-o", "name"]
    )
    if rc != 0:
        raise SystemExit(f"[fatal] kubectl get ns 失敗 (rc={rc}): {err.strip()}")
    names = [
        line.split("/", 1)[1]
        for line in out.splitlines()
        if line.startswith("namespace/")
    ]
    prod = sorted(n for n in names if _PROD_NS_RE.match(n))
    stg = sorted(n for n in names if _STG_NS_RE.match(n))
    targets: list[tuple[str, str]] = []
    if env in ("prod", "both"):
        targets += [("prod", n) for n in prod]
    if env in ("stg", "both"):
        targets += [("stg", n) for n in stg]
    return targets


def list_releases(context: str, ns: str) -> tuple[Optional[list[dict]], Optional[str]]:
    """helm list -o json（該 ns 所有 release）。回 (releases, error)。"""
    rc, out, err = _run(
        ["helm", "--kube-context", context, "list", "-n", ns, "-o", "json"]
    )
    if rc != 0:
        return None, f"helm list 失敗 (rc={rc}): {err.strip()}"
    try:
        return json.loads(out or "[]"), None
    except json.JSONDecodeError as exc:
        return None, f"helm list JSON parse 失敗: {exc}"


def get_stored_values(
    context: str, ns: str, release: str
) -> tuple[Optional[dict], Optional[str]]:
    """helm get values（stored 顯式 user-supplied，不帶 --all）。回 (values, error)。"""
    rc, out, err = _run(
        ["helm", "--kube-context", context, "get", "values", release, "-n", ns, "-o", "json"]
    )
    if rc != 0:
        return None, f"helm get values 失敗 (rc={rc}): {err.strip()}"
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        return None, f"helm get values JSON parse 失敗: {exc}"
    if isinstance(data, dict):
        data.pop(_HELM4_SENTINEL_KEY, None)  # 剝 helm 4 sentinel
    return data, None


def _mask_subtree(node: Any) -> Any:
    """整棵子樹遮罩成 ***（保留結構）。"""
    if isinstance(node, dict):
        return {k: _mask_subtree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_subtree(x) for x in node]
    return "***"


def sanitize(node: Any, path: tuple[str, ...] = ()) -> Any:
    """遮罩敏感值。對齊 operator 規則 ＋ 鍵名 heuristic。"""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            child = path + (key,)
            if (
                key == "secret"
                or child[-2:] == ("config", "sensitive")
                or _SECRET_KEY_HEURISTIC_RE.search(key)
            ):
                out[key] = _mask_subtree(value)
            else:
                out[key] = sanitize(value, child)
        return out
    if isinstance(node, list):
        return [sanitize(x, path) for x in node]
    return node


def compute_platform_values(env: str, ns: str, release: str, chart: str) -> dict:
    """【STUB — 第二階段接上】平台側「可重算 values」全量組裝（template defaults + 平台注入）。

    單一真相源＝W2 `dev-411-pipeline` 的 helper（經 odoo shell dry-run 呼叫）。
    第一階段**禁止自寫 template defaults merge 邏輯**——自寫會與 helper 產生假 diff。
    第二階段：在此呼叫 helper（例如 odoo shell 執行組裝並回 JSON），回傳可重算 values dict。
    """
    raise NotImplementedError(
        "可重算 values 引擎待接 W2 dev-411-pipeline helper（第二階段）"
    )


def classify_diff(stored: dict, recomputed: Optional[dict]) -> str:
    """【框架 STUB】stored vs recomputed 差異分類：

      (a) injector 類會自癒 —— install-time injector 寫入的鍵（如 networkPolicy.apiServerCidrs
          #404、config.allowedOrigins #399），翻語意後平台會重新注入，缺了也會自癒。
      (b) 平台該管卻漏寫 → backfill 清單 —— 平台會算出、stored 缺，且非 injector 自癒。
      (c) out-of-band 無法解釋 → 待 Eugene 裁決 —— stored 有、平台算不出（人手改過或殘留）。

    第一階段 recomputed 為 stub（None），一律回 'pending-phase2'。
    """
    if recomputed is None:
        return "pending-phase2"
    # 第二階段實作：逐鍵比對 stored/recomputed，套 injector 白名單分 (a)/(b)/(c)。
    raise NotImplementedError("classify_diff 實作待第二階段")


def _helm_version() -> str:
    rc, out, _ = _run(["helm", "version", "--short"])
    return out.strip() if rc == 0 else "unknown"


def _json_block(data: Any) -> str:
    return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


def render_report(records: list[dict], meta: dict) -> str:
    """產出遮罩後的 markdown 報告骨架。"""
    lines: list[str] = []
    lines.append("# #411 前置盤點：Reset Values Backfill 差異報告（第一階段）")
    lines.append("")
    lines.append(
        f"> 生成：`scripts/audit/audit_411_reset_values.py`｜context=`{meta['context']}`"
        f"｜env=`{meta['env']}`｜生成時間 `{meta['generated_at']}`"
    )
    lines.append(
        f"> helm：`{meta['helm_version']}` 讀取（release 由 operator helm 3.13.3 部署，"
        f"`get values`/`list` 唯讀相容；已剝除 helm 4 `USER-SUPPLIED VALUES` sentinel）"
    )
    lines.append(
        "> 狀態：**第一階段＝收集端＋骨架**。`recomputed` 側與 diff 分類待第二階段接 "
        "W2 `dev-411-pipeline` helper。"
    )
    lines.append(
        "> 鐵律：唯讀 `get`/`list`；raw values（含明文 secret）不進 git（落點見文末）；"
        "本報告 stored values 已遮罩（對齊 operator `_sanitize_release_values` ＋ 鍵名 heuristic）。"
    )
    lines.append("")

    ok = [r for r in records if not r.get("error")]
    errs = [r for r in records if r.get("error")]

    # 摘要表
    lines.append("## 摘要")
    lines.append("")
    lines.append(
        f"- 目標 ns：{meta['ns_count']}｜release：{len(ok)} 收集成功、{len(errs)} 失敗"
    )
    lines.append("")
    lines.append("| env | namespace | release | chart | status | rev | stored 鍵數 | 分類 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in ok:
        lines.append(
            f"| {r['env']} | `{r['ns']}` | `{r['release']}` | `{r['chart']}` | "
            f"{r['status']} | {r['revision']} | {r['stored_key_count']} | {r['classification']} |"
        )
    lines.append("")
    if errs:
        lines.append("### 收集失敗")
        lines.append("")
        lines.append("| env | namespace | release | error |")
        lines.append("| --- | --- | --- | --- |")
        for r in errs:
            lines.append(
                f"| {r['env']} | `{r['ns']}` | `{r.get('release','-')}` | {r['error']} |"
            )
        lines.append("")

    # Per-release stored values（遮罩後）
    lines.append("## Per-release stored values（已遮罩）")
    lines.append("")
    for r in ok:
        lines.append(f"### {r['env']} / `{r['ns']}` / `{r['release']}`（chart `{r['chart']}`）")
        lines.append("")
        lines.append(_json_block(r["masked_stored"]))
        lines.append("")

    # 分類框架
    lines.append("## 差異分類框架（diff 待第二階段填）")
    lines.append("")
    lines.append(
        "- **(a) injector 類會自癒**：install-time injector 寫入的鍵，翻語意後平台重新注入、"
        "缺了自癒。實例：`networkPolicy.apiServerCidrs`（#404）、`config.allowedOrigins`（#399）。"
    )
    lines.append(
        "- **(b) 平台該管卻漏寫 → backfill**：平台會算出、stored 缺、非 injector 自癒。"
        "翻 reset 語意前須顯式 backfill 進 stored values。"
    )
    lines.append(
        "- **(c) out-of-band 無法解釋 → 待 Eugene 裁決**：stored 有、平台算不出（人手改過或殘留）。"
    )
    lines.append("")

    # 比對引擎 stub
    lines.append("## 比對引擎（stub）")
    lines.append("")
    lines.append(
        "`compute_platform_values()` 為 stub，待接 W2 `dev-411-pipeline` 全量組裝 helper"
        "（經 odoo shell dry-run）。**禁止自寫 template defaults merge**——單一真相源＝該 helper，"
        "自寫會產生假 diff。第二階段接上後 `classify_diff()` 才輸出 (a)/(b)/(c)。"
    )
    lines.append("")

    # 鐵律與 raw dump 落點
    lines.append("## 鐵律與 raw dump 落點")
    lines.append("")
    lines.append(f"- raw values（**未遮罩、含明文 secret**）：`{meta['raw_dir']}` —— **不進 git**。")
    lines.append(
        "- 本報告已遮罩；non-secret 基礎設施值（chart 版本、resources、CF tunnel host、"
        "#404 apiServerCidrs 內網 IP 等）保留，供 backfill 分析。"
    )
    lines.append(
        "- 遮罩 heuristic 涵蓋 `pass`/`token`/`secret`/`credential`/`key` 命名；命名不含上列"
        "字樣的 secret（如 `dsn`/`connectionString`）不保證遮罩，故 raw dump 一律不進 git 為最終防線。"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="#411 reset-values backfill 盤點（第一階段：收集＋骨架）")
    parser.add_argument("--context", default=DEFAULT_CONTEXT, help="kube context（預設 woow-k3s）")
    parser.add_argument(
        "--env", choices=["prod", "stg", "both"], default="both", help="盤點環境（預設 both）"
    )
    parser.add_argument(
        "--report",
        default="docs/tracking/reports/411-reset-values-audit.md",
        help="markdown 報告輸出路徑（遮罩後、可進 git）",
    )
    parser.add_argument(
        "--raw-dir",
        default=None,
        help="raw values（未遮罩）落點；預設系統 tmp（repo 外，勿進 git）",
    )
    args = parser.parse_args(argv)

    generated_at = time.strftime("%Y-%m-%d %H:%M:%S %z")
    raw_dir = Path(args.raw_dir) if args.raw_dir else Path(
        tempfile.mkdtemp(prefix="audit-411-raw-")
    )
    raw_dir.mkdir(parents=True, exist_ok=True)

    targets = list_target_namespaces(args.context, args.env)
    print(f"[info] 目標 ns：{len(targets)}（env={args.env}）", file=sys.stderr)

    records: list[dict] = []
    for env_tag, ns in targets:
        releases, err = list_releases(args.context, ns)
        if err:
            records.append({"env": env_tag, "ns": ns, "error": err})
            print(f"[warn] {ns}: {err}", file=sys.stderr)
            continue
        for rel in releases or []:
            name = rel.get("name", "?")
            stored, serr = get_stored_values(args.context, ns, name)
            if serr:
                records.append({"env": env_tag, "ns": ns, "release": name, "error": serr})
                print(f"[warn] {ns}/{name}: {serr}", file=sys.stderr)
                continue

            # raw（未遮罩）寫 repo 外 tmp，絕不進 git
            (raw_dir / f"{ns}__{name}.json").write_text(
                json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            masked = sanitize(stored)
            try:
                recomputed = compute_platform_values(env_tag, ns, name, rel.get("chart", "?"))
            except NotImplementedError:
                recomputed = None  # 第一階段 stub
            classification = classify_diff(masked, recomputed)

            records.append(
                {
                    "env": env_tag,
                    "ns": ns,
                    "release": name,
                    "chart": rel.get("chart", "?"),
                    "status": rel.get("status", "?"),
                    "revision": rel.get("revision", "?"),
                    "stored_key_count": len(stored) if isinstance(stored, dict) else 0,
                    "masked_stored": masked,
                    "classification": classification,
                }
            )
            print(f"[ok] {ns}/{name} chart={rel.get('chart')}", file=sys.stderr)

    meta = {
        "context": args.context,
        "env": args.env,
        "generated_at": generated_at,
        "helm_version": _helm_version(),
        "raw_dir": str(raw_dir),
        "ns_count": len(targets),
    }
    report = render_report(records, meta)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    print(f"[done] 報告：{report_path}", file=sys.stderr)
    print(f"[done] raw dump（不進 git）：{raw_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

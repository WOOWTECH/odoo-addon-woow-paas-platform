#!/usr/bin/env python3
"""#1441 既有租戶 namespace 回填：egress baseline（paas-baseline-egress）＋Pod Security 標籤。

operator 只在 create_namespace（每次在該工作區開通服務都會重跑）與
POST /api/namespaces/{name}/baseline-netpol 套用，而且要等 TENANT_BASELINE_EGRESS_ENABLED／
TENANT_POD_SECURITY_ENABLED 打開。既有 namespace 用這支腳本**分批、可預覽、可回滾**地補上。
manifest 與標籤由 ``src/services/tenant_baseline.py`` 產生——和 operator 同一份，不會分岔。

只用標準函式庫＋kubectl（不需要 operator 的 Python 相依套件）。預設只做預覽：

    # 1. 預覽（不寫入；含 server-side dry-run，會列出 PSA 不合規的既有 pod）
    python3 extra/paas-operator/scripts/ws_lan_backfill.py --ns paas-ws-7a3676a7,paas-ws-5698f051

    # 2. 真的套用（逐 ns；任何一個 ns 失敗就停）
    python3 extra/paas-operator/scripts/ws_lan_backfill.py --ns paas-ws-7a3676a7 --apply

    # 3. 回滾（刪 paas-baseline-egress、移除 PSA 標籤；同樣先預覽再 --apply）
    python3 extra/paas-operator/scripts/ws_lan_backfill.py --ns paas-ws-7a3676a7 --rollback
    python3 extra/paas-operator/scripts/ws_lan_backfill.py --ns paas-ws-7a3676a7 --rollback --apply

對象：``paas-ws-`` 開頭、帶 ``paas-managed=true`` 與 ``woow.tw/workspace-id`` 標籤的
namespace（＝CloudService 工作區）。agent-runtime 的 ns（沒有 workspace-id）刻意排除：它的
egress 由 agent-runtime chart 收得比 baseline 更緊，加上 baseline 反而會因為 NP 聯集放寬。
``--all`` 選全部工作區；``--env prod|stg`` 再依 ``paas.woowtech.io/env`` 篩選。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src", "services"))

import tenant_baseline as tb  # noqa: E402  (pure module, stdlib only)

TENANT_PREFIX = "paas-ws-"
WORKSPACE_LABEL = "woow.tw/workspace-id"
ENV_LABEL = "paas.woowtech.io/env"


class KubectlError(RuntimeError):
    pass


def kubectl(ctx: str, args: List[str], stdin: Optional[str] = None,
            check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--context", ctx] + args
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise KubectlError(f"{' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc


def warnings_of(proc: subprocess.CompletedProcess) -> List[str]:
    return [ln.strip() for ln in proc.stderr.splitlines() if ln.lower().startswith("warning")]


def select_namespaces(ctx: str, names: List[str], all_ns: bool,
                      env: Optional[str]) -> List[dict]:
    data = json.loads(kubectl(ctx, ["get", "ns", "-o", "json"]).stdout)
    by_name = {n["metadata"]["name"]: n for n in data["items"]}
    if names:
        missing = [n for n in names if n not in by_name]
        if missing:
            raise SystemExit(f"namespace 不存在：{', '.join(missing)}")
        picked = [by_name[n] for n in names]
    elif all_ns:
        picked = [n for n in data["items"] if n["metadata"]["name"].startswith(TENANT_PREFIX)]
    else:
        raise SystemExit("請用 --ns 指定 namespace，或用 --all")
    out = []
    for ns in picked:
        name = ns["metadata"]["name"]
        labels = ns["metadata"].get("labels") or {}
        if not name.startswith(TENANT_PREFIX):
            raise SystemExit(f"{name}：不是租戶 namespace（{TENANT_PREFIX}*），拒絕處理")
        if labels.get("paas-managed") != "true" or WORKSPACE_LABEL not in labels:
            print(f"  略過 {name}：沒有 paas-managed=true＋{WORKSPACE_LABEL}"
                  "（agent-runtime 或未納管的 ns）", file=sys.stderr)
            continue
        if env and labels.get(ENV_LABEL) != env:
            continue
        out.append(ns)
    return sorted(out, key=lambda n: n["metadata"]["name"])


def live_egress_np(ctx: str, ns: str) -> Optional[dict]:
    proc = kubectl(ctx, ["-n", ns, "get", "networkpolicy", tb.BASELINE_EGRESS_NP_NAME,
                         "-o", "json"], check=False)
    if proc.returncode != 0:
        if "NotFound" in proc.stderr or "not found" in proc.stderr:
            return None
        raise KubectlError(proc.stderr.strip())
    return json.loads(proc.stdout)


def desired_psa(ns: str, exempt: List[str], version: str) -> Tuple[Dict[str, str], List[str]]:
    """回傳 (要設定的標籤, 要移除的標籤鍵)。"""
    if tb.is_psa_exempt(ns, exempt):
        return {}, tb.psa_label_keys()
    return tb.build_pod_security_labels(version=version), []


def plan_ns(ctx: str, ns_obj: dict, opts) -> dict:
    ns = ns_obj["metadata"]["name"]
    labels = ns_obj["metadata"].get("labels") or {}
    annotations = ns_obj["metadata"].get("annotations") or {}
    item = {"namespace": ns, "workspace_id": labels.get(WORKSPACE_LABEL),
            "env": labels.get(ENV_LABEL), "egress": None, "psa": None, "warnings": []}

    if opts.only in (None, "egress"):
        live = live_egress_np(ctx, ns)
        want = tb.build_baseline_egress_netpol(ns)
        if opts.rollback:
            item["egress"] = "delete" if live else "absent"
        elif live is None:
            item["egress"] = "create"
        elif live.get("spec") == want["spec"]:
            item["egress"] = "unchanged"
        else:
            item["egress"] = "update"
        if not opts.rollback and item["egress"] in ("create", "update"):
            proc = kubectl(ctx, ["apply", "--dry-run=server", "-f", "-"],
                           stdin=json.dumps(want))
            item["warnings"] += warnings_of(proc)

    if opts.only in (None, "psa"):
        exempt = tb.parse_namespace_list(opts.psa_exempt)
        if opts.rollback:
            set_, remove = {}, tb.psa_label_keys()
        else:
            set_, remove = desired_psa(ns, exempt, opts.psa_version)
        changes = []
        for k, v in set_.items():
            if labels.get(k) != v:
                changes.append(f"{k}: {labels.get(k, '∅')} → {v}")
        for k in remove:
            if k in labels:
                changes.append(f"{k}: {labels[k]} → ∅")
        exempt_ns = (not opts.rollback) and tb.is_psa_exempt(ns, exempt)
        if exempt_ns and annotations.get(tb.PSA_EXEMPT_ANNOTATION) != tb.PSA_EXEMPT_REASON:
            changes.append(f"annotation {tb.PSA_EXEMPT_ANNOTATION}: 寫入例外理由")
        item["psa"] = {"exempt": exempt_ns, "changes": changes}
        if set_ and changes:
            proc = kubectl(ctx, ["label", "--dry-run=server", "--overwrite", "ns", ns]
                           + [f"{k}={v}" for k, v in set_.items()])
            item["warnings"] += warnings_of(proc)
    return item


def apply_ns(ctx: str, item: dict, opts) -> None:
    ns = item["namespace"]
    # 套用順序與 operator 相同：PSA 先、NP 後；回滾反過來。
    if opts.rollback:
        if item["egress"] == "delete":
            kubectl(ctx, ["-n", ns, "delete", "networkpolicy", tb.BASELINE_EGRESS_NP_NAME])
        if item["psa"] and item["psa"]["changes"]:
            kubectl(ctx, ["label", "ns", ns] + [f"{k}-" for k in tb.psa_label_keys()])
        return
    if item["psa"] and item["psa"]["changes"]:
        exempt = tb.parse_namespace_list(opts.psa_exempt)
        set_, remove = desired_psa(ns, exempt, opts.psa_version)
        if set_:
            kubectl(ctx, ["label", "--overwrite", "ns", ns]
                    + [f"{k}={v}" for k, v in set_.items()])
        if remove:
            kubectl(ctx, ["label", "ns", ns] + [f"{k}-" for k in remove])
            kubectl(ctx, ["annotate", "--overwrite", "ns", ns,
                          f"{tb.PSA_EXEMPT_ANNOTATION}={tb.PSA_EXEMPT_REASON}"])
    if item["egress"] in ("create", "update"):
        kubectl(ctx, ["apply", "-f", "-"],
                stdin=json.dumps(tb.build_baseline_egress_netpol(ns)))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--context", default="woow-k3s")
    ap.add_argument("--ns", default="", help="逗號分隔的 namespace")
    ap.add_argument("--all", action="store_true", help="全部 paas-ws-* 工作區")
    ap.add_argument("--env", choices=["prod", "stg"])
    ap.add_argument("--only", choices=["egress", "psa"], help="只處理其中一項")
    ap.add_argument("--rollback", action="store_true",
                    help="刪 paas-baseline-egress、移除 PSA 標籤")
    ap.add_argument("--apply", action="store_true", help="真的寫入（沒有這個旗標只預覽）")
    ap.add_argument("--psa-exempt", default=",".join(tb.DEFAULT_PSA_EXEMPT_NAMESPACES))
    ap.add_argument("--psa-version", default="v1.34")
    ap.add_argument("--report", default="", help="把預覽／結果寫成 JSON")
    opts = ap.parse_args(argv)

    names = tb.parse_namespace_list(opts.ns)
    targets = select_namespaces(opts.context, names, opts.all, opts.env)
    mode = ("回滾" if opts.rollback else "套用") + ("" if opts.apply else "（預覽，不寫入）")
    print(f"# ws-lan backfill {mode}  context={opts.context}  ns={len(targets)}  "
          f"{_dt.datetime.now(_dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    plans = []
    for ns_obj in targets:
        item = plan_ns(opts.context, ns_obj, opts)
        plans.append(item)
        psa = item["psa"] or {}
        tag = "（PSA 例外）" if psa.get("exempt") else ""
        print(f"\n== {item['namespace']}  ws={item['workspace_id']}  env={item['env']}{tag}")
        if item["egress"] is not None:
            print(f"   NetworkPolicy {tb.BASELINE_EGRESS_NP_NAME}: {item['egress']}")
        if item["psa"] is not None:
            for c in psa.get("changes") or ["（標籤不變）"]:
                print(f"   PSA {c}")
        for w in item["warnings"]:
            print(f"   ⚠ {w}")

    if opts.apply:
        for item in plans:
            apply_ns(opts.context, item, opts)
            print(f"   ✔ {item['namespace']} 完成")
    if opts.report:
        with open(opts.report, "w", encoding="utf-8") as fh:
            json.dump({"mode": mode, "plans": plans}, fh, ensure_ascii=False, indent=2)
    n_warn = sum(1 for p in plans if p["warnings"])
    print(f"\n# 合計 {len(plans)} 個 ns；有警告的 ns：{n_warn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

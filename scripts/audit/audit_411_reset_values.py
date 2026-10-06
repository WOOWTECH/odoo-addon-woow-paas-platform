#!/usr/bin/env python3
"""#411 前置盤點：stored release values vs 平台可重算 values 差異盤點。

**兩階段**：第一階段收集端 stored values（helm get values）＋ 報告骨架；第二階段接上
比對引擎 —— 讀 `dump_computed_values.py` 在 stg odoo pod 產出的「平台可重算 values」
（單一真相源＝`CloudService._assemble_deploy_values`，#419），逐鍵三分類差異。

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
- **唯讀僅指叢集側**：對叢集只 `kubectl get/list` 與 `helm list/get values`，零寫入。
  **本機檔案不是唯讀** —— `--report` 指到的路徑會被**無條件覆寫**（不 append、不備份、
  不比對），`--raw-dir` 同理。曾因 `--report` 預設值直指 repo 內已 commit 的報告，對空
  集合環境（prod 零 CloudService）跑一次就把完整報告靜默蓋成空骨架，exit 0 無警告。
  故本腳本現在：預設報告落**系統 tmp**（`411-reset-values-audit-{env}.md`、repo 外），
  **要寫進 git 必須顯式給 `--report`**；且 `--report` 指到的既有報告若 env 標記與本次
  `--env` 不同，fail-loud 要求 `--force`（見 `docs/tracking/pitfalls/`）。
- **namespace 白名單**：只 iterate 租戶 ns —— `paas-ws-*`（prod）/ `paas-ws-stg-*`（stg）。
  system/cloudflare ns 不在盤點對象。跨 ns 的 `get ns` 僅供歸屬分流（唯讀 metadata）。
- **secret 不外洩**：raw values（含明文 secret）只寫 `--raw-dir`（預設系統 tmp、repo 外），
  **絕不進 git**；進 git 的 markdown 報告一律遮罩。

比對引擎單一真相源
------------------
`compute_platform_values()` 讀取 `--computed-json`（`dump_computed_values.py` 在 stg odoo
pod 經 `_assemble_deploy_values()` 產出）。**唯一真相源＝該 helper**，本腳本不自寫 template
defaults merge（自寫會產生假 diff）。未給 `--computed-json` 時退回第一階段行為（只收集、
diff 標 pending）。

helm 版本註記
------------
release 由 operator（helm 3.13.3）部署；本機若為 helm 4，`get values -o json` 會多插一個
sentinel top-level key `USER-SUPPLIED VALUES`（helm 3 無），本腳本會剝除。get values/list
為唯讀，helm 4 讀 helm 3 release 相容。

用法
----
    # 第二階段（W4）：先在 stg pod 產 computed dump（見 dump_computed_values.py），再比對
    # `--report` 顯式指 repo 路徑＝**這份 repo 報告會被整份覆寫**，確認要更新它才給
    python3 scripts/audit/audit_411_reset_values.py --env stg --context woow-k3s \\
        --computed-json computed.json \\
        --report docs/tracking/reports/411-reset-values-audit.md

    # 只收集不比對（第一階段行為；不給 --computed-json）
    # 不給 --report → 報告落系統 tmp 的 411-reset-values-audit-stg.md，repo 完全不動
    python3 scripts/audit/audit_411_reset_values.py --env stg --context woow-k3s
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


# ---------------------------------------------------------------------------
#  第二階段：比對引擎（讀 helper 產出的 computed values，逐鍵三分類）
# ---------------------------------------------------------------------------

def _flatten(node: Any, path: tuple = ()) -> dict:
    """攤平 nested dict 成 {path_tuple: leaf_value}。list 當 leaf（整體比對）。

    路徑用 tuple 而非 dot-join —— 鍵名含點（如 `kubernetes.io/arch`）dot-join 會假陽性。
    """
    out: dict = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_flatten(value, path + (key,)))
    else:
        out[path] = node  # scalar 或 list 視為 leaf
    return out


# 平台 injector 寫入的「整條路徑」鍵（`CloudService._assemble_deploy_values` 的
# value-path 表）。這裡是**複本**（本腳本不能 import Odoo），由
# `test_audit_411_classifier.TestInjectorPathsLockstep` 以 AST 讀
# src/models/cloud_service.py 對帳：那邊新增 injector path 卻沒登記到這裡會紅。
#
# 為什麼一定要登記：沒登記的 injector path 在 stored／computed 不同時會被分到 (b)
# backfill 或 (c) 逐筆裁決——例如 Omnigent 被刪後 stored release 還留著舊的
# omnigentHost.serverUrl、computed 已經沒有，(b) 會叫維運把死掉的網址寫回
# svc.helm_values，從此永遠釘住。injector 值每次部署都重算，差異一律自癒 (a)。
_INJECTOR_VALUE_PATHS = frozenset({
    ("config", "allowedOrigins"),         # allowed_origins injector（open-design，#399）
    ("codeServer", "publicUrl"),          # allowed_origins injector（code-server）
    ("server", "baseUrl"),                # allowed_origins injector（omnigent）
    ("nextcloud", "publicUrl"),           # allowed_origins injector（nextcloud）
    ("seed", "coreConfig", "externalUrl"),  # allowed_origins injector（ha-full，#1388）
    ("config", "dashboardPublicUrl"),     # dashboard_public_url injector（hermes-agent，#424①）
    ("networkPolicy", "apiServerCidrs"),  # apiserver_cidrs injector（#404）
    ("omnigentHost", "serverUrl"),        # workspace_service_url injector（同工作區 Omnigent）
    ("tailscale", "workspaceHeadscaleUrl"),  # workspace_service_url injector（同工作區 Headscale）
    ("llmGateway", "baseUrl"),            # workspace_service_url injector（同工作區 LiteLLM）
    ("config", "nonSensitive", "connection", "base_url"),  # 同上（mcp-litellm）
})


def _is_injector_path(path: tuple) -> bool:
    """injector 類鍵：平台管線每次重算會注入，reset 後自癒、不需人工 backfill。"""
    if not path:
        return False
    if path[-1] == "storageClass":                   # storage_class injector（可 nested）
        return True
    if "imagePullSecrets" in path:                   # image_pull_secrets injector
        return True
    return tuple(path) in _INJECTOR_VALUE_PATHS


def _is_config_sensitive(path: tuple) -> bool:
    """path 落在 config.sensitive 子樹 —— 由 sensitive_values 管線另帶（reset 路徑帶新密碼、
    不在 _assemble_deploy_values 的 computed 內），視同自癒、不列 backfill/裁決。"""
    return any(path[i:i + 2] == ("config", "sensitive") for i in range(len(path) - 1))


def _path_is_secret(path: tuple) -> bool:
    """path 是否落在敏感子樹（供 diff 明細遮罩，沿用第一階段遮罩語意）。"""
    for i in range(len(path) - 1):
        if path[i:i + 2] == ("config", "sensitive"):
            return True
    return any(
        k == "secret" or _SECRET_KEY_HEURISTIC_RE.search(str(k)) for k in path
    )


def _mask_leaf(path: tuple, value: Any, present: bool) -> Any:
    """diff 明細 leaf 值遮罩：不存在 → None；secret path → ***；否則原值。"""
    if not present:
        return None
    if _path_is_secret(path):
        return "***"
    return value


def compute_platform_values(
    computed_lookup: dict, ns: str, release: str
) -> Optional[dict]:
    """從 dump（--computed-json）取該 release 的平台可重算 values。

    單一真相源＝`CloudService._assemble_deploy_values()`（#419），由 dump_computed_values.py
    在 stg odoo pod 產出。本腳本**不自寫** template defaults merge。找不到對應 release
    （dump 未涵蓋、或該 service 組裝失敗）回 None → 該 release diff 標 pending。
    """
    return computed_lookup.get((ns, release))


def classify_diff(stored: dict, computed: Optional[dict]) -> Optional[dict]:
    """逐 leaf path 比對 stored（helm stored）vs computed（平台重算），三分類。

      (a) injector 類（任何差異）：管線每次重算會自癒，無須動作。
      (b) stored 有、computed 無、非 injector：**backfill** —— reset 後會從 manifest 消失，
          須顯式補進 svc.helm_values。
      (c) 同鍵不同值、非 injector：**逐筆裁決** —— 例 image tag pin（stored 釘舊 tag、
          computed 是 template 新 tag，reset 後會被覆蓋）。
      (info) computed 有、stored 無：reset 後自然補上（預期效果，非問題）。
      同鍵同值不列。

    computed 為 None（dump 未涵蓋該 release）→ 回 None（標 pending）。
    回 {'counts': {a,b,c,info}, 'items': [{path, category, stored, computed}...]}；
    items 的 leaf 值已遮罩。
    """
    if computed is None:
        return None
    s = _flatten(stored)
    c = _flatten(computed)
    items: list[dict] = []
    for path in sorted(set(s) | set(c)):
        in_s, in_c = path in s, path in c
        sv, cv = s.get(path), c.get(path)
        # injector 類、或 config.sensitive（sensitive_values 另帶）→ 自癒，不列 backfill/裁決。
        self_healing = _is_injector_path(path) or _is_config_sensitive(path)
        if in_s and in_c:
            if sv == cv:
                continue  # match，不列
            cat = "a" if self_healing else "c"
        elif in_s:  # stored-only
            cat = "a" if self_healing else "b"
        else:       # computed-only
            cat = "info"
        items.append({
            "path": list(path),
            "category": cat,
            "stored": _mask_leaf(path, sv, in_s),
            "computed": _mask_leaf(path, cv, in_c),
        })
    counts = {cat: 0 for cat in ("a", "b", "c", "info")}
    for it in items:
        counts[it["category"]] += 1
    return {"counts": counts, "items": items}


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
    lines.append(f"> 狀態：{meta['diff_status']}")
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

    # 差異分類定義
    lines.append("## 差異分類")
    lines.append("")
    lines.append(
        "- **(a) injector 類會自癒**：storageClass / imagePullSecrets / config.allowedOrigins"
        "（#399）/ codeServer.publicUrl / server.baseUrl / nextcloud.publicUrl / "
        "seed.coreConfig.externalUrl（ha-full，#1388）/ "
        "config.dashboardPublicUrl（#424①）/ networkPolicy.apiServerCidrs（#404）/ "
        "omnigentHost.serverUrl（同工作區 Omnigent，Omnigent 刪除後 stored 殘留舊網址也屬此類，"
        "**不要** backfill）等平台管線每次重算的鍵，reset 後自動補回，無須動作。"
    )
    lines.append(
        "- **(b) backfill**：stored 有、平台 computed 無、且非 injector —— reset 後會從 rendered "
        "manifest 消失，翻語意前須顯式補進 svc.helm_values。"
    )
    lines.append(
        "- **(c) 逐筆裁決**：同鍵不同值、非 injector（例 image tag pin：stored 釘舊 tag、computed "
        "是 template 新 tag，reset 後被覆蓋）。**prod 翻 flag 前 (c) 必須清零。**"
    )
    lines.append(
        "- (info) computed 有、stored 無：reset 後自然補上（預期效果，非問題，不列明細）。"
    )
    lines.append("")

    diffed = [r for r in ok if r.get("diff")]
    if not diffed:
        lines.append(
            "> 本次未提供 `--computed-json`（或 dump 未涵蓋任何 release）—— diff 標 pending。"
            "W4 部署含 #419 的 stg 後，跑 `dump_computed_values.py` 產 computed dump 再比對。"
        )
        lines.append("")
    else:
        def _fmt(v: Any) -> str:
            return "`" + json.dumps(v, ensure_ascii=False) + "`"

        # (b) backfill 清單彙總
        lines.append("### (b) backfill 清單")
        lines.append("")
        b_rows = [(r, it) for r in diffed for it in r["diff"]["items"] if it["category"] == "b"]
        if b_rows:
            lines.append("| namespace | release | path | stored 值 |")
            lines.append("| --- | --- | --- | --- |")
            for r, it in b_rows:
                lines.append(
                    f"| `{r['ns']}` | `{r['release']}` | {_fmt(it['path'])} | {_fmt(it['stored'])} |"
                )
        else:
            lines.append("_（無）_")
        lines.append("")

        # (c) 待裁決清單彙總
        lines.append("### (c) 待裁決清單（prod 翻 flag 前須清零）")
        lines.append("")
        c_rows = [(r, it) for r in diffed for it in r["diff"]["items"] if it["category"] == "c"]
        if c_rows:
            lines.append("| namespace | release | path | stored | computed |")
            lines.append("| --- | --- | --- | --- | --- |")
            for r, it in c_rows:
                lines.append(
                    f"| `{r['ns']}` | `{r['release']}` | {_fmt(it['path'])} | "
                    f"{_fmt(it['stored'])} | {_fmt(it['computed'])} |"
                )
        else:
            lines.append("_（無）_")
        lines.append("")

        # per-release 分類明細
        lines.append("### Per-release 分類明細")
        lines.append("")
        for r in diffed:
            cc = r["diff"]["counts"]
            lines.append(
                f"#### {r['env']} / `{r['ns']}` / `{r['release']}` "
                f"— a:{cc['a']} b:{cc['b']} c:{cc['c']} info:{cc['info']}"
            )
            lines.append("")
            lines.append(_json_block(r["diff"]["items"]))
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


# ---------------------------------------------------------------------------
#  報告落點守線（報告是無條件覆寫 —— 見模組 docstring 鐵律段）
# ---------------------------------------------------------------------------

# render_report header 寫出的 env 標記；覆寫守線據此判斷是否跨環境誤蓋。
_REPORT_ENV_MARKER_RE = re.compile(r"｜env=`([^`]+)`")


def default_report_path(env: str) -> Path:
    """預設報告落點＝系統 tmp、檔名帶 env（與 --raw-dir 落點語意一致，皆 repo 外）。

    刻意不預設寫進 repo：報告一律無條件覆寫，預設指向 repo 內已 commit 的報告時，
    一次空集合環境的盤點就會把完整報告靜默洗成空骨架。要進 git 請顯式
    `--report docs/tracking/reports/...`。檔名帶 env 讓 prod / stg 兩份預設報告不互踩。
    """
    return Path(tempfile.gettempdir()) / f"411-reset-values-audit-{env}.md"


def read_report_env(text: str) -> Optional[str]:
    """從既有報告內容抓 env 標記；抓不到回 None（手寫檔／格式外，守線放行不誤擋）。"""
    match = _REPORT_ENV_MARKER_RE.search(text)
    return match.group(1) if match else None


def guard_report_target(report_path: Path, env: str, force: bool) -> None:
    """跨環境覆寫守線：既有報告的 env 標記 != 本次 `--env` → fail-loud（需 `--force`）。

    只在**明確偵測到**跨環境覆寫時擋；檔案不存在、讀不到、或無 env 標記一律放行。
    須在收集開始前呼叫（跑完幾十分鐘才發現衝突就來不及了）。
    """
    try:
        existing = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return  # 不存在／讀不到 → 無從判斷，放行
    prev_env = read_report_env(existing)
    if prev_env is None or prev_env == env:
        return
    if force:
        print(
            f"[warn] --force：覆寫 env=`{prev_env}` 的既有報告（本次 env=`{env}`）：{report_path}",
            file=sys.stderr,
        )
        return
    raise SystemExit(
        f"[fatal] {report_path} 是 env=`{prev_env}` 的既有報告，本次 --env={env}"
        " —— 覆寫會洗掉另一環境的內容。確認要覆寫請加 --force，或改指其他 --report 路徑。"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="#411 reset-values backfill 盤點（第一階段：收集＋骨架）")
    parser.add_argument("--context", default=DEFAULT_CONTEXT, help="kube context（預設 woow-k3s）")
    parser.add_argument(
        "--env", choices=["prod", "stg", "both"], default="both", help="盤點環境（預設 both）"
    )
    parser.add_argument(
        "--report",
        default=None,
        help=(
            "markdown 報告輸出路徑（遮罩後）。**該路徑會被無條件覆寫**；未給則落系統 tmp "
            "的 411-reset-values-audit-{env}.md（repo 外）。要寫進 git 必須顯式指定。"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="允許覆寫 env 標記與本次 --env 不同的既有報告（預設 fail-loud 擋下）",
    )
    parser.add_argument(
        "--raw-dir",
        default=None,
        help="raw values（未遮罩）落點；預設系統 tmp（repo 外，勿進 git）",
    )
    parser.add_argument(
        "--computed-json",
        default=None,
        help="dump_computed_values.py 產出的平台可重算 values JSON（第二階段比對；未給則只收集）",
    )
    args = parser.parse_args(argv)

    # 報告落點先定案並過守線 —— fail fast：跑完整輪盤點才發現覆寫衝突就來不及了。
    report_path = Path(args.report) if args.report else default_report_path(args.env)
    guard_report_target(report_path, args.env, args.force)

    generated_at = time.strftime("%Y-%m-%d %H:%M:%S %z")
    raw_dir = Path(args.raw_dir) if args.raw_dir else Path(
        tempfile.mkdtemp(prefix="audit-411-raw-")
    )
    raw_dir.mkdir(parents=True, exist_ok=True)

    # 第二階段：載入平台可重算 values dump（--computed-json）→ lookup {(ns, release): values}
    computed_lookup: dict = {}
    if args.computed_json:
        try:
            dump = json.loads(Path(args.computed_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"[fatal] --computed-json 讀取失敗: {exc}")
        for entry in dump:
            if "computed_values" in entry:
                computed_lookup[(entry["namespace"], entry["release"])] = entry["computed_values"]
        print(
            f"[info] computed dump：{len(computed_lookup)} release 有可重算 values",
            file=sys.stderr,
        )

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
            # 比對用真值 stored（classify_diff 內部 _mask_leaf 遮罩 items 值）
            computed = compute_platform_values(computed_lookup, ns, name)
            diff = classify_diff(stored if isinstance(stored, dict) else {}, computed)
            if diff is None:
                classification = "pending"
            else:
                cc = diff["counts"]
                classification = f"a:{cc['a']} b:{cc['b']} c:{cc['c']} info:{cc['info']}"

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
                    "diff": diff,
                }
            )
            print(f"[ok] {ns}/{name} chart={rel.get('chart')} diff={classification}", file=sys.stderr)

    diffed_n = sum(1 for r in records if r.get("diff"))
    meta = {
        "context": args.context,
        "env": args.env,
        "generated_at": generated_at,
        "helm_version": _helm_version(),
        "raw_dir": str(raw_dir),
        "ns_count": len(targets),
        "diff_status": (
            f"已接 helper 比對：{diffed_n} release 有 computed dump（逐鍵三分類）"
            if args.computed_json else
            "未提供 --computed-json，diff 標 pending（W4 部署含 #419 的 stg 後補跑）"
        ),
    }
    report = render_report(records, meta)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    print(f"[done] 報告：{report_path}", file=sys.stderr)
    if not args.report:
        print(
            "[note] 未給 --report → 報告落系統 tmp（repo 未被觸碰）；"
            "要更新 repo 內報告請顯式 --report <路徑>（該檔會被整份覆寫）",
            file=sys.stderr,
        )
    print(f"[done] raw dump（不進 git）：{raw_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

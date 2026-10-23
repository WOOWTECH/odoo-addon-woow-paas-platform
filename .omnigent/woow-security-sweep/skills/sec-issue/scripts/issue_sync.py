#!/usr/bin/env python3
"""woow-security-sweep：把判定成立的 finding 開成 Gitea issue（#1957）。

子命令::

    issue_sync.py sync --run-dir <本輪目錄>/<repo 名> [--dry-run]

開單條件（同一 repo＋fingerprint 只開一張）：

- 人判成立：`jev_gate human` 的 label 為 true_positive、action 為 fix／track／escalate ⇒ 標 sweep/human
- 還沒有人工決定、最後一筆 Jev 判 true_positive 且信心 ≥ issues.jev_min_confidence ⇒ 標 sweep/jev

已開的單（只處理 open 的；人手動關掉的不動）：

- sweep/jev 的單後來人判成立 ⇒ 留言，來源 label 換成 sweep/human
- 人判誤報，或 action 為 drop／accept_risk ⇒ 留言，label 換成 sweep/false-positive 後關閉

安全設計：

- 只寫 gates.yaml 的 issues.owner（Gitea mirror-internal）；repo 名取自本輪 config.json，不接受參數。
  ⛔ 不寫 GitHub：目標 repo 是 public，未修先開就是公開漏洞細節。
- 一律透過 `tea api --login <issues.tea_login>` 寫入，本腳本不碰 token。
- body 不貼程式碼片段；工具訊息與人的 note 先遮蔽秘密值形狀的字串，再放進 code fence
  （finding 訊息可能來自 repo 內容，是不可信資料）；gitleaks 只寫規則與位置。
- 每個寫入動作都追加一筆 `type: issue` 紀錄到 shadow log（只追加）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

SWEEP_DIR = Path(__file__).resolve().parents[3]

_spec = importlib.util.spec_from_file_location("_sweep_jev_gate", SWEEP_DIR / "skills/sec-gate/scripts/jev_gate.py")
jg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jg)

SOURCE_LABEL = "woow-security-sweep"
#: name → (color, description, exclusive)。scope 用 `/`，Gitea 的 exclusive label 同 scope 只留一個。
LABELS: dict[str, tuple[str, str, bool]] = {
    SOURCE_LABEL: ("#5319e7", "woow-security-sweep 自動開立", False),
    "sweep/human": ("#0e8a16", "人在 Inbox 判成立", True),
    "sweep/jev": ("#fbca04", "Jev 高信心判成立，尚未經人確認", True),
    "sweep/false-positive": ("#cccccc", "人判誤報或接受風險，sweep 已關閉", True),
    "severity/info": ("#c5def5", "嚴重度 info", True),
    "severity/low": ("#bfd4f2", "嚴重度 low", True),
    "severity/medium": ("#f9d0c4", "嚴重度 medium", True),
    "severity/high": ("#e99695", "嚴重度 high", True),
    "severity/critical": ("#b60205", "嚴重度 critical", True),
}
POSITIVE_ACTIONS = ("fix", "track", "escalate")
NEGATIVE_ACTIONS = ("drop", "accept_risk")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MARKER_RE = re.compile(r"<!-- woow-security-sweep:fp=([0-9a-f]{16}) -->")

#: 秘密值形狀（sec-report 的遮蔽規則）
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*KEY-----[\s\S]*?(?:-----END [A-Z ]*KEY-----|$)"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    # 不加 \b：access_token=、client_secret= 的底線前沒有字詞邊界
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"[A-Za-z0-9_+/=-]{20,}"),
]


class IssueError(Exception):
    """開單層的錯誤。"""


# ── 純函式 ────────────────────────────────────────────────────────────────────

def mask(text: str | None) -> str:
    out = str(text or "")
    for pat in SECRET_PATTERNS:
        out = pat.sub("***", out)
    return out


def fence(text: str) -> str:
    """不可信文字放進 code fence：不渲染連結、@mention、HTML。"""
    return "~~~text\n" + text.replace("~~~", "~ ~ ~") + "\n~~~"


def fp_key(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]


def classify(group: dict[str, Any], threshold: float) -> str | None:
    """依人工決定與 Jev 答案分類：human（人判成立）／jev（高信心）／negative／None。"""
    h = group.get("human")
    if h:
        if h.get("label") == "false_positive" or h.get("action") in NEGATIVE_ACTIONS:
            return "negative"
        if h.get("label") == "true_positive" and h.get("action") in POSITIVE_ACTIONS:
            return "human"
        return None
    if group.get("last") != "decision":
        return None
    last = group["decisions"][-1]
    info = last.get("info") or {}
    if last.get("error") or last.get("proposed") == "drop":
        return None
    if info.get("verdict") == "true_positive" and float(info.get("confidence") or 0) >= threshold:
        return "jev"
    return None


def safe_path(path: str | None) -> str | None:
    p = str(path or "").removeprefix("./")
    if not p or p.startswith("/") or ".." in p.split("/") or "\n" in p:
        return None
    return p


def issue_title(finding: dict[str, Any], level: str) -> str:
    rule = str(finding.get("rule") or "finding").split(".")[-1][:60]
    loc = safe_path(finding.get("path")) or "?"
    line = finding.get("line")
    title = f"[sweep][{level}] {rule} — {loc}{f':{line}' if line else ''}"
    return re.sub(r"\s+", " ", title)[:150]


def issue_body(*, full_name: str, owner: str, name: str, finding: dict[str, Any], decision: dict[str, Any],
               group: dict[str, Any], head_sha: str | None, trigger: str) -> str:
    info = decision.get("info") or {}
    path = safe_path(finding.get("path"))
    line = finding.get("line")
    loc = f"`{path}{f':{line}' if line else ''}`" if path else "（路徑不安全，已省略）"
    if path and head_sha and re.fullmatch(r"[0-9a-f]{7,40}", head_sha):
        anchor = f"#L{int(line)}" if isinstance(line, int) else ""
        loc = f"[{loc}](https://github.com/{full_name}/blob/{head_sha}/{urllib.parse.quote(path)}{anchor})"
    h = group.get("human")
    if h:
        human = f"{h.get('label')}／{h.get('action')}（{str(h.get('ts', ''))[:10]}）"
    else:
        human = "尚未確認（請到 Omnigent Inbox 回答）"
    why = ("人在 Inbox 判成立" if trigger == "human"
           else f"Jev 判成立且信心 {float(info.get('confidence') or 0):.2f}，尚未經人確認")
    tool = str(finding.get("tool") or "?")
    level = info.get("level") or "?"
    rows = [
        ("repo", f"[{full_name}](https://github.com/{full_name})（鏡像 `{owner}/{name}`）"),
        ("位置", loc),
        ("工具／規則", f"{tool}／`{finding.get('rule')}`"),
        ("嚴重度", f"{level}（工具標示：{finding.get('severity_tool') or '-'}）"),
        ("Jev", f"{info.get('verdict', '?')}，信心 {float(info.get('confidence') or 0):.2f}（{decision.get('model')}）"),
        ("人工決定", human),
        ("decision id", f"`{decision.get('id')}`"),
        ("首次偵測", f"{str(group['decisions'][0].get('ts', ''))[:10]}（共 {len(group['decisions'])} 輪）"),
    ]
    parts = [
        f"> 由 **woow-security-sweep** 自動開立：{why}。",
        "> ⛔ 內容不要轉貼到公開的 GitHub repo；嚴重度 ≥ high 走 GitHub Security Advisory（private fork）。",
        "",
        "| 項目 | 內容 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in rows],
        "",
        "### 說明",
        "",
    ]
    if tool == "gitleaks":
        parts.append("gitleaks 偵測到疑似秘密值（依規則不貼內容，請到原始位置查看）。")
    else:
        parts.append(fence(mask(finding.get("message"))))
    if h and h.get("note"):
        parts += ["", "### 人工備註", "", fence(mask(h.get("note")))]
    parts += [
        "",
        "### 下一步",
        "",
        "- 成立：修正後關閉本 issue；嚴重度 ≥ high 請照 report.md 的 advisory 草稿處理",
        f"- 誤報或接受風險：在 Inbox 回答，或在 runner 執行 `jev_gate.py human --id {decision.get('id')} ...`；"
        "sweep 下一輪會留言並關閉本 issue",
        "",
        f"<!-- woow-security-sweep:fp={fp_key(str(decision.get('fingerprint')))} -->",
    ]
    return "\n".join(parts)


# ── Gitea（透過 tea） ─────────────────────────────────────────────────────────

class TeaForge:
    """`tea api` 包裝。tea 遇到 HTTP 錯誤仍回 exit 0，所以用 -i 讀 stderr 的狀態列判斷。"""

    def __init__(self, owner: str, name: str, login: str):
        self.base = f"repos/{owner}/{name}"
        self.login = login

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        cmd = ["tea", "api", "-i", "--login", self.login, "-X", method]
        with tempfile.TemporaryDirectory() as tmp:
            if body is not None:
                data = Path(tmp) / "body.json"
                data.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
                cmd += ["-d", f"@{data}"]
            try:
                res = subprocess.run(cmd + [f"{self.base}{path}"], capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise IssueError(f"tea 執行失敗：{type(exc).__name__}") from exc
        m = re.search(r"^HTTP/\S+ (\d{3})", res.stderr, re.M)
        status = int(m.group(1)) if m else 0
        if res.returncode != 0 or not 200 <= status < 300:
            raise IssueError(f"{method} {path} 失敗（exit {res.returncode}，HTTP {status or '?'}）")
        out = res.stdout.strip()
        return json.loads(out) if out else None

    def labels(self) -> dict[str, int]:
        return {lb["name"]: lb["id"] for lb in self._call("GET", "/labels?limit=100") or []}

    def create_label(self, name: str, color: str, description: str, exclusive: bool) -> int:
        return self._call("POST", "/labels", {"name": name, "color": color, "description": description,
                                              "exclusive": exclusive})["id"]

    def sweep_issues(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 50):
            rows = self._call("GET", f"/issues?state=all&type=issues&labels={SOURCE_LABEL}&limit=50&page={page}")
            if not rows:
                break
            out += rows
        return out

    def create_issue(self, title: str, body: str, label_ids: list[int]) -> dict[str, Any]:
        return self._call("POST", "/issues", {"title": title, "body": body, "labels": label_ids})

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/issues/{number}/comments", {"body": body})

    def set_labels(self, number: int, label_ids: list[int]) -> None:
        self._call("PUT", f"/issues/{number}/labels", {"labels": label_ids})

    def close(self, number: int) -> None:
        self._call("PATCH", f"/issues/{number}", {"state": "closed"})


# ── 同步 ──────────────────────────────────────────────────────────────────────

def _decision_index(runs_root: Path) -> dict[str, tuple[dict[str, Any], str | None]]:
    """decision id → (finding, 該輪 head_sha)。shadow log 不存 finding 本體。"""
    out: dict[str, tuple[dict[str, Any], str | None]] = {}
    for path in sorted(runs_root.glob("*/*/decisions.jsonl")):
        meta_path = path.parent / "meta.json"
        head = json.loads(meta_path.read_text(encoding="utf-8")).get("head_sha") if meta_path.exists() else None
        for r in jg.read_jsonl(path):
            if r.get("id") and r.get("finding"):
                out[r["id"]] = (r["finding"], head)
    return out


def _ensure_labels(forge: Any, dry_run: bool) -> dict[str, int]:
    ids = forge.labels()
    for name, (color, desc, exclusive) in LABELS.items():
        if name not in ids:
            ids[name] = -1 if dry_run else forge.create_label(name, color, desc, exclusive)
    return ids


def _labels_for(ids: dict[str, int], source: str, level: str | None) -> list[int]:
    names = [SOURCE_LABEL, source] + ([f"severity/{level}"] if f"severity/{level}" in LABELS else [])
    return [ids[n] for n in names]


def sync_run(run_dir: Path, sdir: Path, gates: dict[str, Any], *, forge: Any = None,
             dry_run: bool = False) -> dict[str, Any]:
    conf = gates.get("issues") or {}
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    full_name = str((cfg.get("repo") or {}).get("full_name") or "")
    summary: dict[str, Any] = {"repo": full_name, "dry_run": dry_run, "opened": [], "confirmed": [],
                               "closed": [], "skipped": [], "errors": []}
    if not conf.get("enabled"):
        summary["skipped"].append("issues.enabled=false")
        return summary
    owner, name = str(conf.get("owner") or ""), full_name.rpartition("/")[2]
    if owner != "mirror-internal" or not NAME_RE.match(name):
        raise IssueError(f"不允許的目標：{owner}/{name}（owner 只能是 mirror-internal）")
    threshold = float(conf.get("jev_min_confidence", 0.85))
    forge = forge or TeaForge(owner, name, str(conf.get("tea_login") or "git-prod"))

    shadow = sdir / "shadow.jsonl"
    records = jg.read_jsonl(shadow)
    groups = jg.triage_history(records)
    index = _decision_index(run_dir.parent.parent)
    opened_before = {r.get("fingerprint") for r in records
                     if r.get("type") == "issue" and r.get("repo") == full_name and r.get("event") == "opened"}

    ids = _ensure_labels(forge, dry_run)
    remote: dict[str, dict[str, Any]] = {}
    for issue in forge.sweep_issues():
        m = MARKER_RE.search(issue.get("body") or "")
        if m:
            remote.setdefault(m.group(1), issue)

    def log(event: str, fp: str, decision: dict[str, Any], issue: dict[str, Any], trigger: str | None) -> None:
        if not dry_run:
            jg.append_jsonl(shadow, {
                "type": "issue", "id": uuid.uuid4().hex[:12], "ts": jg.now_iso(), "repo": full_name,
                "fingerprint": fp, "ref": decision.get("id"), "event": event, "trigger": trigger,
                "forge": f"{owner}/{name}", "number": issue.get("number"), "url": issue.get("html_url"),
            })

    for (g_repo, fp), group in groups.items():
        if g_repo != full_name:
            continue
        kind = classify(group, threshold)
        if kind is None:
            continue
        decision = group["decisions"][-1]
        level = (decision.get("info") or {}).get("level")
        existing = remote.get(fp_key(fp))
        item = {"fingerprint": fp, "decision_id": decision.get("id")}
        try:
            if existing is None:
                if kind == "negative":
                    continue
                if fp in opened_before:
                    summary["skipped"].append(dict(item, reason="state 記錄已開過，但遠端找不到（label 被移除？）"))
                    continue
                finding, head = index.get(decision["id"]) or index.get(group["decisions"][0]["id"], ({}, None))
                if not finding:
                    summary["skipped"].append(dict(item, reason="找不到 finding 本體"))
                    continue
                title = issue_title(finding, level or "?")
                body = issue_body(full_name=full_name, owner=owner, name=name, finding=finding, decision=decision,
                                  group=group, head_sha=head, trigger=kind)
                issue = {"number": None, "html_url": None} if dry_run else \
                    forge.create_issue(title, body, _labels_for(ids, f"sweep/{kind}", level))
                log("opened", fp, decision, issue, kind)
                summary["opened"].append(dict(item, trigger=kind, title=title, number=issue.get("number"),
                                              url=issue.get("html_url")))
                continue
            if existing.get("state") != "open":
                continue
            labels = {lb.get("name") for lb in existing.get("labels") or []}
            ref = {"number": existing.get("number"), "html_url": existing.get("html_url")}
            if kind == "human" and "sweep/jev" in labels:
                if not dry_run:
                    forge.comment(existing["number"], "人在 Inbox 判成立，來源 label 改為 `sweep/human`。")
                    forge.set_labels(existing["number"], _labels_for(ids, "sweep/human", level))
                log("confirmed", fp, decision, ref, "human")
                summary["confirmed"].append(dict(item, number=ref["number"], url=ref["html_url"]))
            elif kind == "negative":
                h = group.get("human") or {}
                if not dry_run:
                    forge.comment(existing["number"], f"人判 {h.get('label')}／{h.get('action')}，sweep 關閉本 issue。")
                    forge.set_labels(existing["number"], _labels_for(ids, "sweep/false-positive", level))
                    forge.close(existing["number"])
                log("closed", fp, decision, ref, None)
                summary["closed"].append(dict(item, number=ref["number"], url=ref["html_url"]))
        except IssueError as exc:
            summary["errors"].append(dict(item, error=str(exc)))
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", help="shadow log 目錄（預設 SWEEP_STATE_DIR 或 runner workspace）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", help="依人工決定與 Jev 答案開／更新／關閉 issue")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--dry-run", action="store_true", help="只列出會做的事，不寫 Gitea、不寫 shadow log")
    args = ap.parse_args(argv)
    try:
        summary = sync_run(Path(args.run_dir), jg.state_dir(args.state_dir), jg.load_gates(), dry_run=args.dry_run)
    except (IssueError, jg.GateError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 4 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

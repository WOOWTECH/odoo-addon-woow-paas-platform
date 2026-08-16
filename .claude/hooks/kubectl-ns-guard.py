#!/usr/bin/env python3
"""PreToolUse kubectl-ns-guard：woow-k3s 共用叢集 namespace 白名單的 Bash 層機器守線。

CLAUDE.md「⛔ 鐵律」的機器強制版：`--context woow-k3s` 下寫入／exec 類 kubectl
指令，namespace 必須在白名單內，否則 deny。唯讀動詞（get／describe／logs…）不擋
——CLAUDE.md 明文允許跨 ns 唯讀盤點供歸屬分流。寫入類動詞未帶 `--context` 也擋
（「kubectl 請明確指定 --context」的機器版；唯讀不帶 context 放行，避免本機開發
的日常 get 被誤傷）。

形狀（為什麼是 allowlist）：denylist 比對指令文字擋不住改寫，且會把白名單內的
namespace 誤寫成禁區（2026-08-15 外部工具真的產過這種範例）。fail-closed 白名單
才對齊 .claude/skills/lib/kube-context.sh 的守衛方向。本 hook 是「提醒層」——攔
互動式手打 kubectl 的越界（2026-08 實際發生過 agent exec 進非白名單 ns）；skill
腳本層的保證仍在 kube-context.sh，兩層各管各的入口。

解析邊界（每一條都是為了避開「報告範例 hook」的假陽性）：
- heredoc 內文整段跳過——寫一個「內容含 kubectl」的 script 檔不是執行 kubectl。
- 只看「指令位」的 kubectl：`echo "kubectl ..."`、`git commit -m "... kubectl"`
  的第一個 token 不是 kubectl ⇒ 不解析。引號內的 `&&`／`;` 不當作指令分隔。
- `bash -c "..."` 的 payload 遞迴解析一層——這是最自然的繞道。
- `--` 之後屬遠端指令（`kubectl exec pod -- psql -n other`），停止解析。
- 已知不涵蓋：kubectl 藏在 script 檔裡再 `bash file.sh` 執行。那一層本 hook 看不
  到也不假裝看得到（用「入口的呼叫者」推範圍會漏掉繞過它的路徑——所以才說這是
  提醒層，不是保證層）。

跳過守衛：指令前綴 `KUBECTL_NS_GUARD=off `（一次性）或 export 同名 env（整個
session）。白名單三份副本（CLAUDE.md 表格／shared-cluster-paas-scope-guard.md
散文／本檔 ALLOWED_NAMESPACES）由 repo-lint 的 kubectl-ns-whitelist-sync 對帳。

Fail-open 原則：stdin JSON 壞掉／解析拋例外 → exit 0 放行，絕不擋開發（同
pattern-guard）。fail-open 蓋的是「解析器自己壞了」；指令合法解析出越界 namespace
永遠走 deny，不落入這格。
"""
import json
import os
import re
import shlex
import sys

# 🔴 白名單的第三份副本（另兩份：CLAUDE.md「只能操作以下 namespace」表格、
# docs/guides/deployment/shared-cluster-paas-scope-guard.md「適用範圍」散文）。
# 改任何一份，scripts/audit/repo-lint.py 的 kubectl-ns-whitelist-sync 會逼你同步。
# `fleet-local` 另有「僅限 paas-* 前綴資源」的限定，資源名這層本 hook 不驗（見
# CLAUDE.md 表格下方的 ⚠ 段），只放行 namespace 本身。
ALLOWED_NAMESPACES = (
    "paas-ws-*",
    "paas-ws-stg-*",
    "paas-system",
    "paas-stg-system",
    "paas-cloudflare-system",
    "paas-stg-cloudflare-system",
    "dev-sandbox-*",
    "fleet-local",
)

GUARDED_CONTEXT = "woow-k3s"

# 唯讀動詞：CLAUDE.md 允許跨 ns 唯讀盤點，一律放行（.claude/settings.json 的
# permissions 也早已預先放行 kubectl get／describe／wait）。
READ_VERBS = frozenset((
    "get", "describe", "logs", "top", "wait", "events", "explain", "diff",
    "api-resources", "api-versions", "cluster-info", "auth",
))
# 不碰叢集或不吃 namespace 的動詞：連 --context 都不要求。
NO_CLUSTER_VERBS = frozenset((
    "config", "version", "options", "help", "completion",
))
# 動詞不在上面兩組 ⇒ 一律當寫入類處理（fail-closed：新動詞先擋，錯擋再補進
# READ_VERBS——漏擋比錯擋貴）。

# 帶值的 flag（token 沒用 `=` 黏值時要跳過下一個 token，才不會把值誤認成動詞）。
VALUE_FLAGS = frozenset((
    "--context", "-n", "--namespace", "--kubeconfig", "--cluster", "--user",
    "-s", "--server", "--token", "--as", "--as-group", "--cache-dir",
    "--certificate-authority", "--client-certificate", "--client-key",
    "--request-timeout", "-l", "--selector", "--field-selector",
    "-o", "--output", "-f", "--filename", "-c", "--container",
    "--tail", "--since", "--sort-by", "--template", "-k", "--kustomize",
))

# 包裝指令：剝掉之後再看第一個 token 是不是 kubectl。
WRAPPERS = frozenset((
    "sudo", "command", "exec", "time", "nohup", "nice", "env", "xargs",
    "timeout",
))
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_BARE_NUMBER_RE = re.compile(r"^\d+[smhd]?$")  # timeout 30 / nice 值
_HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")

DOC_REF = ("白名單：CLAUDE.md「⛔ 鐵律」表格；敘事版 docs/guides/deployment/"
           "shared-cluster-paas-scope-guard.md")
BYPASS_HINT = "真要越界（叢集運維協調過）：指令前綴 KUBECTL_NS_GUARD=off "


def allow():
    sys.exit(0)


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _strip_heredocs(text):
    """把 heredoc 內文拿掉（保留起始行）：寫檔內容不是要執行的指令。"""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        m = _HEREDOC_START_RE.search(line)
        if m:
            delim = m.group(2)
            while i < len(lines) and lines[i].lstrip("\t").rstrip() != delim:
                i += 1
            i += 1  # delimiter 行本身也略過
    return "\n".join(out)


def _split_candidates(text):
    """依 shell 分隔符切出候選指令，**引號內不切**——這是與 denylist grep 的
    關鍵差異：`echo "a && kubectl delete"` 不會生出第二個候選。"""
    cands, buf = [], []
    i, n = 0, len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote and (quote == "'" or text[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if text[i:i + 2] in ("&&", "||"):
            cands.append("".join(buf))
            buf = []
            i += 2
            continue
        if text[i:i + 2] == "$(":
            cands.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|\n`":
            cands.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cands.append("".join(buf))
    return [c.strip() for c in cands if c.strip()]


def _tokenize(candidate):
    try:
        return shlex.split(candidate, posix=True)
    except ValueError:
        # 片段引號不成對（多半是切割副作用）：退回空白切，寧可粗也不擋錯。
        return candidate.split()


def _strip_wrappers(tokens):
    while tokens:
        head = tokens[0]
        if _ENV_ASSIGN_RE.match(head) or head in WRAPPERS \
                or _BARE_NUMBER_RE.match(head):
            tokens = tokens[1:]
            continue
        break
    return tokens


def _parse_kubectl(tokens):
    """回 (context, namespace, all_namespaces, verb)。`--` 之後不解析。"""
    context = namespace = verb = None
    all_ns = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            break
        if tok.startswith("-"):
            flag, _, inline = tok.partition("=")
            value = inline if inline else None
            if value is None and flag in VALUE_FLAGS and i + 1 < len(tokens):
                value = tokens[i + 1]
                i += 1
            if flag == "--context":
                context = value
            elif flag in ("-n", "--namespace"):
                namespace = value
            elif flag in ("-A", "--all-namespaces"):
                all_ns = True
            i += 1
            continue
        if verb is None:
            verb = tok
        i += 1
    return context, namespace, all_ns, verb


def _ns_allowed(ns):
    for entry in ALLOWED_NAMESPACES:
        if entry.endswith("*"):
            if ns.startswith(entry[:-1]):
                return True
        elif ns == entry:
            return True
    return False


def _check_kubectl(tokens, candidate):
    context, namespace, all_ns, verb = _parse_kubectl(tokens)
    if verb is None or verb in NO_CLUSTER_VERBS or verb in READ_VERBS:
        return None
    # 以下全是寫入／互動類動詞（含未知動詞，fail-closed）。
    if context is None:
        return ("kubectl 寫入類指令必須明確指定 --context（CLAUDE.md kubectl 段"
                "既有規則；current-context 可能正指著 woow-k3s）：%s" % candidate)
    if context != GUARDED_CONTEXT:
        return None  # docker-desktop 等其他 context 不在本守衛範圍
    if all_ns:
        return ("woow-k3s 是共用叢集，寫入類指令不得 --all-namespaces：%s"
                % candidate)
    if namespace is None:
        return ("woow-k3s 上的寫入類指令必須帶 -n 且在白名單內（default ns 不在"
                "白名單）：%s" % candidate)
    if not _ns_allowed(namespace):
        return ("namespace `%s` 不在 woow-k3s 白名單（%s）。該 ns 屬叢集上其他"
                "部署，絕對不碰：%s" % (namespace, "、".join(ALLOWED_NAMESPACES),
                                        candidate))
    return None


def analyze(command):
    """回傳 deny 理由清單（空＝放行）。"""
    reasons = []
    text = _strip_heredocs(command)
    for candidate in _split_candidates(text):
        tokens = _strip_wrappers(_tokenize(candidate))
        if not tokens:
            continue
        head = tokens[0]
        if head in ("bash", "sh", "zsh", "dash"):
            # bash -c "payload"：遞迴解析一層
            for j, tok in enumerate(tokens[1:], 1):
                if tok.startswith("-") and "c" in tok and j + 1 <= len(tokens) - 1:
                    reasons.extend(analyze(tokens[j + 1]))
                    break
            continue
        if head != "kubectl":
            continue
        reason = _check_kubectl(tokens, candidate)
        if reason:
            reasons.append(reason)
    return reasons


def main():
    if os.environ.get("KUBECTL_NS_GUARD", "").lower() == "off":
        allow()
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "kubectl" not in command:
        allow()  # fast path：絕大多數 Bash 呼叫在這裡就結束
    if re.search(r"(?:^|[\s;])KUBECTL_NS_GUARD=off\b", command):
        allow()
    try:
        reasons = analyze(command)
    except Exception:
        allow()  # fail-open：解析器自己壞了不擋開發
    if reasons:
        deny("kubectl-ns-guard 攔截：\n- %s\n%s\n%s"
             % ("\n- ".join(reasons), DOC_REF, BYPASS_HINT))
    allow()


if __name__ == "__main__":
    main()

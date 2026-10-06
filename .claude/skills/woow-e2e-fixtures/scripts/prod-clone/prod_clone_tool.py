#!/usr/bin/env python3
"""prod clone 中和工具的純函式部分（#1206）：不碰叢集，由 neutralize-clone.sh／seed-from-prod.sh 呼叫。

子命令：
  neutralize-job  由 platform release 的 server Deployment JSON 組一次性 Job（odoo neutralize --stdout）
  extract         從 Job log 取出官方中和 SQL、core_date、沙盒密碼 hash
  np-check        lockdown NetworkPolicy 內容檢查（stdin＝kubectl get networkpolicy -o json）
  assert-sql      由斷言 fixture 產生 psql script（每條輸出一行 CHK|<id>|<value>）
  assert-eval     比對 psql 輸出與 fixture，印斷言表；有 FAIL 時 exit 1

只用 Python 標準庫（tomllib 需 3.11+）。測試：test_prod_clone_tool.py。
"""
import argparse
import json
import re
import sys

MARKER_BEGIN = "===BEGIN-NEUTRALIZE-SQL==="
MARKER_END = "===END-NEUTRALIZE-SQL==="
PLATFORM_SQL_MARKER = "-- woow_paas_platform neutralize (#1201)"
CHECK_ID_RE = re.compile(r"^[A-Z][0-9]{2}_[a-z0-9_]+$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COMPARE_RE = re.compile(r"^(>=|<=|!=|>|<)\s*(-?[0-9]+)$")

# Job 內的 shell：neutralize --stdout 的 SQL 與 log 分流，再用 marker 包起來印到 stdout；
# 另印 core_date（同 woow-odoo chart lib.sh core_date）與沙盒密碼 hash（只在 Secret 還沒有 hash 時算）。
JOB_SCRIPT = r"""set -euo pipefail
# K3s（kube-router）對新 pod 套 NP 有 1～2 秒延遲：這段期間連同 ns postgres 也會被拒（Connection refused），先等 DB 可連
host=$(sed -n 's/^db_host *= *//p' /etc/odoo/odoo.conf | head -1); port=$(sed -n 's/^db_port *= *//p' /etc/odoo/odoo.conf | head -1)
for i in $(seq 1 60); do timeout 2 bash -c "</dev/tcp/${host}/${port:-5432}" 2>/dev/null && break; sleep 1; done
odoo neutralize -c /etc/odoo/odoo.conf -d "$PROD_CLONE_DB" --stdout >/tmp/neutralize.sql 2>/tmp/neutralize.err || {
  echo "odoo neutralize failed:" >&2; tail -n 50 /tmp/neutralize.err >&2; exit 1; }
echo "%(begin)s"
cat /tmp/neutralize.sql
echo "%(end)s"
raw=$(dpkg-query -W -f='${Version}' odoo 2>/dev/null || true)
v=${raw#*:}; v=${v%%%%[-+~]*}; v=$(printf '%%s' "$v" | cut -d. -f1-3)
echo "CORE_DATE=${v}"
if [ -n "${PROD_CLONE_PASSWORD:-}" ] && [ -z "${PROD_CLONE_PASSWORD_HASH:-}" ]; then
  python3 - <<'PY'
import os
from passlib.context import CryptContext
ctx = CryptContext(schemes=["pbkdf2_sha512"], pbkdf2_sha512__rounds=600000)
print("PWHASH=" + ctx.hash(os.environ["PROD_CLONE_PASSWORD"]))
PY
fi
""" % {"begin": MARKER_BEGIN, "end": MARKER_END}


def build_neutralize_job(deploy, name, db, component, login_secret):
    """以 server Deployment 的 pod template 為底：同 image、同 conf Secret、同 pull secret 與 securityContext。

    - 只留 odoo 容器（去掉 bootstrap／upgrade-gate initContainer 與 nginx sidecar）：neutralize --stdout 不載 registry。
    - data PVC 換成 emptyDir：RWO 的 data PVC 可能正被 web pod 掛著（Stage B），而這個 Job 用不到 filestore。
    - label 保留 app.kubernetes.io/instance（pg NP 只放行帶這個 label 的 pod）、component 改成自己的值，
      避免被 Service（component=server）選進去。
    """
    tpl = deploy["spec"]["template"]
    spec = tpl["spec"]
    labels = dict(tpl["metadata"].get("labels", {}))
    if "app.kubernetes.io/instance" not in labels:
        raise ValueError("Deployment pod template 沒有 app.kubernetes.io/instance label")
    labels["app.kubernetes.io/component"] = component
    odoo = next((c for c in spec["containers"] if c["name"] == "odoo"), None)
    if odoo is None:
        raise ValueError("Deployment 沒有名為 odoo 的容器")
    mounts = []
    for m in odoo.get("volumeMounts", []):
        mounts.append({k: v for k, v in m.items() if k in ("name", "mountPath", "subPath", "readOnly")})
    used = {m["name"] for m in mounts}
    volumes = []
    for v in spec.get("volumes", []):
        if v["name"] not in used:
            continue
        if "persistentVolumeClaim" in v:
            volumes.append({"name": v["name"], "emptyDir": {}})
        else:
            volumes.append(v)
    env = [{"name": "PROD_CLONE_DB", "value": db}]
    env += [e for e in odoo.get("env", []) if e.get("name") == "ODOO_RC"]
    if login_secret:
        env.append({"name": "PROD_CLONE_PASSWORD", "valueFrom": {"secretKeyRef": {
            "name": login_secret, "key": "password"}}})
        env.append({"name": "PROD_CLONE_PASSWORD_HASH", "valueFrom": {"secretKeyRef": {
            "name": login_secret, "key": "password-hash", "optional": True}}})
    container = {
        "name": "neutralize",
        "image": odoo["image"],
        "imagePullPolicy": odoo.get("imagePullPolicy", "IfNotPresent"),
        "command": ["/bin/bash", "-c", JOB_SCRIPT],
        "env": env,
        "volumeMounts": mounts,
        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                      "limits": {"cpu": "1", "memory": "1Gi"}},
    }
    if "securityContext" in odoo:
        container["securityContext"] = odoo["securityContext"]
    pod_spec = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "containers": [container],
        "volumes": volumes,
    }
    for key in ("securityContext", "imagePullSecrets", "nodeSelector", "tolerations"):
        if spec.get(key):
            pod_spec[key] = spec[key]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": deploy["metadata"]["namespace"], "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 600,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


def extract(log_text):
    """回傳 (sql, core_date, pwhash)。SQL 去掉官方的 BEGIN;／COMMIT; 包裝（由呼叫端自己包單一交易）。"""
    if MARKER_BEGIN not in log_text or MARKER_END not in log_text:
        raise ValueError("Job log 找不到中和 SQL 的 marker（輸出被截斷或 Job 失敗）")
    body = log_text.split(MARKER_BEGIN, 1)[1].split(MARKER_END, 1)[0]
    lines = body.strip("\n").splitlines()
    if not lines or lines[0].strip() != "BEGIN;" or lines[-1].strip() != "COMMIT;":
        raise ValueError("中和 SQL 不是 BEGIN;…COMMIT; 形狀（Odoo 版本行為改了？）")
    sql = "\n".join(lines[1:-1]).strip() + "\n"
    core = re.search(r"^CORE_DATE=(.*)$", log_text, re.M)
    pwhash = re.search(r"^PWHASH=(\S+)$", log_text, re.M)
    return sql, (core.group(1).strip() if core else ""), (pwhash.group(1) if pwhash else "")


def np_check(items):
    """lockdown NP 內容檢查；fail-closed：看不懂的 selector 一律不通過。回傳 (ok, info_lines, fail_lines)。"""
    errs, infos = [], []
    has_default_deny = False
    for np in items:
        name = np["metadata"]["name"]
        spec = np.get("spec", {})
        if "paas-managed" in json.dumps(spec):
            errs.append(f"{name}: 含 paas-managed selector")
        if spec.get("podSelector") in ({}, None) and "Egress" in (spec.get("policyTypes") or []) \
                and not spec.get("egress"):
            has_default_deny = True
        for direction, key in (("ingress", "from"), ("egress", "to")):
            for rule in spec.get(direction) or []:
                ports = sorted((p.get("protocol", "TCP"), p.get("port")) for p in rule.get("ports") or [])
                peers = rule.get(key)
                if not peers:
                    errs.append(f"{name} {direction}: 規則沒有 peer（＝全部來源／目的）")
                    continue
                for peer in peers:
                    if "ipBlock" in peer:
                        errs.append(f"{name} {direction}: ipBlock {peer['ipBlock'].get('cidr')}")
                        continue
                    if "namespaceSelector" in peer:
                        sel = peer["namespaceSelector"] or {}
                        labels = sel.get("matchLabels") or {}
                        target = labels.get("kubernetes.io/metadata.name")
                        if sel.get("matchExpressions") or len(labels) != 1 or target is None:
                            errs.append(f"{name} {direction}: namespaceSelector 無法判定或不是單一 ns：{sel}")
                            continue
                        if target.startswith("paas-"):
                            errs.append(f"{name} {direction}: 指向 {target}")
                            continue
                        if direction == "egress":
                            if target != "kube-system" or ports != [("TCP", 53), ("UDP", 53)]:
                                errs.append(f"{name} egress: 多出 ns {target} ports={ports}")
                        else:
                            infos.append(f"{name} ingress: 允許來自 ns {target}（ingress-only）")
                        continue
                    if set(peer) == {"podSelector"}:
                        continue  # 同 ns
                    errs.append(f"{name} {direction}: 不認得的 peer：{peer}")
    if not has_default_deny:
        errs.append("沒有 egress default-deny（podSelector {} 且 egress 為空）")
    return not errs, infos, errs


def load_fixture(path):
    import tomllib
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    checks = data.get("check") or []
    seen = set()
    for c in checks:
        cid = c.get("check_id", "")
        if not CHECK_ID_RE.match(cid):
            raise ValueError(f"check_id 格式錯：{cid!r}")
        if cid in seen:
            raise ValueError(f"check_id 重複：{cid}")
        seen.add(cid)
        for field in ("table", "query", "expected"):
            if field not in c:
                raise ValueError(f"{cid} 缺欄位 {field}")
        kind = c.get("kind", "sql")
        if kind not in ("sql", "builtin"):
            raise ValueError(f"{cid} kind 不合法：{kind}")
        if kind == "sql":
            if not IDENT_RE.match(c["table"]):
                raise ValueError(f"{cid} table 不合法：{c['table']!r}")
            for col in c.get("columns", []):
                if not IDENT_RE.match(col):
                    raise ValueError(f"{cid} column 不合法：{col!r}")
            if ";" in c["query"].strip().rstrip(";"):
                raise ValueError(f"{cid} query 只能是單一 SELECT")
        if c.get("when_absent", "fail") not in ("fail", "pass"):
            raise ValueError(f"{cid} when_absent 不合法")
        exp = c["expected"]
        if exp != "*" and exp[:1] in "<>!" and not COMPARE_RE.match(exp):
            raise ValueError(f"{cid} expected 比較式不合法：{exp!r}")
    return checks


def _sql_literal(text):
    return "'" + text.replace("'", "''") + "'"


def assert_sql(checks):
    """每條 SQL 斷言先查表與欄位存在（\\gset＋\\if），存在才跑 query；輸出 CHK|<id>|<value>。"""
    out = ["\\set ON_ERROR_STOP 0", "\\pset tuples_only on", "\\pset format unaligned", "\\pset footer off"]
    for c in checks:
        if c.get("kind", "sql") != "sql":
            continue
        cid, table = c["check_id"], c["table"]
        conds = [f"to_regclass({_sql_literal('public.' + table)}) IS NOT NULL"]
        for col in c.get("columns", []):
            conds.append(
                "EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                f"AND table_name = {_sql_literal(table)} AND column_name = {_sql_literal(col)})")
        query = c["query"].strip().rstrip(";")
        out.append(f"SELECT ({' AND '.join(conds)}) AS chk_present \\gset")
        out.append("\\if :chk_present")
        out.append(f"SELECT 'CHK|{cid}|' || coalesce(({query})::text, '<NULL>');")
        out.append("\\else")
        out.append(f"SELECT 'CHK|{cid}|__ABSENT__';")
        out.append("\\endif")
    return "\n".join(out) + "\n"


def _judge(expected, actual):
    if expected == "*":
        return "INFO"
    m = COMPARE_RE.match(expected)
    if m:
        try:
            a, b = int(actual), int(m.group(2))
        except ValueError:
            return "FAIL"
        ok = {">=": a >= b, "<=": a <= b, "!=": a != b, ">": a > b, "<": a < b}[m.group(1)]
        return "PASS" if ok else "FAIL"
    return "PASS" if actual == expected else "FAIL"


def assert_eval(checks, psql_output, builtins):
    """回傳 (rows, failed)。rows＝[(check_id, status, actual, expected)]。缺輸出＝SQL 錯誤＝FAIL（fail-closed）。"""
    got = {}
    for line in psql_output.splitlines():
        if line.startswith("CHK|"):
            _, cid, value = line.split("|", 2)
            got[cid] = value
    rows, failed = [], 0
    for c in checks:
        cid, exp = c["check_id"], c["expected"]
        if c.get("kind", "sql") == "builtin":
            actual = builtins.get(c["query"])
            if actual is None:
                rows.append((cid, "SKIP", "<not run>", exp))
                continue
        else:
            actual = got.get(cid)
            if actual is None:
                rows.append((cid, "FAIL", "<SQL error>", exp))
                failed += 1
                continue
            if actual == "__ABSENT__":
                status = "PASS" if c.get("when_absent", "fail") == "pass" else "FAIL"
                failed += status == "FAIL"
                rows.append((cid, status, "<table/column absent>", exp))
                continue
        status = _judge(exp, actual)
        failed += status == "FAIL"
        rows.append((cid, status, actual, exp))
    return rows, failed


def _cmd_neutralize_job(a):
    deploy = json.load(sys.stdin)
    job = build_neutralize_job(deploy, a.name, a.db, a.component, a.login_secret)
    json.dump(job, sys.stdout)


def _cmd_extract(a):
    sql, core, pwhash = extract(sys.stdin.read())
    with open(a.sql_out, "w") as fh:
        fh.write(sql)
    print(f"CORE_DATE={core}")
    print(f"HAS_PLATFORM_SQL={'1' if PLATFORM_SQL_MARKER in sql else '0'}")
    if a.pwhash_out and pwhash:
        with open(a.pwhash_out, "w") as fh:
            fh.write(pwhash)


def _cmd_np_check(_a):
    ok, infos, errs = np_check(json.load(sys.stdin)["items"])
    for line in infos:
        print(f"INFO {line}")
    for line in errs:
        print(f"FAIL {line}")
    print("NP-LOCKDOWN-CHECK " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _cmd_assert_sql(a):
    sys.stdout.write(assert_sql(load_fixture(a.fixture)))


def _cmd_assert_eval(a):
    checks = load_fixture(a.fixture)
    with open(a.results) as fh:
        output = fh.read()
    builtins = {}
    for item in a.builtin or []:
        key, _, value = item.partition("=")
        builtins[key] = value
    rows, failed = assert_eval(checks, output, builtins)
    width = max(len(r[0]) for r in rows) if rows else 10
    print(f"{'check_id'.ljust(width)}  status  actual ⇢ expected")
    for cid, status, actual, exp in rows:
        print(f"{cid.ljust(width)}  {status:<6}  {actual} ⇢ {exp}")
    counts = {s: sum(1 for r in rows if r[1] == s) for s in ("PASS", "FAIL", "INFO", "SKIP")}
    print(f"ASSERTIONS pass={counts['PASS']} fail={counts['FAIL']} info={counts['INFO']} skip={counts['SKIP']}")
    if a.require_builtins and counts["SKIP"]:
        print("ASSERTIONS 有 builtin 未執行（--require-builtins）⇒ 視同失敗")
        return 1
    return 1 if failed else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("neutralize-job")
    s.add_argument("--name", required=True)
    s.add_argument("--db", required=True)
    s.add_argument("--component", default="prod-clone-neutralize")
    s.add_argument("--login-secret", default="")
    s.set_defaults(func=_cmd_neutralize_job)
    s = sub.add_parser("extract")
    s.add_argument("--sql-out", required=True)
    s.add_argument("--pwhash-out", default="")
    s.set_defaults(func=_cmd_extract)
    s = sub.add_parser("np-check")
    s.set_defaults(func=_cmd_np_check)
    s = sub.add_parser("assert-sql")
    s.add_argument("--fixture", required=True)
    s.set_defaults(func=_cmd_assert_sql)
    s = sub.add_parser("assert-eval")
    s.add_argument("--fixture", required=True)
    s.add_argument("--results", required=True)
    s.add_argument("--builtin", action="append", help="name=value（builtin 檢查結果）")
    s.add_argument("--require-builtins", action="store_true")
    s.set_defaults(func=_cmd_assert_eval)
    a = p.parse_args(argv)
    try:
        return a.func(a) or 0
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

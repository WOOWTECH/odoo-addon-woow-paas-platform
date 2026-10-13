#!/usr/bin/env bash
# shared-mail.sh — 讓目標服務的 Odoo pod 連得到叢集共用的 mail 服務（mailcow）（standalone-odoo-migrate skill，#1604）
#
# 執行者＝本 repo agent（目標 ns 必須在白名單；prod 每步人工放行）。
#
# 用法：
#   shared-mail.sh open|check|close --context <ctx> --ns <target-ns> --release <R>
#                  [--mail-ns mailcow] [--mail-host mailcow.mailcow.svc.cluster.local] [--ports 587,993]
#                  [--label odoo-mail-platform/tenant=true]
#
# 背景：mailcow 是 woow-k3s 上的共用服務（不在 PaaS 內，歸 woow-machines-operator 管；WMO#37），只有叢集內位址。
#   收信經 mail-bridge 拉取後投進 mailcow；mailcow 用私有 CA（mail_fetchmail_durable 內附 mail-ca.crt）。
#   來源站的寄收信主機若指向它，搬進平台租戶 ns 之後兩端都被擋：
#   - mailcow 那一側只放行帶准入 label 的 namespace；
#   - 租戶 ns 的 baseline egress 只放行同 ns、DNS 與對外網路。
# open：目標 ns 加准入 label；加一條 egress NetworkPolicy（只選 release 的 Odoo pod → mail ns 的指定埠）；最後跑 check。
#   ⚠ label 是整個 ns 的：同一個工作區的其他服務在 mailcow 那一側也會被放行（本腳本的 egress 規則只開給這台 Odoo）。
#   每一站要接上之前先取得使用者同意（harumi：2026-10-12 已同意）。平台 operator 不會清掉額外的 NP 與 ns label。
# check：從 Odoo pod 對每個埠做一次 TCP 連線（只握手，不送資料）。沒有 Running 的 Odoo pod 時 exit 2。
# close：刪掉 NP；label 只有在是本腳本加的（ns 上有本腳本的 annotation）、且沒有別台服務還在用時才移除。
#   移除後約 20 秒內連線才真的被擋（NetworkPolicy 生效有延遲，stg 實測）⇒ 要確認就隔一會兒再跑 check。
# exit：0 成功（check：全部連得到）、1 check 有埠連不到、2 參數或前置條件不符
set -euo pipefail

MARK=standalone-odoo-migrate/shared-mail
usage() { sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ERROR: $2" >&2; exit "$1"; }
case ${1:-} in -h|--help) usage; exit 0 ;; esac
CMD=${1:-}; shift || true
CTX="" NS="" RELEASE="" MAIL_NS=mailcow MAIL_HOST="" PORTS=587,993 LABEL=odoo-mail-platform/tenant=true
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --release) RELEASE=$2; shift 2 ;;
    --mail-ns) MAIL_NS=$2; shift 2 ;;
    --mail-host) MAIL_HOST=$2; shift 2 ;;
    --ports) PORTS=$2; shift 2 ;;
    --label) LABEL=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ $CMD =~ ^(open|check|close)$ && -n $CTX && -n $NS && -n $RELEASE ]] \
  || { usage >&2; die 2 "subcommand, --context, --ns and --release are required"; }
[[ $PORTS =~ ^[0-9]+(,[0-9]+)*$ ]] || die 2 "--ports must be a comma-separated list of port numbers"
[[ $LABEL == *=* ]] || die 2 "--label must be key=value"
MAIL_HOST=${MAIL_HOST:-$MAIL_NS.$MAIL_NS.svc.cluster.local}
NP=standalone-odoo-mail-$RELEASE
K=(kubectl --context "$CTX" -n "$NS")
"${K[@]}" get deploy "$RELEASE" >/dev/null 2>&1 || die 2 "deployment $RELEASE not found in $NS"

check() {
  local pod
  pod=$("${K[@]}" get pod -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=server" \
    --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
  [[ -n $pod ]] || die 2 "no running Odoo pod for $RELEASE (start the service first, then rerun check)"
  # NetworkPolicy 對 pod 生效有約 1 秒的延遲 ⇒ 每個埠最多試幾次
  "${K[@]}" exec "$pod" -c odoo -- python3 -c '
import socket, sys, time
host, ports, bad = sys.argv[1], sys.argv[2].split(","), 0
for port in ports:
    err = None
    for _ in range(5):
        try:
            socket.create_connection((host, int(port)), timeout=5).close()
            err = None
            break
        except OSError as exc:
            err = type(exc).__name__
            time.sleep(1)
    print("%s:%s %s" % (host, port, "reachable" if err is None else "NOT reachable (%s)" % err))
    bad += err is not None
sys.exit(1 if bad else 0)' "$MAIL_HOST" "$PORTS"
}

case $CMD in
  open)
    kubectl --context "$CTX" get ns "$MAIL_NS" >/dev/null 2>&1 || die 2 "namespace $MAIL_NS not found"
    if [[ $(kubectl --context "$CTX" get ns "$NS" -o json | jq -r --arg k "${LABEL%%=*}" '.metadata.labels[$k] // ""') == "${LABEL#*=}" ]]; then
      echo "namespace $NS already has label $LABEL" >&2
    else
      "${K[@]}" label namespace "$NS" "$LABEL" >/dev/null
      "${K[@]}" annotate namespace "$NS" "$MARK=$LABEL" --overwrite >/dev/null
      echo "namespace $NS labelled $LABEL" >&2
    fi
    jq -n --arg np "$NP" --arg rel "$RELEASE" --arg mail "$MAIL_NS" --arg ports "$PORTS" '{
      apiVersion: "networking.k8s.io/v1", kind: "NetworkPolicy",
      metadata: {name: $np, labels: {"app.kubernetes.io/part-of": "standalone-odoo-migrate",
                                      "app.kubernetes.io/component": "shared-mail"}},
      spec: {
        podSelector: {matchLabels: {"app.kubernetes.io/instance": $rel, "app.kubernetes.io/component": "server"}},
        policyTypes: ["Egress"],
        egress: [{to: [{namespaceSelector: {matchLabels: {"kubernetes.io/metadata.name": $mail}}}],
                  ports: [$ports | split(",")[] | {protocol: "TCP", port: tonumber}]}]
      }}' | "${K[@]}" apply -f - >/dev/null
    echo "networkpolicy $NP applied (Odoo pod of $RELEASE → $MAIL_NS: $PORTS)" >&2
    check
    ;;
  check)
    check
    ;;
  close)
    "${K[@]}" delete networkpolicy "$NP" --ignore-not-found >/dev/null
    echo "networkpolicy $NP removed" >&2
    others=$("${K[@]}" get networkpolicy -l app.kubernetes.io/component=shared-mail -o name | wc -l)
    marked=$(kubectl --context "$CTX" get ns "$NS" -o json | jq -r --arg k "$MARK" '.metadata.annotations[$k] // ""')
    if [[ -n $marked && $others == 0 ]]; then
      "${K[@]}" label namespace "$NS" "${marked%%=*}-" >/dev/null
      "${K[@]}" annotate namespace "$NS" "$MARK-" >/dev/null
      echo "namespace $NS: label ${marked%%=*} removed" >&2
    else
      echo "namespace $NS: label kept (not added by this script, or another service still uses it)" >&2
    fi
    ;;
esac

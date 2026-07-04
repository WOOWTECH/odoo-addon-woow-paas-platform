# Pitfall: woow-k3s NetworkPolicy 有「新建 pod 啟動空窗」——用臨時 pod 測 NP 會系統性誤判「控制器全毀」

> 來源：Eugene 租戶隔離疑慮（PR #287，n8n 跨 ns 是否互通）→ repo-agent-team-leader 組 4-lens workflow + 親手多輪 kube-router 控制實驗（2026-07-04，`--context woow-k3s`，全在臨時 ns / dev-sandbox，已清）。
>
> **核心教訓（修正版）**：**kube-router netpol 在 woow-k3s 運作正常**——`default-deny`、`ipBlock`、`namespaceSelector`、`podSelector`、ingress、egress **全部對 established pod 正確生效**。但 kube-router 對**新建 pod** 的 per-pod firewall chain / peer-ipset 編程是**非同步、有啟動延遲**的：pod 起頭數秒落在**空窗**——它的 egress 尚未受限、它的 IP 尚未被加進他人的 allow-ipset。**用「臨時 pod 一起來就發連線」測 NP，會系統性落在此空窗 → 讀到假的『selector allow 不生效 / egress 不 enforce』。**
>
> **誠實過程留痕**：本調查結論**三度修正**——① workflow 誤用 production 流量當「namespaceSelector 生效」證據（實為 `from: podSelector:{}` 首規則 confound）；② 作者用臨時 probe pod 實測，得「selector allow + egress 全毀」結論（**這是 fresh-pod race artifact，不是控制器壞**）；③ 由 node 層 ipset forensic（ipsets 其實 populated）裂解、再以 established-pod 對照組鎖死。**教訓：測 NP 一律用 established pod（`kubectl exec` 進已存在的 pod），別用「起 pod 即 curl」的臨時 pod。**
>
> **範圍（Eugene 2026-07-04 拍板）**：本調查連帶發現的 from-less 跨租戶洞**只在 stg**（dev-sandbox / test 租戶）；**prod 尚空、未建任何租戶服務 → 非 prod 活躍事件**。#280 是上線前 W1 強化，按 W1 節奏。
>
> Cross-link：**#280**（移除 CS from-less + operator baseline default-deny）、**#279**（agent→CS-MCP 授權，namespaceSelector 設計**可行**）、**#284**（operator RBAC 不 enforce ns prefix，F-ENV-8）。

## TL;DR — 更正後能力矩陣

| NetworkPolicy primitive | established pod | 新 pod 啟動空窗（數秒） |
| --- | --- | --- |
| ingress `default-deny` | ✅ ENFORCE | ✅（target 長駐、不受 src 空窗影響） |
| ingress `ipBlock` allow | ✅ ENFORCE | ✅（靜態 CIDR，不吃 peer-ipset 編程） |
| ingress `namespaceSelector` allow | ✅ **ENFORCE** | ❌ race（新 src IP 尚未進 KUBE-SRC ipset → 被暫時擋） |
| ingress `podSelector` allow | ✅ **ENFORCE**（正確 scoped：cross-ns 仍擋） | ❌ race |
| egress `default-deny` | ✅ **ENFORCE**（連線層） | ❌ race（新 src 尚未被 firewall → egress 暫時放行） |

**結論**：kube-router 沒壞；標準 NetworkPolicy 語意成立。唯一 caveat = **新建 pod 的編程啟動空窗**。

> **啟動空窗實測**（單次、idle 叢集、同一 fresh pod 每秒探測 → temporal 翻轉）：**ingress-allow ≈ 1s**（fresh src IP 進 peer-ipset：`t=0 擋 → t=1s 通`）、**egress-deny ≈ 2s**（fresh src 裝上 per-pod FW chain：`t=0~1s 可出 → t=2s 起擋`）。皆**個位數秒**、非 30s 級。含意：新 pod 短暫（a）連 same-workspace 服務可能被擋 ~1s（client retry 即解）、（b）egress 不受限 ~2s（**窄 SSRF 窗口**）。註：單次量測、隨節點負載/pod 數可能浮動，取數量級即可。

叢集：K3s `v1.34.5+k3s1`、4 節點 Ubuntu 24.04、flannel + **K3s 內建 kube-router-based netpol 控制器**（無 Calico/Cilium/Canal、無 Kyverno/Gatekeeper/PSA-restricted）。pod CIDR `10.42.0.0/16`、service CIDR `10.43.0.0/16`、node 私網 `192.168.10.0/24`。

---

## 真正的 pitfall：fresh-pod 測試 artifact（決定性對照 repro）

同一組 policy、同一叢集，**唯一變數是 source pod 是 established（已存在，`kubectl exec` 進去發連線）還是 fresh（臨時 pod，起來即 curl）**：

### A. ingress `namespaceSelector` allow（target 套 default-deny + allow from labeled ns）

```
ESTABLISHED src (exec，×3)  → HTTP:200 / 200 / 200      ✅ allow 生效
FRESH 臨時 src              → HTTP:000 exit:7 (BLOCKED)  ❌ 假訊號（新 src 尚未進 KUBE-SRC ipset）
```

### B. egress `default-deny`（src 套 egress default-deny，打 target）

```
ESTABLISHED src (exec, by IP) → HTTP:000 exit:7 (BLOCKED)  ✅ egress 確實 enforce（連 DNS 都解不到 → 連線層也擋）
FRESH 臨時 src                → HTTP:200 exit:0            ❌ 假訊號（新 src 尚未被 firewall）
```

### C. ingress `podSelector` allow（established，驗 scope 正確）

```
same-ns established src  → HTTP:200            ✅ podSelector 生效
cross-ns established src → HTTP:000 (BLOCKED)  ✅ 正確 scoped（podSelector 不含他 ns）
```

> 為何先前「看似全毀」而 default-deny / ipBlock「看似 work」：作者早期 harness 每輪都用臨時 probe pod（`kubectl run ... -- curl`，pod 一 Ready 就發連線）。default-deny 的判定者是**長駐的 target**（已編程）→ 正常擋；ipBlock 是**靜態 CIDR**、不需把 pod IP 解析進 ipset → 立即生效；唯獨 selector-allow（需新 src IP 進 ipset）與 egress-deny（需新 src 被 firewall）依賴「對**新** pod 的編程」→ 撞上啟動空窗 → 系統性假訊號。

---

## Why：node 層機制（證明控制器正常、斷點不在控制器）

在 target 所在節點（`kubectl debug node --profile=sysadmin` + busybox/alpine，read-only）取證：

1. **iptables 規則生成完全正確**：target 的 `KUBE-POD-FW-*` chain 有跳到該 policy 的 `KUBE-NWPLCY-*` chain，內含
   ```
   -m set --match-set KUBE-SRC-<h> src -m set --match-set KUBE-DST-<h> dst --dport 80 -j MARK --set-xmark 0x10000/0x10000
   ```
   （SRC ipset = namespaceSelector 解析出的來源 pod、DST ipset = 目標 pod；雙比中就 MARK=allow，chain 末端 `! --mark 0x10000 -j REJECT`）。
2. **ipsets 確實 populated**（alpine + `apk add ipset` 直讀 host netns）：established src 的 IP **在** `KUBE-SRC-<h>` set、target IP **在** `KUBE-DST-<h>` set。→ 規則會 match → established 放行（與 repro A established=200 一致）。
3. 控制器有 411+ 條 `KUBE-ROUTER` iptables 規則、244 條 `match-set`、`ip_set`/`xt_set` 模組載入、mark-based 方案（`KUBE-NWPLCY-DEFAULT` 無條件 MARK=allow 給無策略 pod）。→ **排除「規則消失」(k3d #1513) 與「ipset 機制壞」兩種假說**。

**斷點結論**：不在規則生成、不在 ipset 機制——在 kube-router 對**新建 pod** 把其 IP 灌進 peer-ipset / 裝上 per-pod FW chain 的**時間差**。established pod（IP 早已入 set / 早被 firewall）一切正常。

> **取證誠實修正**：第一輪 `ipset list -n | wc -l` 回 `0`，一度誤判「ipset 全空 = 根因」；查驗才知是 busybox chroot **無 `ipset` CLI binary**（exit 127）造成的假訊號（host 未裝 ipset 套件，kube-router 走 netlink）。改用 `alpine + apk add ipset` 才讀到真實成員。**教訓：node 取證缺 binary + `2>/dev/null` 會偽造「空集合」，先驗 binary 再解讀。**

---

## 仍然真實：from-less 跨租戶洞（#280，stg-only）

這條**與啟動空窗無關**、是設計層 fail-open（from-less allow = allow-all，不依賴 ipset，故臨時 pod 也能穩定觸發）：

```bash
# 從無關 default ns 臨時 pod（stg）
curl http://svc-02c671c7-mcp-server.paas-ws-stg-86f34bd4.svc.cluster.local:8080/   # → HTTP 200
```

- CS 每 service 的 allow NP 是 `from-less`（mcp-server :8080）/ `- {}`（Bitnami odoo :8069、postgres :5432）→ **對所有來源開放**，K8s allow-union 蓋掉同 pod 的 default-deny。
- 主 app（n8n）pod **零 NP** + 該 ns **無 ns 級 default-deny** → 全開。
- **範圍：stg（dev-sandbox / test 租戶）；prod 尚空 → 上線前 W1 強化非活躍事件。**

---

## 對 #279 / #280 的（修正後）設計含意

控制器正常 → **回歸標準 NetworkPolicy 做法**，先前「只能 ipBlock / 修換 CNI」的結論**作廢**：

- **#280（移除 from-less + operator baseline）**：
  - operator 建 tenant ns 即 stamp ingress `default-deny` baseline（chart-independent，兜住 n8n 這類無 NP 的 pod）；`helm.py:737 create_namespace` + `agent_runtimes.py:81 _ensure_namespace` 兩處。
  - CS 的 allow NP 補上 `from` 來源限制（用 **namespaceSelector**，對 established 生效）：mcp-server 只放行同 workspace（見 #279）、Bitnami odoo/postgres 用 `allowExternal:false`（走原生 podSelector，**對 established 生效**）。
- **#279（agent→CS-MCP 授權）→ namespaceSelector 設計可行**：agent-runtime chart 的 `egress + namespaceSelector{woow.tw/workspace-id}` 只要 operator 在 CS ns 與 agent ns stamp 一致的 `woow.tw/workspace-id` label（現行缺，見 charts recon）即可生效。**不需**改成 ipBlock。
- **egress SSRF 收斂 → 可行**：egress default-deny + `ipBlock 0.0.0.0/0` except 內網段，對 established pod 生效。
- **不需** operator-reconciled ipBlock /32 全包、**不需**修/換 CNI（Calico/Cilium）、**控制器路線決策取消**。
- **⚠️ 一個真實但小的安全 nuance（啟動空窗）**：新 pod 起頭數秒 egress-deny 尚未生效 → 惡意 pod 理論上可在啟動瞬間對外/對內網 egress（SSRF 窗口），且新 pod 對外的**主動連線**不受 egress-deny 約束直到被編程。緩解：關鍵隔離別只靠「pod 剛啟動就得擋」的假設；必要時加 init 延遲 / app-layer;此為 hardening note、非 blocker。#279 的 same-workspace ingress 也有對稱空窗（新 agent pod 起頭連 CS 可能被暫擋 → client retry 即可）。

---

## agent→CS-MCP 是「雙腿單路徑」——兩腿都放行才通（#279 + #280 lock-step）

agent→CS-MCP 是單一路徑、兩端各自 default-deny，**兩腿都放行才通**（與 woow-paas-charts 2026-07-04 對齊）：

| 腿 | 方向 | chart 機制 | 需要什麼 |
| --- | --- | --- | --- |
| 腿1 = agent egress | agent pod → CS ns | agent-runtime `egressWorkspaceIds` → `egress namespaceSelector{woow.tw/workspace-id:<id>}` | (a) operator 送**非空** `egressWorkspaceIds` **且**(b) **CS ns 帶 `woow.tw/workspace-id:<id>` label**（#279 缺的）|
| 腿2 = CS ingress | agent ns → mcp-server pod | mcp-server `ingressNamespaces` → `ingress namespaceSelector{kubernetes.io/metadata.name:<agent-ns>}` | operator 把**agent ns 名**填進 mcp-server values（#280 收緊那步）|

**兩腿 selector key 不對稱**：腿1 用 workspace-id **label**（語意、一 label 對應 workspace 內多 ns）、腿2 用 ns **name**（具體 agent ns 名）→ operator 需**枚舉 + reconcile** 該 workspace 的 agent ns 名填進 mcp-server ingressNamespaces。

**推論（與 corrected model 一致）**：
- agent→CS 現「network-dead」的真因是**腿1 未 wire**，**不是 selector 壞**。charts 已在 template 層坐實：agent-runtime `network-policy.yaml` 那條 egress-to-CS 規則整段包在 `{{- if .Values.networkPolicy.egressWorkspaceIds }}` 內 → **空 list = 整段規則不 render**（不是 render 了但 match 0 ns）。先前 blast-radius 觀察到「live agent NP 完全無指向 CS ns 的 egress 規則」正是此表徵（`egressWorkspaceIds` 當時為空）。wiring 腿1 仍須 **(a) operator 送非空 `egressWorkspaceIds` + (b) CS ns 帶 label** 兩者都到位。
- **#280 關 mcp-server from-less（腿2 收緊）不會弄斷 agent→CS**——它本就因腿1 未 wire 而不通。
- **#279（wire 腿1）+ #280（wire 腿2 到 agent ns）必須一起做**，才把這條路**安全**接通（單做 #279 會讓腿2 維持 from-less 全開）。呼應 `two-layer-acl-lockstep`。

## must-allow（部署 ingress default-deny 前必驗；用 established pod 測，別用臨時 pod）

- kubelet health/readiness probe（node 私網 `192.168.10.0/24` → pod；node forensics 見 `--src-type LOCAL -j ACCEPT` 已放行本機來源，須實測確認）。
- CF / cloudflared → 主 app 埠（來源 `cloudflare-tunnel-stg` pod）——可用 namespaceSelector（對 established 生效）。
- 同 ns app↔postgres:5432、主 app↔mcp sidecar（podSelector same-ns，對 established 生效）。
- Odoo→agent WS:8000（實測 agent WS 為 pod 主動 egress，非 Odoo→pod ingress）。
- DNS→kube-system:53（egress default-deny 下必放行，否則 svc 解析全掛）。
- agent→LLM / n8n→外部 API：egress `ipBlock 0.0.0.0/0` + except[`10.0.0.0/8`,`172.16.0.0/12`,`192.168.0.0/16`,`169.254.0.0/16`,`100.64.0.0/10`,`127.0.0.0/8`]。

## 殘留 bypass（NetworkPolicy 之外，需 admission / PSA / storage）

hostNetwork pod、NodePort/LoadBalancer、共用 RWX PV 跨 ns、default SA token 自動掛載打 API server、CoreDNS 枚舉、**pod 啟動空窗**（上述）。叢集無 Kyverno/Gatekeeper/PSA-restricted → 現無守（與 #284 F-ENV-8 合流）。

## 如何正確驗證 NP（避免 fresh-pod artifact）

```bash
# 先把 source pod 建好、等它 Ready 且穩定數十秒（讓 kube-router 完成編程），再 exec 進去測——不要用「起 pod 即 curl」
kubectl -n src-ns run probe --image=curlimages/curl --command -- sleep 3600
kubectl -n src-ns wait --for=condition=Ready pod/probe --timeout=90s
sleep 45   # 關鍵：跨過新 pod 編程空窗
kubectl -n src-ns exec probe -- curl -sS -m6 -o /dev/null -w "%{http_code}\n" http://<target>/
```

## Related

- **Issues**：#280（移除 from-less + operator baseline default-deny）、#279（agent→CS-MCP namespaceSelector 授權 + operator stamp `woow.tw/workspace-id` label）、#284（RBAC/F-ENV-8）。
- **Pattern**：`docs/decisions/patterns/two-layer-acl-lockstep.md`（#280 收緊 CS ingress 必與 #279 補 agent→CS carve-out lock-step）。
- **chart 事實（woow-paas-charts recon）**：agent-runtime `egressWorkspaceIds` → `egress + namespaceSelector{woow.tw/workspace-id}`（**設計可行**，需 operator stamp label）；mcp-server from-less :8080 是 pilot 預設（`ingressNamespaces:[]`），收緊用 namespaceSelector（對 established 生效）；odoo/postgres 走 Bitnami，`allowExternal:false`（podSelector，對 established 生效，**免 extraIngress ipBlock workaround**）。

# Pitfall: woow-k3s 上 NetworkPolicy「存在 ≠ 生效」——K3s v1.34.5 內建 kube-router netpol 只 enforce ingress default-deny + ingress ipBlock，其餘全毀

> 來源：Eugene 租戶隔離疑慮（PR #287，n8n 跨 ns 是否互通）→ repo-agent-team-leader 組 4-lens investigation workflow + 親手 5 輪 kube-router 控制實驗（2026-07-04，`--context woow-k3s`，全在臨時 ns / dev-sandbox，已清）。
>
> **核心教訓**：任何假設 K8s NetworkPolicy 標準語意的人在此叢集都會被咬爆。實測此叢集**只有 ingress `default-deny` 與 ingress `ipBlock` allow 生效**；ingress 的 `namespaceSelector` / `podSelector` allow **完全不生效**（9 分鐘 steady-state 排除 reconciliation lag、連 same-ns podSelector 都不放行）、**egress NetworkPolicy 完全不 enforce**（egress default-deny 過 2.5 分鐘仍全通）。連帶暴露 **LIVE 跨租戶網路洞**（已從無關 ns curl 租戶服務 → HTTP 200）。
>
> **信度分層（誠實標註）**：symptom（能力矩陣 + 跨租戶洞）＝leader 親手實測、**高信度**；root-cause（kube-router 細部 bug）＝已排除「#1513 規則消失」類、機制部分刻畫（mark-based + ipset 有在用），**precise defect 待 upstream triage**，別在查實前就下「降版 / 換 CNI」結論。
>
> Cross-link：**#280**（cross-tenant default-deny，P0）、**#279**（agent→CS-MCP 授權 NP dead-path）、**#284**（operator RBAC 不 enforce ns prefix，F-ENV-8）。

## TL;DR — 實測 primitive 能力矩陣

| NetworkPolicy primitive | ingress | egress |
| --- | --- | --- |
| `default-deny`（`podSelector:{}` + 對應 `policyTypes`，無 allow rule） | ✅ **ENFORCE** | ❌ **不 enforce** |
| `ipBlock` allow（靜態 CIDR） | ✅ **ENFORCE** | （moot：egress 本就沒擋） |
| `namespaceSelector` allow | ❌ 不生效（9min steady-state → 非 lag） | ❌ |
| `podSelector` allow（連 same-ns） | ❌ 不生效 | ❌ |

**唯一可靠的隔離槓桿 = ingress `default-deny` + ingress `ipBlock` allow。** 其餘全是「NP 寫了、apply 成功、`kubectl describe` 看得到規則，但實際流量不受其約束」。

叢集：K3s `v1.34.5+k3s1`、4 節點 Ubuntu 24.04、flannel + **K3s 內建 kube-router-based netpol 控制器**（無 Calico/Cilium/Canal 獨立 CNI、無 Kyverno/Gatekeeper/PSA-restricted）。pod CIDR `10.42.0.0/16`（每節點 /24）、service CIDR `10.43.0.0/16`、node 私網 `192.168.10.0/24`。

---

## Symptom — LIVE 跨租戶網路洞（自證）

租戶 CloudService 的 in-cluster 服務可被**任一 pod 跨 namespace 直連**。從一個與該租戶無關的 `default` ns 臨時 pod：

```bash
kubectl --context woow-k3s -n default run probe --image=curlimages/curl --restart=Never --command -- \
  sh -c 'curl -sS -m 8 -w "%{http_code}\n" \
    http://svc-02c671c7-mcp-server.paas-ws-stg-86f34bd4.svc.cluster.local:8080/'
# → HTTP 200
```

根因（設計層，與控制器行為**無關**）：租戶 CS ns（`paas-ws-stg-86f34bd4`）的 per-service allow NP 是 **all-open ingress**——

```bash
kubectl --context woow-k3s -n paas-ws-stg-86f34bd4 get netpol -o yaml
# svc-02c671c7-mcp-server-allow  : ingress ports:[8080] 且【完全沒有 from:】= 對所有來源開放
# svc-06b55af1-odoo              : ingress 8069 帶 `- {}`（全開）
# svc-06b55af1-postgresql        : ingress 5432 帶 `- {}`（全開）
```

K8s NetworkPolicy 多 policy 為 **allow 聯集**：一條 from-less allow 覆蓋掉同 pod 的 default-deny，等於對「所有來源」開放。此外**主 app（n8n）pod 完全沒有任何 NP** + 該 ns **無 namespace 級 default-deny** → in-cluster 全埠全開。

**衝擊**：任一租戶 workload（或取得任一租戶 pod code-exec 者，如 n8n Code 節點）可跨 ns 觸及每個其他租戶的 **MCP:8080 / Odoo web:8069 / PostgreSQL:5432**。FQDN 由 Odoo 端 `src/models/cloud_service.py:984-989` 生成、外露即可枚舉，不外露也能掃 `10.42.0.0/16`。**這是 #280 要關的核心。**

**雙向暴露**：ingress 如上；egress 因 NP 不 enforce（見下），任一租戶 pod 亦可無限制 egress 到內網（他租戶 pod IP、k8s API `10.43.0.1`、kubelet `192.168.10.x:10250`、metadata `169.254.169.254`）——且 **egress 這側現在無法用 NetworkPolicy 關**。

---

## 控制實驗 repro（每格矩陣：manifests + curl + 時間）

所有實驗：target ns 起一個 `nginx:alpine` pod + Service，src ns 起 `curlimages/curl` pod 打 target（direct pod IP 或 svc FQDN），用 curl exit code 辨識（0=通、7=擋/reject、28=drop-timeout）。臨時 ns 用後即刪。

### ① ingress `default-deny` → ✅ ENFORCE

target ns 套 `{ podSelector:{}, policyTypes:[Ingress] }`（無 allow），src 無 NP：

```
+25s  HTTP:000 exit:7   (BLOCKED)
+45s  HTTP:000 exit:7   (BLOCKED)
```

### ② ingress `ipBlock` allow → ✅ ENFORCE

target ns 套 default-deny + `ingress from ipBlock 10.42.0.0/16 :80`，src(10.42.3.120) 打：

```
HTTP:200 exit:0   (ALLOWED)
```

### ③ ingress `namespaceSelector` allow → ❌ 不生效（9min steady-state，非 lag）

target ns 套單條 NP，src ns 帶 label `np-test-allow=yes`：

```yaml
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
  - from: [ { namespaceSelector: { matchLabels: { np-test-allow: "yes" } } } ]
    ports: [ { protocol: TCP, port: 80 } ]
```

跨 ~9 分鐘、6 輪探測（遠超 kube-router ~5min full-resync 窗）：

```
+3s    [labeled src] HTTP:000 exit:7    +3s    [unlabeled default] HTTP:000 exit:7
+60s   [labeled src] HTTP:000 exit:7    ...    (default 恆 BLOCKED，符合預期)
+90s   [labeled src] HTTP:000 exit:7
+120s  [labeled src] HTTP:000 exit:7
+120s  [labeled src] HTTP:000 exit:7
+90s   [labeled src] HTTP:000 exit:7    ← labeled 來源【全程被擋】，一次都沒轉 200
```

`kubectl describe netpol` 確認規則正確 parse（`From: NamespaceSelector: np-test-allow=yes`）、`kubectl get ns --show-labels` 確認 label 存在。**排除 timing / label / parse artifact → namespaceSelector ingress allow 是真的不生效，不是 reconciliation lag。** built-in `kubernetes.io/metadata.name` 選擇器同樣不生效（另測）。

### ④ ingress `podSelector` allow（連 same-ns）→ ❌ 不生效

```yaml
  ingress:
  - from: [ { podSelector: {} } ]     # = 同 ns 內所有 pod
    ports: [ { protocol: TCP, port: 80 } ]
```

```
cross-ns from src        HTTP:000 exit:7   (BLOCKED — 排除「podSelector 誤放跨 ns」的 leak 理論)
same-ns  from target ns  HTTP:000 exit:7   (BLOCKED — 但本該 200！same-ns podSelector 都不放行)
```

> ⚠️ 這條同時**推翻**了一個誘人的誤判：曾有 production 流量（CF tunnel pod → Odoo nginx 跨 ns HTTP 200）被拿來「證明 namespaceSelector 生效」，但那條 Odoo NP 的**首條 ingress 規則是 `from: podSelector:{}`**，疑似被它放行。本實驗證明 same-ns podSelector 也不放行 → CF 能通**另有原因**（host/kube-proxy 路徑，見下 node forensics 的 `--src-type LOCAL -j ACCEPT`），**確定不是 namespaceSelector**。

### ⑤ egress `default-deny` → ❌ 不 enforce（2.5min steady-state + ingress 正對照）

src ns 套 `{ podSelector:{}, policyTypes:[Egress] }`（無 allow），target 無 NP，src 直打 target pod IP（避開 DNS/service DNAT confound）：

```
[Phase A: src EGRESS default-deny]
+30s   HTTP:200 exit:0    (應 BLOCKED，實際通)
+90s   HTTP:200 exit:0    (仍通)
+150s  HTTP:200 exit:0    (仍通 — egress default-deny 完全沒擋)

[Phase B: 同結構改成 target INGRESS default-deny（正對照）]
+25s   HTTP:000 exit:7    (BLOCKED)
+45s   HTTP:000 exit:7    (BLOCKED)
```

**同一組 pod、同一叢集：ingress default-deny 擋、egress default-deny 不擋。** 決定性證明 egress NP 不 enforce。

> 這也**解釋並推翻**了另一個誤判：dev-sandbox / agent NP 的 DNS egress 用 `namespaceSelector{kube-system}:53` 且「DNS 32 天正常」曾被當成「egress namespaceSelector 生效」的證據——真因是 **egress 根本不受限**、DNS 當然通，與那條 allow 規則無關。

---

## Why it happens — node 層 forensics（root-cause）

在節點 `ubuntuserver-31zix`（`kubectl debug node/... --profile=sysadmin` + `chroot /host`，read-only）取證：

- **kube-router 控制器有在跑**：filter 表有 `KUBE-ROUTER-FORWARD/INPUT/OUTPUT` chains，全表 411 條 `KUBE-ROUTER` 規則。→ **排除 k3d #1513 那種「重啟後規則消失」**。
- **ipset 有在用**（排除「ipset 機制壞掉」假說）：kernel 模組 `ip_set` / `xt_set` / `ip_set_hash_net`(10 refs) / `ip_set_hash_ip`(152 refs) 全載入；iptables 有 **244 條 `match-set`（ipset 引用）**規則。
- **enforcement = mark-based 方案**：每個受管 pod 有 `KUBE-POD-FW-<hash>` chain，ingress/egress 各跳到 policy chain，符合放行條件就打 `MARK 0x10000`，chain 末端 `! --mark 0x10000 -j REJECT`。`KUBE-NWPLCY-DEFAULT` chain 的內容是**無條件** `MARK --set-xmark 0x10000/0x10000`（= 放行）。
- pod-FW chain 內含 `-m addrtype --src-type LOCAL -j ACCEPT`（放行來源為 pod 所在節點的本機流量）——這解釋了為何某些「跨 ns」流量看似通（實際走了 LOCAL 節點路徑），**不可**拿來當 namespaceSelector 生效的證據。

> **誠實修正（過程記錄，供後人少走冤路）**：取證第一輪 `ipset list -n | wc -l` 回 `0`，一度誤判「ipset 全空 = 根因」。第二輪加驗發現真相是 **busybox chroot 內沒有 `ipset` CLI binary**（`ipset: not found`, exit 127），不是沒有 ipset——ipsets 實際大量在用（244 match-set + 模組載入）。**教訓：node 取證時 `2>/dev/null` + 缺 binary 會偽造「空集合」訊號，務必先驗 binary 存在再解讀計數。**

**root-cause 結論（信度分層）**：控制器在跑、ipset 在用、規則齊全（非規則消失），但 ingress selector-allow 與 egress 實際不生效——這是 **K3s v1.34.5 內建 kube-router netpol 在「selector-allow 規則生成 / egress 強制」的細部 bug**（mark 未正確打上，或 egress chain 未正確接上 REJECT）。**precise defect 未逐行 pin、快速 upstream 搜尋未撈到 v1.34 對應公開 issue** → 是「regression / config / 其他」中的哪一種，需平台團隊做 kube-router + k3s issue tracker 深度 triage + 節點 `journalctl -u k3s` 完整比對，**再**決定降穩定版 / Calico policy-only overlay / 修內建控制器。

---

## 對 W1 設計的硬約束（#280 / #279）

- **#280 短期止血（可行，只靠已證生效的 ingress default-deny）**：外科式移除 CS 三條 all-open ingress（`mcp-server-allow` / `odoo` / `postgresql` 的 from-less 規則），讓既有 per-service ingress default-deny 生效 → 立即關 ingress 跨租戶洞。**別寄望 ns-wide default-deny 或 egress default-deny**（egress 是空砲、ns-wide 還會誤傷 CF→主 app 與 DNS）。MCP:8080 只給同 workspace agent 用、現本就 network-dead，關掉不破壞現況。
- **#280 長期 baseline**：operator 建 tenant ns 即 stamp **ingress** default-deny（chart-independent，兜住 n8n 這類無 NP 的 pod）；`extra/paas-operator/src/services/helm.py:737 create_namespace` 與 `extra/paas-operator/src/api/agent_runtimes.py:81 _ensure_namespace` 兩處都要。
- **#279 same-workspace 放行**：`namespaceSelector` + `podSelector` 全死 → 唯一 NP 選項是 **operator-reconciled ingress `ipBlock /32`**（來源＝CF tunnel pod、同 workspace agent pod；pod 刪除必「先移 /32 再釋放 IP」壓 IP-reuse TOCTOU）。脆弱、維運重，只適合少量平台可控來源，**不可**當通用機制（`10.42/16` = 全叢集，用它等於沒隔離）。
- **egress / SSRF**：**NP 在此叢集救不了**（egress 不 enforce）→ SSRF 收斂只能 app-layer egress proxy 或修控制器。原設計「對外 egress 用 `ipBlock 0.0.0.0/0` + except 內網段」對策**在此叢集無效**。
- **長期正解 = 修 / 換 NP 控制器（非可選，是 launch-blocking 前置）**：查 K3s v1.34.5 kube-router netpol regression / 降穩定線 / Calico policy-only 疊 flannel（Canal，資料面不動、風險遠低於全量換 CNI，可用 GlobalNetworkPolicy 一條做叢集級 cross-tenant default-deny）。**只有恢復 label-selector + egress enforcement，#279 的 workspace-id 設計與 egress SSRF 收斂才可能。**

### chart-side 事實（woow-paas-charts recon 確認，2026-07-04）

- **版本**：mcp-server **0.1.4**、agent-runtime **0.4.3**（NP template 自 chart 初始 commit `bbbdafe` 未再改，版本差不影響結論）。
- **agent→CS-MCP 授權 NP 確認為 dead-path**：`agent-runtime/templates/network-policy.yaml` 的 `-allow` policy 把 `egressWorkspaceIds` render 成 **`egress` + `namespaceSelector{ woow.tw/workspace-id: <id> }`**（label key 預設 `woow.tw/workspace-id`，`values.yaml:318`）。在 kube-router **雙重死**：egress-type（不 enforce）＋ namespaceSelector（不生效）。該 policy 其餘規則（ingress ← Odoo ns、egress → kube-dns / Odoo ns）全 namespaceSelector → 全 no-op；只有 `-default-deny` 的 **ingress 側**生效。**=> agent→CS-MCP 授權在 woow-k3s 幾乎確定從沒生效過**，現況能通純因 mcp-server from-less 全開遮蔽（一旦 #280 收緊 CS ingress，agent→CS 立刻全斷 → 印證 #279 必與 #280 lock-step）。
- **mcp-server from-less :8080 是 chart pilot 預設**（`values.yaml:136 ingressNamespaces: []` → `from:` 省略 → all-open），非 operator 漏帶；template 註解明寫「empty (pilot default) → allow from ANY source… TODO tighten post-pilot」。
- **⚠️ chart 現有參數無法在此叢集收緊 mcp-server ingress**：設 `ingressNamespaces` 會 render 成 **namespaceSelector** `from`（不生效）→ 疊上 `-default-deny`（`ingress:[]`）反而讓 :8080 **完全不可達**（打斷 tunnel），而非選擇性放行。chart ingress **不支援 ipBlock** → 要限來源必須**新增 ipBlock 參數**（例 `ingressCIDRs`）。
- **odoo:8069 / postgresql:5432 的 all-open NP = 上游 Bitnami chart 自帶**（`networkPolicy.enabled:true` 預設 render，**非 operator 注入**；odoo 28.2.10 + postgresql subchart，見 `src/data/cloud_app_templates.xml`。cluster 上 `kubectl get netpol -o yaml` 看 `managed-by: Helm` annotation 佐證）。all-open 來源 = **`allowExternal:true`（預設）** → `from:` 省略 → port-only 全開。**收緊陷阱**：`allowExternal:false` 改走 **podSelector**（app 需帶 `<pg-fullname>-client:"true"` label）→ kube-router podSelector no-op → **8069/5432 對誰都不可達、會斷 odoo↔自己的 postgres**（Bitnami odoo→pg 授權就靠此 podSelector）。唯一可行 = **`allowExternal:false` + `extraIngress` ipBlock**（Bitnami values 逃生口注入 /32、免改 chart；來源 /32 須 operator-reconciled，同 mcp-server TOCTOU）。〔woow-paas-charts recon (a) 查證 + (c) `helm template` render proof 佐證〕
- **controller 路線的 chart 影響**：續用 kube-router → mcp-server 需新增 ipBlock 來源參數、agent 隔離只能靠 CS 側 ingress ipBlock /32；換 Calico/Cilium → 兩 chart 現有 namespaceSelector 設計「開始生效」，但前提 operator 要在 CS ns stamp `woow.tw/workspace-id` label（#279 的 label gap，operator `build_managed_ns_labels` 要補）。

### 實測驗證：mixed `podSelector`(no-op) + `ipBlock`(enforce) 同一 NP → #280 remediation 形狀成立

Bitnami `allowExternal:false + extraIngress ipBlock`（以及 mcp-server 未來的 ipBlock 參數）都會 render 成「同一 NP 內 rule1=podSelector（kube-router 忽略）+ rule2=ipBlock」。leader 實機驗證此形狀（target ns nginx，src pod /32 放進 ipBlock）：

```yaml
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
  - from: [ { podSelector: { matchLabels: { nonexistent-client: "true" } } } ]  # rule1: Bitnami 原生形狀，kube-router no-op
    ports: [ { protocol: TCP, port: 80 } ]
  - from: [ { ipBlock: { cidr: <src-pod-ip>/32 } } ]                            # rule2: extraIngress，kube-router enforce
    ports: [ { protocol: TCP, port: 80 } ]
```

```
src（在 ipBlock 內）      → HTTP:200 curl-exit:0   (ipBlock rule 生效放行)
other ns（不在 ipBlock）  → HTTP:000 curl-exit:7   (BLOCKED)
```

**證明**：coexisting 的 no-op podSelector 規則**不干擾** ipBlock 規則；ipBlock 選擇性放行、其餘 deny → `allowExternal:false + extraIngress ipBlock` 在 kube-router 上**可行**，是 odoo / postgres / mcp-server 三者在此叢集唯一的收緊路徑（皆須 operator-reconciled /32）。

### must-allow（部署 ingress default-deny 前必驗，漏了壞租戶；全部 re-allow 只能用 ipBlock）

- kubelet health/readiness probe（node 私網 `192.168.10.0/24` → pod）——node forensics 顯示已有 `--src-type LOCAL -j ACCEPT` 放行本機來源，須實測確認 probe 不被誤殺。
- CF / cloudflared → 主 app 埠（來源 `cloudflare-tunnel-stg` pod，實測 `10.42.2.222`）。
- 同 ns app↔postgres:5432、主 app↔mcp sidecar（same-ns 也被 ingress default-deny 擋、且 same-ns podSelector 放行**無效** → 只能 ipBlock 來源 pod）。
- Odoo→agent WS:8000：實測 agent WS 其實是 **pod 主動 egress**，非 Odoo→pod ingress，別依賴這條 ingress。
- DNS / 對外 LLM / n8n 外連：因 egress 不 enforce，現階段本就全通、不受 default-deny 影響。

---

## 殘留 bypass（NetworkPolicy 之外，需 admission / PSA / storage 層）

叢集無 Kyverno/Gatekeeper/PSA-restricted → 以下現在無守（與 #284 F-ENV-8「防禦縱深」合流）：

- **hostNetwork pod** 繞過所有 NP（用 node IP）。
- **NodePort / LoadBalancer Service** 經 node IP 對外/他租戶暴露（ResourceQuota 不限 Service type）。
- **共用 RWX PV 跨 ns**（Longhorn NFS，靜態 PV 可被兩 ns PVC 綁定 → 儲存層跨租戶，NP 管不到）。
- **default SA token 自動掛載**（`automountServiceAccountToken=true`）→ pod 可打 k8s API（即使 operator ClusterRole 不 enforce prefix，#284）。egress 又擋不住 → 無守。
- **CoreDNS 枚舉** `*.svc.cluster.local`（DNS 必開 → 可枚舉他租戶 service 名/ClusterIP，metadata 洩漏；資料面仍受 ingress default-deny 擋）。

---

## 如何驗證你的叢集是否也中招

任何依賴 NetworkPolicy 做隔離的變更上線前，**必跑**這個 30 秒 smoke test（別信 `kubectl describe` 看得到規則就以為生效）：

```bash
# 1) 建兩個臨時 ns + target
kubectl create ns np-smoke-t; kubectl create ns np-smoke-s
kubectl -n np-smoke-t run t --image=nginx:alpine --port=80
kubectl -n np-smoke-t wait --for=condition=Ready pod/t --timeout=90s
kubectl -n np-smoke-t expose pod t --port=80

# 2) egress default-deny 冒煙（此叢集會是 200 = 不 enforce）
kubectl -n np-smoke-s apply -f - <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: egdeny }
spec: { podSelector: {}, policyTypes: [Egress] }
YAML
sleep 25
kubectl -n np-smoke-s run p --image=curlimages/curl --restart=Never -it --rm --command -- \
  curl -sS -m6 -o /dev/null -w "egress-deny 應 000/失敗，得到=%{http_code}\n" \
  http://t.np-smoke-t.svc.cluster.local/

# 3) 清理
kubectl delete ns np-smoke-t np-smoke-s
```

---

## Related

- **Issues**：#280（cross-tenant default-deny，P0）、#279（agent→CS-MCP 授權 NP dead-path）、#284（operator RBAC 不 enforce ns prefix，F-ENV-8 防禦縱深）。
- **Pattern / pitfall**：`docs/decisions/patterns/two-layer-acl-lockstep.md`（#280 收緊 CS ingress 必與 #279 補 agent→CS carve-out lock-step，否則 agent→CS 全斷或維持全開）。
- **前置 recon**：✅ woow-paas-charts 已確認 `egressWorkspaceIds` → `egress + namespaceSelector{woow.tw/workspace-id}`（kube-router 雙重死，見上 chart-side 事實）；⏳ 平台團隊待做 kube-router v1.34.5 netpol upstream triage（本 doc「Why it happens」已排除規則消失類、precise defect 待查）；⏳ Bitnami odoo/postgres per-release NP 實際 shape 待查（#280 odoo/pg 收緊路徑）。

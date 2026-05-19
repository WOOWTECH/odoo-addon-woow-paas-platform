# stg sandbox NetworkPolicy 跨 NS 連線被擋

- 日期：2026-05-09
- 影響環境：stg（namespace `dev-sandbox-stg`, helm release `stg`）
- 涉及 chart：`odoo-dev-sandbox-0.1.0`
- 修復 PR：<https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/22>

## 1. 原由

stg 環境 sandbox 需要透過 `paas-stg-cloudflare-system` namespace 的 cloudflared tunnel 對外暴露服務（`stg.woowtech.io`）。helm values 已正確設定：

```yaml
networkPolicy:
  allowEgressToNamespaces:
  - paas-stg-system
  allowIngressFromNamespaces:
  - paas-stg-cloudflare-system
```

預期 cloudflared → `dev-sandbox-stg/nginx` 應該通，外部走 `stg.woowtech.io` 應該回 200。

## 2. 問題

### 現象

cloudflared → `dev-sandbox-stg/nginx` 連線一律 **connection refused**（kernel-level reject, 0–90ms），`stg.woowtech.io` 不通。

### 觀察與排除

| # | 觀察 | 推論 |
|---|------|------|
| 1 | 同 NS pod 互通 OK | ingress `podSelector: {}` rule 有生效 |
| 2 | 跨 NS 全部被擋（含 default、paas-system、cloudflare-system） | NP 確實在 enforce |
| 3 | 刪除兩個 NP（`default-deny` + `allow-internal`）後 cross-NS 全通 | 確認問題出在 NP 本身 |
| 4 | cloudflared pod IP `10.42.1.54` 落在 cluster CIDR | **不是 hostNetwork**，hypothesis 排除 |
| 5 | namespace 有 `kubernetes.io/metadata.name=paas-stg-cloudflare-system` label | namespaceSelector 該能 match |

### Root cause

`templates/network-policy.yaml` 的 egress 區塊有一條 RFC1918 全擋的 ipBlock：

```yaml
- to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except:
        - 10.0.0.0/8
        - 172.16.0.0/12
        - 192.168.0.0/16
```

K3s pod CIDR `10.42.0.0/16` 落在 `10.0.0.0/8` 內，被 except 排除。當 cloudflared 的 SYN 抵達 nginx，nginx 的 SYN-ACK reply 對應不到任何 egress allow rule：

- `cloudflare-system` 不在 `allowEgressToNamespaces`
- 目標 IP `10.42.1.54` 被 ipBlock except 擋掉
- 不是 same-NS、不是 DNS

理論上 NetworkPolicy 是 stateful，conntrack 應自動放行 established connection 的 reply packet，**但 K3s 內建 NP enforcer 對 established connection return path 處理有 quirk**，會把 reply 當新 egress 重新檢查 → DROP/REJECT → sender 端看到 connection refused。

> 辨識線索：「connection refused」是 kernel-level REJECT 訊號（TCP RST 或 ICMP port-unreachable），跟 NP DROP 行為（會 timeout）不同。看到 0–90ms 的 refuse 而非 timeout，是 NP enforcer 用 REJECT 規則的特徵。

## 3. 解法

`templates/network-policy.yaml` 增加 11 行：把 `allowIngressFromNamespaces` mirror 到 egress section，繞過 K3s reply-path quirk。

```yaml
egress:
  ...existing...
  # Mirror ingress allowlist for reply traffic (K3s NP enforcer reply-path quirk)
  {{- range .Values.networkPolicy.allowIngressFromNamespaces }}
  - to:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: {{ . }}
  {{- end }}
```

### 驗證

1. Apply minimal-test NP（單獨 ingress+egress 對 `paas-stg-cloudflare-system`）→ `stg.woowtech.io` 立即 200
2. `helm upgrade stg` rev 19，chart fix + minimal-test 並存 → 200
3. 刪除 minimal-test 只留 chart NP → 連續 3 次 200，chart fix 獨立有效

暫時 workaround（NP 全刪）已撤回，恢復 NP 隔離。

## 4. 影響範圍

- **直接受影響**：stg sandbox（`stg.woowtech.io`）。在 fix 落地前，外部完全無法訪問。
- **潛在受影響**：任何使用此 chart 且需要 cross-NS ingress 的 sandbox 部署（如未來其他 stg 變體、demo 環境等）。
- **未受影響**：
  - `local` mode：通常不啟用 cloudflare tunnel，沒踩到 cross-NS ingress 場景
  - `ci` mode：`allowIngressFromNamespaces` 預設為空，reply-path quirk 沒觸發條件
- **修復後**：PR #22 merge 並重新 helm upgrade 即可自動套用，無需額外手動操作。

## 5. 為什麼之前沒爆

此 chart 不是首發，但這是第一次在 stg mode 真正啟用 `allowIngressFromNamespaces` + 啟用完整 NP 隔離的組合。

- `local` mode 開發時通常不啟 cloudflare tunnel ingress
- `ci` mode 走 internal smoke test，不需要 cross-NS ingress
- 所以 ipBlock except 把 cluster CIDR 擋掉的「地雷」一直沒被踩到，直到 stg 啟用完整外部入口

## 6. Known limitations / Follow-up

當前 fix 是**最小可行修復**，刻意避開 breaking change。以下 follow-up 已知但未在此 PR 處理：

- [ ] `allowIngressFromCIDRs` 欄位 — 給 hostNetwork pod（source IP 是 node IP，namespaceSelector 永遠不 match）的 escape hatch
- [ ] `ipBlock` external rule 加 `exceptOverrides` — 讓使用者能放回特定 cluster CIDR，避免有「只允許 ingress 不允許 egress」場景時卡住
- [ ] 把 `allowIngressFromNamespaces` / `allowEgressToNamespaces` 合併成 `allowedNamespaces` — 簡化 mental model，但屬 breaking change，需 chart bump major version 並提供 migration note
- [ ] 在 `values.yaml` 的 `allowIngressFromNamespaces` 上方註解明確說明「此 list 會自動 mirror 到 egress」，避免未來想分開控制 in/out 的人踩坑

## 7. 學到的（給未來 NP debug 的人）

1. **K8s NetworkPolicy 是雙向 enforce**。conntrack 理論上會處理 reply packet，但**不要相信 enforcer 實作**，尤其 K3s 內建版本。看到「ingress 寫對了還是不通」時，記得檢查 egress 是否能放行 reply。
2. **`ipBlock 0.0.0.0/0` + RFC1918 except 看似安全（防 SSRF），但會把 cluster pod CIDR 一起擋**。如果 NP 同時要允許 cross-NS pod 通訊，必須額外用 namespaceSelector 補回來。
3. **「connection refused」at kernel-level 0–90ms = REJECT 訊號**，跟 NP DROP（timeout）行為不同。這是辨識 enforcer 用 REJECT 規則 vs 純 DROP 規則的重要線索。
4. **debug NP 時優先做 minimal-repro NP**：把現有 NP 全刪，apply 一個只有單條 rule 的 NP 測試該 rule 本身是否 work。能快速隔離「rule 本身錯」vs 「多 NP 互動 bug」vs 「enforcer bug」。
5. **chart 不在 paas-charts repo**：`odoo-dev-sandbox` 在 `WOOWTECH/ecpay_odoo18/charts/odoo-dev-sandbox`，並 mirror 到 `woow_paas_platform/charts/odoo-dev-sandbox`。修 chart 改後者。

## 8. 參考

- 修復 PR：<https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/22>
- Chart 路徑（canonical）：`WOOWTECH/ecpay_odoo18/charts/odoo-dev-sandbox/templates/network-policy.yaml`
- Chart 路徑（mirror，PR 改的位置）：`woow_paas_platform/charts/odoo-dev-sandbox/templates/network-policy.yaml`
- HA E2E 驗證：sm-api-doc §9

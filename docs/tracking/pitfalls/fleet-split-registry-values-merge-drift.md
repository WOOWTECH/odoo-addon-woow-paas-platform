# Pitfall: Fleet 同名 map 跨 valuesFiles + valuesFrom 拆分 → 非 deep-merge 掉 key → secret 永久 drift（latent imagePullSecret null 風險）

> 來源：prod operator #207/#208 rollout 前置調查（2026-06-21，唯讀診斷）。stg 發佈 operator #208 epoch 修正時，paas-platform-stg 順帶發現 prod `paas-app-bundle-paas-operator` bundle 是 `0/1 Modified`——secret `paas-system/paas-operator-registry` 的 `registry-server`/`registry-username` 被 Fleet desired 算成 `null`。深挖後是 **prod 的 `registry` Helm values 被拆成兩個來源、Fleet 合併時沒 deep-merge** 導致的既有 drift，與本次 #207/#208 無關（prod 完全沒被碰）。

## Symptom

- Fleet bundle `paas-app-bundle-paas-operator`（prod，chart pin 0.1.8）顯示 `Modified`，`bundledeployment.status.modifiedStatus`：
  ```
  secret.v1 paas-system/paas-operator-registry modified
  patch: {"data":{"registry-server":null,"registry-username":null}}
  ```
  （patch 是「live → desired」要套的變更 → **desired 想把這兩個 key 設 null**，password 不在 diff 裡＝一致。）
- live secret **有有效值**：`registry-server=jcr-prod.woowtech.io`、`registry-username=woow-prod`、`registry-password` 在。
- prod operator pod **Running、0 restart、image 正常拉取** → **功能完全正常**，沒有任何 outage。drift 只是「Fleet 偵測到 live≠desired」、非 force，故沒去動它。
- 全部測試與 stg 發佈都看不到——這是 prod-only、Fleet 層、跨 repo（paas-app-bundle-cd）的設定 drift。

## Why it happens

prod 的 `registry` Helm values **被拆成兩個來源**，而 chart 把 `registry-server`/`registry-username` 綁在同一個 `if .Values.registry.server` 區塊渲染（password 另一獨立區塊）：

- `paas-app-bundle-cd/paas-operator/values-prod.yaml:22`（`valuesFiles`，明文）→ `registry.server: "jcr-prod.woowtech.io"`
- `fleet-local/paas-operator-fleet-secrets`（`valuesFrom` secret 的 `values.yaml`）→ `registry.username` + `registry.password`

Fleet 合併 `valuesFiles` 與 `valuesFrom` 時，**兩個同名 `registry:` map 不是 deep-merge**——`valuesFrom` 那塊整個覆蓋掉 `values-prod.yaml` 的 `registry`，把只在 values-prod.yaml 出現的 `registry.server` 吃掉。合併後 desired 缺 `server` → chart 的 `if registry.server` 區塊不渲染 → **server + username 兩個 key 一起消失**（password 在獨立區塊、保留）。live 仍是較早一次「values 還完整」時渲染的三-key 版本 → Fleet 報 drift。

> chart template 在版本間也變了：prod 的 chart **0.1.8** 的 `secret-registry.yaml` 渲染 server/username/password 三個 key；現況 **0.1.15** 的 `secret-registry.yaml` 只渲染 **一個** key（`existingSecretKey`←password，gated 在 `registry.server && registry.password && not existingSecret`），server/username 改由別處消費。所以 prod 一旦 bump 到 0.1.15，這顆 secret 的形狀本來就會重整。

## 風險

- **目前休眠**：Fleet 偵測但不自動修正，pod 長存（18 天）、creds 還在 → 不影響運行。
- **潛在**：若有人對該 bundle `force sync` / 開 drift-correction / `forceSyncGeneration` bump，或 pod 被重排到沒快取該 image 的節點，secret 的 server/username 一旦被 null → **prod operator 拉 image 斷**（chart gate `not existingSecret` 代表這就是真正的 imagePullSecret、非廢物）。屬「平時無感、踩到就是 prod image pull 斷線」的延遲炸彈。

## How to avoid / 守線

1. **同一個 Helm map 不要跨 `valuesFiles` 與 `valuesFrom` 拆 key。** Fleet 合併同名 map 時的覆蓋語意會默默掉 key。把 `registry` 三個值收斂到**單一來源**（要嘛全進 secret、要嘛 server 也放進 secret 讓覆蓋勝出那塊帶齊），desired 才會與 live 一致、drift 自然清。
2. **prod 0.1.8→0.1.15 rollout 時一併重整。** 0.1.15 的 registry secret 模型已變（單 key），屆時把 prod 的 registry values 對齊 0.1.15 的 `existingSecret`/`existingSecretKey` 慣例，最乾淨。
3. **判 Fleet drift 方向看 `bundledeployment.status.modifiedStatus.patch`**（live→desired 的 strategic-merge patch），別只看「Modified」字樣或猜 live 被改——本案就是 desired 端掉 key、live 才是對的。
4. **延伸**：任何 Fleet bundle 對自家 Helm-managed Secret 報 `Modified` 都值得查 patch 方向——若 desired 會 null 掉關鍵 credential（imagePullSecret / API token），就是這類「force sync 即 prod 斷線」延遲炸彈，優先收斂 values 來源。

## 相關

- prod operator #207/#208 rollout（暫緩、observe stg 後再排）會一併帶上本 drift 修正——見對應 issue。
- [[cf-ns-orphan-no-account-gc]] — 同期 stg/prod 共用 cluster 的 operator 部署調查家族（孤兒/GC 方向）。

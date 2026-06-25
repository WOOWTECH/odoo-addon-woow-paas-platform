# Pitfall: Fleet bundle 對自家 secret 報 Modified → 先看 patch 方向，別急著套「split-values 非 deep-merge」框架（✅ 已解決 2026-06-25，附根因更正）

> 來源：prod operator #207/#208 rollout 前置調查（2026-06-21，唯讀診斷）。stg 發佈 operator #208 epoch 修正時，paas-platform-stg 順帶發現 prod `paas-app-bundle-paas-operator` bundle 是 `0/1 Modified`——secret `paas-system/paas-operator-registry` 的 `registry-server`/`registry-username` 被 Fleet desired 算成 `null`。

## ⚠️ 根因更正 & RESOLVED（2026-06-25，經 paas-charts 複核 + live 證據）

**本 doc 原始（2026-06-21）診斷「split-values 非 deep-merge 吃掉 `registry.server`」是誤診，保留於下方作教訓對照，但不是真因。**

實際根因（2026-06-25 深查確認）：

- chart 的 `secret-registry.yaml` **自 0.1.6 起就是單 key 模型**（全史只有一個 commit `be310d3`，0.1.8 與 0.1.16 的模板**逐字相同**，只渲染 `registry-password`）。`registry.server` / `registry.username` **從來不進這顆 secret**，一直走 **plaintext deployment env**——live 實證：`REGISTRY_SERVER=jcr-prod.woowtech.io`、`REGISTRY_USERNAME=woow-prod` 都是 env `value`（非 `secretKeyRef`），只有 `REGISTRY_PASSWORD` 讀 secret key `registry-password`。
- 既然 chart 從不渲染 server/username 進 secret，「split-merge 把 secret 的 server 吃掉」這個框架**根本不成立**（secret 裡本來就不該有那兩個 key）。
- live secret 上那兩個 key 是 **chart 外殘留**（secret 帶 `kubectl.kubernetes.io/last-applied-configuration` annotation = 有人手動 `kubectl apply` 補的，pre-0.1.6 或舊「Fleet local chart」時代），**零 consumer**（全叢集只有 operator deploy 引用此 secret 的 `registry-password`）。Fleet desired（chart 渲染）只含 `registry-password` → 它的 drift patch `{"registry-server":null,"registry-username":null}` 就是「把這兩個多餘 key 刪掉」。

**修復**：`kubectl -n paas-system patch secret paas-operator-registry --type=json -p='[{"op":"remove","path":"/data/registry-server"},{"op":"remove","path":"/data/registry-username"}]'` → live 收斂到 desired → bundle `ready:1`、GitRepo `5/5`、operator pod 0 restart、plaintext env 不受影響。**非**改 CD repo（git/chart desired 本來就對）。

**真正可帶走的教訓**（比原框架更準）：

1. **Fleet bundle 對自家 Helm-managed secret 報 `Modified` 時，第一步看 `bundledeployment.status.modifiedStatus.patch` 的方向**（live→desired 的 strategic-merge patch）。`null` = desired 要**刪**該 key。本案是 **live 多了 desired 沒有的 key**（外部殘留），不是 desired 端缺 key。
2. **別假設「secret 裡有的 key 都是 chart 渲染的」**——查 chart 模板實際渲染哪些 key（`helm template` 或讀 `templates/secret-*.yaml`），再對照 live。本案誤診正是源於「假設 chart 0.1.8 渲染三個 key」這個未經查證的前提。
3. 殘留 key 帶 `last-applied-configuration` annotation = 手動 `kubectl apply` 的指紋，可據此判斷是 chart 外注入。

---

> **以下為原始（2026-06-21）診斷，已證誤，保留作「未查 chart 模板就套框架」的教訓對照。**
>
> ~~深挖後是 **prod 的 `registry` Helm values 被拆成兩個來源、Fleet 合併時沒 deep-merge** 導致的既有 drift，與本次 #207/#208 無關（prod 完全沒被碰）。~~

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

## Why it happens（⚠️ 原始假設，已證誤 — 真因見上方「根因更正」）

> 下段為 2026-06-21 未查 chart 模板就推導的假設。**致命錯誤在 line 「chart 0.1.8 渲染三個 key」**——paas-charts 實測 0.1.8 與 0.1.16 的 `secret-registry.yaml` 逐字相同、只渲染 `registry-password`，server/username 從不進 secret。前提一錯，整套 split-merge 推論失效。保留供「未驗 chart 渲染就套框架」對照。

prod 的 `registry` Helm values **被拆成兩個來源**，而 chart 把 `registry-server`/`registry-username` 綁在同一個 `if .Values.registry.server` 區塊渲染（password 另一獨立區塊）：

- `paas-app-bundle-cd/paas-operator/values-prod.yaml:22`（`valuesFiles`，明文）→ `registry.server: "jcr-prod.woowtech.io"`
- `fleet-local/paas-operator-fleet-secrets`（`valuesFrom` secret 的 `values.yaml`）→ `registry.username` + `registry.password`

Fleet 合併 `valuesFiles` 與 `valuesFrom` 時，**兩個同名 `registry:` map 不是 deep-merge**——`valuesFrom` 那塊整個覆蓋掉 `values-prod.yaml` 的 `registry`，把只在 values-prod.yaml 出現的 `registry.server` 吃掉。合併後 desired 缺 `server` → chart 的 `if registry.server` 區塊不渲染 → **server + username 兩個 key 一起消失**（password 在獨立區塊、保留）。live 仍是較早一次「values 還完整」時渲染的三-key 版本 → Fleet 報 drift。

> chart template 在版本間也變了：prod 的 chart **0.1.8** 的 `secret-registry.yaml` 渲染 server/username/password 三個 key；現況 **0.1.15** 的 `secret-registry.yaml` 只渲染 **一個** key（`existingSecretKey`←password，gated 在 `registry.server && registry.password && not existingSecret`），server/username 改由別處消費。所以 prod 一旦 bump 到 0.1.15，這顆 secret 的形狀本來就會重整。

## 風險（⚠️ 已證誤 — 無延遲炸彈）

> 原判「force sync 會 null 掉 server/username → prod 拉 image 斷」的延遲炸彈**不存在**：那兩個 key 零 consumer，operator 從 plaintext env 拿 server/username。移除它們（甚至 force sync）對 prod image pull **無影響**。2026-06-25 實際移除後 operator 0 restart、image 正常。原始（已證誤）論述如下：

- ~~**潛在**：若有人對該 bundle `force sync`...secret 的 server/username 一旦被 null → **prod operator 拉 image 斷**...延遲炸彈。~~（**不成立**：server/username 走 plaintext env，非 imagePullSecret 來源。）

## How to avoid / 守線

> **正解見上方「根因更正」的三條教訓。** 下面 1–2 是基於誤診的建議（split-merge 非本案真因，且 prod 的 registry 並無實際 server 掉值問題——live `REGISTRY_SERVER` 一直正常），保留供對照；3–4 仍成立。

1. ~~同一個 Helm map 不要跨 `valuesFiles` 與 `valuesFrom` 拆 key。~~（仍是一般 Fleet 值得注意的 gotcha，但**非本案真因**——本案 secret 從不含 server/username。）
2. ~~prod 0.1.8→0.1.15 rollout 時一併重整 registry secret 模型。~~（不需要：0.1.6 起就是單 key 模型，無模型變更要對齊。）
3. **判 Fleet drift 方向看 `bundledeployment.status.modifiedStatus.patch`**（live→desired 的 strategic-merge patch），別只看「Modified」字樣或猜 live 被改——本案就是 **live 多了 desired 沒有的殘留 key**。✅
4. **延伸**：任何 Fleet bundle 對自家 Helm-managed Secret 報 `Modified`，先查 patch 方向 + 查 chart 模板實際渲染哪些 key，再判斷是「desired 缺 key」還是「live 多 key（外部殘留）」。✅

## 相關

- prod operator #207/#208（及後續）已於 2026-06-25 promote 到 prod（chart `0.1.16` / image `20260624.1022`，與 stg 同版）；本 drift 已**獨立**修復（移除殘留 secret key），非靠 rollout 帶上。見 `docs/guides/deployment/prod-release-sop.md` §10.1。
- [paas-app-bundle-cd#18](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/18)（drift 追蹤 issue，可關閉）。
- [[cf-ns-orphan-no-account-gc]] — 同期 stg/prod 共用 cluster 的 operator 部署調查家族（孤兒/GC 方向）。

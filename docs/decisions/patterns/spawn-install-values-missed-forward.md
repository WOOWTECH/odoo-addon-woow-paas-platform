# Pattern: spawn install-values 的 missed-forward class —— 修一個就全盤對齊 chart required values

> 適用：你的 operator / controller 有一個「**把 request / 環境資料組成 helm install values（或等價的下游 config）**」的組裝點（本 epic = `agent_runtimes._build_install_values` + `_names`），而下游 chart 用 `required` / validate 函式硬性要求一組 value。當下游撞「缺某個 value」的 render / 部署錯時，**那幾乎不會只缺一個** —— 同一個組裝點通常系統性漏轉了**多個** chart 要的 value。對策：**修一個 missed-forward 時，立刻全盤對齊 chart 的所有 required values，並加一條 contract test 鎖住整組契約**，而不是一個個撞、一個個補（每補一個 = 一輪 PR + CI + redeploy）。

## When to use

當下列全中：

1. 有一個組裝點把上游資料 → 下游 config（helm values / env / ConfigMap / API payload），且**上游手上有的欄位**和**下游真正要的欄位**是兩份清單，靠人工 forward 對齊。
2. 下游對「缺 value」是**硬失敗**（chart `required` / validate `fail` / fail-closed env / schema required）。
3. 組裝點的單元測試 **mock 掉下游**（不會真的 render / 部署），所以漏轉在 CI 綠、只在真部署現形。

→ 本 epic 四個同 class instance（pilot 9-gap chain 的 gap 2/4/5/6-NP，見 [[agent-runtime-pilot-e2e-gap-chain]]）：

| 漏轉的 value | 下游怎麼要 | 撞點 |
|--------------|-----------|------|
| namespace prefix（`_names` hardcode `paas-ws-pa-` 沒從 `settings.namespace_prefix` 衍生） | `validate_namespace` raise | spawn 500 |
| `projectId`（`_build_install_values` 只給 `projectHash`） | chart `_helpers.tpl` validate `fail` | chart render 500 |
| `imagePullSecrets`（沒 forward + ns 無 registry secret） | chart 用 default（空）→ pod 拉不到私有 image | ImagePullBackOff（**含 plan-gap：creds provision**，見 [[plan-gap-stop-and-report]]） |
| `networkPolicy.odooNamespace`（沒 forward）**← 第 4 例** | chart 用 default（`paas-system` 錯 placeholder）→ agent pod 自身 NP 不放行真 odoo ns | pod↔odoo refused（chart 0.1.1 + operator forward 才 durable） |

四者共同點：**operator 手上其實有資料**（`request.project_id`、`settings.namespace_prefix`、`settings.odoo_base_url`、自身 registry secret），只是組裝點沒 forward 給下游 —— 是 **missed-forward**，不是「沒資料」的 design gap（後者見 [[plan-gap-stop-and-report]]）。

> **第 4 例的 source 不是乾淨欄位**：`projectId`/`projectHash` 是 request 直接欄位、直接 forward；但 `odooNamespace` **無對應欄位**，要從 `settings.odoo_base_url`（in-cluster svc FQDN `<svc>.<ns>.svc.cluster.local`）**衍生**第 2 段。這把 missed-forward 推到與 plan-gap 的邊界：**有確定性來源 → 衍生（不是硬塞）+ 無法衍生時 fallback（omit 讓 chart 用 default、不亂猜）**。判準仍是「有沒有正確來源」——有（svc FQDN）→ 衍生即可，非 plan-gap。`_derive_odoo_namespace()` + contract test 鎖此衍生。

### F-PILOT-10 再現：optional value 漏轉是 silent（contract test 須涵蓋 optional）★

真 LLM cluster e2e（[[agent-runtime-fpilot10-cluster-e2e-gap-chain]]）同一個組裝點 `_build_install_values` 又系統漏轉 3 個，但這次撞的是 chart **optional** value：

| 漏轉的 value | 下游怎麼要 | 撞點 | chart 是 |
|--------------|-----------|------|----------|
| `imagePullSecrets`（operator stale 沒 forward + chart 無 default backstop）| 空 → pod 拉私有 image 失敗 | ImagePullBackOff（**silent**）| optional |
| `LLM_MODEL_CONFIG` env（**chart 漏 wire** 映射）| pod 無 model/api_base config | pod 起但無回覆（**silent**）| optional |
| `externalEgress`（operator 沒 forward）| NP default 不開 → 真 LLM egress 被擋 | LLM 連不出去（**silent**）| optional |

**關鍵深化（vs pilot）**：pilot 漏轉撞 chart **required** value → validate `fail` → spawn **500**（loud，立刻知道）。F-PILOT-10 漏轉撞 chart **optional** value → chart 用 default/empty、**pod 照起、只 runtime 行為錯** → **沒 render error 可循**（要挖 `logs`/`describe`/`get -o yaml`）。**optional 漏轉比 required 更陰**，因為它不觸發任何硬失敗。

→ **contract test 必須涵蓋 optional-but-load-bearing key，不只 required**：把 `test_build_install_values_carries_all_chart_required_keys` 擴成（或新增）斷言 optional key（`imagePullSecrets` / `LLM_MODEL_CONFIG` / `externalEgress`）也被 forward。required key 漏轉部署會 loud 撞到；**optional key 漏轉只有 contract test 擋得住**（否則一路 silent 到 cluster e2e runtime 才現形）。

> 另一半 **G6 是 chart 漏 wire**（operator forward 了、chart template 沒映射進 pod env）—— missed-forward 的 **chart 半**。光 operator contract test 抓不到；要 **render-to-pod-spec integration test**（真 `helm template` → assert 渲染出的 pod spec env 有該 key）才守得住 operator↔chart 兩半的接縫。

## The trap

一個個撞、一個個補：撞 `projectId` → 補 `projectId` → 重建 image → redeploy → 重 flip → 撞 `auth.currentKey`（其實有 default 沒撞，但下一個是 `odooNamespace`）→ 補 → 又一輪…… 每輪 PR + CI build + ephemeral redeploy + 人工 flip 重測，**N 個漏轉 = N 輪**。pilot 中若不全盤對齊，gap 4 之後還會冒 gap 4.5 / 4.6。

## The pattern

**修 missed-forward #1 時，立刻做這三件事：**

1. **拉出下游的完整 required 契約**（不是只看撞到的那個）：
   - chart：`helm pull` 後 grep `templates/_helpers.tpl` 的 validate 函式 + 所有 `required` / `fail` 呼叫 + `values.yaml` 標 `REQUIRED` 的註解。
   - 本 epic：權威清單 = `agent-runtime.validate` 的 `projectHash` + `projectId` + `auth.currentKey`(manageSecret=true)。
2. **逐一對照組裝點實際 forward 的**，補齊**所有**缺口（一個 PR）：本 epic 對齊後確認 `projectId` 是唯一缺口（`projectHash`/`auth.currentKey` 已轉、`currentKeyName` 有 chart default）。
3. **加 contract test 鎖整組契約**：斷言組裝點輸出含**每個** required key，未來再漏 forward 會在 CI 紅、不會等到 live spawn。本 epic：`test_build_install_values_carries_all_chart_required_keys`（斷言 `projectHash` + `projectId` + `auth.currentKey` 全在）。

```python
# contract test 形狀（operator <-> chart required values）
def test_build_install_values_carries_all_chart_required_keys():
    values = _build_install_values(SpawnOrAttachRequest(**SPAWN_BODY), "tok")
    assert values.get("projectHash") == SPAWN_BODY["project_hash"]
    assert values.get("projectId") == SPAWN_BODY["project_id"]     # 曾漏轉的
    assert values.get("auth", {}).get("currentKey")                # required when manageSecret
```

## Why it works

- **全盤對齊**把「N 輪撞補」壓成「1 輪」——render 一次帶齊所有 value 驗 render 完整過（本 epic 補 projectId 後 dry-run 全 manifest render OK，確認無 gap 4.5）。
- **contract test** 把「人工 forward 對齊」這個易漏的耦合**固化成 CI 守線**：chart 之後加 required value、或組裝點重構漏轉，都在 unit 層紅，回到 mock 之下也能抓（補上了 mock 掩蓋 integration 契約的洞）。

## Forward-looking invariant

組裝點（`_build_install_values` / `_names` / 任何 spawn config builder）**未來再加 chart value**（required **或 optional**，如 Phase E/F/G 新 chart 欄位）時：(1) 同步 forward；(2) 擴充 contract test 斷言——**含 optional-but-load-bearing key**（F-PILOT-10 教訓：optional 漏轉 silent，比 required 更需要 test 鎖）；(3) 補 **render-to-pod-spec integration test**（真 `helm template` → assert pod spec 真有該 env/secret/NP），守住 operator forward + chart wire **兩半的接縫**（光 operator contract test 抓不到 chart 漏 wire）。chart 端**理想把環境相關 default 設成 required**（逼 operator 必傳），別留會在某環境錯的 placeholder default（如 `odooNamespace: paas-system` 是反例）。

## See also

- [[agent-runtime-pilot-e2e-gap-chain]] — 本 pattern 的三個 instance（pilot gap 2/4/5）所在的完整 chain
- [[agent-runtime-fpilot10-cluster-e2e-gap-chain]] — F-PILOT-10 再現 instance（G2/G6/G7，**optional value silent 漏轉** + chart 漏 wire 半 + render-to-pod-spec 防線）
- [[plan-gap-stop-and-report]] — 對照：當「不是漏轉、是沒資料」時的守線（別硬塞）
- [[phase-versioned-test-assertion]] — 同 epic 的另一條 test-discipline pattern（multi-phase schema 斷言用 monotonic）
- [[credential-claim-test-fixture-coupling]] — 同 epic：硬化 credential 路徑時的 test fixture forward-coupling（contract-lock 的姊妹概念）

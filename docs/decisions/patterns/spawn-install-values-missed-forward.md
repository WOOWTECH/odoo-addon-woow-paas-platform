# Pattern: spawn install-values 的 missed-forward class —— 修一個就全盤對齊 chart required values

> 適用：你的 operator / controller 有一個「**把 request / 環境資料組成 helm install values（或等價的下游 config）**」的組裝點（本 epic = `agent_runtimes._build_install_values` + `_names`），而下游 chart 用 `required` / validate 函式硬性要求一組 value。當下游撞「缺某個 value」的 render / 部署錯時，**那幾乎不會只缺一個** —— 同一個組裝點通常系統性漏轉了**多個** chart 要的 value。對策：**修一個 missed-forward 時，立刻全盤對齊 chart 的所有 required values，並加一條 contract test 鎖住整組契約**，而不是一個個撞、一個個補（每補一個 = 一輪 PR + CI + redeploy）。

## When to use

當下列全中：

1. 有一個組裝點把上游資料 → 下游 config（helm values / env / ConfigMap / API payload），且**上游手上有的欄位**和**下游真正要的欄位**是兩份清單，靠人工 forward 對齊。
2. 下游對「缺 value」是**硬失敗**（chart `required` / validate `fail` / fail-closed env / schema required）。
3. 組裝點的單元測試 **mock 掉下游**（不會真的 render / 部署），所以漏轉在 CI 綠、只在真部署現形。

→ 本 epic 三個同 class instance（pilot 9-gap chain 的 gap 2/4/5，見 [[agent-runtime-pilot-e2e-gap-chain]]）：

| 漏轉的 value | 下游怎麼要 | 撞點 |
|--------------|-----------|------|
| namespace prefix（`_names` hardcode `paas-ws-pa-` 沒從 `settings.namespace_prefix` 衍生） | `validate_namespace` raise | spawn 500 |
| `projectId`（`_build_install_values` 只給 `projectHash`） | chart `_helpers.tpl` validate `fail` | chart render 500 |
| `imagePullSecrets` / `networkPolicy.odooNamespace`（沒 forward） | chart 用 default（空 / 錯 placeholder）→ pod 拉不到 image / NP 擋 | ImagePullBackOff / pod↔odoo refused |

三者共同點：**operator 手上其實有資料**（`request.project_id`、`settings.namespace_prefix`、`settings.odoo_base_url`、自身 registry secret），只是組裝點沒 forward 給下游 —— 是 **missed-forward**，不是「沒資料」的 design gap（後者見 [[plan-gap-stop-and-report]]）。

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

組裝點（`_build_install_values` / `_names` / 任何 spawn config builder）**未來再加 chart required value**（如 Phase E/F 新 chart 欄位）時：(1) 同步 forward；(2) 擴充 contract test 斷言。chart 端**理想把環境相關 default 設成 required**（逼 operator 必傳），別留會在某環境錯的 placeholder default（如 `odooNamespace: paas-system` 是反例）。

## See also

- [[agent-runtime-pilot-e2e-gap-chain]] — 本 pattern 的三個 instance（gap 2/4/5）所在的完整 chain
- [[plan-gap-stop-and-report]] — 對照：當「不是漏轉、是沒資料」時的守線（別硬塞）
- [[phase-versioned-test-assertion]] — 同 epic 的另一條 test-discipline pattern（multi-phase schema 斷言用 monotonic）
- [[credential-claim-test-fixture-coupling]] — 同 epic：硬化 credential 路徑時的 test fixture forward-coupling（contract-lock 的姊妹概念）

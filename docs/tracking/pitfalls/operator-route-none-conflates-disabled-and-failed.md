# operator 的 `route` 回 bare `None` 同時代表「沒開 Cloudflare」與「route 建失敗」

> 一句話：任何「呼叫端沒拿到 route 就當成失敗」的守線，會在**沒開 Cloudflare 的環境**把**每一次部署**翻成 `error`——而 `cloudflare_enabled` 的預設值就是 `false`。

## 症狀

平台端加一道看似理所當然的 fail-closed：「我要求了 expose，operator 卻沒回 route ⇒ route 沒建成 ⇒ 服務不可達 ⇒ 寫 `state='error'`」。

上線後在**沒有配置 Cloudflare 的環境**（本機沙盒、自架 operator、任何沒設 `CLOUDFLARE_*` 的部署）表現為：每一台服務部署完都翻 `error`，錯誤訊息說「public hostname 建立失敗」，但叢集裡一切正常、pod 全 Running。**比原本要修的 bug 嚴重得多**——原 bug 只影響 DNS 慢的那幾秒，這個影響 100% 的部署。

而且它在 prod 不會出現（prod 的 CF 是開的），所以最可能的發現地點是別人的沙盒或 e2e，而不是寫的人自己的環境。

## 根因

operator 的 `_create_cloudflare_route()`（`extra/paas-operator/src/api/releases.py`）有**多條** `return None`，語意完全不同卻**在回應上不可區分**：

| 情形 | 修前的回傳 | 真正的語意 |
| --- | --- | --- |
| Cloudflare 整合沒開 | `None` | 正常，這個部署本來就沒有對外網址 |
| 找不到可 expose 的 service | `None` | 失敗 |
| 推不出可用的 port | `None` | 失敗 |
| `CloudflareException` | `None` | 失敗 |
| 未預期例外 | `None` | 失敗 |

`ReleaseInfo.route` 於是有兩種完全不同的 `None`，而呼叫端只能看到「沒有」。

放大它的是預設值：`extra/paas-operator/src/config.py` 的 `cloudflare_enabled` 預設 **`False`**；而且 `CloudflareService.__init__` 在 `enabled` 為真但 `account_id` / `tunnel_id` / `api_token` / `domain` 缺任一個時會把它**強制關回 `False`**。⇒「沒開 CF」不是罕見的邊角，是**預設狀態**。

還有第三個狀態容易被漏掉：`dns_enabled = enabled and bool(zone_id)`。CF 開著但沒給 `CLOUDFLARE_ZONE_ID` 時，tunnel ingress 會建、DNS record 不會建。這一格既不是 failed 也不是 disabled，而是「DNS 由人工管理」——同一個 `create_dns_record` 在這個狀態下直接 return 而不嘗試。

## 修法

**operator 側**：讓三態在回應上可分辨，不要靠 `None` 承載語意。

- `RouteInfo` 新增 `status`（`RouteStatus.OK` / `RouteStatus.FAILED`）與 `dns_failed`。
- expose 被要求而**失敗**時，回一個 `status=failed` 的 `RouteInfo`，不再回 `None`。
- `route is None` 從此只有一個意思：**沒有要求 expose，或這個部署沒開 Cloudflare**。schema 的 class docstring 把這句話寫死在契約裡。
- `dns_failed` 只在 DNS 管理**真的啟用**時才可能為真——`create_dns_record` 在 `dns_enabled` 為假時直接 return，不會拋，所以「刻意關掉」永遠不會偽裝成「失敗」。這條不變式寫在 `create_route` Step 1 的註解裡。

**呼叫端（平台）側**：fail-closed 的判準是**明確的失敗訊號**，不是「東西不在」。

- ✅ `route.dns_failed` 為真 ⇒ fail-closed
- ✅ `route.status == 'failed'` ⇒ fail-closed
- ⛔ `route` 缺席 ⇒ **不得** fail-closed（那同時涵蓋「沒開 CF」與「舊 operator 還沒有這個欄位」）

第三條是本 pitfall 的核心。它也順帶解決了跨版本相容：新平台配舊 operator 時 `route` 恆為 `None`，若把缺席當失敗，升級當下所有部署一起紅。

## 這一類的通則

**「欄位不存在」不是失敗訊號，只是「這一版／這個設定沒有這個資訊」。** 要 fail-closed 就必須有人**明說**失敗——具名欄位（`dns_failed`）或明確狀態（`status='failed'`）。用「缺席」當判準的守線，會在兩個場合誤擊：功能沒開的環境，以及還沒有那個欄位的舊版本。兩者都不是故障。

同族先例：#484 的 `TunnelCreateResponse.dns_failed` 就是為了把「`dns_record_id` 是 None 因為失敗」與「是 None 因為刻意不建」分開才存在的；它的 field description 原文已經把規則寫清楚，只是 release 這條路徑當初沒有跟上。

## 守線

**機械化狀態：pattern-guard（`paas-operator` ＋ `model-write-guard` class）＋ repo-lint `route-contract-golden`。兩者守的是不同性質，都不完整，下面逐一講清楚守到哪、沒守到哪。**

### (1) 「呼叫端把缺席當失敗」——**沒有** gate，刻意的

評估過但**否決** repo-lint gate，理由寫在這裡以免日後有人以為是漏做：缺席判斷可以寫成 `if not route:`、`if route is None:`、`if not release.get('route'):`、或先取值再判空——AST/grep 都認得出其中幾種、認不出其餘幾種。一個必然大量漏抓的 gate，會讓後來的人以為這條已經被守住，**假安全感比沒有 gate 更糟**（同 `odoo-object-button-return-value-is-an-action` 的判斷）。

改用 pattern-guard：編輯 operator 的 route 建立路徑、或平台端讀 release 回應的 model 時，hook 會把本文列進必讀。守的是「動到這裡的人一定看過這個坑」，而不是「所有寫法都被偵測到」——後者做不到，宣稱做得到才是問題。

行為層另有測試釘住（平台 repo `src/tests/test_route_dns_failed_648.py`）：`route` 缺席時**不得** fail-closed、舊 operator 不回 `dns_failed` 鍵時**不得**被當成失敗。這兩條是對照組，紅了就代表守線被改成用缺席當判準。

### (2) 「wire 形狀改了、golden 沒重產」——repo-lint `route-contract-golden`

⚠ 這是**另一個性質**，不要跟 (1) 混為一談（並排讀很容易以為前面說否決、後面又加了一條，自相矛盾）。(1) 守的是**呼叫端怎麼判**，(2) 守的是**兩端的契約載體有沒有脫節**。

`route` 的形狀是 operator↔Odoo 唯一的契約載體：Odoo 端不吃 operator 的碼，它讀 `src/tests/fixtures/route_contract_golden.json` 當 fixture。operator 端有測試斷言「現在的序列化 == golden」，但那道斷言長在 `extra/paas-operator/`——**沒有 CI runner**，而 Odoo 端讀舊 golden 照樣全綠 ⇒ 「改了序列化又忘了重產 golden」完全無聲。gate 把 `RouteInfo`／`RouteStatus` 的欄位形狀雜湊成指紋嵌進 golden，重算比對。

🔴 **這條的盲區（選擇留下的，不是沒看到）**：指紋只涵蓋 **schema 形狀**，**不涵蓋 `_create_cloudflare_route` 的行為**。有人改那支讓某條路改回 `status="ok"`，schema 沒動 ⇒ 指紋不變 ⇒ gate 綠，而 golden 已 stale。那一格只有 operator 端的單元測試抓得到，而那些測試同樣沒有 runner。刻意不把函式體 AST 也雜湊：任何無關重構都會誤紅，噪音大的 gate 會被關掉。

🔴 **兩條 gate 都擋不住 merge**：repo-lint 不是 develop 的 required check（唯一 required 的是 `manifest-version-gate`）。它們的價值是「動手的人當場會看到」，不是機器擋住。

行為層另有測試釘住（平台 repo `src/tests/`）：`route` 缺席時**不得** fail-closed、舊 operator 不回 `dns_failed` 鍵時**不得**被當成失敗。這兩條是對照組，紅了就代表守線被改成用缺席當判準。

Refs #648 #484

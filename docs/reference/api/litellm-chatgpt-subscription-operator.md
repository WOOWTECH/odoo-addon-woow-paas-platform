---
title: LiteLLM「ChatGPT 訂閱」登入——operator 端點與 chart CLI 契約
category: reference
status: active（operator 端已實作；平台 UI 與 chart 0.2.0 各自的 PR 見文末）
created: 2026-10-07
updated: 2026-10-07
source-of-truth:
  - extra/paas-operator/src/services/chatgpt_subscription.py（白名單、名稱驗證、exec、結果分類、token 鍵刪除）
  - extra/paas-operator/src/api/chatgpt_subscription.py（route、403／422／500）
  - extra/paas-operator/src/models/schemas.py（ChatGPTSubscriptionRequest／ChatGPTSubscriptionResponse）
  - woow-paas-charts charts/litellm/files/woow_chatgpt.py（chart CLI，0.2.0 起）
related:
  - docs/reference/features/cloud-services.md
  - extra/paas-operator/README.md
---

# LiteLLM「ChatGPT 訂閱」登入——operator 端點與 chart CLI 契約

LiteLLM 的 `chatgpt/` provider 用 ChatGPT Plus/Pro 訂閱額度，登入是 OAuth device code
（借用 Codex CLI 的登入身分，**不是** OpenAI 官方的第三方方案；用量算租戶自己的訂閱、帳號風險
自負——平台 UI 要明講）。上游把登入代碼印在 pod log、租戶看不到；woow-paas-charts
`charts/litellm` 0.2.0 起在 proxy 容器內附一支 CLI，平台經 operator 的 `kubectl exec` 呼叫它，
把代碼顯示在服務頁。

```
平台服務頁「ChatGPT 訂閱」→ 平台 cloud.service._chatgpt_subscription
  → POST /api/releases/{namespace}/{release_name}/chatgpt-subscription/{action}（operator）
    → kubectl exec <litellm pod> -c litellm -- python /woow/py/woow_chatgpt.py <action> [args...]
```

## 1. operator 端點

```
POST /api/releases/{namespace}/{release_name}/chatgpt-subscription/{action}
```

- `action` ∈ `status` `start` `poll` `disconnect` `add-models` `test`；其他值（含 chart 自用的
  `restore`／`selfcheck`）→ **422**，不 exec。
- body 可整個省略：
  - `add-models`：`{"models": ["gpt-5.4", "chatgpt/gpt-5.5"]}`，最多 12 個；省略或空陣列＝chart 預設清單。
  - `test`：`{"model": "chatgpt/gpt-5.4"}`；省略＝第一個 `chatgpt/` 模型。名稱原樣傳給 CLI（不補前綴：
    `status.models` 回的是 LiteLLM 的 `model_name`，可能是別名）。
  - 其他動作忽略這兩個欄位。
  - 名稱必須 fullmatch `^(chatgpt/)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，否則 **200**＋
    `success:false, error_code:"invalid_model", error_kind:"not_executed"`（不 exec）。
- 找 pod：selector `app.kubernetes.io/instance={release_name},app.kubernetes.io/name=litellm,app.kubernetes.io/component=app`，
  container `litellm`；command `["python", "/woow/py/woow_chatgpt.py", <action>, *names]`。argv 只有指令與
  模型名（會出現在 operator 的 `Executing: kubectl …` log），沒有秘密、不用 stdin。
- exec 逾時：`status`／`start`／`poll`／`disconnect` 30 s；`add-models` 60 s；`test` 150 s。
  預算階梯：exec 逾時＋找 pod 餘裕 10 s ＜ 平台 client 逾時（45／90／180 s），由
  `tests/test_chatgpt_subscription.py` 驗算。
- namespace 不在租戶前綴 → **403**；缺 `X-API-Key` → 401；非預期例外 → **500**，
  `detail = {"message", "error_step": null, "error_kind": "unknown"}`（不帶 stderr／data）。

### 回應 `ChatGPTSubscriptionResponse`（業務結果一律 200）

```json
{"success": true, "data": {...}, "error": null, "error_code": null, "error_kind": null}
```

| 情況 | `success` | `error_code` | `error_kind` | `data` |
|---|---|---|---|---|
| CLI 印 `ok:true` | true | null | null | CLI JSON 去掉 `ok` |
| CLI 印 `ok:false`（exit 1；exit 2 的 `usage` 同） | false | CLI 的 `error`（見下） | `failed` | CLI JSON 去掉 `ok` |
| 名稱不合法／超過 12 個 | false | `invalid_model` | `not_executed` | null |
| 找不到 pod（或 release 名不是合法 label 值） | false | `pod_not_found` | `not_executed` | null |
| 舊 chart（0.1.x，或關掉 `chatgptSubscription`）：stdout 沒有 JSON，stderr 有 `woow_chatgpt.py`＋`No such file`／`can't open file` | false | `unsupported_chart` | `not_executed` | null |
| exec 逾時 | false | `exec_timeout` | `unknown` | null |
| 其他 exec 失敗且沒有 JSON、或輸出不可解析（最後一個非空行不是含布林 `ok` 的 JSON object） | false | `bad_output` | `unknown` | null |

`ok:false` 時的 `error_code`：CLI 的 `error` 若是碼的形狀（`[a-z][a-z0-9_]{0,63}`）就照用
（`device_code_request_failed`、`device_code_response_incomplete`、`poll_failed`、
`token_exchange_failed`、`token_response_incomplete`、`db_delete_failed`、`not_connected`、
`model_info_failed`、`no_chatgpt_model`、`internal`、`usage`）；否則依動作給穩定的碼：
`add-models` 的 `failed` 非空且沒有 `error` → `models_failed`；`test` 的上游訊息（例 401 提示）→
`test_failed`（原文在 `error` 與 `data.error`）；其餘 → `command_failed`。

### 防禦

- `data` 裡的 `access_token`／`refresh_token`／`id_token`（以及 device flow 的中間值
  `authorization_code`／`code_verifier`／`device_auth_id`）一律**遞迴刪掉**——CLI 本來就不印，這是第二道。
- `data.pending.user_code` 是要顯示給租戶的一次性代碼（15 分鐘），屬敏感但必要：operator 只把它
  放在回應裡，log 只記 action、success、error_code，**不記 stdout／stderr／data**。平台同樣不可寫進 log。
- 錯誤回應從不帶 kubectl 的 stderr 原文。

## 2. chart CLI（woow-paas-charts `charts/litellm` ≥ 0.2.0）

每個指令只在 stdout 印一行 JSON（`ok: true|false`；exit 0＝ok、1＝ok:false、2＝用法錯），絕不含 token。

| command | 成功輸出（例） | 失敗 `error` |
|---|---|---|
| `status` | `{"ok":true,"connected":true,"email":…,"plan_type":"plus","account_id_tail":…,"access_token_expires_at":…,"persisted":true,"pending":null,"models":["chatgpt/gpt-5.4"]}` | `internal` |
| `start` | `{"ok":true,"state":"pending","connected":<舊狀態>,"pending":{"verification_url":"https://auth.openai.com/codex/device","user_code":"ABCD-12345","expires_at":<epoch>,"interval":5}}` | `device_code_request_failed`（＋`status_code`）、`device_code_response_incomplete` |
| `poll` | `state` ∈ `pending`／`connected`／`expired`／`idle` | `poll_failed`、`token_exchange_failed`、`token_response_incomplete`（皆 `state:"error"`） |
| `disconnect` | `{"ok":true,"state":"disconnected","connected":false}` | `db_delete_failed` |
| `add-models [model ...]` | `{"ok":true,"added":[…],"kept":[…],"failed":[]}` | `not_connected`、`model_info_failed`，或 `ok:false` 且 `failed` 非空 |
| `test [model]` | `{"ok":true,"model":"chatgpt/gpt-5.4","seconds":2.1,"reply":"OK"}` | `no_chatgpt_model`；或 `{"ok":false,"model":…,"status_code":401,"error":"<上游訊息>"}` |

- poll 間隔＝`pending.interval`（5 秒）；代碼 15 分鐘失效（`expires_at`）。登入進行中的狀態存在 pod 的
  tmpfs（Deployment 是 `replicas: 1`＋`Recreate`），pod 換掉就要重新 `start`。
- 模型要**登入後**才加：LiteLLM 建 deployment 時就會取 token，未登入時加的模型存進 DB 但不上線。

## 3. 部署順序

charts PR（litellm 0.2.0 publish）→ **operator（本端點，先 stg 再 prod 換 pin）** → 平台 PR
（範本 `chatgpt_subscription_supported`、seed chart 0.2.0、服務頁卡片）→ 既有 LiteLLM 服務重新部署到 0.2.0。
平台遇到舊 operator 的 404 要顯示「功能尚未開放」；遇到 `unsupported_chart` 要提示重新部署。

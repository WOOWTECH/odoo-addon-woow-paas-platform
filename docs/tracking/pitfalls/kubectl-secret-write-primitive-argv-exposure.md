# 寫 Secret 的兩個原語只有一個不把明文放上 argv——而 log 遮蔽看起來像已經解決了

> 一句話：`patch_secret` 把 base64 payload 組進 `--patch=` **放上 kubectl argv**（`/proc/<pid>/cmdline`），`apply_manifest` 走 temp YAML、argv 只有檔名。兩支的 log 都被 `_redact_cmd` 遮得乾乾淨淨，所以「有沒有洩漏」在**任何 log 上都看不出差別**——選錯原語不會有任何訊號。
>
> **發現日期**：2026-08-29（#955 F-GC-1 實作前查證）
> **相關**：[[two-stage-remote-provision-idempotency]]（同為「operator 代管憑證」家族）

## 症狀

沒有症狀。這正是重點——它不會壞、不會紅、不會出現在 log 裡。要看到它，必須有人在 operator pod 上讀 `/proc/<pid>/cmdline`，或在能看到 process 列表的地方旁觀一次寫入。

## 機制

`extra/paas-operator/src/services/helm.py` 有兩支寫 K8s 物件的原語：

| 原語 | argv 長什麼樣 | 明文在哪 |
| --- | --- | --- |
| `patch_secret` | `kubectl patch secret <name> -n <ns> --type=merge --patch={"data":{"K":"<base64>"}}` | **argv 裡就是它**。base64 是編碼不是保護 |
| `apply_manifest` | `kubectl apply -f /tmp/xxxx.yaml --output json` | temp 檔（0600、用完即刪），argv 只有路徑 |

兩支都經 `_run_command`，而 `_run_command` 印 log 前會過 `_redact_cmd`（`_OPAQUE_PAYLOAD_FIELDS` 涵蓋 `--patch`）。所以：

- `tests/test_helm.py::TestSecretsInPayloadsNeverLogged::test_patch_secret_data_never_reaches_log` 是**綠的**，而且它同時斷言「subprocess 確實收到了 base64 後的真值」——那條斷言就是本 pitfall 的存在證明，只是當初寫的時候關心的是 log 那一面。
- 換句話說：**遮蔽測試綠 ≠ 明文沒有外洩**。它證明的是「log 讀不到」，不是「沒有人讀得到」。

## 判準（要寫敏感值進 Secret 時）

問的不是「這支有沒有遮 log」，而是「**這支會不會把值放上 argv**」：

- 走 `--patch` / `--from-literal` / `kubectl set env` / `helm --set` ⇒ **會**。⛔ 不要用。
- 走 `-f <file>` / `--values <file>` ⇒ 不會。✅

`kubectl set env` 還多一個面：值會落進 Deployment spec 的 literal `env[].value`，任何有該 ns `get deployment` 權限的人永久讀得到。

## 現況（#955 之後）

- `_provision_skill_bundle_secret` / `_provision_llm_secret` 都用 `apply_manifest`，docstring 寫明**為什麼不用 `patch_secret`**。
- `set_deployment_env` 對敏感 key 名 fail-fast（`_sensitive_env_key_marker`），唯一的洞是 per-call 顯式指名的 `allow_sensitive_keys`，只有 `agent_runtime_skill_bundle_secret_enabled=False` 的 rollback 呼叫點用它。
- `patch_secret` **沒有**同型 guard——它今天的呼叫端是 n8n API key（`n8n_init.py`），那條路徑本身就在這個 pitfall 的射程內，只是不在 #955 的射程內。⚠️ **這是已知未關的一格，不是已解決。**

## 為什麼機械化狀態是 pattern-guard 而不是 repo-lint gate

「這個值敏不敏感」是呼叫端的語意，不是 `patch_secret` 這支函式的性質。要靜態判定，只能猜參數名或猜字串內容——前者對 `data=payload` 這種變數傳遞完全無效，後者對任意字串必然大量誤判。一個必然大量漏抓的 gate 會讓後來的人以為這條已經守住了，那比沒有 gate 更糟（同 `odoo-object-button-return-value-is-an-action` 的判斷）。

改用 pattern-guard：編輯 `extra/paas-operator/` 的人會被要求先讀這份 doc。守的是「動到這裡的人一定看過這個坑」，不是「所有寫法都被偵測到」。

Refs #955

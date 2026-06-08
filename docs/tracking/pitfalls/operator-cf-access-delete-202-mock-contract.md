# Pitfall：operator Cloudflare Access delete 路徑——mock 假設 200、真 CF DELETE 回 202 → 誤判失敗 + 孤兒

> **一句話**：`paas-operator` 的 Cloudflare Access app/policy `DELETE` 把 HTTP 狀態白名單寫死成 `200`（policy）/ `(200,404)`（app），但**真 Cloudflare 的 Access DELETE 回 `202 Accepted`**（body `success:true`）→ 被誤判成失敗丟 exception；teardown「先刪 app 再刪 policy」的順序讓刪 app 在 202 就 raise → policy 清除迴圈沒跑到 → **孤兒 reusable policy**。**所有 mock 單元/整合測試全綠**（mock 回 200），**只有 live stg E2E（直接打 `PUT /api/access/sync`）才暴露**。
>
> **發現日期**：2026-06-08（Security Access P2 Access Control live stg 驗證）
> **修復**：PR #166（develop merge `38220bb`）+ operator image `20260608.0541`
> **相關 pattern**：[[two-stage-remote-provision-idempotency]]（孤兒防線）、[[unittest-mock-dual-namespace-patch]]（mock 形狀偏離真實 API 的同family）

## 三個串在一起的 bug（同根：mock 形狀 ≠ 真 CF 契約）

### Bug 1（major）DELETE 狀態白名單太窄 → 真 CF 的 202 被當失敗

```python
# delete_reusable_policy（修前）
if response.status_code != 200:          # ← 真 CF DELETE 回 202
    raise CloudflareException(...)
# _delete_access_app（修前）
if response.status_code not in (200, 404):   # ← 同樣漏 202
    raise CloudflareException(...)
```

實打 stg 證據（清孤兒時直接 raw delete 觀察）：

```
DELETE /accounts/{aid}/access/policies/{pid} -> 202
  { "result": {"id": "..."}, "success": true, "errors": [] }
```

CF 已**真的刪掉**，operator 卻回失敗。

### Bug 2（連帶）teardown 順序 → 孤兒

`sync_access_app` teardown path 是**先刪 app 再刪 policy**：

```python
if not enabled or not protected_hostnames:
    if existing_app_id:
        await self._delete_access_app(existing_app_id)   # ← 在 202 就 raise
    for pid in existing_policy_ids:                       # ← 永遠跑不到
        await self.delete_reusable_policy(pid)
```

Bug 1 讓刪 app 丟 exception → policy 迴圈被跳過 → **reusable policy 孤兒**。對 caller（Odoo）而言還收到「teardown 失敗」，SA 卡 error，但 CF 其實已刪 app。

### Bug 3（resync 變體）刪舊 policy 前沒先脫鉤 → in-use 拒刪 → 孤兒

resync path 原本「先刪舊 policy → create new → PUT app」。刪舊 policy 時它**還被 app 引用**，CF 拒刪 in-use（非 202 的錯誤）→ 被 `try/except` 吞成 warning → 孤兒。實證：同一筆 policy 在 app 脫鉤後 delete 回 202 success（可刪），證明先前刪不掉是 in-use 而非別的原因。

## 為什麼三層測試全綠卻沒擋住

| 層 | 為什麼遮蔽 |
| --- | --- |
| operator 單元測試 | `fake_delete` 全部 `status_code = 200`（假設值），與真 CF 的 202 不符 → 修前的窄白名單在測試裡「剛好對」 |
| operator 整合測試 | 同樣 mock CF（無真 cluster CI）|
| Odoo 後端測試 | `sync_access_app` 被 mock，根本不打真 CF |

**唯一攔截網 = live 真 Cloudflare E2E**（直接 `kubectl exec` 打 operator `PUT /api/access/sync` 走 create→resync→teardown 全程）。與 [[orm-test-green-endpoint-leak-blindness]] 同精神：**mock 綠 ≠ 真實契約 secure**，差別在這裡 mock 偏離的是**外部 API 的 HTTP 狀態碼形狀**。

## 額外抓到（對抗式審查，非 live）

- **(major) best-effort cleanup catch 太窄**：detach-before-delete 把刪舊 policy 移到 app PUT **成功後**（success path），但只 `except CloudflareException`——暫態 `httpx.TimeoutException`/`ConnectError`（**非** CloudflareException）會逸出 → 已收斂的 sync 失敗 → Odoo `savepoint` rollback 掉新 app/policy id 的寫回 → **DB↔CF 分歧 + 累積孤兒**（正是要修的失敗類別）。修：三個 cleanup 迴圈 catch 放寬到 `Exception`。
- **(critical) 測試假綠**：新增的 `test_..._raises_on_real_error` 漏 `@pytest.mark.asyncio`（pytest-asyncio strict mode）→ **靜默 skip**，從沒真的跑。改測試要 `-rs` 看 skip reason 防假綠。

## 修復（全 live 驗證）

1. `_delete_access_app` / `delete_reusable_policy` 接受 `(200, 202, 404)`
2. resync 改 **detach-before-delete**：create new → PUT app 指向 new（脫鉤舊的）→ **才**刪 old
3. 三個 cleanup 迴圈 catch 放寬到 `Exception`（best-effort 不該因暫態錯失敗已收斂的 sync）
4. operator 回傳 `orphaned_policy_ids`，Odoo `_sync_access_to_operator` log warning（對齊 `unresolved_idp_types` 訊號通道，孤兒可觀測）

**live 重驗（fixed operator 0541，真 CF）**：CREATE 200 + RE-SYNC 200（same app_id，`orphaned=[]`）+ TEARDOWN **200**（修前 202 誤判）`app_id=null`、`orphaned=[]`，CF 最終掃描零殘留。

## Token scope 前置（同場 live 發現）

operator 的 Access app/policy 是**帳戶級** endpoint（`/accounts/{aid}/access/{apps,policies}`）。stg token 一開始把「Access: Apps and Policies (Edit)」加在**區域 (Zone)** scope → create policy 回 `code 10000 Authentication error`。改成**帳戶 (Account)** scope 後通。**IdP 列表（`/identity_providers`）也是帳戶級，需「Access: Organizations, Identity Providers and Groups (Read)」**。詳見 follow-ups §F-SA F-SA-3 / F-SA-4（prod token 同前置）。

## 對照守線

- **凡 operator 包外部 API（Cloudflare / k8s / SaaS）的 DELETE/mutation**：mock 的 HTTP 狀態碼要對齊**該 API 真實回的狀態**（CF Access DELETE = 202；CF DNS/tunnel DELETE = 200，已由 P1 live E2E 實證，故那兩條 narrow `!=200` **不是** bug）。寫死假設值的 mock 會讓窄白名單在測試裡假綠。
- **凡先刪遠端資源再重建/重指向**：先脫鉤（repoint）再刪舊，否則 in-use 拒刪 → 孤兒。
- **best-effort cleanup 迴圈**：catch `Exception`（含暫態 transport 錯），別只 catch 自家 exception type；失敗要回報訊號（如 `orphaned_*`）讓 caller 可觀測，不要靜默吞。
- **新增 async 測試**：`-rs` 確認沒被 strict-mode 靜默 skip。

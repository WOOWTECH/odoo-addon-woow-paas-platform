# Pitfall：x2many `(5,0,0)+creates` 整組替換在「瞬時空狀態」觸發 child-model 的 eager 外部 sync → operator 收到不合法中途態 422

> **一句話**：編輯 SA Access policy 時 controller 走 `pol.write({'rule_ids': [(5,0,0)] + creates})` 整組替換 rule。Odoo **先把 `(5,0,0)` unlink 完整跑完（含 rule model `unlink()` 內的 `route._sync_access_to_operator()`）再跑 `(0,0,)` creates** → 那次 per-rule sync 在「舊 rule 已刪、新 rule 未建」的瞬時態把 policy 以 **`include:[]`** 推給 operator → operator 的「≥1 include」驗證回 **422** → 整個編輯 abort。`sa_rule_replace` context 當初只擋了 rule-level 的 `_check_at_least_one_include` **檢查**、**沒擋 unlink 的 operator SYNC**。
>
> **發現日期**：2026-06-13（SA P-rule 子模型上線後，user 在 stg 編輯 bypass policy「改不了」）
> **修復**：rule model 加 `_suppress_route_sync()`，create/write/unlink 在 `sa_rule_replace`（與既有 `sa_skip_route_sync`）時跳過 per-rule sync——policy 自己的 `write()` 在替換 settle 後做最終 full-state sync。
> **相關 pattern**：[[operator-cf-access-delete-202-mock-contract]]（同 family：**mock 不 validate → 全綠，只有 live operator 暴露**）、[[two-layer-acl-lockstep]]（check 與 sync 兩個 side-effect 要 lock-step 同步抑制）

## 為什麼三層測試全綠、只有 live operator 暴露

| 層 | 為何遮蔽 |
| --- | --- |
| 後端 ORM 單元測試（PR #193 原有 ~20 條） | mock 的 `sync_access_app` 是 permissive MagicMock，**不驗 include 非空**——中途瞬時態被外推也回 200，測試看不到。且多數測試用 `sa_skip_route_sync=True` 建 fixture，連 sync 都靜音。 |
| server-half odoo-shell 重現（本次 debug 初期） | 我第一版重現**也帶了 `sa_skip_route_sync=True`** → 把該爆的 sync 一起靜音 → 假性「include 留存」綠。**遮蔽變數要逐一拆**：只該設 `sa_rule_replace`（真實 controller 路徑），不要順手加 skip。 |
| Phase 5 live stg E2E（2026-06-13 上午） | **只測了 create / delete，沒測 edit**。edit 才走 `(5,0,0)+creates` 替換路徑——這條路徑從沒對 live operator 跑過。 |

唯一暴露點＝**對 live operator 跑 edit（rule 整組替換）**。`docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md` 的精神在此重演一次：**mock 綠 ≠ 對外契約綠**，差別在 mock 是否複刻真 API 的 validation。

## 根因：x2many 多命令 write 的執行順序 + child eager side-effect

`write({'rule_ids': [(5,0,0), (0,0,{...})]})` 時 Odoo **依命令順序逐一處理**：先 `(5,0,0)` 把現有 rule 全部 unlink（會呼叫 rule model 的 `unlink()`，其中 `route._sync_access_to_operator()` 在此觸發），**再** `(0,0,)` 建新 rule。所以 unlink 的 side-effect 跑在 policy「零 rule」的瞬時態。

stg 實證 stack（決定性證據）：
```
security_access_access_policy.py:226  in write       (處理 (5,0,0))
security_access_access_policy_rule.py:171 in unlink  ← sync 從 rule unlink 觸發
    route._sync_access_to_operator()
security_access.py:1248  client.sync_access_app(self._build_access_payload())
→ operator 422: input={'name':'test3','decision':'bypass','include':[],...}
```

spy 捕捉每次 sync 的 include 狀態（修前）：
```
call #1 (rule UNLINK (5,0,0)): test3 include=[]            ← live operator 在此 422 → edit abort
call #2 (rule CREATE (0,0,)):  test3 include=[{ip_range}]  ✓
call #3 (policy WRITE 最終):   test3 include=[{ip_range}]  ✓
```

## 修法

`security_access_access_policy_rule.py`：
```python
def _suppress_route_sync(self):
    # sa_skip_route_sync：migration / provision bulk move 各自 re-sync。
    # sa_rule_replace：policy-driven 整組替換，(5,0,0) unlink 的 per-rule sync
    #   會在瞬時空 include 態外推 → operator 422 → 整個 edit abort。policy 的
    #   write() 在替換 settle 後做最終 full-state sync，故 per-rule sync 既錯又冗。
    return (_is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC)
            or _is_sync_control(self.env, _CTX_RULE_REPLACE))
```
create / write / unlink 三處 sync 條件改 `if not self._suppress_route_sync():`。
修後 replace 從 **3 次 route sync 降為 1 次**（只剩 policy 最終 sync），且無瞬時空態。

> 🔴 **2026-08-19（#536）：上面那段是現況；原始版本寫的是
> `bool(ctx.get('sa_skip_route_sync') or ctx.get('sa_rule_replace'))`，而那是
> 租戶可偽造的** —— `/web/dataset/call_kw` 把呼叫端 context 原封不動換進 env，
> 所以只判 truthy 的 key，租戶在 JSON-RPC payload 多塞一行就打得開。兩個 key
> 現在都只認模組私有 sentinel（`models/security_access.py` 的
> `_INTERNAL_SYNC_CONTROL`），開閘一律走 `_skip_route_sync()` / `_rule_replace()`
> helper，**不要傳 `True`**（傳了不會報錯，只會無聲失去抑制）。
> 機制與判準見 [[su-env-exempt-guard-false-green]] 的「#536」段。

## 守線（下次遇到同形狀如何避免）

1. **child 子模型有 eager 外部 side-effect（operator push / webhook / 計費）時，凡是被 parent 透過 x2many 多命令 write 編排的 replace，child-level side-effect 必須在 replace context 下抑制，由 parent 在最終態統一觸發**——因為 `(5,0,0)+creates` 中途必有不合法瞬時態。
2. **抑制 replace 中途態時，「檢查」與「sync」兩個 side-effect 要 lock-step 一起抑制**。本 bug 正是只抑制了 check（`_check_at_least_one_include`）漏抑制 sync。
3. **mock 要複刻真 API 的 validation**（operator 對 `include:[]` 回 422 是對的 defense-in-depth；該 422 是好事，留著當 backstop）。單元測試應對 `sync_access_app` 加「任一 policy 空 include 即視為 bug」的斷言（見 `TestRouteAccessSync.test_rule_replace_never_syncs_transient_empty_include`）。
4. **重現 bug 時不要順手加遮蔽旗標**（`sa_skip_route_sync` 把 sync 整個靜音）——只設真實路徑會設的那個（`sa_rule_replace`），否則假綠。

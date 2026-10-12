# 安全存取修正候選：完整可執行範圍回歸

2026-10-12 UTC。原生測試已通過，來源與自有環境清理已由 root 核對；獨立覆核亦已接受本次原生執行範圍。**商用 NO-GO；尚未 merge 或 deploy。**

## 行為與本次修正

原產品候選修補分享身分、Access 發布與 OAuth 身分邊界：分享改為指定登入信箱的待接受邀請；CS／SA／SH 放行 origin 前驗完整 typed receipt；多筆取消保護先 park 全部並驗完全部結果，失敗保留 pending；OAuth 核發與使用均核對 active client 與 active non-public subject。

完整回歸找出的舊 fixture 維護以測試修改處理，相對已發布 commit `139d383d9601a7478ba0a2f2256d925332dd5da5`：41 個既有測試檔、新增 1 個邀請 helper；產品檔、既有 test method ID、collection baseline、known-failure baseline 沒有增刪或改名。分享正控制透過正確登入信箱的收件者明確接受，再核對 membership／role；Access 正控制回覆補齊型別與數量，負控制仍驗例外、不完整回覆及 context forgery；OAuth 正控制採用明確啟用的測試身分，停用身分的 HTTP 401 與四個 resource lookup 未被呼叫斷言保留。

Callback cleanup 的舊斷言誤把資料庫 lock timeout 下的未知付款當成可取消。修正維持未知交易 pending，並另建立具 durable payment error 的正控制，證明 replay timeout 後 cleanup 仍處理確定失敗的另一筆；兩位使用者都不增加 wallet points／topup transactions。沒有放寬產品 fail-closed 行為。

## 原生實測

第三輪於 2026-10-12 01:24 UTC 完成：**5,792 個唯一 test method，0 failed、0 errors、0 skips，exit 0**。實際 `Starting` 集合與真正 Odoo loader 的預期集合精確相同。官方預設 selector 範圍為 5,711 methods／1,039 classes；額外 opt-in 自有 DB 範圍為 81 methods／10 classes；本輪共 1,049 classes。

完整 selector：

```text
/woow_paas_platform,auto_recredit_864,billing_incident_864,callback_replay_864,double_charge_cooldown_864,failopen_794
```

純 loader universe 共 5,799 methods；其中 2 個 `ecpay_real` 真金流與 5 個真 browser case 未包含於本輪，不算通過。Browser 依賴補齊後，實際 Chromium 仍回報 `No usable sandbox`；需可正常使用 sandbox 的 runner 執行。

六個 C0 sentinel、C2 baseline 對帳、完整原始 log 的 C4 零 skip 與 C5 零新增失敗都通過。Stock C3 對擴充完整 log 報額外 10 classes，原始 RC1 保留；另將相同 raw log 的官方 1,039 class stats 及其原始子節點逐行投影，僅供 C3 對帳，結果通過。投影全部 8,830 行與原始行號逐 byte 相同，不改寫失敗／skip／result；完整 5,792 集合與結果以原始 log 為準，沒有更動 baseline。

來源為 948 檔，tree SHA-256：

```text
fe438f06c203b19b7b026fe3e2765d8b13cd41050be01269abade64d577d28d5
```

最終原生 log SHA-256：

```text
3fec8ab087435d60549760b8ec4fba230ffc53eb2212486c2e04bebc5f0a3178
```

Archive／upload／host-before／host-after／actual Pod-after 的 948 檔 path/hash map 完全一致。使用獨立 namespace、loopback PostgreSQL、deny-all NetworkPolicy、emptyDir、無 ServiceAccount token；正常刪除含 captured UID precondition，root 實際 GET 確認 namespace NotFound，沒有建立持久資源或修改 PV／StorageClass policy。

可核對的完整原始封存位於執行工作區 `woow-access-audit-20261011/full-v3-5792-root-seal/`，`root-verification.json` 保存來源、原始 log、投影、method multiset、清理及 hash。這不是 formal deployment receipt。

## 保留的失敗與限制

第一輪同產品候選的預設 5,693 tests 為 81 failures／147 errors；第二輪修補來源的完整 5,792 tests 為 1 failure／0 errors，兩輪原始結果保留。單一 callback 修補先通過 46 methods，再以第三輪全套驗證，不把不同來源的 focused runs 相加。

舊 collector 在建立 Registry 前設定 test_enable，可能自動執行 at_install；其硬寫 `testcases_executed:0` 已撤回作為純收集證據。新 collector 在 Registry 建立完成後直接使用真 loader，stdout／stderr 沒有 Starting／test stats／result；零案例 selector 與初版 C3 不完整投影的錯誤也保留。

本地 repo-lint、collection matcher self-test、C2 static audit 與 diff check 已通過；新增 helper 與 docs index 已納入最後提交檢查；最終 Git head 的 manifest gate、Git blob 與原生來源綁定結果另列於 PR。PR CI 不包含 Odoo runtime，需保留本次原生測試證據。

仍缺真 mailbox／SMTP／IdP challenge、5 個 browser case、正式 edge/origin convergence、PaaS 成員移除後 CF／VPN／HA／SSH 既有 credential／stream 的逐人撤權、最小權限及復原營運門檻。HA 真登入及自有 session 撤銷、HA／SSH 單次錯密碼拒絕有各自實測，但不替代跨服務撤權。Operator 原生測試依自己的 frozen source／node IDs 另外記錄。

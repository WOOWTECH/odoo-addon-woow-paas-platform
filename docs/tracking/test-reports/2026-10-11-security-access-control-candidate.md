---
title: 安全分享、Access 發布與 OAuth 身分邊界候選修正驗證
category: tracking
status: active
created: 2026-10-11
updated: 2026-10-11
---

# 安全存取候選修正驗證

候選修正讓工作區分享透過有效期限、單次使用、可撤銷的邀請完成；登入身分與指定信箱必須吻合，個人檔案信箱不能取代登入身分驗證。邀請管理沿用工作區角色與租戶邊界。

Cloud Service、Security Access 與 Smart Home 的六個 Access 同步／清除路徑，在更新本地識別碼與放行來源前驗證完整回覆。多筆取消保護先暫停全部來源，再驗證全部清除回覆，最後才逐組恢復公開；恢復失敗保留 pending 供重試。這避免遠端已刪除 Access app、資料庫交易卻回滾成 protected 時仍公開來源。

OAuth token 的使用與核發均要求有效 client、啟用中的非 public 使用者；有效 portal 使用者仍可正常使用。停用是即時有效性檢查，重新啟用後未過期且未明確撤銷的舊 token 可能恢復有效。

## 受測版本與結果

- Base：`3c46f593d4dd1c3de324332efb0def877f341e58`；manifest 保持 `18.0.1.0.226`，僅新增邀請信資料登記。
- Native Odoo：選定安全存取 645 個唯一方法、91 classes，0 failed、0 errors、exit 0；2026-10-11 21:58 UTC 完成。包含既有角色／租戶控制、邀請、發布失敗與重試、Access receipt 與 OAuth 控制。
- OAuth 真 HTTP：5 個 `HttpCase.url_open()` 方法，觀察 `/oauth2/token` 與 `/api/smarthome/workspaces` 的 200／400／401、token row 及身分邊界。這五案已包含於 645，另跑的 38 案與 focused tests 不重複累加。
- 前端 Node contract：10 passed，0 failed、0 skipped。先前 headed OWL 驗證屬歷史來源快照，不當作本輪完整端對端登入。
- Collection audit 的全 repo 靜態檢查、repo-lint、docs index 與 diff check 通過。PR CI 本身不執行上述 native Odoo tests。
- 947 個來源檔案的 canonical SHA-256：`201e1c4b2486d4b1fd5f751835f42eb1fd3edf58e1b4147fd45971c24c587643`。以相對 `src/` 路徑到個別 SHA-256 的 sorted JSON（compact separators）計算；最終 archive 不含 pycache 檔案。
- Source archive SHA-256：`e2965c8f4d5dc7b39e1daab21a0610dffff9f030419adc5e7cd8476b8c740239`；native log：`470a01805d051133540646c33fd3c32d4c0a94044b388661c1fac0902ddd1412`。
- 本次自建 namespace 使用 UID precondition 清理；root 再驗證 NotFound。未建立持久儲存資源，未修改 PV／SC／全域政策。

完整封存位於執行工作區 `woow-access-audit-20261011/combined645-root-seal/`，包含 source、exact collection、native receipt／log、uploaded source hashes、cleanup 與 root verification。歷史失敗、preliminary High 報告與較早成功快照保留；最後封存為本輪證據。

## 重跑與範圍

精確選取集合見 [645 方法／91 類](2026-10-11-security-access-control-collection.json)，[test-tags](2026-10-11-security-access-control-tags.txt) 可配合 [沙盒測試操作](../../guides/testing/TESTING.md)，在 sandbox Odoo 命令中指定相同 `--test-tags` 重跑。三個新方法的 15 個 subTest 情境不另加為 15 個方法。完整 addon 原生 suite 尚未執行；全量 log auditor 的六個 sentinel 不在這次選取集合，該 auditor 的 C0 不通過，不能拿本次選取集合宣稱全量差集成立。

## 失敗證據與測試調整

原始 receipt 反例執行 610 案、38 failed／6 errors。批次取消保護反例 16 案、3 failed／0 errors；修正後 focused 17 案與完整 645 案通過。原 OAuth HTTP 五案對舊產品有三案失敗，修正後五案通過。

舊 transport fake 補齊嚴格型別、policy／IdP 數量與 disabled 回覆形狀。兩個 unresolved-IdP 舊測試改為拒絕未完成回覆並保留 anchors／pending；原期待以個人檔案信箱取得分享權的三個漏洞診斷改為拒絕或信箱驗證流程。角色與跨租戶授權斷言沒有為取得綠燈而放寬。

## 商用驗收仍待完成

目前結論是 sandbox 原生 ORM／HTTP 與 desired-state receipt 契約驗證，商用驗收仍為 NO-GO。

- 暫停／發布 API 回覆不能證明 Cloudflare edge 已阻斷流量或完成傳播。
- 新增 per-identity 遠端狀態 fake：Cloud Service 雙服務、SA 同／不同 gateway、SH 同／不同 home，共五種分組；三個方法含 15 個實際完成標記，驗證 partial-second-delete、park failure、release failure／retry 的逐身分狀態與事件順序。包括精確 pending、成功組不重複放行、真正刪除 side effect 不重做、未納入操作的第三個 peer、資料庫 rollback；仍是模擬遠端狀態，不能代替真正流量阻擋。
- 需要可收碼的專用信箱或使用者完成真 Google 登入，才能驗證 SMTP 送達、OTP 與真正邀請加入流程。
- 移除工作區成員沒有完整接到 Cloudflare／VPN／HA local token／既有 WebSocket／SSH 的逐人撤權；未證明永久撤銷或既有連線中斷 SLA。
- 原始 HA／SSH 入口的保護選擇、TLS／HSTS、來源繞過、正式最小權限與全系統復原／營運驗收仍需完成。唯讀 Cloudflare 設定請求回覆 403，不能推論設定值或 ACM 授權。

本候選供人工覆核，未合併、未發版、未部署至 production。Operator 憑證 stdin／錯誤訊息修正屬獨立 [PR #1661](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/1661)。

# project 529 test false green batch0

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_529_test_false_green_batch0`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："2198 個 Odoo 測試稽核 → PR#529 修掉 10 處假綠已 merge；真問題不是「太多」是「2198 個從不執行」，批次 1-4 未動"

2026-08-02。13-agent workflow 稽核 `src/tests/` 全套（182 檔／2198 個 `def test_*`／
測試碼 55,568 LOC vs 生產碼 30,740 LOC ＝ 1.81:1），結論與落地：

**稽核結論**：密度**合理**（多租戶平台守租戶隔離＋金流＋K8s 生命週期，302 條 ACL/security
是必要的），冗餘只有 ~2%（45–50 條）。**真正的問題是這 2198 個測試沒有任何 CI 在跑**
——`.gitea/workflows/` 只有 build-paas-operator / docs-link-check / repo-lint /
trigger-odoo-build。所以「減量」省不到 CI 時間，動機只有維護成本與假綠風險。
⇒ 分類必須拆三層：**冗餘**（白付維護費）／**低價值**（資訊量低）／**有害**（假綠，
讓人以為某條線有守而實際沒有）。有害的即使個位數也優先於冗餘。

**已落地**：PR #529（merge commit `453f6e69`）修 10 處、駁回 3 處，7 檔 `+116/-42`，
全套回歸 `0F/0E of 2170`（develop 基準 2171，差 1 ＝刪掉的恆綠 parity 測試）。

**批次 1 也已落地**（2026-08-02 同日）：PR #534（A 組，3 條 `inspect.getsource` 子字串守衛
→ 端點行為測試，`0F/0E of 2193`）＋ PR #532（B 組，XSS 改守形態不守字面量，`0F/0E of 2197`），
兩者皆 merged（`cfe5cd7a` / `c61f7345`）。

✅ **批次 1 挖出一條比它自己更嚴重的待辦——已於 2026-08-03 收掉**（PR #541，commit `d5ad9d14`，
merge `d8bfcf32`）。原況：`src/tests/test_low_balance_notification.py:187` 的
`assertIn('_attempt_auto_topup', source)` 是**唯一憑據**——實測拔掉 `_try_settle_invoice`
裡的 `_attempt_auto_topup` 呼叫後跑全套 `0 failed of 2193`，**自動儲值可以整個被移除而
全套全綠**（碰的是金流）。#541 已改為行為測試；develop 上該檔仍出現的
`inspect.getsource` / `assertIn('_attempt_auto_topup', ...)` 字樣**全部是註解與 docstring**
（`:84`／`:174`／`:212`／`:213` 記錄「取代原本的…」），不是還活著的斷言——
grep 到它們別誤判成未修。
同檔 `:91` 相反：拔掉後 `TestLowBalanceDedupRetry` 紅 8 條，行為層有厚實覆蓋 ⇒ 同病不急。

⚠ 上面這段原本標「🔴 尚未裁決」，2026-08-03 leader6 收線前實查更正——**這正是
[[feedback_comment_env_state_claims_expire]] 說的第三種載體：驗證結論會過期成假話**，
而且它帶著實測數字與原始輸出，看起來完全可信。

🔴 **本次稽核的覆蓋缺口（2026-08-03 由 leader7 的 #544 揭露，我獨立驗證屬實）**：
「從未執行過的測試」全 repo 實際是 **62 條，我只找到 15 條（24%）**。
我的完備性檢查是「磁碟檔案數 vs `src/tests/__init__.py` import 數」——那個維度**是對的、也真的
抓到 15 條**，但它只能發現「**沒被匯入**」這一種機制。另有 47 條（`test_naming.py` 38 ＋
`test_reconcile_delete_gate.py` 9）**已匯入卻仍從未執行**：它們是純 `unittest.TestCase`
（非 Odoo `TransactionCase`/`HttpCase`）且無 `@tagged`，Odoo loader 的 at_install/post_install
根本不收集 ⇒ 我的查法**結構上看不到**。
（`test_reconciler_diff.py` 22 條混有 3 個 Odoo TestCase，不算在內。）
⇒ 這是 [[feedback_absence_conclusion_scope_not_universe]] 的變體，也是「**驗過一個維度不代表
其他維度也驗過**」的實例——而且**我那個維度通過了，反而讓我沒去想還有別的維度**。
⇒ 根治方向不是人工再盤一次（第三種機制還會出現），而是讓 CI gate 能列出
「哪些預期的 test class／module 不在**實際執行清單**裡」，見
[[feedback_completeness_check_output_which_not_how_many]]。

**未動（Eugene 未裁決）**：上述 `:187`、批次 2/3（刪除／合併 ~39 條）、批次 4（**15 條死測試**＝
`test_ai_assistant.py` 12 條＋`test_security_access_p_rule_migration.py` 3 條未被
`src/tests/__init__.py` 匯入，從未執行過；**先匯入跑一次再判，不要沒跑就刪**）。
另有 ~60 條 seed 測試宣稱「對齊 chart」實際只比對本 repo 自己（本 repo 零 values.yaml、
零 helm 呼叫）——`test_hermes_agent_seed_424.py:68` 註解寫「以 `helm show values` 向
registry 直驗」但檔內 `subprocess` 命中數為 0，是作者當時手動跑一次。

**教訓**：

1. **我的派工建議被 teammate 正確駁回**：詳見 [[feedback_test_pins_transient_fact_as_contract]]
   新增的「邊界」段——`EXPECTED_PLANS` 價格表的被測物**就是 seed 內容本身**，不是快照。
2. **blanket `except` 可以頂替 mock 而沒人發現**：`test_agent_pod_phase_a_cloud_service_mn.py`
   檔頭 Boundary 段宣稱 helm「mocked away」，**檔案裡從來沒有 mock**，是拿
   `except Exception: pass` 頂替；而 `_do_cleanup` 內部本來就自己 catch 了 helm 失敗
   ⇒ 外層那個 except 真正遮蔽的是**其後的一切**（state write、quota push、`unlink()`
   的訂閱結算）。**檔頭註解宣稱的邊界要當「待驗宣稱」而非事實**。
3. **恆綠守衛會掛著 invariant 的名號**：`test_mcp_acl_rule_count_parity` 用
   `search_count(...) >= 6`，且註解宣稱要 filter module-defined 而 **domain 裡根本沒
   這個條件** ⇒ 加規則永不紅、刪一條補一條恆綠。刪它之前逐條確認 6 條 xmlid 都被
   R.1/R.2/R.3 的 `env.ref`＋`assertTrue` 守住（覆蓋不損失）才刪。
4. **注入式驗證撞到「0F/0E 但 grep marker 0 筆」時**：那代表你無法分辨「例外被吞掉」
   與「根本沒跑到那段」——加 `_logger.error` marker 重跑，證明程式真的執行到 `raise`
   而測試仍報綠。teammate 這步做對了，是本次品質的關鍵。
5. **注入要打在被測物**（seed XML、controller domain、`ir_rules.xml`、`cloud_service.py`），
   不是改測試自己的斷言。

批次 1 再淬出四條**建守衛時**的設計模式（都來自 #534／#532，值得複用）：

6. **要判斷「某行為有沒有覆蓋」，逐點注入而不是籠統問**：Gatepost 把三個端點**分開**拔過濾
   ——cloud-services 拔掉造出 3F，smart-home／security-access 拔掉是 `0F of 2191`。
   籠統跑一次只會得到「有測試紅了」的假安心，分開注入才釘得死零覆蓋的**範圍**。
7. **gate 自己是新程式碼，必須配自檢**：Scour 為了守 XSS 寫了詞法掃描器，同時加 XSS.0
   ——對 9 種刁鑽輸入驗行為、驗 mask 真的遮掉字串內容、**被騙時要大聲失敗**、確認真的掃到
   >50 個檔。用容易寫錯的東西當守衛就得這樣做；不打算配自檢，就別做那個守衛
   （我因此裁掉 SSE.10 的手寫 brace matcher）。
8. **allowlist／豁免的 key 用「正規化後的原文」，不要用行號**：行號會被格式化重排改掉
   ＝製造新的假紅。並且要擋**殭屍豁免**（清單裡有已不存在的條目就紅）。
9. **排除第三方資產用具名清單＋封閉性斷言，不要用目錄式過濾**：`lib/` 式的目錄排除會讓
   日後任何丟進去的自家檔案自動繞過整個 gate。#532 的 XSS.4 斷言「`lib/` 實際內容必須
   等於這份具名清單」，實測往裡面丟含未消毒 `innerHTML` 的檔案會讓兩條同時紅。

相關：[[reference_odoo_empty_string_vs_false_vacuous_assertnotin]]（本次最有價值的
框架發現）、[[feedback_negative_verification_needs_positive_control]]、
[[feedback_test_asserting_absence_locks_shortcut]]、[[reference_repo_two_release_trains]]

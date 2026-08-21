# project cd77 module version gate

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_cd77_module_version_gate`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："cd#77 A′ 版本感知 initContainer 上 prod——用不變式取代 SKIP_MODULES_UPDATE 三段人治，-u 輸出終於可見"

**woow-paas/paas-app-bundle-cd#77 A′＋C2 已上 prod**（PR#81 6e0cc3f＋PR#82 94a3e67，2026-08-01；**issue 保持 open 到 .89 驗完**）。

**方案選擇的推理**（Eugene 問「A 是不是治本」時的裁決）：A（一次性 upgrade Job）治**流程形態**的本，但「忘記」只是變形（從忘記切 flag → 忘記跑 Job），且新增舊碼×新 schema 的偏斜視窗。**A′（版本感知 initContainer）才是治不變式的本**——「pod 起得來 ⟹ DB latest_version == image manifest 版號」由 pod 自己 boot 時保證，冪等、無人工順序依賴。C2（版號驗收）與 A′ 正交，A′ 之後降級為稽核性檢查但不可省略。

**gate 實作要點**：與主容器共用 image；DB 憑證從 odoo.conf 讀（bitnami 自己也走這條，故 conf 必然完整）⇒ 不必鏡射 secret；requirements 已全在 image venv ⇒ 不連外；讀不到 image 版號＝fail-closed 擋 pod（不靜默跳過）；**刻意不做逃生門**（GATE=off 會把「忘記切回」的人治風險原樣搬回，恢復路徑是回滾 image pin）。failure mode：Recreate＋replicas 1 ⇒ -u 失敗＝服務中斷，換取失敗看得見＋DB transaction rollback 不留半升級。

**我 review 加的那條（本週教訓的同形狀）**：`-u` 回 0 只證明「指令沒失敗」，不證明「DB 版號真的推進」——**要對側效果本身取證**（post-verify 重查 DB 版號，!= 就 exit 1）。同 [[29-389-mixin-sudo-dedup-landing]] 的「我做了動作 ≠ 側效果發生了」。注入式驗證＝把 odoo-bin 換成 `true` 應該紅。

**踩到的坑**：Fleet values 前處理 → [[fleet-values-go-template-preprocess]]（代價：gate image 退化成寫死 digest，新增人工同步點，靠 checklist §1b 守；根治見 cd#83）。

**完整驗收落在 .89 發版**（平台 PR#507 新 stored 欄位硬性需 -u）：要看到 gate log 出現 `Running migration` 與「完成並複驗」。本次 prod 首驗只走過「版本相等秒退」路徑。

## 🔴〔2026-08-03 更新〕那個「完整驗收」到現在還沒發生，而且接棒的是 #552

prod 實測（`paas-system/paas-odoo`）：
```
[module-version-gate] image=18.0.1.0.88 db=18.0.1.0.88
[module-version-gate] 版本一致，無需 module update；skip
exitCode=0  finishedAt=2026-08-01T15:39:59Z
```
⇒ 上面那句「完整驗收落在 `.89`」**到 2026-08-03 為止仍未兌現**——那次發版沒有發生，
prod 停在 gate 首次部署時的同一版，所以 gate **只走過「版本相等秒退」這一條分支**。
⇒ **下一次「image 版號 ≠ DB 版號」的發版，才會是這道 gate 第一次真的走 `-u` 分支**，
因此那次發版**同時是 cd#77 的首次實戰驗收**：要看 gate log 出現 `Running migration` 與「完成並複驗」，
**不能只看 pod 起來**。

⚠ **本節刻意不寫「prod 現在是哪一版、develop 現在是哪一版」**——那是快照，會被後人當成基準。
判斷「gate 這次會不會真的跑 `-u`」的正確做法是**當下自己量兩邊**：
```
image 側：git show origin/develop:src/__manifest__.py | grep version
DB   側：psql -tAc "SELECT latest_version FROM ir_module_module WHERE name='woow_paas_platform'"
```
（上方那段 gate log 是 **2026-08-03 的實測輸出原文**——它證明的是「相等就 skip 這條分支跑過」，不是「prod 應該是那一版」。）

⚠ **post-verify 驗的是「模組版號有沒有推進」，不驗「seed 欄位值有沒有寫進去」。** 兩者會因 sticky
`ir_model_data.noupdate` 分離：noupdate 仍為 true 時 `-u` 會**靜默 skip record 更新**（PR#561 實測重現：
第一次 `-u` exit 0、0 個 ERROR、四值仍空），而**版號照樣推進** ⇒ post-verify 通過 ⇒ pod 正常起來
⇒ **guard 生效＋seed 值仍空**。#552 那次的具體風險是「在售商品開通全掛且零紅燈」。
⇒ 發版驗收要**psql 讀回實際欄位值**（2026-08-03 實查 prod 那 15 筆 noupdate 全為 `f`，當下安全——但這是
「發版當下再查一次」的項目，不是可以引用的結論）。

## 🔴 `docs/guides/deployment/prod-release-sop.md` 尚未更新，仍教舊做法

SOP 最後更新 **2026-07-25**，A′ 上 prod 是 **08-01** ⇒ **過期的是 SOP**。它 §5.1 仍要求
`ODOO_SKIP_MODULES_UPDATE` 兩段式（PR-A 設 `"no"` → 驗證 → PR-B 設回 `"yes"`）＋ ≤30 分鐘曝險窗
＋收官三重確認——**而那正是 A′ 要取代的「三段人治」**。

⚠ 兩者是**不同機制、可以並存**：`ODOO_SKIP_MODULES_UPDATE=yes`（實測 prod 主容器仍是 yes）只管
Bitnami 主容器 entrypoint；gate 是獨立 initContainer，在主容器起來前自己跑 `-u`。

## 🔴🔴 但**不能因此廢掉 SOP**——兩者涵蓋範圍不同（本檔作者原先漏掉，dispatcher 補上）

```
gate                     : odoo-bin -u woow_paas_platform      ← 只有這一個模組
ODOO_SKIP_MODULES_UPDATE : Bitnami entrypoint 的全域開關        ← 可能涵蓋所有已安裝模組
```

⇒ **若某次發版的 seed 變更落在別的 addon**（相依的 `sh_subscription`／`ecpay` 系列等），
**gate 根本不會碰它們**，而 SOP 那條路會。⇒ **「gate 取代 SOP」只在「變更全部落在 `woow_paas_platform` 內」時成立。**

⚠ 本檔第一版把它寫成「照舊 SOP 做不會壞，只會白開曝險窗」——**那句不完整且會誤導**（讀成 SOP 可廢）。
病灶：我拿 #552 這個**單一 addon 的個案**去推**機制的通則**，而 gate 的 `-u` 目標寫死單一模組這件事，
我讀 script 時看到了卻沒把它當成範圍限制。

⚠ **未驗**：「`ODOO_SKIP_MODULES_UPDATE=no` 時實際升了哪些模組」**沒有人觀察過**，
上表右欄是讀 Bitnami 行為推的。要廢 SOP 之前必須先驗這一格。

⇒ 判準：**發版前看 seed diff 落在哪個 addon**。全部在 `woow_paas_platform` ⇒ gate 足夠；
只要有一條落在相依 addon ⇒ 仍需 SOP 那條路。

⇒ 這是 [[feedback_comment_env_state_claims_expire.md]] 的「處方最容易過期」在 SOP 層的實例：主結論
（有 seed 變更要跑 `-u`）仍然對，過期的是**怎麼跑**；但**過期不等於作廢**，新機制的涵蓋範圍要單獨量。

# project ci46 platform docs sync（已封存）

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_ci46_platform_docs_sync`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description："paas-odoo-ci#46 平台側文件同步收官（PR#511 merged）＋衍生 #510 待裁決；核心教訓＝把 source 層訊號寫成 image 層保證，等於在修這個缺陷的文件裡再犯一次"

paas-odoo-ci#46 的**平台側文件同步**收官：PR#511 merged（merge commit `449d4e31`，base develop），**不改程式碼**。CI 側 fail-closed 是另一條線（PR#49、#46 closed），見 [[reference_paas_odoo_ci_addon_ref_main_stale]]——那條是 CI 行為與 `addon_ref` 語意的權威載體，硬事實別在本檔重寫。

**續篇：ci#50 同步（PR#513 merged，merge commit `7b10c9bf`，2026-08-02）。** #50 補 image 端 gate 時**順手把 step 改名**，`Built addon version:` 這個字串從 CI 輸出消失 ⇒ 我上一輪寫進 SOP checklist／§5.1.4／坑 #12／deploy.md／pitfall 的驗收錨點**全部變成死字串**（照著發版的人會去找一行不存在的 log）。已改為三行錨點（`at ref:`／`Cloned addon version:`／`Image addon version:`）。

**這輪自己推導出的關鍵**（brief 未提，我認為比字串更新重要）：`Image addon version:` 句尾的 `matches clone` **只證明 image == clone，不證明 clone 是你要的 ref**。兩道 gate 正交——#50 擋「image ≠ clone」，#46 擋「`addon_ref` 留空」，而 **#46 只擋留空、擋不了填錯**。填成 `develop` 時三行會**一致地顯示錯版號、gate 全綠**，唯一露餡的是 `at ref:` 與「版號 vs 發版單」。⇒ 驗收指示一律寫成「三行都跟預期版號核對」，不是「看三行互相一致」。

落點：SOP `docs/guides/deployment/prod-release-sop.md` §1.1／§5.1 步驟 4／**§5.1.4 新增**／§9 坑 #12／§11 checklist 1.5，`.claude/commands/woow/deploy.md`，新 pitfall `docs/tracking/pitfalls/ci-addon-ref-empty-drifting-default-branch.md`。順手修掉坑 #9 已腐化的「無 approval gate」（早被同檔 §10 第 3 點的 2026-06-25 複驗推翻）。

**發現：整份 613 行 SOP 原本完全沒有「CI 失敗要重跑」段落**——grep 重跑/rerun/retry 只命中站點 smoke test 的重試。fail-closed 上線後重跑的人本來沒有任何文件可循。

## #510 待 Eugene 裁決（open）

`/woow:deploy-stg` 的 `addon_ref` 是**誤導性 no-op**：`SKILL.md:10/22/113-114/262` 宣告了它，但 `deploy-stg.sh` 只在 `:268`/`:396` **印出來**，從未進任何 CI payload（`:298` 建 tag 只帶 `tag_name`＋`target=<paas-odoo-ci main SHA>`），實際永遠烤 `develop`。建議 A=真的實作／B=從介面移除（傾向 B）。**SKILL.md 一個字未動。** 早在 2026-06-26 就被實測記錄於 `docs/tracking/followups/follow-ups-archive.md:17`，當時沒回寫到 SKILL.md。

## 核心教訓（被 review 抓下的活例）

第一版把 `Built addon version:` 寫成「從產出的 image 裡反讀」並宣稱「能擋掉 ref 對但 build context 錯」——查實體發現**剛好完全相反**（效力邊界見上面那條 reference memory）。

**把 source 層訊號寫成 image 層保證，等於在修這個缺陷的文件裡把同一個錯再犯一次**，而且傷害更大：下一個人會照著它做發版驗收、以為自己驗過了 image。諷刺點在於那份文件的 Class 級教訓正是「驗證鏈可以每一環都通過而整體無意義」。

⇒ **寫「這項驗收能擋住 X」之前，先去讀那個訊號的產生點**——它在 pipeline 哪一段、讀什麼、跑在什麼之前、失敗時擋不擋。**訊號的名字會誤導你，產生它的那幾行不會。** 已落 repo（pitfall Class 級教訓第 2 條含此活例＋SOP §5.1.4「已知極限」段）。

**同一條規則的第二個活例（隔天就應驗，已補進同一條 pitfall）**：那個名字不只誤導，一天後就被上游改掉了。⇒ **寫死外部系統的輸出字串＝把它的實作細節變成自己的契約，而對方沒有義務為你維持它。** 升級 checklist 時要問：**這條驗收依賴的字串，誰有權改、改了誰會知道？** 本案答案是「上游隨時可改、沒有人會通知」——所以它註定會再腐一次。

## 另一個自我更正

worktree base（`bf89fed7`）比同一輪稍早勘查的 `bdeb562a` 新 3 個 PR，pitfall 索引已從 35 變 36。我的「35→36」Edit 失敗時**一度誤判成工具不可靠**（受 [[reference_worktree_edit_tool_unreliable_multiagent]] 先入為主），查 `git show HEAD:` 才確認是自己的前提過時、正確值 37。⇒ **工具回報失敗時，先驗自己的前提再懷疑工具。**

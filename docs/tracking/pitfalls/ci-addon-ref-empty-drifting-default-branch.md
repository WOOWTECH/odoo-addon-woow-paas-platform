---
name: ci-addon-ref-empty-drifting-default-branch
description: 人工重跑 build 忘填 addon_ref → CI clone 移動中的預設分支 HEAD；image_tag 與 digest 都是新的、CI 全綠、pod digest 閉環也全對，但 image 內容是另一個版本，要到 pod 跑 -u 才現形
metadata:
  type: pitfall
---

# Pitfall: build 產物「三方閉環全對」，但 image 裡裝的是另一個版本

> 來源：2026-08-01 paas-odoo-ci issue #46。發版路徑本身沒問題，出事的是**人工重跑**：
> `build-image.yml` 的 `addon_ref` 留空時，CI 會 clone `woow_paas_platform` 的**預設分支**
> 而不是本次發版的 tag。整條 CI 綠燈、產出的 image tag 與 digest 都是全新的，
> 現行 SOP 的每一項驗收都通過——但 image 內容與發版紀錄脫鉤。

## Symptom

- 人工 `workflow_dispatch` 重跑 paas-odoo-ci 的 `build-image.yml`，**全綠**。
- 產出 `image_tag=YYYYMMDD.HHMM`（新的）、該 tag 的 `sha256` digest（也是新的）。
- 照 SOP 把 tag + digest pin 進 CD、Fleet reconcile、驗 pod `imageID` == 預期 digest、
  `BundleDeployment nonModified=true` —— **三方閉環一項不缺，全部通過**。
- 直到 pod 起來跑 `-u`（或事後查 image 內 `__manifest__.py`）才發現：裝進去的程式碼
  **不是**發版單上那個版號。
- 發版紀錄寫「image X ＝ v18.0.1.0.88」，實際內容是 .89 開發中的 commit，**溯源整條斷掉**。

## Root cause

四件事疊加，每一件單獨看都合理：

1. **`addon_ref` 留空的 fallback 是「repo 預設分支」**，也就是 `develop` —— 一個
   **移動中的指標**，不是不可變錨點。同一組輸入在不同時刻跑會烤出不同 image，
   而且必然含有尚未發版的 commit。
2. **「預設分支是誰」不在版控裡**。它是 Gitea 的 repo 設定，CI 零校驗。
   今天指向 `develop`（漂移、含未發版 commit），哪天被改成 `main` 就直接變成
   **版號倒退**（`main` 長期落後 `develop`）—— 同一個缺陷、兩種失效形態，
   而且**切換的當下沒有任何訊號**，沒有 PR、沒有 diff、沒有 CI 紅燈。
3. **所有既有驗收信號對此全部無效**，這是它能靜默的關鍵：

   | 驗收項 | 為什麼擋不住 |
   |--------|-------------|
   | CI 綠燈 | 只證明「有 build 成功」，不證明「build 的是誰」 |
   | `image_tag` 是新的 | tag 是 CI 自己產的 UTC 時間戳，與 addon 版號無關（SOP §9 坑 #4 已知） |
   | `digest` 是新的 | 坑 #4 給的解法。但**新 build 本來就產生新 digest** —— 對「烤錯 ref」零鑑別力 |
   | pod `imageID` == pin 的 digest | 只證明「跑的是我 pin 的那顆」，**不證明那顆裝的是我要的程式碼** |
   | Fleet `nonModified=true` | 只證明「live 沒漂離 git 宣告態」，宣告態本身指錯照樣全綠 |
   | `Image addon version:`（#50 後新增的 image 端 gate） | **對本缺陷仍然無鑑別力**：clone 到錯的 ref 時，image 就是照著那份錯的 clone 烤的 —— 兩邊版號**完美相符**、gate 全綠。它擋的是「image ≠ clone」，與本缺陷（「clone ≠ 你要的 ref」）**正交** |

   整條驗證鏈證明的是**內部一致性**，沒有任何一環把 image 內容錨回「本次要發的那個 ref」。
   #50 補上 image↔clone 這一環之後，這句話**依然成立**——它錨的是 image 對 clone，
   不是 image 對「你要發的那個版本」。錨回意圖的只有 `at ref:` 那一行、`addon_ref`
   的 fail-closed（#46），以及「把版號拿去跟發版單核對」這個人工動作。
4. **只有人工路徑會留空，而人工路徑正是出事時的補救動作**。正式發版
   （`.gitea/workflows/trigger-odoo-build.yml:14,26`）從 tag 名推導 `TAG_REF`、恆帶
   `addon_ref`，結構上不可能為空 —— 所以平常永遠不會踩到。踩到的時機是
   **build 失敗、approval gate 撞 Gitea 5xx 陣亡之後急著重跑**：注意力全在「讓它跑起來」，
   最不可能停下來檢查一個看起來可選的輸入欄。

## Fix

- **CI 層 fail-closed**（paas-odoo-ci `build-image.yml`，issue #46）：`addon_ref` 留空且
  非 `stg-*` 路徑 → 直接 `exit 1`，要求呼叫端明示 ref。
  **`stg-*` → `develop` 的 fallback 刻意保留不動** —— `deploy-stg.sh:298` 建 tag 時從不傳
  `addon_ref`，完全依賴這個 fallback，一併收掉會讓 `/woow:deploy-stg` 立刻全壞。
- **build log 三行可驗錨點**（前兩行 #46 同批、第三行 #50 新增）：

  | 行 | 層 | 證明 |
  |---|---|---|
  | `Cloning … at ref:` | source | clone 的是指定的那個 ref |
  | `Cloned addon version:` | source | 該 ref 的 `src/__manifest__.py` 版號 |
  | `Image addon version:` | image | 產出 image 裡那份 addon source 的版號 == clone 側版號 |

  **前兩行同源、不互相獨立** —— 都出自同一次 `git clone`，`Cloned addon version:` 讀的是
  `/tmp/woow_paas_platform/src/__manifest__.py`（clone 原件，連 build context 裡那份複本
  都不是），step 跑在 `docker build` **之前**。

  **第三行是 image 端 fail-closed gate**（#50）：`docker run --rm --entrypoint cat` 從剛
  build 出來的 image 讀 `/opt/bitnami/odoo/custom-addons/woow_paas_platform/__manifest__.py`
  與 clone 側比對，四種情況 `exit 1`（版號不符／讀不到 manifest ＝`COPY` 路徑或 build
  context 錯／解析不出版號／clone 側基準缺失），**位在 `Push image` 之前 ⇒ 不符的 image
  進不了 registry**。

  ⚠ **它的 `matches clone` 只證明 image == clone，不證明 clone 是你要的 ref**（見上方
  Root cause 表最後一列）。**三行都要拿去跟本次預期版號核對，不是核對三行互相一致。**

  **剩下的缺口**：錨到的是**版號字串**這一層。同版號但檔案被動過、addon 以外的內容
  （vendored `addons/`、`requirements-paas.txt`）、以及「registry 上之後被 pin 的是不是
  本次驗過的這一顆」（屬 digest pin 範疇），都不在範圍內。
- **平台側 SOP 補驗收與重跑路徑**（本 pitfall 同批，#50 後同步更新）：
  - `docs/guides/deployment/prod-release-sop.md` §5.1.4 —— 為什麼 tag/digest 驗不出來、
    三行錨點各證明什麼、已知極限、CI 失敗要重跑的正確做法（整份 SOP 原本**完全沒有**
    重跑段落）
  - 同檔 §9 坑 #12、§11 checklist step 1.5（STOP gate，#50 後降級為**複核**）、§1.1 CI 觸發欄
  - `.claude/commands/woow/deploy.md` —— flow 明示 `addon_ref` 來源 + 重跑必帶

## Guard

- **CI 兩道 fail-closed**（`paas-odoo-ci/.gitea/workflows/build-image.yml`）—— 主守線，
  但**在另一個 repo**，本 repo 的 `scripts/audit/repo-lint.py` 管不到：
  - `Validate addon ref`（#46）：`addon_ref` 留空且非 `stg-*` → `exit 1`。**擋本 pitfall 的缺陷**，
    但只擋「留空」，擋不了「填錯」。
  - `Verify image addon version matches clone (image side)`（#50）：image 版號 ≠ clone 版號
    → `exit 1`，位在 push 之前。擋的是**另一類**（image ≠ clone），與本 pitfall 正交。
- **SOP §11 checklist step 1.5** 是平台側 gate。#50 之後它從「唯一防線」**降級為複核**——
  image↔clone 不符 CI 自己就會擋；人工核對真正不可取代的部分是**「`at ref:` 與版號 vs 發版單」**，
  因為 ref 填錯時 CI 三行會一致地全綠。
- 本 repo 無 lint gate：唯一的呼叫端 `trigger-odoo-build.yml` 已恆帶 `addon_ref`，
  沒有可 grep 的缺陷形態；真正的風險在**人工操作**與**跨 repo 設定**，兩者都不在本 repo 可判定範圍。

## Class 級教訓

1. **「build 成功」與「build 的是誰」是兩個獨立命題**，CI 綠燈只證前者。任何以
   「CI 綠 → 產物正確」為前提的驗收都有這個缺口。
2. **驗證鏈可以每一環都通過而整體無意義**。digest 閉環證明的是「pod 跑的是我 pin 的那顆」，
   pin 證明的是「我寫進 CD 的值」—— 沒有一環把 image **內容**錨回**意圖**。
   設計驗收時要問的不是「有沒有查」，是「這項查驗在缺陷發生時會不會變紅」。
   本案的答案是：全部不會。

   > **這條教訓的活例，就發生在本 pitfall 自己的修復過程中。** 本文件與 SOP 的第一版把
   > `Built addon version:` 描述成「從產出的 image 裡反讀」，並宣稱它「能同時擋掉 ref 對
   > 但 build context 錯」。去讀 `build-image.yml` 實體才發現：它讀的是
   > `/tmp/woow_paas_platform/src/__manifest__.py`（clone 下來的原始碼），而且跑在
   > `docker build` **之前** —— 那個宣稱剛好完全相反。
   > **把 source 層的訊號寫成 image 層的保證**，等於在修這個缺陷的文件裡把同一個錯再犯
   > 一次，而且傷害更大：下一個人會照著它做發版驗收，並以為自己驗過了 image。
   > 由 PR #511 的 review 抓下（2026-08-01）。
   >
   > 可帶走的規則：**寫「這項驗收能擋住 X」之前，先去讀那個訊號的產生點**——
   > 它在 pipeline 的哪一段、讀的是什麼、跑在什麼之前、失敗時擋不擋。
   > 訊號的名字（`Built addon version`）會誤導你，只有產生它的那幾行不會。

   > **第二個活例（2026-08-02，同一條規則的另一面）**：上面那句「訊號的名字會誤導你」
   > 一天後就應驗得更徹底 —— paas-odoo-ci #50 補 image 端 gate 時，**順手把那個誤導的
   > step 改名了**（`Record baked addon version` → `Record addon version from clone (source side)`，
   > 理由正是 `baked` 讓人以為 image 端已驗過）。於是 `Built addon version:` **這個字串
   > 從 CI 輸出裡消失**，而本文件與 SOP checklist 把它當驗收錨點寫死了 ——
   > 下一個照著發版的人會去 log 裡找一行**不存在**的字串，要嘛卡住、要嘛就直接放行。
   >
   > ⇒ **寫死外部系統的輸出字串，就是把它的實作細節變成自己的契約**，而對方沒有義務
   > 為你維持它。這類引用要嘛標明來源與查證日期（讓下一個人知道該去哪裡復驗）、
   > 要嘛就別寫死。**升級 checklist 的同時要問：這條驗收依賴的字串，誰有權改、改了誰會知道？**
   > 本案的答案是「上游隨時可改、沒有人會通知」——所以它註定會再腐一次。
3. **「fallback 到預設分支」＝ 把一個不在版控的設定變成 build 的隱含輸入**。
   不可變錨點（tag / commit SHA）與移動指標（branch HEAD）在 build 系統裡語意天差地遠，
   前者可重現、後者不可；讓後者當預設值等於預設放棄可重現性。
4. **fail-open 的預設值，會在最糟的時機生效**。它只在人工介入時才有機會觸發，
   而人工介入通常發生在「已經出事、正在補救」—— 那正是注意力最稀缺、最不會停下來
   檢查可選輸入的時候。這類預設值該 fail-closed。
5. 宣告了卻不生效的參數是同一類病的另一面：見「相關」的 `deploy-stg` 條目。

## 相關

- 索引：[docs/tracking/pitfalls/README.md](README.md)
- 發版 SOP：[docs/guides/deployment/prod-release-sop.md](../../guides/deployment/prod-release-sop.md)
  §5.1.4（build 產物驗收與 CI 重跑）、§9 坑 #12、§11 checklist step 1.5
- 發版指令：`.claude/commands/woow/deploy.md`
- 上游 issue：paas-odoo-ci #46（fail-closed 實作與 build log 錨點）
- **同形缺陷（另案）**：`.claude/skills/deploy-stg/SKILL.md` 宣告了 `addon_ref` 參數
  （`:10`/`:22`/`:113-114`/`:262`），但 `deploy-stg.sh` 從未把它放進任何 CI payload
  （`:268`、`:396` 只是印出來），實際永遠烤 `develop`。同樣是「宣告的行為與實際不符」，
  但屬 deploy-stg 的獨立缺陷。早在 2026-06-26 就被實測記錄於
  `docs/tracking/followups/follow-ups-archive.md:17`，當時沒回寫到 SKILL.md。

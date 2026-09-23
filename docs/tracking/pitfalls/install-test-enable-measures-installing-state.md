---
name: install-test-enable-measures-installing-state
description: "`-i <module> --test-enable` 讓 test 在安裝流程尚未完成時執行，造成與安裝完成後再 `-u` 不同的假性紅燈"
metadata:
  type: pitfall
---

# Pitfall：`-i <module> --test-enable` 測到安裝中的狀態，不等同 upgrade 後行為

> `-i <module> --test-enable` 會讓 test 在模組安裝流程尚未完成時執行。若 test 依賴 seed 或 `post_init` 才建立的資料，這條路徑會量到中途狀態；它不等同「先安裝完成，再對同一個 DB 做 upgrade 測試」。

## Symptom

同一份程式碼走兩條測試路徑，結果完全不同：

- fresh install 直接使用 `-i woow_paas_platform --test-enable`：121 紅（29 failed + 92 error）。
- 已安裝 DB 的 upgrade 路徑：0 failed。

121 條全部停在同一個錯誤：

```text
odoo.exceptions.UserError: No journal could be found in company My Company for any of those types: sale
```

這不是 121 個獨立 regression。當時的 `sale` journal 要到 seed／`post_init` 才產生，而 test 在安裝流程抵達該步驟前就已執行。

## Root cause

### 1. install 與 install 完成後的 upgrade 是不同狀態

把 `-i` 與 `--test-enable` 放在同一次 invocation，量到的是「模組正在安裝」的狀態，不是「模組已安裝完成」的乾淨環境。依賴安裝尾段資料的 test 因此會在資料尚未存在時失敗。

已安裝 DB 的 upgrade 測試則自然先具備完整安裝結果，再執行 `-u` 與 test。也就是說，**升級路徑常常無意間就是對的**；若只看到它全綠、卻不知道兩條路徑的狀態差異，換成 fresh DB 時仍會用錯測法。

## Fix

fresh install 的驗證要拆成兩步，並對同一個 DB 執行：

```bash
odoo -d <newdb> -i woow_paas_platform --without-demo=all --stop-after-init
odoo -d <newdb> -u woow_paas_platform --test-enable --test-tags '/woow_paas_platform' --stop-after-init
```

第一步只完成安裝，不帶 `--test-enable`；第二步才在安裝完成後，以 `-u` 跑模組 test。這樣測到的狀態才與既有 DB 的 upgrade 行為可比。

### 2. 先查 DB 最終狀態，再完成對照實驗

當時直接查兩個 DB 的最終狀態：

```text
失敗的 DB（安裝完成後）：sale journal = 1
全綠的 DB：sale journal = 1
```

兩邊最終都有一筆 `sale` journal，先排除了「fresh install 最後仍缺 journal」的假設，也指出**失敗當下狀態不等於最終狀態**。這比等待整組對照實驗更早把問題定位到安裝時序。

但 DB 直查不能取代對照實驗。同一個 DB 改用 `-u` 重跑並讓 121 紅歸零，才能排除「journal 只是眾多缺件之一」；若另有缺件，第二輪仍會失敗。因此，直查用來提早定位原因，對照仍須跑完。

## How to avoid／守線

### 3. 紅燈時用收集總數判定問題層級，但必須鎖定同一份程式碼

本次兩條路徑在**同一份程式碼**（`73a022ac`）都收集到 2426 tests；一邊全綠、另一邊 121 紅。這組證據指向執行期的環境狀態，而不是 collection 期靜默 deselect，也讓錯誤集中在 journal 的歸因站得住。

「同一份程式碼」不可省略。若兩條路徑的程式碼不同，即使總數相同，也可能是一邊多收 X 條、另一邊少收 X 條的巧合。因此不能一般化成「case 數相同，就一定沒有 collection 問題」。

### 4. 排他性宣稱同時需要集合證據、哨兵與運算自證

要宣稱「121 紅裡沒有一條屬於四張修法的新增 test」，需要一起提供：

```text
交集：四張的 55 條 ∩ 失敗清單 = 空集合
哨兵：四張的 55 條在該輪 55/55 都有執行
```

只報交集 0 不夠。若 55 條根本沒有執行，交集同樣是 0；表面輸出相同，意義卻相反。55/55 哨兵用來證明被排除的集合確實進入該輪測試。

還要對交集運算本身做注入式驗證：從「55 條與失敗清單的聯集」取樣後再執行同一個交集流程，結果必須非 0。原理是餵入已知應命中的資料，確認欄位格式、正規化與集合方向真的相容；否則空集合可能只是檔案順序寫反或格式不匹配造成的恆空。

### 5. 聯集全套的結論不得超出 test 涵蓋範圍

聯集全套能證明的是：**在這 2426 條 test 涵蓋的範圍內沒有交互作用**。它不涵蓋：

- 共用 model 欄位；
- `ir.rule`；
- `config_parameter`；
- 只在真實 K8s 才出現的交互作用。

報告必須保留這個邊界，不能改寫成「四張之間絕無交互」。

### 機械化狀態

狀態為 `—`。這是測試流程與執行時狀態的判斷：需要比較安裝時序、DB 狀態、同一份程式碼下的收集集合、哨兵與注入驗證，無法單靠 grep 靜態判定。應以測試紀錄與對照實驗守線，不新增會產生假安全感的機械化 gate。

## Related

- [sandbox-module-test-false-green.md](sandbox-module-test-false-green.md)：`0 tests` 也報綠是不同症狀；本篇是 test 確實有收集，卻在安裝中途狀態執行。
- [tag-scoped-test-misses-full-u-failures.md](tag-scoped-test-misses-full-u-failures.md)：`--test-tags` 會限制驗證範圍；本篇只連結該範圍陷阱，不重述其內容。

## 來源

以下是 issue #582 保存的歷史結果，**不是本次文件整理重新執行的結果**：

| 項目 | 歷史結果 |
| --- | --- |
| develop | `73a022ac`／`18.0.1.0.109` |
| 路徑① upgrade（`.105` → `.109`） | 0 failed, 0 error of 2426；哨兵 55/55 |
| 路徑② fresh install（兩步） | 0 failed, 0 error of 2426；哨兵 55/55 |
| 已知 flaky `test_partial_unique_index_present` | 三輪都未觸發（修法 PR #581 未合，不阻擋發版） |
| 四張新增 test 數 | 55 = #566 12 ＋ #568 8 ＋ #570 26 ＋ #569 9（三個獨立來源對帳一致） |

- 原始 issue：[odoo-addons/woow_paas_platform#582](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/582)
- 原 issue 參照：#554、#525、#563。

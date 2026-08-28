> **出處**：dev-803 於 #876 併入 develop 後寫的交接文件（2026-08-23），自 worktree scratchpad
> promote 進 repo——scratchpad 會在 worktree 收割日被清掉，那是本檔唯一的副本。
> 內文**一字未改**（原檔 sha256 `c098d3e2106f5f941d536343d7512d0759325056b5f5d5bf69dd83dda77eec26`）。
> ⚠ 檔內「⚠ 讀之前：狀態已經變了」那段**是內容的一部分**——它記錄的正是本文件寫到一半時
> PR 已被合併、前提改變這件事，不要當成過時說明刪掉。
> 相關：#803 / PR #876（merged 2026-08-23，merge commit `b8432ebf`）、
> `scripts/audit/verify-cron-wiring.py`、follow-ups `F-803-1` / `F-803-2`。

# #803 接手包（dev-803 → 接手者）

**這份是路由不是倉庫**：只放「不重做調查就動得了手」需要的東西，指標一律指向 repo 內的
權威來源。凡是會腐爛的量（版號、筆數、sha、頻率的依據）一律寫**怎麼重新量**，不寫讀數。

產出時間（UTC）：2026-08-23T08:18:27Z

## ⚠ 讀之前：狀態已經變了

本檔的委託前提是「Eugene 覆核**退回**要改，接手的人不是你」。實際狀況是
**PR #876 已於 2026-08-23T06:04:52Z 由 `eugene` 合併**（見第 6 格）。
所以這份不再是「改 PR」的包，而是**已落地程式碼的維護包**——退一步改、或下一輪
擴充時的起點。第 1 格三題中，**1.1 頻率已裁**（Eugene 2026-08-22）、1.2／1.3 未裁——後兩者
現在是既成事實的預設值。三者的狀態與後果見格 1。

---

## 格 1：三個拍板題（**1.1 已裁，1.2／1.3 未裁**）

| 題 | 狀態 |
| --- | --- |
| **1.1 頻率＝15 分鐘** | 🔴 **已裁決**。裁決者 **Eugene**，日期 **2026-08-22**。（#876 呈 Eugene 是**覆核落地對不對**，不是重開這題。） |
| 1.2 被 skip 列的重試策略 | **未裁**。以下是 dev-803 推出來的預設值與理由。 |
| 1.3 停權旁路 | **未裁**。以下是 dev-803 推出來的預設值與理由。 |

### 🔴 1.1 已裁這件事，後果比裁決本身重要

**頻率已經被 `noupdate="1"` 鎖死。** 那筆 ir.cron 記錄寫在
`src/data/access_resync_cron.xml` 的 `<data noupdate="1">` 裡 ⇒ 模組升級**不會**更新
已存在記錄的 `interval_number` / `interval_type`。要改只能改 DB 或另外寫一支 migration。

⇒ 接手的人若誤以為「這題還沒裁」而動手改頻率，會**同時**撞上兩件事：

1. **改不動**——改了 XML 也不會生效（而且是靜默的：模組升級成功、值沒變、零訊號）。
2. **推翻一個已經拍過的板**——那是 Eugene 2026-08-22 的定案，不是預設值。

**要改頻率，走的是「重開一個已裁決策 ＋ 寫 migration」兩道程序，不是改一行 XML。**

### 這一格的來歷（留著，因為這個形狀會再出現）

本檔第一版把三題**全部**寫成「還沒裁」——那是照抄了委託訊息裡的一句話。
而 #803 工單第 1 節逐字寫著「✅ 已定案：**15 分鐘**，Eugene 2026-08-22 拍板」。
兩個來源矛盾。

當時的處置是：**兩邊都寫、標出矛盾、講出實際後果、要求發令的人自己裁**，
而不是「以最新指示為準」。後者會把一個已拍板的決策在備份文件裡降級成未決，
接手的人拿到的就是一份**會讓他去推翻 Eugene 的文件**——而且不會有任何錯誤訊息。

leader5 2026-08-23 裁定：**1.1 已裁**，委託訊息裡那句「三題都還沒裁」是轉寫時
把「覆核點」外推成「未裁」所致。

下面 1.2 / 1.3 標的「傾向」一律是 **dev-803 的判斷**，不是 Eugene 的裁決。


### 1.1 頻率選型

| 選項 | 代價 |
| --- | --- |
| **15 分鐘**（已落地） | 對 operator 的加壓面比 cf reap 的每小時密 4 倍。靠「批次上限 `limit` ＋ gate 過濾」壓住，不是靠頻率。健康系統上旗標列是個位數 ⇒ 絕大多數輪次是一次 `search` ＋ 一行 log。 |
| 每小時（抄 cf reap） | 一個對外可達、本來要求登入的服務最多多裸露 1 小時。⛔ cf reap 的推導理由逐字是「待回收物件**不影響任何線上流量**」——那個前提在本單**不成立**，抄它等於抄一個不適用的前提。 |
| 每日（抄 subscription_lifecycle） | 同上但更嚴重。 |
| 事件驅動（旗標打開當下就重推） | 沒有這條路：旗標唯一的寫入者是 fail-open 補償，而它跑在**刪除交易已經回捲**之後的獨立交易裡——那一刻重推正是剛剛失敗的那件事。 |

**裁決：15 分鐘（Eugene 2026-08-22）。** 拍板時的推導理由是不對稱（見格 2 第 4 段）：
太密可回復、太疏不可回復。⚠ 這不是 dev-803 的傾向，是既定值——**上線後改不動**
（`<data noupdate="1">`），見本格開頭。

### 1.2 被 gate 擋掉的列，重試策略

| 選項 | 代價 |
| --- | --- |
| **每輪照樣掃、照樣試、擋掉就算 gated**（已落地） | 停權租戶的旗標列每 15 分鐘被走一次（一次 `_access_sync_skip_reason()`，零外呼）。彙總行會一直顯示 `gated>0` 並升 WARNING ⇒ **長期停權的租戶會讓那行每 15 分鐘 warn 一次**。 |
| 加「卡多久算不正常」的時間基準 | 三腿旗標是純 Boolean、**沒有時間欄位** ⇒ 要做就得新增欄位＝新增 migration＝被迫 bump manifest（#691 連鎖），形狀變太多。issue-leader 已裁「第一版不做」。 |
| 終態淘汰（把 `deleting` 之類永久終態排除出 domain） | 終態定義**未查證**。好處是那只改 domain、**不受 `noupdate` 綁**，有資料隨時可加。 |

**dev-803 傾向：維持現狀，但把「WARNING 每 15 分鐘重複」列為第一個該回頭看的地方**
（見格 5）。終態淘汰是成本最低的下一步。

### 1.3 停權中要不要開 force 旁路

| 選項 | 代價 |
| --- | --- |
| **不開**（已落地） | 停權／`deleting` 中的旗標列收斂不了，旗標一直留著。但那是**實話**——那一刻對外根本沒有東西可達。 |
| 開 `_force_access_sync` | 在停權中／`deleting` 中把 Access app 建回去；與 #536 處理過的 force 偽造面衝突；而且會消滅一個 UI 正在用來說「保護未生效」的訊號。 |

**dev-803 傾向：不開**，且理由 1（「被擋掉時保護沒生效本來就是實話」）在 SA 腿
**已查證成立**——查法與限制見格 5。

---

## 格 2：15 分鐘的五段推導 —— **怎麼重新量**，不是結論數字

⛔ 下面每一段都不寫讀數。要重新論證這個值，逐段照做。

### 第 1 段｜窗口什麼時候打開

**量法**：`grep -rn "_access_mark_protection_lost" src/` → 找出**呼叫者**（不是定義處，三腿
各有一個同名定義）。今天只有一個呼叫者，在 `cf_access_policy_reap.py` 的
`_compensate_fail_open_after_rollback` 內、獨立交易。
**要重驗的性質**：呼叫者是不是仍然只有一個？多一個就代表窗口有新的開啟途徑，
第 3 段的「旗標列是個位數」可能不再成立。

### 第 2 段｜自然關閉路徑

**量法**：讀 `src/models/security_access.py::_access_mark_protection_lost` 的 docstring
（它逐字列出那三種事件），再 `grep -rn "_sync_access_to_operator()" src/ | grep -v def`
枚舉實際呼叫點。
**要重驗的性質**：那三種是否仍**都需要人動手**。若有人加了自動觸發點，本 cron 的
必要性就要重新論證。

### 第 3 段｜每輪代價

🔴 **這一格的量是 live 的，必須在目標環境現查，不得沿用任何讀數。**
**量法**（prod / stg 的 Odoo shell 或 DB）：三張表各數一次
`access_protection_lost = TRUE` 的列數，並拆成「gate 擋得住的」與「推得動的」。
外呼次數 ≤ 推得動的那部分，且受 `limit` 管（**逐腿**各算一次，見格 5）。
**判準**：若旗標列不再是個位數，「絕大多數輪次是一次 search ＋ 一行 log」這個前提垮掉，
頻率與 `limit` 都要重算。

### 第 4 段｜不對稱

**量法**：這一段是質性的，但它踩在一個**可查證的前提**上——「被 gate 擋住時對外沒有東西
可達」。查法見格 5 的「SA 停權可達性」。前提若不成立，太疏那一側的代價就不只是
「多裸露一段時間」，而是「一直是開的」，整個推導要重來。

### 第 5 段｜結論值的參照

**量法**：`grep -n "interval_number\|interval_type" src/data/reach_reconciler_cron.xml`
（平台內既有「安全相關收斂」的節奏）。
⛔ **不要拿 `cf_access_reap_cron.xml` 當參照**——理由見格 1.1。
⛔ **也不要拿它的 XML 註解當五段推導的範本**：那份寫的是「為什麼要有這條 cron」。
真正的五段式範本在 `src/models/cf_access_policy_reap.py` 的 `_STUCK_PENDING_DAYS`
註解（用 `grep -n "_STUCK_PENDING_DAYS = " -B 12` 定位，⛔ 別記行號）。

---

## 格 3：pre-flight gate 探索（撞到／繞過的，各自判準）

### 3.1 🔴 `check_provision_sentinel_allowlist` —— **掃註解，不分程式碼**

**這格最容易原地重踩。** 判準：用 regex 掃**整個檔案**（不區分註解與程式碼），命中
`_INTERNAL_PROVISION` / `_internal_provision` / `woow_internal_provision` 任一字面、
而檔案不在 allowlist 上 ⇒ 違規。

掃描 root 是 `("src/", "scripts/", "extra/", ".claude/")` × 副檔名
`(".py", ".sh", ".js", ".xml")`（現查：`grep -n "_PROVISION_SENTINEL_ROOTS" repo-lint.py`）
⇒ **`src/data/*.xml` 也在掃描範圍內**。

⇒ **想在 cron XML 註解裡寫「本 cron 不走 #519 的 provision 開閘」這種說明，會把自己
種進違規清單。** 這與「PR body 裡寫『為什麼不關 issue』會把 close token 種進去」是
同一型：**解釋一件事的動作本身生產出那件事的識別字**。

**繞法**：換個講法，別寫那三個字面。#803 本來就不該碰 provision sentinel。

### 3.2 `check_data_noupdate`

新增帶 `noupdate="1"` 的 data 檔會被擋，訊息自己把 cron 列為正當案例，要跑
`repo-lint.py --update-baseline` 收錄。
⚠ **`--update-baseline` 有兩支同名旗標、行為不同**：
* `scripts/audit/repo-lint.py --update-baseline` ⇒ **一次重產四檔**（會把別人合法的調降
  靜默帶進你的 commit）
* `scripts/audit/test_collection_audit.py --update-baseline` ⇒ **只寫 test-collection.txt 一檔**

⇒ 跑完**逐檔** `git show HEAD:<f>` 對帳「只增不減」，並在報告裡說清楚用的是哪一支。

### 3.3 `check_internal_ctx_key_registry`（#788）

守的 bug class 是「拿 context key 當『這是內部呼叫』的憑證、判定只看 truthy」，而且
**新寫的一把根本不經過 runtime WARNING**。
⇒ ⛔ 不得為了通過 identity guard 而發明新的 context key。要走的是 `_internal_identity` /
`_internal_sm_route_identity` / `_internal_cs_access_identity` 這種**sentinel 物件**路線。
本 cron 沒有自己碰身分錨點——它呼叫 `_sync_access_to_operator()`，回存那段本來就走 sentinel。

### 3.4 `test-collectability`（`check_test_module_collection` ＋ `check_test_class_execution`）

新 test class 必須 `@tagged('post_install', '-at_install')` 且在 `src/tests/__init__.py`
被 import。判準是**逐 class** 不是逐 module（混合檔會被點名到 class）。

### 3.5 `check_assertlogs_numeric_level`

`assertLogs` 的 level **必須傳數值**（`logging.WARNING`），不可傳字串。
Odoo 的 `addLevelName(25, "INFO")` 覆寫了名稱→數值表 ⇒ 字串會解析錯，
`assertLogs` 恆紅、`assertNoLogs` 恆綠。

### 3.6 `manifest-version-gate`（唯一的 required check）

#691 改制後：**一般 PR 的版號必須「等於」develop**，不是 bump（只有新增 migration ／
`release/*` PR 才 bump）。
⚠ **base 每次前進都要重驗這一格**——兩邊改成同一個值時 git **不報衝突、結果卻錯**。
判準：`git diff origin/develop -- src/__manifest__.py | grep -E '^[+-].*"version"'` 應為空。
**驗完做陽性對照**（暫時改一個字元 ⇒ 應出現 2 行 diff），否則分不出「沒改」與「判準壞了」。

### 3.7 `test_collection_audit.py` 的 C2

新增 test class ⇒ baseline 要有對應行。#836 之後它多了一段「條數 0」的獨立列名區——
mixin（非 TestCase 子類、無 `def test*`）會出現在那段，**那是正常的不是問題**。

### 3.8 CI 觸發面（覆核者最容易誤讀的一格）

`odoo-tests.yml` 的 `on:` **只有** `push`(develop) / `schedule` / `workflow_dispatch`
——**沒有 `pull_request`** ⇒ **PR 的 CI 綠燈不含任何 Odoo 測試**。
判準用**讀 workflow 的 `on:` 區塊**，⛔ 不要用「CI 清單裡沒看到」推論。

---

## 格 4：改了哪些檔、每個檔為什麼（一行一檔）

| 檔 | 為什麼 |
| --- | --- |
| `src/models/access_resync_cron.py` | **新增**。AbstractModel（不存資料⇒無表⇒不需 ACL）宿主 ＋ `_cron_resync_access_protection()` 游標式三腿掃描。docstring 裡有「為什麼是 AbstractModel 而不是 Model」「為什麼預算逐腿計」「為什麼不開 force 旁路」「旗標由誰清」四段設計理由。 |
| `src/data/access_resync_cron.xml` | **新增**。ir.cron 記錄（15 / minutes、`<data noupdate="1">`）。🔴 **五段推導的唯一落點就在這個檔的註解裡**——model docstring 只放一行指針，刻意不雙寫。 |
| `src/tests/test_access_resync_cron_803.py` | **新增**。四個 class × 5 條。檔頭有「判別式紀律」四格（patch 點三腿不同、assertLogs 數值 level、⛔ 不用 assertRaises 驗例外後 DB 狀態、blocker 必須是永遠推不動的列）。 |
| `src/models/__init__.py` | 一行 import ＋ 一行 why 註解（照檔尾慣例）。 |
| `src/tests/__init__.py` | 一段 why 註解 ＋ 一行 import（照檔尾慣例）。⚠ 這個檔是**檔尾 append 衝突的常客**，merge develop 幾乎必撞，解法一律「兩側 import 全保留」。 |
| `src/__manifest__.py` | `data` 清單加一行 cron XML ＋ 一行註解。**`version` 一個字都沒動**。 |
| `scripts/audit/baselines/data-noupdate.txt` | 收錄新的 noupdate data 檔（見格 3.2）。 |
| `scripts/audit/baselines/test-collection.txt` | 四個新 class 各 5 條。 |

---

## 格 5：我知道但沒寫進 PR 的東西

### 5.1 試過但放棄的做法

* **注入式紅燈的第一版**：leader5 原本指定「把 `_internal_identity` patch 成 passthrough」。
  我推過一遍**沒有真的跑**就放棄了——那會讓守衛**拋例外** ⇒ sync 中斷 ⇒ 旗標不會被清 ⇒
  A2/B2/C2/D1/A5/B5/C5 全部跟著紅，隔離不出 D5。改用「**回存被靜默丟棄**」
  （把 SA route 那行回存改成把值丟掉），sync 照樣成功、旗標照樣清，只有 `access_app_id`
  沒落地 ⇒ 只有 D5 紅。**下次要驗這一格，用後者。**
* **k8s dev sandbox 路線**：查到 chart 預設要現拉四顆本機沒有的 image
  （`odoo:18.0` / `pgvector/pgvector:pg15` / `nginx:alpine` / `bitnamilegacy/kubectl`），
  當時磁碟只剩 ~9 GiB。探索過 `--image <本機已有的 paas-odoo> --mode local` 這個組合
  （`create.sh` 的 usage 明說 `--image` 隱含 ci-local「**除非搭 `--mode`**」），
  但**沒有實跑驗證**，兩個風險沒查：image 內 bake 的 addon 與 hostPath 掛進去的那份在
  `addons_path` 上誰贏；08-18 的 image 內第三方 addon 是否仍滿足現在的 `depends`。
  最後走純 Docker 容器路線（見 5.4）繞開整件事。
* **`_access_sync_skip_reason()` 自己判 gated**：想過在 cron 裡先呼叫它來分類，放棄了
  ——那會**複製 gate 判定**。改用 `_sync_access_to_operator()` 的回傳值
  （#528 刻意讓「被擋掉」回 `False`、「推了而且成功」回 `True`），判定仍只有一處。

### 5.2 我覺得脆弱的地方（按我的擔心程度排）

1. 🔴 **彙總行在 `gated>0` 時升 WARNING** ⇒ 長期停權的租戶會讓它**每 15 分鐘 warn 一次**
   （96 次／日）。這正是 `_warn_if_deferred` docstring 要避的洪水形態，只是量級小得多。
   我實作的是 leader5 核可的分級，但**我自己有疑慮**：`gated` 是常態（停權租戶），
   `failed` 才是異常。**如果只改一個地方，我會改這裡**——把升級條件改成只看 `failed`，
   `gated` 留在 INFO（計數照樣印，訊號不丟）。
2. **D3 / D4 斷言的是精確計數**（`scanned=5`、`scanned=0`）。若哪天測試 DB 裡有殘留的
   旗標列（seed 或別的測試沒清乾淨），這兩條會紅，而**紅的理由與程式碼無關**。
   要更耐用的話，把斷言改成「本測試建立的那些列的計數」而不是全域計數。
3. **A5/B5/C5 的建立順序**是這三條測試的全部價值所在（blocker 必須佔住較小 id）。
   我第一輪就踩過（見 PR body 的 RED 段）。**任何人新增第四條腿、或重構 setUp，
   都可能無聲地把它弄回去**——症狀是那三條照樣綠，但什麼都沒守。
   前提斷言（`assertTrue(all(b.id < target.id ...))`）是唯一的防線，**不要刪它**。
4. **預算逐腿計** ⇒ 一輪最壞情況是 `3 * limit` 次外呼。這是刻意的取捨（共用預算會讓
   第一腿餓死後兩腿），但如果 operator 那側對 QPS 敏感，這是第一個要重算的數。

### 5.3 「如果要退一步改，我會從哪裡改」

* **只想降低雜訊** ⇒ 改 5.2 第 1 點（WARNING 升級條件）。不動 `noupdate`、不動 domain。
* **想收斂得更乾淨** ⇒ 加**終態淘汰**（把 `deleting` 之類永久終態排除出 domain）。
  成本最低：只改 `_resync_leg` 的 domain，**不受 `noupdate` 綁**。前置是先查證終態定義。
* **想加時間基準** ⇒ 要新增欄位 ⇒ migration ⇒ manifest bump ⇒ #691 連鎖。形狀變很大，
  應該另開單而不是塞進本單。
* **想整條停掉** ⇒ 把 ir.cron 的 `active` 設 False。⚠ 但那筆記錄在 `noupdate="1"` 裡，
  改 XML **不會**更新已存在的記錄——要改 DB 或走 migration。

### 5.4 復現驗證環境的 recipe（環境已銷毀，重建用）

不用 k8s sandbox，純 Docker 兩個容器，**兩顆 image 本機已有 ⇒ 零拉取**：

```
docker network create wt803
docker run -d --name wt803-db --network wt803 \
  -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -e POSTGRES_DB=postgres postgres:16-alpine
docker run -d --name wt803-red --network wt803 \
  -v <worktree>/src:/opt/bitnami/odoo/custom-addons/woow_paas_platform:ro \
  jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo:20260818.0400 bash -c "sleep infinity"
# 自寫 /opt/bitnami/odoo/conf/odoo.conf（image 內沒有；addons_path 三段、db_host=wt803-db）
```

⚠ 三個實測到的坑：
* **`bash -lc` 會把 PATH 整條洗掉**（`/etc/profile` 覆寫不是 append）⇒ `which odoo` 找不到。
  用 `bash -c`。
* **`pgrep -c -f "odoo -c"` 會抓到自己那條 `bash -c`**（命令列裡就有那個字串）⇒ 永遠回非零，
  「還在跑」與「跑完了」不可區分。改用「log 裡有沒有寫入完成標記」當判準。
* **跑測試要帶 `--log-handler=odoo.tests.stats:DEBUG`**，否則沒有 Detailed Tests Report，
  任何基於它的集合比對都會報「一條都沒跑」——**那是假紅**，長得跟 tag 選型錯誤一模一樣。
  （`woow-test.sh` 沒帶它，而且把 `--test-tags` 寫死成 `/$MODULE` ＝全模組。）

窄跑法：`--test-tags "/woow_paas_platform:TestSaRouteAccessResync,…"`（四個 class 逗號分隔），
已實測對 `post_install` class **選得中**。

### 5.5 環境的已知偏差（重建時會再遇到）

* DB 用 `postgres:16-alpine` 而非平台 chart 的 `pgvector/pgvector:pg15` ⇒ 安裝期
  `ai_base_gt` 全程 `pgvector extension not available`（WARNING，模組自行降級）。
* 全模組回歸會有 **3 個 error**，全部是 `AGENT_RUNTIME_NAMESPACE_SECRET is not configured`
  ⇒ **#853**。🔑 **根因是容器缺 env 不是碼**：另一個 agent 的容器有設
  `AGENT_RUNTIME_NAMESPACE_SECRET=integration-test-secret`。設上去應該就會消失，
  **但我沒有實際設上去跑過來證明**。
* 安裝期另有兩個既有紅（`res_company.sh_journal_id` 無法設 NOT NULL、
  `sh.subscription.report` 無 `_description`）。**我沒有確認它們是否屬於 #804 / #853**，
  當時判斷是第三類。

### 5.6 SA 停權可達性（格 1.3 理由 1 的依據）—— 查法與限制

**結論：SA 的 `suspended` 與 SH 的「停權 ⇒ ingress 已 503」是同一個機制，不是類比。**

查法（可重跑）：
1. `_do_suspend()` 的實作：`_sync_routes_to_operator(override_service_url='http_status:503')`。
2. `grep -n "'state': 'suspended'" src/models/security_access.py` ⇒ 確認**只有一個寫入者**
   （`_do_suspend` 自己），而它推 503 失敗時寫的是 `'error'` ——`error` **不在** gate 清單內
   ⇒ ingress 沒凍住的那一格 cron 反而推得動，方向是對的。
3. `grep -rn "_sync_routes_to_operator" src/` 枚舉呼叫點 ⇒ 三個 route 層呼叫點
   （create/write/unlink）都帶著**與 Access gate 完全相同的** state 清單
   ⇒ 停權期間沒有任何路徑會解凍 ingress。
4. `deleting` 只在 tunnel 確認消失後才寫入（`_do_cleanup`，清不乾淨時寫 `error`）。

🔴 **限制**：以上全是**靜態控制流閱讀**，**沒有**對停權中的 SA hostname 發過任何 HTTP 請求、
也沒看過 503 回應。operator 側 `sync_tunnel_routes` 的實作若與此處假設不符，或有人手動改過
CF ingress，本分析都看不到。

### 5.7 順手撿到、值得寫進別的單的東西

* **`_do_resume` 有一段真實的「hostname 活著、Access 可能不在」狀態**：它是「先還原
  ingress、再重推 Access」，後者拋例外時 state 寫 `error` 但 ingress 那半已在 CF 生效、
  回捲不掉。`error` 不在 gate 清單內 ⇒ **本 cron 涵蓋它**（這是支持本 cron 的正面論據，
  已寫進 PR body）。
* **cron 接線的靜態驗可以做成 repo-lint 的一條 check**：method 名拼錯時 `ir.cron` 每次到點
  都**靜默失敗**，而「安裝成功／data 載入無誤／單元測試直呼全綠」三件在拼錯時**全部照樣
  成立** ⇒ 三個綠燈都看不見那一格。我寫的一次性檢查器在
  `<scratchpad>/803-check-cron-wiring.py`（AST ＋ `xml.etree`，列名不用 `grep -c`，
  對全 repo 17 筆 ir.cron 都有效，三格陽性對照驗過）。**尚未開單。**

---

## 格 6：分支現況（**全部現查**，⛔ 未引用任何舊讀數）

```
查詢時間（UTC）：2026-08-23T08:22:35Z

branch          feat/803-access-resync-cron
worktree        /Users/eugene/Documents/woow/AREA-odoo/woow-addons/wt-803

# 遠端分支當下指向（git ls-remote，打遠端；rev-parse 對此無鑑別力——
#  30+ worktree 共用同一個 .git，別人 fetch 成功也會更新你的本地 ref）
  96b214aa294ce93f21dc9ac1f2528d0b99d50f0b	refs/heads/feat/803-access-resync-cron
  ls-remote exit=0

# develop 當下指向
  bcf37935c0cd5578e68165d480f3af8be7f41327	refs/heads/develop

# 本地 worktree（刻意與上面並列，讓落差看得見）
  local HEAD                    851a33b874b9d8d16dcdfb5d69520c696f7d2f51
  local git status --porcelain  ''   ← 空＝乾淨
```

### 🔴 遠端分支上有一顆我沒看過的 commit

本地 HEAD 是 `851a33b8`，但遠端分支現在指向另一顆。它是：

```
  96b214aa Merge branch 'develop' into feat/803-access-resync-cron
  parent1 = 851a33b874b9d8d16dcdfb5d69520c696f7d2f51   ← 我的 851a33b8
  parent2 = d559aaa44ad51a0456b1b414865d8861425a9062   ← develop
```

⇒ 是 Gitea 的 **Update Branch**（純 develop 併進來），**不是有人改了我的碼**。
驗法（blob 物件同一性，比 diff 強）：

```
  access_resync_cron.py              我的=f1fbd0c2fb43 PR頭=f1fbd0c2fb43 develop=f1fbd0c2fb43 同一物件
  access_resync_cron.xml             我的=b4e8bee5789e PR頭=b4e8bee5789e develop=b4e8bee5789e 同一物件
  test_access_resync_cron_803.py     我的=6feccf57f9a8 PR頭=6feccf57f9a8 develop=6feccf57f9a8 同一物件
```

### PR 現況（權威：API，不是畫面）

```
  PR #876  state=closed  merged=True
  merged_at        2026-08-23T06:04:52Z
  merged_by        eugene
  merge_commit_sha b8432ebfff3b4afb29d1683fabce1d291fad6d13
  head.sha         96b214aa294ce93f21dc9ac1f2528d0b99d50f0b
```

⇒ **已合併。** 標題上的 ⛔ 勿代 merge 約束不到 repo 擁有者，這是既有的已知性質。

### 三個 commit（本單自己的產出）

```
  851a33b8 Merge remote-tracking branch 'origin/develop' into feat/803-access-resync-cron
  7860490d chore: merge origin/develop（d9bad876）——解 tests/__init__.py 檔尾 append 衝突，兩邊 import 都保留
  c4b5b483 feat: #803 Access 保護的週期性收斂 cron（三腿掃 access_protection_lost，15 分鐘）
  de653924 test: #803 修 A5/B5/C5 的建立順序——blocker 必須佔住較小的 id
  694ef356 test: #803 Access 週期性收斂 cron 的 RED 守線（三腿各一套＋掃描形狀四條）
```

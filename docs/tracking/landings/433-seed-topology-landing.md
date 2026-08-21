# project 433 seed topology landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_433_seed_topology_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#433 seed 拓撲硬編斷言收斂（PR#439）——12 個斷言點改真不變式，`>=` 修法被實測推翻"

2026-07-26，#433 收斂 seed 拓撲硬編快照斷言，PR **#439** 已 merge（develop `4686eefd`），#433 auto-close。

**驗收硬證**：baseline `develop 58213aeb` = `6 failed, 2 error(s) of 1799 tests`；PR = `3 failed, 2 error(s) of 1800 tests`。修好的 3 個恰為 A/C/D，**新引入失敗＝空集合**；殘存 3F/2E 即 #437 / #436 兩類。A 的 XML 解析修法在 fresh install 與 `-u` **兩軌都實測通過**（有 `Starting` 行證明真的執行，非跳過）。

處理 **12 個斷言點**（5 個已紅 + 7 個未爆）。issue #433 內文**漏列了 `test_plan_group_keys_368.py:74`**（17→24），因為同 method 的 :42 先炸擋住了。

## 核心決策：`assertGreaterEqual` 被實測推翻

issue 工作項 2 原建議改 monotonic `>=`（依據既有 pattern `phase-versioned-test-assertion`）。**不採用**——「seed 只增不減」是假的：`d13e4602`（PR #341，#336 Wave A）在主線上**零 record 增刪、純翻 `storefront_hidden`** 就讓可見 base 從 32 掉到 26。另有 `active_test` 過濾（`sh.subscription.plan` 有 `active` 欄位，M2M 讀取會靜默濾掉封存者）與 seed xmlid 真刪（`2bcea232`）兩個縮水機制。

`>=` 對「集合被靜默縮小」最寬容，而**縮小正是這裡的主要威脅**——守衛會裝反。也不弱化成 `> 0`：這些計數守的是**覆蓋率**不是數量（`assertFalse(taxed)` 的 `taxed` 是 filtered 出來的，集合縮水就 vacuously 綠）。

改用三類真不變式：**集合等值 / 零丟棄 / 結構關係**。寫成新 pattern `docs/decisions/patterns/seed-topology-invariants.md`（第 29 條），並與 `phase-versioned-test-assertion` 互相 cross-ref——那條的 `>=` 建議前提是**被守的量 monotonic**，判準加一問「這個量會不會下降？下降時該紅嗎？」。

## 關鍵不對稱（最容易照抄錯）

- `subscription_template_links*.xml` 是 `noupdate="0"` ＋ 全量 `(6, 0, [...])` reset ⇒ **DB 鏡射檔案**，可以解析 XML 當 expected。
- `subscription_products*.xml` 是 `noupdate="1"` ⇒ **XML 數 ≠ DB 數**（升級軌上「從 XML 刪掉的 record 仍留在 DB」是正常的），照抄會在 `-u` 軌假紅，要改用零丟棄斷言。

延伸：A 的斷言在升級軌**順帶守住「改 seed 可見性就要補 migration」的既有慣例**（#336 的 `.54`、#431 的 `.79`）——改了 XML 卻忘補 migration 會 fresh 綠而 `-u` 紅，那正是該紅的。

## generator 樣板的結構性缺陷

`test_plan_variants_generated.py` 檔頭寫「DO NOT EDIT BY HAND」，但 `267`/`216` 是寫死在 `generate_plan_variants.py` 的 `_TESTS_TEMPLATE` 字面量裡（`267` 裡的 base `51` 更在 generator 輸入域外）⇒ **撞紅的人照檔頭重跑 generator 是 no-op**。已改成樣板內零計數字面量，並在 generator 自測反向守 `assertIn('len(VARIANTS)', src)` 讓字面量無法回流。

## Gate

repo-lint 第 5 條 `check_seed_topology`：靜態 parse seed XML 算六數對 baseline（`51/216/267/48/48/24`），一變就 fail 並列出所有持有相關計數的位置。成因是「改 seed 時 bump 了 3 個持有計數的檔、漏掉 2 個，因為沒有共用清單」。A 軌（AST 規則）另記 #438。

衍生 issue：**#436**（`addCleanup(os.environ.pop)` 無條件刪除，17 處/11 檔）、**#437**（退點測試日期敏感，fixture 假設月=30 天）、**#438**（A 軌 lint）。

驗收見 [[reference_odoo_sandbox_ci_local_two_track_test]]（兩軌跑法＋baseline 對照）。

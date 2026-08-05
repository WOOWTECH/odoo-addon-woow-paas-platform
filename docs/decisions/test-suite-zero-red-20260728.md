# project test suite zero red 20260728

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_test_suite_zero_red_20260728`。盤點依據見 [memory-inventory-2026-08-05](../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description："2026-07-28 全套測試收斂到 0F/0E（#433→#436→#437 三張連做）＋兩道 repo-lint gate；此後任何一條紅都是真信號"

2026-07-28，`woow_paas_platform` 全套 `--test-tags /woow_paas_platform` 從長期「常態帶紅」收斂到 **0 failed / 0 error（1841 tests）**。三張單連做：

| # | PR | 內容 | 結果 |
|---|---|---|---|
| #433 | #439 | seed 拓撲硬編快照斷言 → 真不變式（12 個斷言點）＋ repo-lint `check_seed_topology` | 6F/2E → 3F/2E |
| #436 | #451 | `addCleanup(os.environ.pop)`（無條件刪除）→ `patch.dict`（還原原值），17 處/11 檔＋ repo-lint `test-env-restore` | 3F/2E → 3F/0E |
| #437 | #452 | 退點測試日期敏感 → 固定測試時鐘＋跨週期不變式 | 3F/0E → **0F/0E**（1836→1841 tests） |

**為什麼值得記**：「常態帶紅」本身就是最貴的成本——#433 那條斷言在落地後 **9 小時 40 分**就被同日的 #375 打破，卻在一片紅裡藏了 8 天沒人發現。0F/0E 之後任何一條紅都是真信號。

**三個可複用的判準**：

1. **快照 vs 不變式**：計數斷言（`assertEqual(len(x), 34)`）多半是把「某一刻的事實」誤鎖成永久不變式。修法不是改數字、也不是放寬成 `>=`（seed 並非單調成長，純翻 `storefront_hidden` 就會下降）——要換成集合等值／零丟棄／結構關係。見 [[project_433_seed_topology_landing]]。
2. **奇數週期沒有整數中點**：#437 的正解不是硬湊 50.0，而是改用「期初 100／期末 0／逾期 0／單調遞減」這組**在所有週期長度下都成立**的不變式，中點 50.0 只在偶數週期斷言。硬湊不存在的數學性質＝造假。
3. **驗收 intent 是「不再敏感」不是「今天綠」**：日期敏感類的測試，必須在多個模擬時點下都綠才算過（#437 實測 28/29/30/31 四種週期）。

**兩道 gate 已上線**（`scripts/audit/repo-lint.py`）：`check_seed_topology`（改 seed 當下就擋，附待檢清單）、`test-env-restore`（擋 `addCleanup(os.environ.pop` 形狀；裝設時前後對照 exit 1／17 違規 → exit 0）。

**勘誤（推翻舊記載）**：舊 memory 說 `TestNamespaceLeftSetUnion` 那 2E「與有無 `-e` 注入無關」——**#436 之後已不成立**。現在該類確實依賴環境注入 `AGENT_RUNTIME_NAMESPACE_SECRET`，沒注入就 fail-closed raise（`agent_session.py:240`）。跑全套前要確認容器有注入，否則會誤判 baseline。**該類自己不設 env 是刻意的**（隱含驗證「正式環境必須注入」），要不要讓它自帶 `patch.dict` 是未決的產品決策。

關聯 [[project_433_seed_topology_landing]]、[[reference_odoo_test_docker_desktop_paas_image]]。

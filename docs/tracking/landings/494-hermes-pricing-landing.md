# project 494 hermes pricing landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_494_hermes_pricing_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#494 Hermes Agent 定價上架收官（PR#495 merged 8acea757、.86）——4500/36000＋7天 trial、min_vcpu 1→2 收 #456；quota 守線必須斷言邊際貢獻而非含 floor 值；衍生 #496"

2026-07-31：Hermes Agent 完整應用正式定價上架（解封 #424① 的 `is_hidden=True`）。
issue **#494** ／ PR **#495**（`feat/494-hermes-pricing` → develop，base `f0bcd5ec`）。
拍板：月 4,500／年 36,000（×8）／7 天 trial，形制沿 opendesign。版號 `18.0.1.0.85`。

**本次最有價值的一條技術判斷——quota 守線要斷言「邊際貢獻」不是「含 floor 的 quota」**：
`_compute_ns_quota` 是 `max(Σ allocated × 3, floor)`，CPU floor ＝ 8 vCPU。#456 的缺陷
（`min_vcpu=1` 的邊際 +3 蓋不住全容器 `limits.cpu` 合計 3.4）在**空 workspace** 被 floor
蓋住 ⇒ #424① 的 E2E 全綠，一進共用 workspace 才露餡（主 pod 永久卡 `FailedCreate`，
DB／UI 照樣 Running）。所以守線寫 `min_vcpu * 3 >= Σlimits.cpu`；若照 RAM 那條的寫法套
`max(..., 8)`，缺陷值 1 會**假綠**、守線等於不存在。新增 template 時同理。

**其他要點**：
- plan 一律不設 `cs_*`：非空會讓 `_allocated_vals_from` 改吃 plan 值、蓋掉 template 的
  `min_*`（那是 **quota 峰值模型**不是售賣規格）。#456 的 stg 現場就是 0 元測試 plan 填了
  `cs 1/3.0/5`，於是 `min_vcpu=2` 的修法會被整個繞過。opendesign／HAOS 同慣例。
- chart 契約值（auth-proxy `limits.cpu=200m`）以 `helm show values oci://…` 向 registry
  直驗，可跑、不需 VPN；與 #456 的事件訊息 `exceeded quota: limits.cpu=3200m` 交叉印證。
- 解封只翻 `is_hidden`，`conceal_instances` 自始不設（#471 兩層語意）。
- seed 拓撲 gate：`base_plans 53→55` 等六數，`--update-baseline` 前逐一檢視
  `SEED_TOPOLOGY_DEPENDENTS` 五處——無席數變體時只有 `_MIN_SEEDED_PRODUCTS` 要動。

**兩個踩到的坑**（已各自持久化）：
- 471／hidden-chain 的 hidden-only fixture 綁著 hermes-agent，上架即集體變紅 →
  改合成 fixture。見 [[feedback_test_pins_transient_fact_as_contract]]。
- #489 的 migration gate 把 manifest 版號硬編成 `.84`，任何 bump 都撞紅 → 查 Odoo 原始碼
  後改成正確不變式。見 [[reference_odoo_migration_version_window]]。

**測試基準**：全套 `1 failed / 0 error of 2082`（含 #489／#493 的 develop），唯一紅＝
#492 已記載的既有紅（`TestMcpAdminPassword.test_chart_version_drift_is_logged`）。判斷
回歸前先扣掉它。

**Review 兩個 finding（leader3，已修於 `ee0800d2`）**——都是「只修了一半」型：
1. RAM 兩條守線仍套 `max(..., 8)` 的 floor，而我自己在 CPU 側寫明了「套 floor 會讓缺陷值
   假綠」。漂移證明（離線純算術）：`limits.memory` 合計 ≤ 8Gi 時，covers 連 `0.5` 都放行，
   **minimal-step 更會解出 0.5 並用 assertEqual 強制把不安全值寫進 seed**。修法＝兩側各
   收斂成單一定義點 `_marginal_gi` / `_marginal_cpu`，含 floor 的舊 helper 刪掉。
2. auth-proxy 的 resources 是以常數代表 chart 契約值計入 quota，卻沒有守線禁止 seed 覆寫
   （chart 有該鍵、seed 走全量 values 管線 ⇒ 覆寫得掉，覆寫後四條斷言全以低估值假綠）。
   修法＝`test_auth_proxy_resources_stay_chart_contract` 鎖 `resources` ＋ `initResources`。
   **注入式驗證**（寫進 seed → -u → 該條如期紅 1F/33，還原後 0F/45）比「跑綠」有力得多，
   值得對每條新守線都做一次。

**撞車處理**：#493（#482）獨立做出與我**完全同源**的 migration gate 修法（同診斷、同樣
`assertEqual`→`assertLessEqual`）並先合併 ⇒ rebase 衝突**解為上游版本**、我方那份整個撤出
（驗收＝該檔與 `origin/develop` 逐字零差異），連帶版號 `.85`→`.86`，並把 commit message
裡描述該修正的段落刪掉——留著就是與程式碼不符的敘述。同型前例見
[[project_477_fail_open_family_closeout]]。

**最終狀態**：PR #495 經 leader3 四維度＋對抗性驗證覆核通過（`ee0800d2`），Eugene 於
2026-07-31 親按 merge（develop `8acea757`、.86），**#494／#456 隨 merge 自動 close**（本
repo develop 即預設分支，兩個 Closes 都生效；tea issues 的標題折行會把 `(closed)` 擠到第
二行，用 head 截前幾行會誤判狀態）。衍生 **#496**（HAOS 兩條 quota 守線的同構縫：
`test_haos_seed_423.py:373` storage floor 100、`:391` RAM floor 8；現值兩條都不靠 floor
成立故非緊急，另案處理）。

**merge 後 stg 收尾（hermes-plan 執行、leader3 獨立抽查雙驗，2026-07-31 完成）**：
stg .80→.86（image `20260731.0432@sha256:7e845b2b…`，CI log／pod imageID／pod 內
__manifest__／DB 四方一致）；upgrade job `stg-upgrade-86` 比照 449 樣板——**RWO
Multi-Attach 的解法不是先縮 deploy，而是 nodeSelector 把 job 釘到 running odoo pod
同一個 node**（filestore RWO 同 node 可多掛）；deploy 用 helm `--reuse-values --set
odoo.image.tag=…`（rev 72）避免 kubectl set image 造成 helm state 漂移。實證：
links `(6,0)` 全量 reset 如預期把 DB 直改的 0 元 plan 268 解掉、換上 271/272
（4500/36000、trial 7 天）；268 已 archive（無 xmlid，-u 不會重建）；svc60 詳情頁
全功能可達＝反證 conceal 未被誤設；UI 截圖四張佐證。#478（Free 標籤）症狀如預期
現形（Hermes MCP 也中＝普遍症狀非回歸），另案；prod 發版另計（現 .80，.82–.86 同班車）。

相關：[[project_424_item1_hermes_agent_cs_landing]]、[[project_471_conceal_instances_split]]、
[[reference_odoo_test_docker_desktop_paas_image]]

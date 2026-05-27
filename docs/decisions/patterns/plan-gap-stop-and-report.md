# Pattern: plan-gap 停下回報，別硬塞假值掩蓋（leader hand-off 標準守線）

> 適用：你在連續修一條 e2e gap chain（每修一關露下一關），或執行 leader hand-off 的任務時。**區分兩種「缺」**：(1) **missed-forward** —— 你手上有資料、只是沒傳下去（見 [[spawn-install-values-missed-forward]]，直接補）；(2) **plan-gap** —— 下游要某東西，但**你根本沒有對應的來源資料 / 那是個沒被設計過的決策點**。守線：**plan-gap 一律停下回報 + escalate，絕不硬塞假值 / parse 脆弱推導 / 塞 placeholder 掩蓋讓 e2e 表面往下走**。硬塞會把「設計缺口」偽裝成「已解決」，埋進更難查的下游（B1-relapse 同型風險）。

## When to use

leader 把任務交給 agent 時，hand-off 應**明文帶這條守線**（本 epic leader 原話）：

> 「若全盤對發現 chart 要的某 required value，operator 根本沒有對應 request 欄位 / 來源資料可填（不是漏轉、是 design gap）→ 別硬塞假值掩蓋，停下來回報我，這是 plan-level gap 要對焦。」

agent 端在每個 gap 自問：**「我手上有正確來源能填這個嗎？」**
- 有 → missed-forward，直接補（forward + contract test）。
- 沒有 / 要新 config / 要 parse 別的東西硬湊 / 牽涉安全或架構決策 → **plan-gap，停下回報**。

## The 3 instances（本 epic pilot，守線生效）

| gap | 為何是 plan-gap（非漏轉） | 若硬塞會怎樣 | agent 實際做的 |
|-----|--------------------------|--------------|----------------|
| **registry secret provision**（gap 5） | operator 沒有「把私有 registry creds provision 進動態租戶 ns」的機制 / config；牽涉**creds 來源 + 租戶 ns 放 creds 的安全**決策 | 硬 copy operator 自身 jfrog-registry 進每個租戶 ns = 未經安全審的 creds 擴散 | 停下回報 plan-gap，列 3 個方向（copy / 新 config / 沿用 marketplace paas-registry）交 leader + 使用者拍 |
| **LLM credential 注入**（gap 9） | 「模型金鑰怎麼到 pod」未設計（skill-bundle？template？secret？）；echo 行為與綁不綁 model_config 無關 → 佐證是 image 邏輯 + 沒設計的 cred 路徑 | 硬塞一把測試金鑰 = 假裝有 LLM、掩蓋「cred 注入無設計」 | 停下回報，標 plan-level、交 image team + 設計拍板 |
| **probe path ↔ image health**（gap 6，跨 lane） | chart 探 `/health`、image 不 serve；不是 operator 漏轉，是 chart↔image 契約 | operator 私自 patch chart 輸出掩蓋 = 越 lane 改別人交付物 | 停下回報，標 paas-charts lane；ephemeral patch 僅為解封續測、durable 交 chart owner |

> 注意：plan-gap **停下回報 ≠ 卡死**。本 epic 每個 plan-gap 仍可用 **ephemeral patch 解封續測**（copy secret / patch NP / patch probe）讓 e2e 往下驗，但 **durable 修正分軌交對的 lane / 設計拍板**，且回報時講清「這是 ephemeral、durable 是 plan-gap」。解封 ≠ 掩蓋；關鍵在**有沒有誠實標記它是未解的設計缺口**。

## impl/test 層延伸（Phase E0 T4.1a）：撞錯 test 假設，修 test 別 bend impl 湊綠

上面 3 個是 **operator↔plan 層**（agent 沒有來源資料填 plan 要的東西）。同一條守線在 **impl↔test 層**同等成立：**implementation 撞到一個錯的 test 假設時，停下回報修 test，別 bend impl 去滿足錯 test 而湊綠**。

| | Phase E0 T4.1a |
|---|---|
| 撞到什麼 | red test `T4.1a` 把 `ensure_registry_secret` mock 在 **HelmService** 上，但該方法實際屬 **KubernetesService**（`k8s_service`）|
| 「湊綠」會怎樣 | 把 impl 改成 `helm_service.ensure_registry_secret(...)` 去對上 mock → test 立刻綠，但 **prod `helm_service` 無此方法 → AttributeError 炸**（test-green 掩蓋 prod-crash）|
| agent 實際做的 | 停下回報「**test mock 對象錯、不是 impl 錯**」→ 修 **test**（`e2a3d7b`：mock 改到 `k8s_service`）+ tester 自核實確認 → impl 維持正確的 `k8s_service.ensure_registry_secret`（green 是真綠）|

**判準（與上 3 instance 同）**：紅的時候自問「**是 impl 錯，還是 test 假設錯？**」
- impl 錯 → 改 impl。
- **test 假設錯**（mock 對象 / 期望值 / fixture 假設與真實 API 不符）→ **停下回報修 test，別把 impl 扭去滿足錯 test**。後者是 [[credential-claim-test-fixture-coupling]]「在 test 裡繞過 gate 假綠」的鏡像：那邊是「改 test 去配合 impl」，這邊是「改 impl 去配合錯 test」，**兩者都讓 green 失真、把 bug 埋進 prod**。

> 為什麼值得獨立記：plan-gap 停下回報常被理解為「planner / design 層」的事；E0 證明同一守線在 **TDD red→green 的日常 impl 迴圈**裡同等重要 —— red test 不是聖經，它也可能假設錯；green 的價值在「impl 真的對」，不在「surface 變綠」。

## Why it works

- **保真**：e2e「表面往下走」不等於「真的解了」。硬塞假值會讓 round-trip 假性通過，把設計缺口埋進更深的下游（如硬塞 registry creds → pilot 過了，但 prod / 多租戶時 creds 擴散爆雷）。停下回報讓缺口**停在它真正該被決策的層級**。
- **lane 正確**：plan-gap 常跨 lane（設計 / 別的 repo agent）。agent 越 lane 硬改 = 改錯交付物 + 繞過 owner 的審查。回報 + escalate 讓對的 owner 處理。
- **誠實的 e2e 結論**：pilot 收尾時能誠實宣告「transport round-trip 閉環 ✅、skill-bundle 實際 fetch 待真 agent（echo 不行使）、LLM cred 是 plan-gap」——而非「全綠」掩蓋兩個未決設計。誠實回報是這次 pilot 最有價值的產出之一。

## How to apply

1. **leader hand-off 帶守線**：交跨 component / 連續修復任務時，明文寫「plan-gap 別硬塞、停下回報」。
2. **agent 每 gap 自問來源**：有正確來源 → 補；沒有 → 停。
3. **plan-gap 回報格式**：標 plan-level + 列可選方向 + 點出設計 / 安全考量 + 指明該誰拍 / 哪個 lane；可附 ephemeral 解封讓 e2e 續跑，但明標 ephemeral vs durable。
4. **收尾誠實**：宣告 e2e 結論時，未被行使 / 硬塞掩蓋的點要誠實列出（本 epic：skill-bundle 實際 fetch、LLM cred、registry provision durable）。

## See also

- [[spawn-install-values-missed-forward]] — 對照：真的是漏轉時直接補（別把漏轉誤判成 plan-gap 而過度 escalate）
- [[agent-runtime-pilot-e2e-gap-chain]] — 三個 plan-gap instance 所在的完整 chain
- [[constraint-input-trust-expansion]] — 「別讓便利機制退化成攻擊面」同源警覺（硬塞 owner / creds 的 B1-relapse 風險）
- [[credential-claim-test-fixture-coupling]] — impl/test 層延伸的鏡像：那條是「改 test 去配合 impl（假綠 / missing-negative）」，本條 impl/test 段是「改 impl 去配合錯 test（湊綠）」，同根
- `docs/tracking/reviewer-report-2026-05-27-agent-runtime-pod-phase-e0.md` §2 + commit `e2a3d7b` — T4.1a mock-target 修 test 非改 impl 的落地 case
- `follow-ups.md` §F-PILOT-4 / §F-PILOT-8 — 兩個 plan-gap 的 durable 追蹤條目（operator green 已落 Phase E0，見 §F-PILOT 收口）

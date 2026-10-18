# decisions/ — 解釋導向

「**為什麼這樣設計**」的論述。philosophy、refactor 決策、可複用 pattern、PR review verdict 都歸這。

## 進入條件

- 含 **trade-off 論述**（至少 2 個選項與選擇理由）
- 可能被未來 PR / code review 引用
- 對象是要理解設計脈絡或避免重蹈覆轍的人

## 退出條件

- 決策被推翻 → frontmatter 加 `status: superseded` 與 `superseded-by: ...`
- **不刪檔**（保留歷史脈絡）

## 子目錄

| 路徑 | 內容 |
|------|------|
| [`philosophy/`](philosophy/) | 跨多個 feature 的基礎設計原則（billing-philosophy 等） |
| [`refactor/`](refactor/) | 大規模重構決策（含 invariants） |
| [`patterns/`](patterns/) | 可複用的 pattern（odoo addon override、ACL 收緊、date helper）——完整索引見 [`patterns/README.md`](patterns/README.md) |
| [`reviews/`](reviews/) | PR review verdict、CAQA report、PR description draft |
| （根層） | 不屬上列子類的獨立裁定：[`agent-dataplane-in-cluster-no-cf-access-ip-bypass`](agent-dataplane-in-cluster-no-cf-access-ip-bypass.md)（agent 資料面全 in-cluster）、[`agent-runtime-operator-chart-install-contract`](agent-runtime-operator-chart-install-contract.md)（operator chart 安裝契約）、[`namespace-prefix-enforcement-app-layer-not-rbac`](namespace-prefix-enforcement-app-layer-not-rbac.md)（ns prefix 由 app 層強制）、[`cdi-datavolume-gc-must-stay-off`](cdi-datavolume-gc-must-stay-off.md)（CDI DV GC 必須關——租戶資料刪除保護鏈前提）、[`hermes-agent-command-execution-planes`](hermes-agent-command-execution-planes.md)（hermes-agent 四個指令執行面與 L3 信任邊界）、[`864-wallet-charged-not-credited`](864-wallet-charged-not-credited.md)（#864 A 級：錢包「已扣款未入帳」的 query gate ＋ auto-heal 修法）、[`tenant-egress-gate-and-node-avoidance`](tenant-egress-gate-and-node-avoidance.md)（租戶 pod 的 egress gate 與節點迴避：helm post-renderer 補 NetworkPolicy 啟動空窗與 59mqj 落點） |

## 命名

- `<topic>-<aspect>.md`（如 `cloud-service-project-decouple.md`）
- 易過期型可加 `YYYY-MM-DD-` 前綴（如 review verdict、CAQA 報告）
- `kebab-case`，禁空格與底線

## 寫作慣例

- 每份文件帶 **Context → Options → Decision → Consequences** 四段
- Options 至少列 2 個（含被否決的方案與否決理由）
- Invariants（不可違反的不變條件）用列點清楚標示
- 引用 plan 或 tracking 用相對路徑：`[plan](../plans/2026-Q2/2026-05-16-cloud-service-project-decouple-plan.md)`
- patterns/ 內每個 pattern 含「適用情境」「Anti-pattern」段落

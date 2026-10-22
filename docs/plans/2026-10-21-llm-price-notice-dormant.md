# 漲價公告、email、七天生效 gate：dormant 實作草稿

狀態：純 Python 可審查核心；未接入產品，不解除 GA blocker。沒有 Odoo schema、cron、route、mail queue、SMTP、new-api writer 或 apply-settings 變更。

## 基底與來源

獨立分支 `draft/llm-price-notice-dormant-1021`，worktree `impl/wt-llm-price-notice-dormant-1021`，base `b03f607304d7bdbef29d6a48b5fd29dc864fb03b`。採這個已有近期通知實作、可重現的本地來源供草稿對照；不是新的 release base，也不代表其 C1 已完成。未改 DR/health/infra/C1 等其他 worktree；後續可只取本次差異到 owner 指定整合基底。

權威需求：專案根 `DECISIONS.md:44`（漲價前七天公告）、`DESIGN.md:768`（分頁橫幅＋email，只改價格倍率，R 不用來調價）、`tickets/LLM-11-reminders-notifications.md` 補充（草稿條款第3條第4項承諾兩個渠道，尚缺實作）。D6 不退、不保價；D15 法務／負責人定稿。

已讀的本地實作：

| 來源 | 已有能力／缺口 |
|---|---|
| `src/services/llm_pricing.py` | Decimal 有效點數價格與公開 DTO；只接受 default=1.3/free=0。不是跨版本公告或切價器。不能以原始 ModelRatio 上升／下降單獨判定消費者是否漲價；須包含 output/cache/fixed 與所有有效倍率。 |
| `src/models/llm_swr.py::_cron_llm_pricing_refresh` | AP 抓價目並刷新快取；不是漲價審批 gate。 |
| `src/models/llm_notifications.py::_llm_notice` | 逐帳戶、通知與 mail queue 原子去重；kind/subject 有封閉白名單，沒有 price_change。`force_send=False` 表示排隊，不證明寄出；不能假借現有 kind 產公告。 |
| `src/data/llm_notification_mail_templates.xml` | 現有模板 auto_delete=True，寄出後 mail row 可刪；不能據缺少 mail row 推定成功或失敗，不能直接借 mail_id 作長期回執。 |
| `src/controllers/llm.py` `/api/llm/models`；`src/static/src/paas/pages/llm/LlmPage.js` | 價目入口／AI 分頁容器；可接公告，但本草稿未改 controller/UI。 |
| 外部 repo `impl/Woow_k3s_newapi` @ `a1fb90209c2124e7b255d9c05b15a188609454ba`，`scripts/newapi_admin.py::cmd_apply_pricing` | 合併 ModelRatio/CompletionRatio/CacheRatio，刪除相應 ModelPrice，逐個 PUT，沒有七天公告檢查，也沒有多選項原子切價。`cmd_apply_settings` 與直接主控台亦是可能的定價寫入口。這些檔案只讀。 |
| 專案 `impl/na04-prod-impact-census-20261021.md` | 既有匿名觀察記錄兩個啟用中的非 PaaS default 帳號及真實使用；用途不能從聚合判斷。本輪未重新查資料、不輸出身份。不能假設內部、搬 group、豁免通知或自行批准 1→1.3 的切換。 |

## 本次可執行的核心

`src/services/llm_price_notice.py` 無 Odoo／網路／寄信／檔案操作，未從任何 runtime init 匯入。`Policy()` 預設 dormant；即使手動傳 enabled=True，也只產生 inert 資料與純檢查結果，沒有 delivery 或 apply 能力。

- `prepare_notice`：必須有 environment、owner 決策、法務及 recipient-scope 參考；以已正確計算的 default 有效價格快照比較漲幅。涵蓋 input/output/cache_read/cache_write/per_call，包含有漲有降的混合改價；浮點、NaN、未知價格、tiered、模型集合／計費模式／group 改變均拒絕。純降價／無變動也不從這個草稿取得繞過許可。
- `announcement`／`email_drafts`：同一份不可變版號的中文正文、台北時間、前後價格與單位；email 僅為 recipient opaque ID／subject／body／dedupe key 的資料草稿，不解析真實地址、不寄信、不寫 queue。正文與所有價格、環境、受眾、批准參考及時間均綁 fingerprint。公開公告 DTO 不含受眾或批准參考。
- `check_effective_gate`：當前價格須等於已公告 baseline、目標等於已公告 target，當前受眾與原快照相同，政策參考仍一致。公告必須有 committed/visible 證據；每個受眾恰有一份同版 `sent` 回執。outgoing/exception/cancel/缺失/重複/異版都拒絕。
- 最早允許時間是 `max(公告的最早日期, 實際分頁發布時間+604800秒, 最後一封 sent_at+604800秒)`。不按曆日，不從 enqueue／prepared time 起算；未來或早於 publication 的 sent_at 拒絕。正文明示通知延遲會順延。此順延文案仍需法務核可。
- audience 變動一律阻擋，沒有猜測新開戶不需通知、或自動遺漏新帳戶。撤公告/換環境/改價格/換內容不能沿用舊回執。eligible 結果明確帶 `runtime_apply_implemented=False`。

重要：**非空的 approval ref 不是身分驗證或簽名。** 本核心只檢查受信 adapter 提供的證據一致性；不得將 Policy、Notice、PublicationEvidence、sent_at 直接從 RPC、context 或手改 JSON 接入。任意 caller 自行建 dataclass 就能造假「證據」，因此這不是可供現有 CLI 使用的授權 receipt。沒有實作跨程序防重放、時鐘來源、持久性、RBAC、publication 原子性或價格 writer 鎖；這些不能以純測試冒稱通過。

## Owner／法務決策邊界

1. Owner 須私下確認兩個既有 default 帳號的用途、最終 group／價格處置、外部受影響對象及可驗證通知管道。若對外服務，不能用沒有 PaaS account 當作不通知的理由。`legacy:*` 只是合成測試／adapter 識別格式，未指定真實帳號或處置。
2. Owner 須核定「全體 AI 帳戶」範圍（含 suspended/error/closed 的處理、API-only 使用者、legacy 使用者），不能直接沿用現有 `_llm_notice` 排除 closed 的行為。缺 email、退信、無法確定受眾，保守保持 pending；沒有免寄 checkbox。
3. 新開戶、收件地址變更、受眾離開或公告撤回的處置需定稿。目前 audience 不同就拒絕；可能需要重公告或 owner 核定更完整制度，不暗自採截止日豁免。
4. 法務須核可公告/email 文案、最早生效／延遲順延、寄出與送達承諾，以及條款 Q20 的最終措辭。本草稿不改條款版本、同意記錄或正式生效承諾；SMTP accepted 不等於收件匣送達。
5. GroupRatio.default 的 1→1.3 既有使用者影響，以及新模型、模式切換、tiered、GroupGroupRatio 等不在這個可比較子集的切價，需額外權威價格與影響分析；不可假裝 `normalize_pricing`（鎖1.3）已涵蓋它們。R=32 不變。

## 最小接線順序（本次沒有執行）

1. **受信持久 publication adapter**：具名有權管理者／法務批准記錄綁全文 digest、環境／upstream/config revision；不可由 sudo/context 冒名。新增專用 announcement + per-recipient obligations（唯一 fingerprint/recipient）與稽核。在同一 PostgreSQL 交易公布公告及排完整 outbox；以實際 commit 後可見時間與 server clock 產生回執。不在新 schema 核可前註冊模型／XML／cron。
2. **mail evidence adapter**：以現有 mail queue 寄出，不 force-send；保存唯一 mail ID／內容指紋／recipient revision，從實際 SMTP 成功的受信 dispatch 路徑持久記 sent_at，失敗重試保持同一 obligation。須在 mail auto-delete 前留下不可被普通 caller 寫入的持久回執。create_date/write_date/queue state/郵件不存在均不能替代 sent_at；重試可能重複寄送，沒有 SMTP exactly-once 保證。
3. **AI 分頁**：在 `LlmPage` 共用容器顯示伺服器公告，含價目連結；正文以 escaped text 呈現，不允許自由 HTML。公開 DTO 由專用公告讀取產生，不直接修改現價快取以假裝切價。無設定或 dormant 時零公告／零 outbox／零外呼。不同使用者權限與頁籤可見性須實驗。
4. **new-api writer interlock**：在另一獨立 repo/commit 為 apply-pricing、任何可寫價格的 apply-settings 路徑加同一受信 gate，綁 exact environment/current effective catalog/target mutation/nonce/revision/expiry；不可只讀一份可編輯 JSON 或信任 CLI `--announced-at`。檢查到第一次寫入之間要有互斥，限制旁路管理工具；PaaS 本地 gate 無法阻止上游 root 人工改價。
5. **切價安全**：現有逐項 PUT 存在半套用、timeout 已執行、插入其他 writer 與中間價高於公告價的風險。要對全部實際計費維度與受眾證明覆蓋，定義安全切價／失敗復原協定；未證明前維持人工阻擋。成功需以實際 `/api/pricing` 及計費契約核對，不是只驗 option table；不可自動 retry Unknown。

## 驗證與 GA 尚缺

精簡離線命令（Python3.12）：

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/omnigent/venv/bin/python3.12 -B tests/test_llm_price_notice.py -v
PYTHONDONTWRITEBYTECODE=1 /opt/omnigent/venv/bin/python3.12 -B tests/test_llm_pricing.py -v
/data/pi-agent/venv/bin/ruff check --select F --no-cache src/services/llm_price_notice.py tests/test_llm_price_notice.py
PYTHONDONTWRITEBYTECODE=1 /opt/omnigent/venv/bin/python3.12 -B scripts/audit/repo-lint.py
git diff --check
```

本次：17 新測試＋12 既有 pricing 測試；不跑全 Odoo、SMTP、upstream、sandbox 或 staging。測試已依現有 pattern 加入 `.gitea/workflows/llm-services-unit-tests.yml` 的 paths 與執行命令；CI 未觸發。

GA 未達：owner／法務上述決策；受信 PG durable publication/outbox/實際寄出回執；真 AI 分頁顯示與 stg 收件匣證據；未設定／dormant 零副作用與權限測試；跨 repo 寫入 gate、防 stale/replay/concurrent writer、未知結果與半套用復原；兩個既有 default 帳號與所有受影響使用者的收件證據；實際等待至少七天。不能以 mock reference、手改 timestamp、queue 成功或本草稿測試解除門檻。未註冊核心也不等於全 Odoo fresh-install/upgrade 驗證。

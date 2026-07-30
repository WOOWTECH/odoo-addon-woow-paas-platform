---
title: "reset 密碼改非同步 ＋ 預算階梯（#461）設計案"
category: plans
status: active
created: 2026-07-30
updated: 2026-07-30
related:
  - docs/tracking/test-reports/449-password-carrier-stg-e2e-2026-07-29.md
  - docs/tracking/followups/follow-ups.md
  - docs/decisions/patterns/helm-reset-values-unified-pipeline.md
  - docs/decisions/patterns/capability-gate-single-source-field.md
---

# reset 密碼改非同步 ＋ 預算階梯（#461）設計案

> **上游**：[#461](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/461)（F-449-3；`Refs #449 #455`）
> **本檔範圍**：只寫 design。implementation plan 與程式碼變更待本檔通過 leader3 review 後才動工。
> **基線 commit**：`origin/develop@7dd5cf5c`（本檔所有行號以此為準；工作分支 `fix/461-reset-async`）
> **本檔不含任何叢集寫入**：撰寫期間對 `woow-k3s` 只做唯讀查詢（`get` / `exec cat`），命中 namespace 僅 `dev-sandbox-stg`（本 repo 允許範圍內）。

---

## 0. 依據分級（引用紀律）

| 級別 | 定義 | 本檔用法 |
| --- | --- | --- |
| **實測** | 有時序 / 有輸出的真機量測 | #459 stg E2E 的秒數；stg 平台 `odoo.conf` 與 nginx `proxy_read_timeout`（本輪 `kubectl exec cat` 實查） |
| **程式碼實查** | 直接讀 `origin/develop@7dd5cf5c` 的原始碼 | 所有 timeout 常數、呼叫順序、補償路徑、測試斷言 |
| **推論** | 由前二者推導、**尚未實證** | Cloudflare 524 的 100s origin 上限（§3.2 標註）；「reset 夾帶 chart 升版時 `--atomic --wait` 會等新 pod」 |

---

## 1. 事實基線

### 1.1 三層互不知情的預算（程式碼實查）

| 層 | 預算 | 位置 |
| --- | --- | --- |
| 前端 | **30s**（`REQUEST_TIMEOUT_MS`，`resetAdminPassword` 未覆寫） | `src/static/src/paas/services/cloud_service.js:5`、`:14`、`:408-423` |
| 平台 client | **120s**（`HELM_OPERATION_TIMEOUT`） | `src/services/paas_operator.py:18`、`:494` |
| operator | **≈560s 最壞**（見下） | `extra/paas-operator/src/api/releases.py:723-1003` |

operator 最壞路徑逐項（`helm_secret` 路，兩顆 Deployment）：

| 子步 | 上限 | 依據 |
| --- | --- | --- |
| `helm upgrade --atomic` | subprocess **300s**（`settings.helm_timeout`）；helm 自身 `--timeout` **未傳** ⇒ 預設 5m | `helm.py:333`、`helm.py:620-673`、`config.py:23` |
| `rollout restart` ×N | 各 ~1s | `helm.py:2408-2430` |
| `rollout status` ×N | 各 **130s**（`timeout_seconds=120` ＋ subprocess `+10`） | `helm.py:2432-2448` |
| **合計（N=2）** | **≈560s** | |

`config_json` 路另加 `exec_in_pod` 30s ×2（patch ＋ backup cleanup），單顆 Deployment ⇒ ≈490s。

### 1.2 為什麼 30s 這一層也是硬阻塞（實測）

#459 的兩次**成功**案例是 49.9s（hermes-agent）與 76.7s（open-design）——**都超過前端 30s**。也就是說即使只修後端，租戶在瀏覽器上仍會看到 `Request timed out`。#459 的 E2E 是直接打 API 端點、繞過 UI，所以這一層當時沒被觸發。**修後端而不修前端 = 對租戶零改善。**

### 1.3 時間去哪了（實測，#459 §5）

失敗的 120s 幾乎全部落在 `rollout status` 這一段：Recreate ＋ RWO PVC ⇒ 殺 pod → detach(19s，兩顆 PVC 序列 26s) → 重排程 → 跨節點 pull（954MB / 70s）→ 啟動。`helm upgrade` 本身只花 **4–12s**（Secret-only 變更不動 pod template，`--atomic` 隱含的 `--wait` 對已 Ready 的舊 pod 立即通過）。

> **關鍵不對稱**：整個 reset 唯一「慢且不可預測」的部分（rollout）**不是**密碼提交所必需的；密碼在 `helm upgrade` 回來的那一刻就已經是叢集 Secret 的權威值了。

### 1.4 既有可複用的非同步管線（程式碼實查）

| 元件 | 位置 | 語意 |
| --- | --- | --- |
| `upgrade_release` → operator `PATCH` | `paas_operator.py:362-415` → `releases.py:166-218` | **不帶 atomic、不等 rollout**，幾秒回 |
| state stamp | `cloud_service.py:525-528`（reset 成功已 stamp `upgrading`） | 現成 |
| cron 收斂 | `src/data/cloud_service_cron.xml:15-24`（1 min）→ `_cron_check_deploying_services`（`cloud_service.py:1304`）→ `_poll_and_update_status`（`:1541`） | 現成 |
| 前端輪詢 | `ServiceDetailPage.js:130-138`（5s，`upgrading` 在 `pendingStates` 內） | 現成 |
| 動作鈕互鎖 | `ServiceDetailPage.js:164-168` `canPerformActions` 排除 `upgrading`；Reset 鈕 `disabled="!canPerformActions or …"`（`ServiceDetailPage.xml:96`） | 現成 |

**reset 是唯一「在 request 內等 rollout」的動作**——它是管線裡的特例，不是需要新管線。

---

## 2. 方案選擇

### 2.1 三個候選

| | A：同步提交 ＋ rollout 移出 request（**建議**） | B：operator 回 202 ＋ task registry ＋ webhook | C：只把 120s 調大 |
| --- | --- | --- | --- |
| operator API | 保留 200，**不等 rollout** | 202 + `task_id`，新增 task 狀態端點或 HMAC 回呼 | 不變 |
| 新增機制 | 無（複用 `upgrading` + cron + 5s 輪詢） | in-process task registry（單 replica、restart 即失憶）、Odoo webhook receiver、新 secret 供給 | 無 |
| 密碼交付 | **同一個 response 就給最終密碼**（一次按、當下拿到） | 需 pending 密碼落庫，確認後 promote；或讓租戶回頭看 | 同今天 |
| C11 不變式 | 提交點左移到「Secret 已換」，仍是 hard commit（§4） | 未確認憑證必須落庫（新憑證面 ＋ 兩把密碼並存的查詢歧義） | 不變 |
| 典型耗時 | **5–15s**（實測 helm upgrade 4–12s） | <1s（但要多一輪確認） | 50–80s（且冷 cache 仍逾時） |
| F-449-3 明文評價 | 建議落點 (a) | 建議落點 (a) 的重型版 | **明文不建議**（「把機率往後推，不是解」） |
| 受影響測試 | 既有 4 條 C11 守線**全部保持綠**（§4.3） | 4 條全部要重寫 | 0（但問題沒解） |

### 2.2 採 A 的理由

1. **它就是「issue 方向 1」**：rollout 確認確實變成非同步 ＋ 狀態輪詢，只是**不需要在 HTTP 層新造 202 協議**——`upgrading` 狀態機已經是那個協議，而且已在 deploy/upgrade 路徑上跑了很久。
2. **reset 從特例回歸管線**：改完後 reset 與 `redeploy` / `reconfigure` 完全同形（不 atomic 等 rollout、stamp `upgrading`、cron ＋ 前端收斂）。少一條特例路徑 = 少一組只在 reset 才會踩到的行為。
3. **B 的複雜度買不到對應價值**：B 唯一比 A 多的能力是「rollout 完成的顯式確認」，但那個確認**對密碼真相沒有影響**（§4），只影響「服務何時恢復」——而後者 `upgrading` → `running` 已經在表達。代價卻是 task registry 的生命週期問題（operator pod restart / 多 replica）＋ pending 憑證落庫。
4. **A 讓預算階梯第一次成立**：把 request 內的工作壓縮到 `helm upgrade`（實測 4–12s）之後，才有可能讓「外層預算 > 內層硬上限」在合理的 UX 秒數內成立（§3.2）。C 做不到（560s 無法被任何合理的前端預算包住）。

---

## 3. 設計

### 3.1 (a) operator 端 API 語意

**保留同步 200，移除 rollout 等待**。契約是 additive 的：

| 項目 | 改法 |
| --- | --- |
| HTTP 語意 | 仍是 `POST /api/releases/{ns}/{name}/reset-config` → **200**；不引入 202 / `task_id` / 回呼 |
| request schema | **不變**（`ReleaseResetConfigRequest`，`schemas.py:103` 起不動；不新增 `wait_rollout` 旋鈕——見「為何不加旋鈕」） |
| response | 新增 `"rollout_awaited": false`；`restarted_deployments` / `helm_revision` / `config_json_patched` / `admin_password_carrier` 全部保留 |
| 主路 | 兩條路都**刪掉** `rollout_status_deployment` 呼叫（`releases.py:864-867`、`:974-978`） |
| 補償 | 補償內的 `rollout_status_deployment` 也刪（`releases.py:1040`、`:1091`）——否則一個失敗的 reset 要 260s 才回 500，階梯照樣破 |
| `config_json` backup | patch script 前置加一行「先清掉殘留的 `.config.json.reset-backup-*`」；cleanup exec 保留為 best-effort（見 §3.3） |

**為何不加 `wait_rollout` 旋鈕**：非預設分支在生產永遠不會被走到，卻讓補償路徑的測試矩陣翻倍（等/不等 × 各失敗點）。要保留「等」的能力就得同時保留兩套補償語意，而 §4 的整個論證是建立在「不等 ⇒ 不需要補償」上的。旋鈕會把已經拆乾淨的不變式重新黏回去。

**向後相容 / 發版順序**（F-449-2 教訓）：

| 組合 | 行為 | 安全性 |
| --- | --- | --- |
| 新 operator ＋ **舊**平台 | 舊平台不讀 `rollout_awaited`，只感受到「200 來得快」→ 照既有邏輯落庫 ＋ stamp `upgrading` → cron 收斂 | ✅ 安全，且**舊平台立刻受益**（逾時消失） |
| **舊** operator ＋ 新平台 | operator 仍等 rollout（最壞 560s）→ 新平台 75s 預算逾時 → 走 C11「狀態不明」（＝今天的行為） | ✅ 不比今天更糟，但**修復未生效** |

⇒ **建議 operator 先發、平台後發**（與 #449 同一順序理由）。反序不會壞，只是沒修好。發版單須按目錄分流記錄兩次發版（`extra/paas-operator/**` → operator image；`src/**` → paas-odoo image）。

### 3.2 (a′) 預算階梯——本案真正要修的結構

F-449-3 的核心指控是「兩個獨立預算彼此不知道對方」。修法不是把數字改大，而是**宣告一條可機械檢查的階梯**：

> **階梯不變式**：`前端預算 > 平台 client 預算 > operator reset 硬上限`，且 operator 硬上限由 operator 自己的常數算出來（單一權威）。

**外層天花板（先定 UX，再往內切）**：

| 約束 | 值 | 依據 |
| --- | --- | --- |
| stg 平台 Odoo `limit_time_real` | 1200s | 實測（`dev-sandbox-stg` odoo.conf；`workers=0` 執行緒模式） |
| stg 前置 nginx `proxy_read_timeout` | 720s | 實測（`stg-odoo-dev-sandbox-nginx` cm） |
| Cloudflare origin 上限（524） | **~100s** | **推論**（CF 文件行為；#459 有一次 120.6s 的 round-trip 完成，故 prod/stg 該路徑是否真受 100s 約束**未證實**） |
| 租戶可接受的等待 | ≤ ~90s | 產品判斷 |

⇒ 取 **90s 為前端天花板**：同時繞開 CF 524 的不確定性（不需要先證實它），也是合理的 UX 上限。

**建議階梯**：

| 層 | 常數（新增） | 值 | 理由 |
| --- | --- | --- | --- |
| 前端 | `RESET_REQUEST_TIMEOUT_MS`（`cloud_service.js`） | **90_000** | > 平台；≤ CF 疑慮線 |
| 平台 client | `RESET_CONFIG_TIMEOUT`（`paas_operator.py`，**不再借用** `HELM_OPERATION_TIMEOUT`） | **75** | > operator 硬上限 |
| operator 硬上限 | `RESET_HARD_CAP_SECONDS`（`releases.py`，由下列各項相加得出） | **60** | 單一權威 |
| ├ helm upgrade | `RESET_HELM_TIMEOUT_SECONDS=30` → helm `--timeout 30s`；subprocess `30+10=40` | 40 | 實測 4–12s，2.5× 餘裕 |
| ├ config_json patch exec | `RESET_EXEC_TIMEOUT_SECONDS=12`（現行 30） | 12 | 內嵌 python 腳本，10× 餘裕 |
| ├ backup cleanup exec | `RESET_CLEANUP_EXEC_TIMEOUT_SECONDS=5`（best-effort） | 5 | 失敗只 warning |
| └ 探索 ＋ restart 下達 | `RESET_KUBECTL_SLACK_SECONDS=3` | 3 | 各 ~1s 的 kubectl 呼叫 |

典型端到端：**5–15s**（實測 helm upgrade 4–12s ＋ 一次 exec）。

**需要的實作支點**：`helm.py:570` `upgrade()` 新增 `timeout_seconds: Optional[int] = None`——有值就渲染 `--timeout {n}s` 並把該次 subprocess timeout 收成 `n+10`；`None` 時**完全不變**（不出現 `--timeout`、沿用 `settings.helm_timeout`）。只有 reset 路徑傳值，install / 其他 upgrade 路徑零行為變化。

**機械 gate（repo-lint 新增第 10 條 `check_reset_budget_ladder`；`run_all`（`repo-lint.py:737-755`）現註冊 9 條，須同步加進註冊串與通過訊息字串）**：AST 讀 `paas_operator.py` 與 `releases.py` 的常數、regex 讀 `cloud_service.js` 的常數，斷言
`90_000ms > 75s > RESET_HARD_CAP_SECONDS == 40+12+5+3`。
這條 gate 是本案對 F-449-3 的**結構性回應**：以後任何人單改一層的數字都會紅。散文註解攔不住這件事——它已經被攔失敗過一次了。

> ⚠️ **CI paths 盲點**（#463 教訓）：`.gitea/workflows/repo-lint.yml` 現在只在 `src/**` / `scripts/audit/**` 觸發。這條 gate 會讀 `extra/paas-operator/**`，所以**必須同時把該路徑加進 workflow 的 `paths`**，否則「只改 operator 常數」的 PR 不會跑到 gate ＝ 守線休眠。

**已知的權衡（`helm --timeout 30s`）**：reset 會夾帶 chart 升版（`cloud_service.py:465-468`、`:481` 帶 `template.helm_chart_version`，#411 的刻意設計）。若那次 reset 剛好是 chart 版號 bump 後的第一次操作，pod template 會變 ⇒ `--atomic --wait` 會真的等新 pod（**推論**，非實測）⇒ 30s 可能不夠 ⇒ helm 自動 rollback ＋ 500 ＋ 舊密碼保留（fail-safe）⇒ 租戶重按（此時 chart 已由 rollback 回原版；建議先走 redeploy）。
- 為此在平台端加一行**可診斷性**日誌：`helm_chart_version != template.helm_chart_version` 時 log「本次 reset 夾帶 chart 升版 X→Y」。
- 這是 **D2 待裁決項**：若 leader3 認為這種 reset 應該撐過去，可把三層拉成 60/90/105s，代價是貼近 CF 524 的不確定性 ＋ 更長的等待。

### 3.3 (a″) `config_json` 路的 backup 生命週期

移除 rollout 等待後，原本「等 rollout 成功 → 在**新** pod 上刪 backup」的收尾失去落腳點：restart 一下達，Recreate 就把舊 pod 殺掉，緊接著的 `exec` 很可能打在 Terminating／不存在的 pod 上。

三個選項與取捨：

| | 做法 | 補償能力 | 殘留 |
| --- | --- | --- | --- |
| ① | cleanup 移到 restart **之前** | restart 下達失敗時**無法**還原 config.json（backup 已刪）⇒ 要改成誠實的「可能已生效，請重按」文案 | 無 |
| ② | 維持順序，cleanup 變 best-effort | 完整（backup 在 restart 之後才刪） | 每次 reset 可能留一個 `.config.json.reset-backup-*`（**累積**） |
| ③ **建議** | 維持順序 ＋ **patch script 開頭先清掉殘留的舊 backup** | 完整（同 ②） | 最多 **1 個**，下次 reset 必被清 |

③ 的實作：內嵌 python 腳本開頭加一行 `glob` 清除（`for f in glob.glob('/data/.config.json.reset-backup-*'): os.unlink(f)`），其餘順序、補償、文案**全部不動**。殘留檔內含**上一把**舊密碼明文，位置是租戶自己的 PVC、同目錄的 `config.json` 本來就有當前密碼明文 ⇒ 邊際暴露為零、且有上限。

### 3.4 補償語意的新邊界

> **原則**：**request 內還同步的每一步，補償照舊；移出 request 的那一步，不需要補償。**

| 子步 | 仍在 request 內？ | 失敗後叢集密碼 | 平台落庫 | 租戶看到 |
| --- | --- | --- | --- | --- |
| `helm upgrade --atomic` | ✅ | **舊**（helm 自動 rollback） | ✗ | `Reset failed`，重試 |
| Deployment 探索 | ✅ | **舊**（`_rollback_*` helm rollback） | ✗ | `Reset failed` |
| `config.json` exec patch | ✅ | **舊**（backup 還原 ＋ helm rollback） | ✗ | `Reset failed` |
| backup cleanup | ✅（best-effort） | 新 | ✅ | 成功（只 log warning） |
| `rollout restart` 下達 | ✅ | **舊**（helm rollback ＋ 重新下達 restart，**不等**） | ✗ | `previous password restored` |
| **rollout 完成 / pod Ready** | ❌ **移出** | 新（權威） | ✅ 已落庫 | `upgrading` → 輪詢 → `running` |

補償路徑內的 `rollout status` 一併移除後，補償的 `recovery_failed` 判定改為只反映「helm rollback 成功嗎 ＋ restart 重新下達成功嗎」。`_rollback_helm_secret_reset` 既有的「rollback 失敗就不要再滾 pod」判斷（`releases.py:1028-1030`，理由：Secret 還停在新密碼，再滾只會把沒人持有的密碼推上線）**保留不動**——那是正確且仍然適用的。

---

## 4. (b) C11 不變式的新設計

### 4.1 舊 C11 為什麼會被打破 / 其實不會

舊語意（`cloud_service.py:493-515` 註解 ＋ `:523` 的順序）：

> DB 裡的密碼一定是**真的生效過**的那一把 ⇒ client 逾時就不落庫。

它的成立條件是**「operator 會在 rollout 失敗時 rollback 回舊密碼」**。移除 rollout 等待就等於移除那個 rollback ⇒ 「已生效」的語意失去載體。**但不變式本身不需要放棄，只需要把提交點左移**：

### 4.2 C11′（commit-on-secret）

> **C11′**：DB 裡的密碼，一定是**叢集 Secret／config.json 已經持有、且不會再被自動回退**的那一把。
>
> 1. **提交點** ＝ operator 回 2xx。此時新密碼已是權威載體的值，之後沒有任何自動回退（rollout 成功與否都不會改密碼）。
> 2. **2xx 之前的任何失敗都保證叢集還是舊密碼**（helm `--atomic` 自動 rollback ／ backup 還原 ＋ helm rollback，見 §3.4 表）⇒ 平台不落庫是正確的。
> 3. **rollout 成功與否不再影響密碼真相**，只影響「服務何時恢復」——由狀態機表達（`upgrading` → `running`）。
> 4. 因此 **DB ≡ 叢集** 在成功與失敗兩側都成立。

「已生效（rollout confirmed）」這層保證確實被放棄了，換來的是：**DB 與叢集的一致性由「等待運氣」變成「helm 的原子性」**。前者是機率事件（#459 兩台第一次都輸），後者是 4–12s 的確定性操作。

### 4.3 失敗回退語意（含殘留窄窗）

| 情境 | 叢集 | DB | 租戶動作 | 是否被鎖死 |
| --- | --- | --- | --- | --- |
| helm upgrade 失敗 | 舊 | 舊 | 重按 | ❌ 舊密碼可登入 |
| exec patch 失敗 | 舊（backup 還原） | 舊 | 重按 | ❌ |
| restart 下達失敗 | 舊（rollback） | 舊 | 重按 | ❌ |
| **2xx 回應在途遺失**（網路斷 / operator pod 被殺 / worker 被殺） | **新** | 舊 | **重按**（reset 不需要舊密碼；state 仍 `running` ⇒ 鈕可按） | ❌ 重按即收斂 |
| rollout 一直失敗（quota / ImagePull） | 新（權威） | 新 | 等 / 修 quota | ❌ 密碼記錄正確，服務另有問題 |

**殘留窄窗**＝倒數第二列。它是本設計唯一放棄的保證，處置：
- 仍走既有 C11「狀態不明、不落庫」文案（`cloud_service.py:510-515`），文案微調成新事實。
- 曝險從「幾乎必然發生的 120s 等待」縮到「~10s 內的網路/程序故障」。
- 恢復手段就是**再按一次 Reset**（冪等，`#357 C03` 不檢查既有 secret ⇒ 不需要舊密碼）。

### 4.4 被明確否決：pending 密碼欄位

> 方案：`cloud_service_secret` 加 `pending_admin_password` ＋ `pending_at`，呼叫 operator 前先寫 pending，確認後 promote。

否決理由：
1. 它只解 §4.3 的那一個窄窗，代價是**把未確認的憑證落庫**（新的憑證面 ＋ 「查詢密碼時有兩把」的歧義，`/api/…` 與支援流程都要處理）。
2. 冪等重按已能收斂該窄窗，且不留下額外憑證。
3. **要讓 pending 在「程序被殺」時真的存活，得在 request 中途 `cr.commit()`**——那會破壞該 handler 的交易原子性，且與 `_reset_admin_password` 一開始就取的 `SELECT … FOR UPDATE` row lock（`cloud_service.py:434-437`）語意衝突（提早 commit ＝ 提早放鎖）。
4. 若日後真的需要（例如觀測到該窄窗在 prod 發生），它可以在 C11′ 之上獨立追加，不需要現在先付這個複雜度。

### 4.5 守線測試：**不需要重寫**（重要修正）

leader3 的 brief 預期「`test_mcp_admin_password.py:141/172/204/223` 釘死同步語意，改設計須連同重寫」。逐條實查後結論相反——**四條全部保持綠，而且應該刻意不改**：

| 測試 | 釘的東西 | C11′ 下 |
| --- | --- | --- |
| `:141` 逾時不落庫 ＋ 說「status unknown」 | 逾時語意 | ✅ 保留（§4.3 窄窗仍走這條） |
| `:172` 逾時後 state 仍 `running`、重按收斂 | 冪等 | ✅ 保留 |
| `:204` 非逾時失敗要說「failed」 | 失敗語意 | ✅ 保留（§3.4 前三列） |
| `:223` 成功後 stamp `upgrading`、重按有正確文案 | 狀態機 | ✅ 保留（成功仍 stamp `upgrading`） |

這正是選 A 的旁證：**提交點左移不動這四條的語意，只把「等 rollout」從 request 拿走**。要改的是 `cloud_service.py:493-515` 的**註解**（它現在描述的是舊世界的理由：「我們的 120s 不是 operator 的」——階梯建立後這句話不再成立，client 逾時改為代表「operator 超出自己的硬上限＝異常」或網路故障）。

---

## 5. (c) 前端 30s 層與密碼交付時機

| 項目 | 改法 |
| --- | --- |
| 請求預算 | `resetAdminPassword`（`cloud_service.js:408-423`）改傳第三參數 `RESET_REQUEST_TIMEOUT_MS = 90_000`；`jsonRpc` 已支援（`:14`），不動共用預設 30s |
| **密碼交付時機** | **不變**：同一個 200 response 就帶最終密碼（`controllers/paas.py:1346-1354`）。這是 A 相對 B 的最大 UX 優勢——租戶按一次、當下拿到那把**最終**密碼，不必回頭看、不必二次確認 |
| Modal 文案 | `AdminPasswordModal.xml` 增一句：「服務正在重新啟動以套用新密碼（通常 1–3 分鐘），期間可能短暫無法連線。」props / 元件介面不變（純文案） |
| 成功後 | 既有 `await this.loadService()`（`ServiceDetailPage.js:352`）→ state=`upgrading` → `startPollingIfNeeded` 5s 輪詢 ＋ Reset 鈕 disable（`canPerformActions`），全部現成、零改動 |
| 失敗 / 不明 | 既有 `state.resetPasswordError` 顯示；文案沿用 model 端 UserError |

**必須明講的 UX 變化**：今天「成功」＝密碼當下就能登入（因為 operator 已等到 pod Ready）；改後「成功」＝密碼已是權威值，但服務正在重啟（Recreate ⇒ 舊 pod 先被殺，期間服務不可達）。所以 modal 文案不是裝飾，是**正確性的一部分**——沒有它，租戶會拿著正確的密碼在重啟中的服務上撞牆，並回報成「reset 沒生效」。

---

## 6. 狀態機收斂與已知風險（複用既有管線的代價）

1. **`_is_container_ready` 的 fail-open（F-423-1）會被 reset 路徑用到**：`pods` 為空列表時回 `True`（`cloud_service.py:1407-1411`）。Recreate 的「舊 pod 已刪、新 pod 物件還沒建出來」窗口若剛好被 poll 撞上，state 會提前翻回 `running`（badge 說 running、服務其實還在 pull image）。
   - **不在本案修**：這是既有 fail-open，`redeploy` / `reconfigure` 早就同樣暴露（它們也不等 rollout）。reset 改完只是**加入既有風險池**，而 W5a 已把判定收斂成單一事實源（`_is_release_ready`）⇒ 日後修一處即可。
   - 窗口很短（Recreate 幾乎立刻建出 Pending pod，`pods` 非空 ⇒ 正確回 not-ready），poll 間隔 5s／60s。後果是顯示層誤導，不是資料損毀。
   - 本案在 plan 與 follow-up 明白標註，串回 F-423-1。
2. **`upgrading` 停滯沒有上限**：若 rollout 永遠不成功，state 會一直停在 `upgrading`（cron 只 log not-ready，`:1465`）。同樣是既有行為（deploy 路徑相同），本案不修，**新開 follow-up F-461-1**（建議：stuck-state 偵測／`upgrading` 超時轉 `error`）。
3. **row lock 佔用時間變短（附帶收益）**：`FOR UPDATE` 現在最壞握 560s，改後 ≤75s、典型 10s。

---

## 7. 受影響檔案（實作清單，Phase 2 用）

**operator（`extra/paas-operator/**` → operator image）**

| 檔案 | 變更 |
| --- | --- |
| `src/api/releases.py:723-905`（config_json 路） | 刪 `rollout_status`（`:864-867`）；exec timeout 30→12；cleanup timeout →5；patch script 前置清殘留 backup；response 加 `rollout_awaited: False` |
| `src/api/releases.py:908-1003`（helm_secret 路） | 刪 `rollout_status` 迴圈（`:974-978`）；response 加 `rollout_awaited: False` |
| `src/api/releases.py:1006-1095`（兩個補償） | 刪補償內 `rollout_status`（`:1040`、`:1091`）；`recovery_failed` 判定改只看 rollback ＋ restart 下達 |
| `src/api/releases.py`（新常數區） | `RESET_HELM_TIMEOUT_SECONDS` / `RESET_EXEC_TIMEOUT_SECONDS` / `RESET_CLEANUP_EXEC_TIMEOUT_SECONDS` / `RESET_KUBECTL_SLACK_SECONDS` / `RESET_HARD_CAP_SECONDS`（＝前四者和，附推導註解） |
| `src/services/helm.py:570-673` | `upgrade()` 加 `timeout_seconds: Optional[int]=None` → `--timeout {n}s` ＋ 該次 subprocess `n+10`；`None` 行為完全不變 |
| `src/models/schemas.py:103` | 只補 docstring（說明不再等 rollout；request 欄位不變） |

**平台（`src/**` → paas-odoo image）**

| 檔案 | 變更 |
| --- | --- |
| `src/services/paas_operator.py:18` 附近 | 新增 `RESET_CONFIG_TIMEOUT = 75` ＋ 階梯註解；**只**把 `reset_release_config`（`:494`）換成它。`HELM_OPERATION_TIMEOUT` 另有 9 個呼叫點（`:252/341/414/522/552/579/630/663`…）＝其他 helm 操作，**一律不動**（它們沒有 rollout 等待問題，改了等於擴大本案 blast radius） |
| `src/models/cloud_service.py:493-515` | 更新 C11 → C11′ 註解（提交點、階梯、窄窗與恢復手段）；新增 chart 版號漂移的 log |
| `src/static/src/paas/services/cloud_service.js` | 新增 `RESET_REQUEST_TIMEOUT_MS = 90_000`；`resetAdminPassword` 傳入 |
| `src/static/src/paas/components/modal/AdminPasswordModal.xml` | 加「服務正在重新啟動」文案 |
| `scripts/audit/repo-lint.py` | 新增 `check_reset_budget_ladder`（第 10 條）＋ 註冊進 `run_all`（`:737-746`）＋ 更新通過訊息（`:752-754`） |
| `.gitea/workflows/repo-lint.yml` | `paths` 加 `extra/paas-operator/**`（否則新 gate 對 operator-only PR 休眠） |
| `docs/decisions/patterns/` | 新增 `reset-async-budget-ladder.md`（跨層預算階梯 ＋ commit-on-secret 提交點）；`README.md`「何時必看」表加一列；`.claude/hooks/pattern-guard-map.json` 的 `paas-operator` 與 `model-write-guard` class 各 append 指標 |
| `docs/tracking/followups/follow-ups.md` | F-449-3 標 RESOLVED（指向本檔 ＋ PR）；新增 F-461-1（`upgrading` 停滯無上限） |

**pattern-guard 預期會攔的路徑**（Phase 2 動工前先讀對應 docs）：`src/models/cloud_service.py`（model-write-guard）、`src/static/src/paas/**`（owl-template / frontend-capability-gate）、`extra/paas-operator/**`（paas-operator：**merge 前必須本地跑 integration tests**）。

---

## 8. (d) 測試計畫

### 8.1 operator 單元（`extra/paas-operator/tests/test_reset_config.py`）

| # | 測試 | 動作 |
| --- | --- | --- |
| T1 | `test_happy_path_uses_stdin_and_preserves_config`（`:77`） | **改**：`mock_status.assert_not_called()`；response `rollout_awaited is False` |
| T2 | `test_upgrades_and_restarts_every_deployment_without_touching_config_json`（`:219`） | **改**：同上（刪 `:242` 的 status 斷言，改為 not_called） |
| T3 | `test_rollout_failure_restores_config_and_secret`（`:167`） | **改**：觸發點 `rollout_status` → `rollout_restart` 失敗；補償不再等 status |
| T4 | `test_rollout_failure_rolls_helm_back_and_re_rolls_pods`（`:302`） | **改**：同 T3；保留「主路 ＋ 補償都要重新下達 restart」的計數斷言 |
| T5 | `test_recovery_failure_asks_for_intervention`（`:325`） | **改**：觸發點改 restart；保留「rollback 失敗就不再滾 pod」 |
| T6 | **新** `test_reset_passes_bounded_helm_timeout` | `helm_service.upgrade` 收到 `timeout_seconds=RESET_HELM_TIMEOUT_SECONDS` |
| T7 | **新** `test_reset_hard_cap_equals_sum_of_parts` | `RESET_HARD_CAP_SECONDS == 40+12+5+3`（防單改一項） |
| T8 | **新** `test_patch_script_clears_stale_backups` | patch script 內含清殘留的 glob（§3.3 ③） |
| T9 | **新**（`tests/test_helm.py`） | `upgrade(timeout_seconds=30)` 渲染 `--timeout 30s` 且 subprocess timeout=40；**不傳時 argv 不得出現 `--timeout`**（回歸守線） |

### 8.2 operator integration（真 K8s，**merge 前本機必跑**）

| # | 測試 | 為什麼需要真叢集 |
| --- | --- | --- |
| I1 | 既有三支（`tests/integration/test_reset_config_carrier.py:197/234/254`） | **不需改**——它們斷言 Secret 值與 `restartedAt` annotation，不依賴 readiness；改後只會變快 |
| I2 | **新** `test_reset_returns_before_pods_are_ready` | **本案最有價值的新測試**：把合成 chart 加一個 `initContainer: sleep 40`（restart 後的新 pod 要 40s 才 Ready，但 upgrade 當下舊 pod 是 Ready 的 ⇒ `--atomic --wait` 立即通過）。斷言：200 ＋ **wall-clock < 20s** ＋ Secret 已換 ＋ 兩顆 `restartedAt` 已蓋。**這正是 #461 的機制**，把「不等」變成可機械驗證的行為，而不是等下一次 stg 才發現。 |
| I3 | 紅燈對照（手動，寫進 PR 描述） | 暫時把 `rollout_status` 加回去 → I2 必須因 wall-clock 超標而紅（證明斷言有牙，非恆真） |

> 環境：`cd extra/paas-operator && HELM_BINARY=$(which helm) pytest -m integration tests/integration/ -v`（context `docker-desktop`）。殘留 ns 風險與 recipe 見 `tests/integration/README.md`。

### 8.3 平台單元（`src/tests/`）

| # | 測試 | 動作 |
| --- | --- | --- |
| P1 | `test_mcp_admin_password.py:141/172/204/223` | **刻意不改**（§4.5）——它們保持綠就是 C11′ 沒有打破不變式的證據。PR 描述要明講這件事 |
| P2 | **新** `test_reset_uses_dedicated_timeout_budget` | mock `requests.Session.request`，斷言 reset 走的 `timeout == RESET_CONFIG_TIMEOUT`（75）而**非** `HELM_OPERATION_TIMEOUT`（120） |
| P3 | **新** `test_reset_success_stores_password_and_stamps_upgrading` | 補上目前只被間接覆蓋的成功路徑：落庫 ＋ `state=='upgrading'` |
| P4 | **新** `test_chart_version_drift_is_logged`（可選） | 版號漂移時有 log（可診斷性；用 `assertLogs`） |
| P5 | `test_admin_password_carrier_449.py` | 預期不受影響，全套回歸確認 |

### 8.4 gate 與 E2E

| # | 項目 | 說明 |
| --- | --- | --- |
| G1 | `scripts/audit/repo-lint.py --all` | 新 gate 自身要有紅燈對照：暫時把 `RESET_CONFIG_TIMEOUT` 改成 50（< 60）→ gate 必紅 |
| G2 | CI paths | 確認 `repo-lint.yml` 加了 `extra/paas-operator/**`（改前的 operator-only PR 不會跑 gate ＝ 守線休眠，#463 同型） |
| E1 | headed E2E（`tests/e2e/`，比照 `service-reset-button/`） | 按 Reset → modal 出現且含「重新啟動」文案 → badge 變 `Upgrading` → 輪詢後回 `Running`。**建議新目錄 `tests/e2e/service-reset-async/`**，沿用 #464 的 fixture 手法（ci-local sandbox） |
| S1 | stg 真機驗收 | 見 §10 |

---

## 9. (e) 明確不做

1. **不引入 202 / `task_id` / webhook / operator task registry**（§2.2）。
2. **不加 pending 密碼欄位**（§4.4）。
3. **不加 `wait_rollout` 旋鈕**（§3.1）。
4. **不修 `_is_container_ready` 的 fail-open**（F-423-1 已定案 v1 不動；reset 只是加入既有風險池，§6.1）。
5. **不修 `upgrading` 停滯無上限**（新開 F-461-1，§6.2）。
6. **不只把 120s 調大**——本案改的是結構（rollout 移出 request ＋ 宣告階梯 ＋ 機械 gate）；120→75 是階梯的**副產品**，方向甚至是變小。
7. **不動 chart**（`woow-paas-charts`）：不加 `checksum/secret` 讓 helm 自己滾 pod。理由不只是跨 repo——加了之後 pod template 每次都變 ⇒ `--atomic --wait` 會把 rollout 等待**拉回 request 內**，與本案方向相反（要同時改成不 atomic，等於把補償語意重寫一遍）。
8. **不做 image 縮小 / cache 預熱**（F-449-3 選項 c）：屬 chart／CI 領域，另案。
9. **不改 cron 1min 與前端 5s 輪詢頻率**。
10. **不動 `settings.helm_timeout=300` 全域值**——只在 reset 路徑加更緊的界。
11. **不碰 reset 的授權 / 能力 gate / 載體分流**（#357 C01/C05、#449 carrier、#471 conceal gate 全部原樣）。
12. **不碰** F-449-4（已由 #460 修）、#456（quota 公式）、§8.1 of #459（dashboard prefix bug，屬 #424）。

---

## 10. 驗收與發版

| 階段 | 判準 |
| --- | --- |
| 本機 | operator unit 全綠；**operator integration 全綠（含 I2 ＋ I3 紅燈對照）**；`--test-tags /woow_paas_platform` 全套無新增 F/E；`repo-lint.py --all` 綠（含 G1 紅燈對照） |
| PR | zh-tw；`Closes #461`；target `develop`；描述要含 (i) 階梯表 (ii) C11′ 與四條守線保持綠的說明 (iii) integration 實跑輸出 (iv) 發版順序 |
| stg | operator 先發（`operator-v*`）→ 平台後發（`stg-*`）→ 對 hermes-agent／open-design 各 reset **兩次**，第一次就必須 ≤20s 成功（＝#461 的正面證據）；記錄 wall-clock、`restartedAt`、Secret 值、DB 值、state 收斂時間 |
| prod | 隨下一次 rollout；發版單按目錄分流（operator ＋ 平台各一次），並記錄 operator 版本（新平台 ＋ 舊 operator 時階梯不成立，見 §3.1） |

---

## 11. 待裁決（leader3）

| # | 問題 | 建議 |
| --- | --- | --- |
| **D1** | API 語意採 A（同步 200 不等 rollout）而非 202/task | **採 A**（§2.2） |
| **D2** | 階梯數字 90/75/60（helm 30s）vs 105/90/60+（helm 60s） | **採 90/75/60**；代價是 chart-bump-riding reset 可能 30s fail-safe 失敗（§3.2 末） |
| **D3** | `config_json` backup 生命週期三選一 | **採 ③**（維持順序 ＋ patch script 清殘留，§3.3） |
| **D4** | pending 密碼欄位 | **不做**（§4.4） |
| **D5** | 四條 C11 守線測試 | **刻意不改**，作為不變式未破的證據（§4.5） |
| **D6** | repo-lint 第 9 條 gate ＋ CI paths 擴充要不要進本 PR | **要**——這是對「兩個預算互不知情」唯一的機械性回應；且 CI paths 不補等於 gate 休眠 |

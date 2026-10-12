# 回滾設定帳本同步 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Helm 成功回滾後，平台只同步 spec 允許的非憑證設定，失敗可重試且不覆蓋後續修改。

**Architecture:** Controller 將固定的新 release revision、目前帳本與操作時間寫入持久待辦。Background poll 在宣告 Ready 前呼叫 operator 的內部白名單 configuration projection endpoint（Helm --all --revision 固定值），用既有 _init_guarded row-lock/CAS 檢查 pending、帳本及 operation anchor；合格才經 _apply_helm_values 寫入。所有一般 _apply_helm_values 操作撤銷舊待辦；operator 只回標量與缺席鍵，禁止 credential 及 preserved 路徑，不回整份 values。

**Tech Stack:** Odoo 18 / PostgreSQL CAS、FastAPI/Pydantic、Helm 3、pytest。

---

### Task 1: 最小紅燈與 projection 合約
**Files:** Create `src/services/rollback_config.py`, `extra/paas-operator/src/services/config_projection.py`; Modify `extra/paas-operator/src/models/schemas.py`, `extra/paas-operator/src/api/releases.py`, `extra/paas-operator/src/services/helm.py`。
1. 新增最小 pytest，固定實際漂移的時區、PHP memory、WS、retention、DNS 等非密碼 leaf。
2. 驗證完整 Helm 預設值讀取、固定 revision、回應只含 allowlist 標量、preserved/malformed/object 路徑拒絕。
3. 先跑紅燈，再加安全投影；既有 rollback 及 preserving 協定不改。

### Task 2: 平台持久待辦與 CAS
**Files:** Modify `src/services/paas_operator.py`, `src/models/cloud_service.py`, `src/controllers/paas.py`。
1. 由 spec 產生公開鍵列表（排除 password、_init、create_only、保留路徑）；欠同步寫入 only-platform Text 欄。
2. Rollback 只記錄目標預期的新 revision、ledger snapshot、last_upgraded_at，不增加 request 內 operator 呼叫。
3. Poll 只有確認 projection 等於預期 revision，才取得 row lock並重驗 pending/ledger/anchor，套用 public values / 缺席欄刪除；保留現行密码。
4. 不成功就保持待辦，下輪重試；一般配置寫入取消待辦；stale generation/revision/anchor 不寫舊值。
5. API get/list 不增加寫入，不回 pending 的 ledger snapshot。

### Task 3: 有意義驗證與部署限制
**Files:** Create `extra/paas-operator/tests/test_config_projection.py`, `src/tests/test_rollback_settings_sync.py`（真 Odoo）；Create 私有 AST 呼叫鏈 harness（本機有限驗證）。
1. 驅動真 endpoint 與 Helm fake transport，所有現有 rollback 相關 operator tests 通過。
2. 本機 harness 只聲稱實際函式控制流/合約測試，不冒充 Odoo ORM/PostgreSQL sandbox；真 Odoo sandbox 和真 Helm integration 為部署前 required gates。
3. 保存來源 SHA、patch、RED/GREEN、pending/CAS/credential/缺席/失敗重試證據；不升 manifest，不代 merge 或簽 prod build approval。
4. 使用者已要求继续，直接執行，無須再詢問計畫執行模式。

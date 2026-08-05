# reference odoo test docker desktop paas image

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_odoo_test_docker_desktop_paas_image`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：本機 docker-desktop 用現成 paas-odoo 映像跑 Odoo 模組測試（免 woow-k3s/Tailscale，amd64 模擬但夠快）

本機 arm64 Mac 跑 woow_paas_platform Odoo 測試的**第三條路**（補 [[reference_odoo_test_amd64_woowk3s_cp_overlay]] 的 woow-k3s cp-overlay 與 [[reference_docker_build_local_odoo_test]]）：直接用本地已 pull 的 **paas-odoo 映像**（`docker images | grep paas-odoo`，如 `jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo:*`，內含 woow_paas_platform + 全依賴 addon）＋ **bind-mount worktree src** over baked addon。amd64 走 qemu 模擬但實測**夠快**：80 模組 install ~50s、單 test-class `-u` 循環 ~數秒~1 分。2026-07-09 #318 全程紅→綠＋全套 1502 tests 都在此跑。

Recipe（Bitnami odoo 映像）：
1. `docker network create net`；`docker run -d --name pg --network net -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -e POSTGRES_DB=postgres postgres:15`（原生 arm64、快）。
2. `docker run -d --name odoo --network net -v <worktree>/src:/opt/bitnami/odoo/custom-addons/woow_paas_platform --entrypoint bash <IMG> -c 'sleep infinity'`（覆蓋 entrypoint，用 exec 驅動）。
3. 一次性 install：`docker exec odoo /opt/bitnami/odoo/bin/odoo -d test -i woow_paas_platform --stop-after-init --without-demo=all --no-http --addons-path=/opt/bitnami/odoo/lib/odoo/addons,/opt/bitnami/odoo/custom-addons --data-dir=/tmp/odoo-data --db_host pg --db_user odoo --db_password odoo`。
4. 每次跑測試：同上但 `-u woow_paas_platform --test-enable --test-tags /woow_paas_platform:<Class>`；加 `--log-handler=odoo.tests.result:INFO`（`--log-level=warn` 會藏 0-fail 的 INFO summary），抓 `"N failed, M error(s) of K tests"`。

**踩雷點**：
- ⚠️⚠️ **背景任務被 kill ≠ 容器內的 odoo 停了；反過來也會咬人**（2026-08-02 #525 A2/A3 實證）：`TaskStop`／harness 砍掉的只是 `docker exec` **客戶端**，容器內的 python 進程繼續跑（`docker exec <c> pgrep -fa bin/odoo` 才看得到真相）。更糟的是它之後可能自己死掉，留下**沒有 summary 行的截斷 log** ⇒ `grep "failed, .* error(s) of"` 得到**空輸出**，看起來跟「還沒跑到」一模一樣。跑長時間全套一律 `docker exec -d <c> bash -c '... > /tmp/x.log 2>&1; echo "DONE exit=$?" >> /tmp/x.log'` 把進程與客戶端解耦，並用 `DONE exit=` 哨兵當完成判準；只用 `wc -c` 看 log 大小不夠（截斷的 log 也很大）。
- ⚠️⚠️ **`-i` 與 `--test-enable` 合成一步跑全套＝大規模假紅**（2026-08-02 #520 實證）：省略步驟 3／4 的分離、直接 `-i woow_paas_platform --test-enable --test-tags /woow_paas_platform`，實測 **29F/89E of 2171**；同一組 DB 改用兩步法（先 `-i` 裝完、再 `-u --test-enable`）＝ **0F/0E of 2171**。根因是安裝**當次**的 post_install 環境還沒收斂——例如 `company.sh_journal_id` 由 post_init_hook／`<function>` 設定，bare install 那一輪讀到的還是空值，所有走 `_create_and_confirm_subscription` 的測試集體撞 `Subscription journal is not configured`。→ **步驟 3／4 必須分兩次 exec，不可合併**；看到幾十個 F/E 先懷疑自己合併了步驟，別開始 bisect。附帶教訓：**測試 fixture 要自帶前提**（該檔已在 setUp 補 sale journal ＋ `res.company._ensure_subscription_journal()`），否則「陽性對照走不完」會讓配套的否定性斷言退化成假綠。
- ⚠️ **同一個 DB 先跑過 e2e／A/B 再跑全套＝假紅燈**（2026-07-31 #482 實證）：e2e 腳本 `set_param` 的 `ir.config_parameter`（如 `storage_class_name='hostpath'`）＋ 留下的 cloud service 記錄會被測試讀到，實測多出 **5F/11E**（例如 `test_redeploy_passes_reset_values_true` 報 `'hostpath' != 'longhorn-delete'`）。→ **跑全套一律另建乾淨 DB**（`-i` 新 DB），baseline 與被測分支兩邊都要乾淨才可比。
- core addons 是 `/opt/bitnami/odoo/lib/odoo/addons`（**`/opt/bitnami/odoo/addons` 是空的** → 直接用會 "not a valid addons directory"）。
- `psql` **不在 odoo 容器**內；查 DB 用 **pg 容器**的 psql（`docker exec pg psql -U odoo -d test -tAc "..."`）。
- postgres:15 無 pgvector → ai_base_gt WARNING「vector search disabled」但**非致命**（優雅降級）。
- `--without-demo=all` OK：wallet loyalty program（`loyalty_program_wallet`）是 **data 非 demo**，`get_wallet_loyalty_card()` 仍自動建 card。
- `TestNamespaceLeftSetUnion` 的 2 個 error：**#436 合併（`1054b29c`，2026-07-27）後語意已變，見下方時間線，別套用舊結論**。
  - **#436 之前**＝測試間污染，與有無 `-e` 無關：`test_agent_pod_phase_a_new_models.py:229-230` 的 `addCleanup(os.environ.pop, 'AGENT_RUNTIME_NAMESPACE_SECRET', None)` ＝**無條件刪除**而非還原原值 → agent 系列（字母序在前）跑完把外部注入的變數 pop 掉 → 字母序在後的 `test_reconciler_ns` 讀不到 → `agent_session.py:240` fail-closed raise UserError。當時給 `-e` 也沒用。
  - **#436 之後**＝**純環境依賴**（2026-07-28 於 `1054b29c` 實證）：污染源已改 `patch.dict` 還原原值，所以「有注入就綠、沒注入就紅」。同一容器：不給 `-e` → 全套 3F/**2E**；`docker exec -e AGENT_RUNTIME_NAMESPACE_SECRET=<任意值>` → 該 class **5/5 全綠**、全套 0E。
  → **判斷全套基準時**：先確認自己有沒有注入該變數，否則會拿 3F/2E 去對別人的 3F/0E 而誤判「多了 2 個 error」。跑全套一律帶 `docker exec -e AGENT_RUNTIME_NAMESPACE_SECRET=integration-test-secret`。
  - `patch.dict` 這個正確寫法已由 `scripts/audit/repo-lint.py` 的 `check_test_env_restore` gate 機械把關（#436）。
- **2026-07-28 實測 baseline（develop @ 1054b29c，有注入 `-e`）＝3F/0E of 1836 tests**；那 3F 就是 #437 的
  `TestRefundRequiresPayment`（已由 PR#452 修掉 → 0F/0E of 1841）。全套約 5 分半。
- **2026-07-27 實測 baseline（develop @ 4686eefd）＝3F/2E**：`TestNamespaceLeftSetUnion` ×2 ERROR
  （上述污染）＋ `TestRefundRequiresPayment` ×3 FAIL（`test_done_deduct_refunds_prorata` /
  `test_manual_invoice_pay_stamps_subscription_and_enables_refund` /
  `test_prorata_formula_still_reachable_without_gate`）。**歸因法**：另起一個容器 bind-mount
  主 repo 的乾淨 `src` + 另建一個 DB，只跑該 class → 對照取差集，別靠猜。全套約 5.5 分鐘
  （2519 tests / 332s），install 約 90s。

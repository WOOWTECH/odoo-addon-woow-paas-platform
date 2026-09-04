# reference odoo sandbox ci local two track test

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_odoo_sandbox_ci_local_two_track_test`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：ci-local 沙盒跑全套 Odoo 測試的三個坑＋「-i 邊裝邊測」假失敗 vs 「先裝再 -u 測」乾淨基準（實測差 28F/73E）

在 woow-k3s 用 **ci-local 模式**（`create.sh --image <stg-image>`，不加 `--mode`）跑 `woow_paas_platform` 全套測試，2026-07-26（#433）實測踩到三個坑：

**1. slug 不能以數字開頭。** `--branch 433seed` → helm install 失敗 `Service "433seed-...-postgres" is invalid: metadata.name ... DNS-1035 label must ... start with an alphabetic character`。用 `seed433` 這種字母開頭。失敗會留 2 顆 Released PV（resource policy keep），要自己 `kubectl delete pv`。

**2. ci-local 的 addons_path 與 stg image 不對齊。** chart 產的 `/etc/odoo/odoo.conf` 寫死 `addons_path = /mnt/extra-addons`（那是 **local 模式**的 hostPath 掛載點），但 `paas-odoo-stg` image 的 addon 在 `/opt/bitnami/odoo/custom-addons/`。結果 create.sh 卡在 `Install failed: woow_paas_platform state='not present'`。
**解**：exec 時用 CLI 覆蓋 conf——`--addons-path=/opt/bitnami/odoo/lib/odoo/addons,/opt/bitnami/odoo/custom-addons`。（`--mode stg` 沒這問題，見 [[reference_odoo_test_amd64_woowk3s_cp_overlay]]。）

**3. exec 進 pod 跑 odoo 必帶 `--http-port`**（主 process 佔 8069，否則 `Address already in use` 秒退）。HttpCase 需要真 HTTP，不能用 `--no-http`。

## ⚠️ 最重要：兩種跑法的結果差很多，別把「邊裝邊測」當基準

同一個 DB、同一份 code，只差在測試跑的時機：

| 跑法 | 結果 |
|---|---|
| `-i woow_paas_platform --test-enable --test-tags /woow_paas_platform`（邊裝邊測） | **31 failed, 75 error(s)** |
| 先 `-i` 裝好（不測），再 `-u woow_paas_platform --test-enable`（裝完再測） | **3 failed, 2 error(s)** |

那 28F/73E 全是假失敗：traceback 是 `UserError: No journal could be found in company My Company for any of those types: sale`——install transaction 內 accounting／ECPay／operator 尚未就緒（`--without-demo=all` 且 create.sh 的 post-create seed hook 沒跑到）。**要拿來對照「已知基準」的一律用兩步跑法。**

### 🔁 2026-08-02 再現一次（#501）——散文記錄擋不住，需要機械化

teammate 用一次到位 recipe 量到 `origin/develop` baseline ＝ **29 failed / 89 errors of 2171**，並準備用它當驗收基準開 PR。攔下的依據是本檔與 [[project_test_suite_zero_red_20260728]] 記載的「develop 已收斂 0F/0E，此後任何一條紅都是真信號」——**兩個數字差這麼多就是 recipe 錯的訊號**，不要當成「develop 長了新紅」。

該次的 journal 類佔 146 次命中（`Subscription journal is not configured` 61、`No journal could be found ... sale` 57、`NotNullViolation: journal_id` 28）。根因寫在 `src/hooks.py` 的 `post_init_hook` docstring：`res.company._ensure_subscription_journal()` 只連結**既有**的 sale journal，「sale journal 晚於本模組安裝的環境會讓 `sh_journal_id` 永遠 NULL」。

**判定手法（可複用）**：跑完後查 DB——sale journal 存在、但測試當下的 error 說找不到它 ⇒ 證明它是**測試之後**才建的。對照組更硬：兩段式跑的 DB `sh_journal_id=1`，三個一次到位的 DB 全是 NULL。

**為什麼要記這次**：本檔早就寫了這條，teammate 仍踩——因為這是**我的** memory，派出去的 agent 讀不到。散文落點無效，正確落點是 repo 內的機械檢查（例如 plugin `test.sh`（原 `woow-test.sh`）跑完偵測 error 訊息裡出現大量 journal 類 traceback 就 fail-loud 提示 recipe 錯），或至少寫進 CLAUDE.md 的測試段落。**派工時要主動把 recipe 講明，不要假設對方會查到。**

危險性在於差集法看似嚴謹卻會遮蔽：它只抓得到「新引入的紅」，抓不到「本來就沒跑起來的測試裡的回歸」——當 PR 改動範圍與失效測試範圍有交集時（該次是 subscription/wallet 對上 `_create_service` 的訂閱建立），差集分析完全不能替代乾淨基準實跑。

## ⚠️ 測試「總數」中途取樣會誤報——Odoo 分兩階段跑（2026-07-29 #423 W5a 實案）

Odoo 測試分 **at_install**（模組載入期，在 `loading.py` 印 `Modules loaded.` **之前**）與 **post_install**（由 `service/server.py` 在那**之後**才 `make_suite(..., 'post_install')`）兩階段。用 `until ps aux | grep -c odoo` 輪詢、逾時就取當下 `Starting Test` 計數，會報出**中途快照**而非最終總數。

實案：同一份 code 兩次回報 **907** 與 **896**（看起來像「淨增測試卻總數下降」＝與整檔被 deselect 的假綠同型訊號），實際上 896 正好卡在 `Modules loaded.` 那行（at_install 結束點）、907 是 post 階段剛開始，重跑到 **1866 仍在增加**。

**判「是否真有測試被略過」的三條硬證據**（別靠總數推論）：
1. **靜態計數對照**：`git grep -h "^    def test_" <branch> -- src/tests/ | wc -l` vs 實際跑完數字。本案 develop 1892 → 分支 1937，**差 45 恰等於新增三檔案數**，決定性。
2. **`N modules loaded` 對照**兩次執行是否相同（不同才是載入範圍問題）。
3. **grep skip 清單逐行看**——多數命中是被測程式自己的 log 或「測試名稱含 skipped」，不是測試被略過。

**正確做法**：計數一律等 `ps` 歸零，或以 `Modules loaded.` 行號切分後**分別報 at_install / post_install**，不報中途快照。另：debug 用的測試 log **留到 merge 後再清**，否則要比對時已無從調閱。

## 歸因用 baseline 對照，不要用推論

要證明「這批失敗不是我造成的」，最硬的證據是**同沙盒換 addon 版本跑第二次取差集**：把 pod 內 addon 換成 base 分支原版（`git archive --format=tar <base>:src | gzip`），用**完全相同的兩步流程**跑同一套，然後 `comm` 兩邊的 `FAIL:/ERROR:` 清單。

#433 實測：baseline `6 failed, 2 error(s) of 1799` vs 改動版 `3 failed, 2 error(s) of 1800`，差集恰為修好的 3 個、反向差集空集合（零新增失敗）。比 [[reference_k8s_sandbox_test_gotchas]] 的「看 diff 範圍推論」強得多，成本只多一輪（~6 分鐘）。

覆蓋 addon 用 tar 串流比逐檔 `kubectl cp` 快：
`tar czf - -C src . | kubectl exec -i <pod> -- sh -c 'rm -rf <dst> && mkdir -p <dst> && tar xzf - -C <dst>'`
（整包覆蓋就沒有 [[reference_odoo_test_amd64_woowk3s_cp_overlay]] 的 `tests/__init__.py` ImportError 問題。）

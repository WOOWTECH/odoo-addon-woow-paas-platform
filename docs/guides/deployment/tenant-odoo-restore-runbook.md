---
title: odoo 還原 runbook（租戶旁路 fallback／restoring 脫困／DR／platform profile 人工還原）
category: guides
status: draft
created: 2026-10-06T16:40:00Z
updated: 2026-10-16T12:00:00Z
related: [docs/guides/deployment/platform-odoo-cutover-runbook.md, docs/guides/deployment/platform-odoo-cutover-rehearsal-runbook.md, docs/guides/deployment/prod-clone-neutralization-runbook.md, docs/guides/deployment/prod-release-sop.md]
---

# odoo 還原 runbook

> 票：odoo-addons/woow_paas_platform#1217（Part of #1174）。chart 契約權威：woow-paas/woow-paas-charts `charts/woow-odoo/README.md` §備份／還原、`files/backup-restore.sh`、`files/restore.sh`。

## 狀態

`status: draft`。本檔依 #1217 票面分段落地，目前只完成 **P 段（platform profile 人工還原）**，內容是 2026-10-06 在沙盒實跑 chart 0.2.8 還原的版本。

| 段 | 狀態 |
|---|---|
| 第 0 步（`cloud_service` 手動寫 `restoring`） | 未落地 |
| 租戶旁路 fallback（stage → prepare → switch → finalize） | 未落地；P 段的「自組 Job」做法可直接沿用 |
| `restoring` 卡死脫困 | 未落地 |
| 升級回退（R3）人工版 | 部分：platform 版見 P 段「升級回退」，租戶版未落地 |
| DR 段 | 部分：platform 空 PG 的做法見 P 段「DR」 |
| **platform profile 人工還原** | ✅ 本檔 P 段（2026-10-06 演練實跑） |
| result.json 怎麼讀 | 部分：P6 列了演練看到的欄位 |

## 術語

引用 [CONTEXT.md](../../../CONTEXT.md)，不重新定義：
- **平台 Odoo**（:77）、**備份**（:97）、**升級前備份**（:121）、**還原**（:124）、**升級回退**（:127）、**prod clone**（:130）、**中和**（:133）。
- 平台不是 odoo `cloud_service`。本檔借用「還原」「升級回退」時都明寫「平台」。

## 操作者圖例

| 標記 | 意義 |
|---|---|
| 【人類】 | 發起人或叢集運維親手執行；agent 不代跑。 |
| 【agent】 | agent 可自行執行：只限 `dev-sandbox-*`、唯讀 get、本 repo 檔案。 |
| 【agent 執行・人類逐步放行】 | agent 備妥指令，人類逐步看過、放行後才執行（prod 寫入一律如此）。 |

## P platform profile 人工還原

platform release（`paas-odoo-platform`，`paas-system`）預設 `restore.enabled=false`，不 render `<release>-restore` 樣板 CronJob，也不經 operator（平台 release 由 Fleet 管）。

因此平台還原的做法是：**用同一份 chart 樣板在本機 render，自組 stage／switch Job**，等同 operator 端點的手動版。⛔ 不手刻 `pg_restore`：chart 的 stage 會一併做 manifest 身分／locale／PG 版本檢查、空間檢查、模組版本閘、附件引用驗證與 OID journal，手刻會全部跳過。

### P0 適用情境與差異

| 情境 | 與標準流程的差異 |
|---|---|
| 原地還原（同 release、同 ns、live DB 在） | 無；照 P1～P7 |
| 平台升級回退（還原升級前備份） | 目標 image 必須等於 manifest 的 `odoo_image_digest`（P1）；Fleet 先把 image 釘回同一顆 |
| DR：postgres PVC 重建後是空 PG | P4 前要先建 `odoo` 角色與 Odoo 形狀的佔位 live DB（P4-DR） |
| 跨 ns／跨 release（沙盒演練、DR 到新 ns） | 自組 Job 要把 `WOOW_RELEASE`／`WOOW_NAMESPACE` 覆寫成 manifest 值（P3）；沙盒還要中和（P8） |

### P1 選片

1. 【agent】從備份 Job 的 log 或 terminationMessage 讀 index（`backups[].backup_id／digest／db_size`）。
   - 唯讀即可，不必掛 PVC：`kubectl --context woow-k3s -n paas-system logs <backup job pod>`。
   - 列「備份成功」只看 `woow.paas/task=backup` 的 Job（chart README）。
2. 【agent】確認 `digest`（＝備份當下的 `upgrade_done` marker）與還原後要跑的 image digest 的關係。
   - **相等** ⇒ 開機時升級閘 skip（演練實測，P7）。
   - **不等** ⇒ 開機會跑 `-u all`（CONTEXT.md:103「模組升級」）。
   - ⚠ 升級前備份是升級**前**的 schema：`pre-upgrade-backup-210` 的 marker 是 .209 image（`76debd4b…`），不是 .210。要「還原後不跑 `-u`」，目標 image 就要釘回 manifest 那顆（＝升級回退，CONTEXT.md:127）。

### P2 前置（停機前，不影響服務）

1. 【agent 執行・人類逐步放行】suspend `paas-odoo-platform-backup` 與 `paas-odoo-platform-housekeeping`（兩者與還原共用 advisory lock 與 PVC）。
   - 確認沒有 Running 的 `component=db-backup` Job（含 `task=housekeeping`）。
2. 【agent】確認空間：data PVC 可用空間 ≥ filestore×1.1；PG 容量減掉全部 DB 大小 ≥ 備份 DB×1.2。
   - 不夠會在 stage 時 exit 10，不會動 live。
   - stage 期間 live 與 stage 兩份 filestore 並存（2026-10-06 約 2 GB×2）。

### P3 render 樣板、自組 Job

1. 【agent】在本機 render 同版號 chart 的 restore 樣板：

   ```bash
   helm template paas-odoo-platform <woow-odoo chart 目錄或 tgz，版號＝現役> -n paas-system \
     -f <Fleet bundle 的 values（不含密碼）> \
     --set credentials.postgresPassword=x --set credentials.odooPassword=x --set credentials.adminPasswd=x \
     --set restore.enabled=true --show-only templates/restore-cronjob.yaml > restore-cronjob.yaml
   ```

   - 樣板只用 `secretKeyRef` 引用 `<release>-credentials`，**不含密碼值** ⇒ credentials 給佔位值即可。
   - ⛔ 不要對 prod 跑 `helm get values`：平台 values 含三把密碼。
2. 【agent】從樣板的 `.spec.jobTemplate` 組出兩支 Job：`<release>-restore-<request_id>-stage`、`-switch`（演練用的 helper 見附錄 R）。
   - 注入 `MODE`、`REQUEST_ID`（1～24 個小寫英數／`_`／`-`）、`BACKUP_ID`（`<kind>/<UTC ts>`）。
   - switch 的 `activeDeadlineSeconds` 取樣板 annotation `woow.paas/restore-switch-active-deadline-seconds`（600）。
   - 兩支都設 `ttlSecondsAfterFinished`（7 d）。
   - 目標 image 與現役不同時，要**一起改**：initContainer `image-modules` 的 image 與 `WOOW_IMAGE_DIGEST`，以及主容器的 `TARGET_IMAGE_DIGEST`。
   - 跨 ns／release 時，覆寫 `WOOW_RELEASE`／`WOOW_NAMESPACE` 為 manifest 值。不覆寫會 exit 10 `Manifest identity/locale/PG version mismatch; cross-release restore requires support runbook`（演練實測）。
3. 【agent】節點放置（依 chart 版本）：
   - stage 與 switch 都掛 data＋backup 兩顆 RWO PVC。
   - **chart ≥ 0.2.12**（woow-paas-charts#198）：樣板 pod 自帶 required podAffinity 綁 server pod 同節點；server／cron 已 scale 0（switch、停權中的服務）時自我親和放行、排任意節點。**不必**也**不要**再手動 pin 節點，直接用樣板即可。
   - **chart ≤ 0.2.11**：樣板沒有 affinity。不 pin 節點會被排到非 server pod 的節點，data／backup PVC 回報 `FailedAttachVolume … Multi-Attach error`，stage Job 卡在 Init 直到 `activeDeadlineSeconds`（商用驗收 DATA-ODOO-005 實測 597 s 後 DeadlineExceeded，沒建 side／stage DB）。**必須**兩支 Job 都加 `nodeSelector` 釘在 data PVC 目前所在的 node（stage 取 server pod 的 node）。
   - 演練時 switch pod 被排到另一台 node，Longhorn 重新 attach，Job 總時長 99 s，腳本本身只跑 2 s。

### P4 stage（不停機）

1. 【agent 執行・人類逐步放行】apply stage Job，等它結束。
   - ⚠ 不要只 `kubectl wait --for=condition=complete`：Job 失敗時會等到 timeout。要輪詢 `.status.succeeded`／`.status.failed`。
2. 【agent】讀 log 與 terminationMessage：`{"status": "staged", "exit_code": 0, "result_path": "restore/<id>/result.json"}`。
   - exit 10：前置不符、什麼都沒建。
   - exit 20：stage 失敗、live 未動。
   - 兩者的處置見 chart README。

**P4-DR（空 PG 才需要）**：chart stage 要求 live DB 存在（`Live DB missing; use DR runbook`，exit 10）；platform bootstrap 在 DB 不存在時又 exit 3「先 restore」。兩邊互卡，所以 stage 前先由 superuser 做三件事：

1. 【agent 執行・人類逐步放行】建 `odoo` 角色，屬性同 bootstrap ②（`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`）。
   - 密碼取 `<release>-credentials` 的 `odoo-password`，經 psql 變數帶入、不上 argv。
2. 【agent 執行・人類逐步放行】建空的佔位 live DB：`CREATE DATABASE woow_prod ENCODING 'UTF8' LC_COLLATE 'C' TEMPLATE template0 OWNER odoo`。
3. 【agent 執行・人類逐步放行】佔位庫內以 `odoo` 建兩張空表：`res_users (id integer PRIMARY KEY, active boolean)`、`res_groups_users_rel (gid integer, uid integer)`。
   - 原因：switch 的 `security_snapshot` 會讀 live 的這兩張表。少了它，switch 在改名前就失敗，exit 3（不在 10／20／30／31 契約內，但 live／stage 都沒動，補表後同一個 request 可以重跑 switch；演練實測）。
   - switch 後佔位庫變成 `woow_prod__old_<id>`。核對 OID 等於 result.json 的 `live_oid` 後可以直接 DROP，或等 housekeeping 到期回收。

### P5 prepare（開始停機）

1. 【agent 執行・人類逐步放行】`component=server` **與** `component=cron` 同時 scale 0，等 pod 全部消失（#1217 留言 14679：platform 要兩個一起歸零）。
   - Fleet 管的 release 要用 Fleet 的方式改副本，不能直接 `kubectl scale`，否則會被拉回。
   - switch 會檢查 live 沒有 client 連線，有的話 exit 10。

### P6 switch

1. 【agent 執行・人類逐步放行】apply switch Job。
   - 成功：`{"status": "switched", ...}`；log `Switch complete; woow_prod__old_<id> retained for 24h`。
   - exit 30：已 undo。
   - exit 31，或 journal 停在 `switching`：**不要啟動 Odoo**，由 ops 依 OID 手動復原（chart README）。
2. 【agent】讀 `restore/<id>/result.json`（在 backup PVC 上）。演練看到的欄位：
   - 稽核欄位：`status`、`live_oid`、`stage_oid`、`old_db`、`expires_at`（`__old` 保留期限，+24 h）、`target_image_digest`、`release`／`namespace`。
   - `rewind`：`outgoing_mail_count`、`overdue_cron`（逾時 cron 清單）、`latest_payment_reference`。⚠ 最後這項是交易 reference，屬敏感資料，不要貼進票或留言。
   - `security_diff`：`users_added_or_changed`、`users_removed_or_changed`、`api_keys_added`、`api_keys_removed`。
     - 演練是從佔位庫切換，所以 51 位使用者、10 把 API key 全部列為「新增」。原地還原時要逐筆看：備份時點之後新增或撤銷的帳號與 API key 會被回捲。
3. switch 會清掉 `data/sessions/` ⇒ 所有使用者都要重新登入（與 cutover 相同）。

### P7 finalize（恢復服務）

1. 【agent 執行・人類逐步放行】server 先拉回 1；確認 `upgrade-gate` log：
   - marker 相等：`skip: woow_paas_platform.upgrade_done.digest == current image digest`（演練實測：web Ready 24 s）。
   - 不等：跑 `-u all`，timeout 20 分，屬預期。
2. 【agent 執行・人類逐步放行】cron 拉回 1。`cron-wait` 會等到 marker 相等才放行。
   - 演練中第一次輪詢遇到 NP 生效延遲，10 s 後放行；cron Ready 22 s。
3. 【agent 執行・人類逐步放行】unsuspend backup／housekeeping，並跑一次 on-demand 備份。
4. 【人類】升級回退時，Fleet 的 image 釘回要保持到 forward-fix 就緒（CONTEXT.md:127）。

### P8 驗收（演練用的方法，原地還原同樣適用）

| 項目 | 方法 | 2026-10-06 演練 |
|---|---|---|
| 搬運完整（跨 ns 才有） | 來源端與接收端 `sha256sum --strict -c` | tar／manifest 全符 |
| 模組 | 還原後 DB 的 `name=latest_version`（`state in installed／to upgrade`，`ORDER BY name COLLATE "C"`）sha256＝manifest `modules` 同口徑 | 208 筆，`f02cb8cf135e`＝`f02cb8cf135e` |
| 附件清單 | `SELECT DISTINCT store_fname … ORDER BY store_fname` 的 sha256＝備份組 `attachments.txt` 的 sha256 | 7,266 行，`e334c179…` 全等 |
| filestore | 檔數與位元組（`find -type f`）＝來源；全部 distinct `store_fname` 檔案的 sha1＝`ir_attachment.checksum` | 7,368 檔／2,029,208,943 B 相等；7,266／7,266 sha1 全符 |
| 憑證與身分 | `database.secret`／`database.uuid`／admin login:password 的 sha256 前 12 碼＝prod（中和前比） | 三項皆等於 prod 唯讀查詢 |
| 時點 | prod 唯讀：`create_date <= 備份時點` 的筆數＝還原後 | partner 652、move line 7,745、payment tx 90、users 51 相等；attachment 差 8 筆，全是 `/web/assets/%`（.210 升級重建 asset bundle），排除後 12,427／7,214 兩邊相等 |
| 開機 | `/web/health`、`/web/login`、admin 登入、`/woow`、附件下載 | 200／200（303 → `/en/web/login`）／uid 2／200（含 `assets_paas`）／抽樣 8 筆 200 且長度相符 |
| 沒有跑 `-u` | 開機前後的模組指紋相同 | `addfd4b3bad9`（installed 208），開機前後一致 |

- ⚠ manifest 的 `filestore_bytes` 用 `du -sb`，會把目錄本身算進去。演練多出 1,052,672 B，正好是 257 個目錄 × 4096，不是資料差異。比對用 `find -type f` 加總。
- ⚠ 以 `kubectl port-forward` 走 http 驗登入時，`session_id` cookie 帶 `Secure`，標準 cookie jar 不會回送。要手動帶 `Cookie` header，否則登入後打 `/woow` 仍會被導回登入頁、附件回 404（演練第一次誤判）。

### P9 收尾

1. 【agent】`__old_<id>` 與它的 filestore 保留到 `expires_at`（24 h）後，由下一次 housekeeping 回收；這是這次還原唯一的 undo。
2. 【agent】`restore/<id>/result.json` 留在 backup PVC 作稽核；result.json 的 `rewind` 與 `security_diff` 摘要（不含 reference 原文）寫進工單。

## 演練紀錄（2026-10-06，跨 ns＋中和）

> 授權：#1231 留言 18707。沙盒 `dev-sandbox-restore-1`（UID `3c75e633…`；chart 0.2.8＋`paas-odoo-official:20261005.1128@sha256:76debd4b…`，lockdown），2026-10-06T16:29Z 已銷毀，3＋1 顆 PV 與 Longhorn volume 皆 NotFound。只列 count／耗時／指紋前綴。

| 段 | 耗時與結果 |
|---|---|
| 來源（【人類】，paas-system） | 唯讀掛 `paas-odoo-backup-pvc`，把 `pre-upgrade/2026-10-06T09:03:29Z` 原樣 tar：53 s／2,062,960,640 B；叢集內 PUT 到沙盒 receiver 38 s；三個 PUT 皆 201、sha256 相符；`attachments.txt` 引用缺檔 0 |
| 關搬運洞＋lockdown gate | np_content PASS、marker present、probe blocked、transfer_hole absent |
| 解包到沙盒 backup PVC | Job 93 s（`sha256sum -c` 33 s＋untar 41 s）；db.dump／attachments.txt／filestore 與來源逐項相同；incoming 隨即刪除 |
| 身分檢查負向測試 | 不覆寫 `WOOW_RELEASE／WOOW_NAMESPACE` ⇒ exit 10（符合契約） |
| P4-DR 佔位 | 建 `odoo` 角色＋空 `woow_prod`。第一次 switch 撞 `relation "res_users" does not exist` ⇒ exit 3、未改名；補兩張空表後重跑成功 |
| **stage** | 腳本 88 s（Job 114 s）：建 `woow_prod__restore_drill1`、`pg_restore`、rsync 2 GB、引用驗證、模組閘 → `staged` |
| **switch** | 腳本 2 s（Job 99 s，大部分是跨 node 重新 attach）→ `switched`；佔位舊庫核對 OID 後 DROP |
| 指紋（中和前） | 見 P8 表，全部相符 |
| Stage A 中和＋gate | 33 s；斷言 **64 PASS／0 FAIL**；`upgrade_done` 為備份本身的值，**沒有預寫** |
| web | Ready 24 s；升級閘 skip；bootstrap 只跑 ①② |
| Stage B／cron／gate | Stage B 32 s、64/0；cron Ready 22 s；cron 起來後 gate 64/0 |
| log | web／cron：Traceback 0、外連失敗 0；ERROR 各 1 行（開機固定的 `odoo.tests.common` import） |

**原地還原的停機估算**（由演練實測組合）：

| 步 | 估計 |
|---|---|
| stage | 約 90 s，不停機 |
| server＋cron 歸 0 | 約 40～90 s（優雅終止，cutover 實測同級） |
| switch | 腳本 2 s，加上 Job 起跑；釘 node 約 10～20 s，跨 node 重新 attach 約 1.5 分 |
| web Ready | 約 25 s；cron 約 20 s |
| **技術停機** | **約 2～4 分**（不含 Fleet 同步與人工驗收） |

## 附錄 R：自組 Job helper（演練實用版）

```python
#!/usr/bin/env python3
"""mkjob.py <rendered restore-cronjob.yaml> <restore-stage|restore-switch> <request_id> <backup_id> [KEY=VAL ...]
從 chart <release>-restore 樣板組 stage／switch Job；額外 KEY=VAL 覆寫 restore 容器 env（例：WOOW_RELEASE=…）。"""
import sys, yaml
src, mode, rid, bid, *extra = sys.argv[1:]
cj = yaml.safe_load(open(src)); jt = cj["spec"]["jobTemplate"]; ann = cj["metadata"]["annotations"]
suffix = "stage" if mode in ("restore-stage", "restore-side") else "switch"
job = {"apiVersion": "batch/v1", "kind": "Job",
       "metadata": {"name": f"{cj['metadata']['name']}-{rid}-{suffix}", "labels": jt["metadata"]["labels"]},
       "spec": dict(jt["spec"])}
if cj["metadata"].get("namespace"):
    job["metadata"]["namespace"] = cj["metadata"]["namespace"]
if suffix == "switch":
    job["spec"]["activeDeadlineSeconds"] = int(ann["woow.paas/restore-switch-active-deadline-seconds"])
job["spec"]["ttlSecondsAfterFinished"] = 7 * 86400
over = {"MODE": mode, "REQUEST_ID": rid, "BACKUP_ID": bid, **dict(kv.split("=", 1) for kv in extra)}
c = job["spec"]["template"]["spec"]["containers"][0]
for e in c["env"]:
    if e["name"] in over:
        e.pop("valueFrom", None); e["value"] = over.pop(e["name"])
c["env"] += [{"name": k, "value": v} for k, v in over.items()]
yaml.safe_dump(job, sys.stdout, sort_keys=False)
```

- 換目標 image 時，另外改 initContainer `image-modules` 的 `image`／`WOOW_IMAGE_DIGEST` 與主容器 `TARGET_IMAGE_DIGEST`（P3 第 2 步）；helper 不代改，避免漏改其中一處。
- 跨 ns 搬運（沙盒演練）：receiver／搬運洞用 `seed-from-prod.sh receive`；來源 Job 唯讀掛 backup PVC、整個備份組目錄 tar＋HTTP PUT（形狀同 [cutover runbook](platform-odoo-cutover-runbook.md) 附錄 T4）；restore 前用 `seed-from-prod.sh close` 關洞並跑 lockdown gate；中和用 `neutralize-clone.sh`（**不帶** `--write-upgrade-marker`，才能驗到備份本身的 marker）。細節見 [prod clone 中和 runbook](prod-clone-neutralization-runbook.md)。

## 參考

- #1174、#1217、#1231（演練授權 18707）、#1144、#1145、#1162、#1206；woow-paas/woow-paas-charts#81／#87

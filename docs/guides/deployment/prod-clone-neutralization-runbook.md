---
title: prod clone 中和：彩排沙盒的取資料、中和、斷言與銷毀 runbook
category: guides
status: draft
created: 2026-10-05
updated: 2026-10-05
related: [docs/guides/deployment/platform-odoo-cutover-rehearsal-runbook.md, docs/guides/deployment/shared-cluster-paas-scope-guard.md, docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md]
---

# prod clone 中和：彩排沙盒的取資料、中和、斷言與銷毀 runbook

> 票：odoo-addons/woow_paas_platform#1206（交付 3）。規範正本：#1162 resolution（留言 14057）與 charts#81 Round 2 對它的修訂（#1162 留言 14676、#1206 留言 14673）。
> 這份 runbook 只管「把平台 `woow_prod` 的 prod clone 安全地灌進拋棄式沙盒，然後銷毀」；chart 切換彩排本身見 [彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md)（它的 A3 引用本檔）。

## 狀態

`status: draft`——第一次真彩排前，下列兩件事必須完成，才改 `active`：

1. 【人類】覆核並 commit 斷言 fixture（`.claude/skills/woow-e2e-fixtures/scripts/prod-clone/assertions.toml`）。改 fixture 就是改 gate。
2. 【人類】§B 取資料段的 recipe 在 prod 實跑一次。標〔待驗〕的指令，要在那次確認可行後回填。

## 操作者圖例

| 標記 | 意義 |
|---|---|
| 【人類】 | Eugene 或叢集運維親手執行。agent 不代跑（classifier 擋 prod 讀取與資料外流；#1162 D8）。 |
| 【agent】 | agent 可自行執行，只限 `dev-sandbox-<slug>`。 |
| 【覆核者】 | 覆核者**親跑**（#1162 D10 (4)），不能只看 agent 貼的輸出。 |

## 工具一覽

| 工具 | 位置 | 做什麼 |
|---|---|---|
| `create --mode platform`＋`SANDBOX_NEUTRALIZE_LOCKDOWN=1` | woow-dev plugin（cc/woowprd#8、#5） | 建 lockdown 平台沙盒：web／cron 0 副本、egress 只剩 DNS＋同 ns |
| `seed-from-prod.sh receive／restore／stage-b／gate／close` | `.claude/skills/woow-e2e-fixtures/scripts/` | 沙盒段：搬運洞、restore、Stage A／B、gate |
| `neutralize-clone.sh` | 同上 | 中和：官方 `odoo neutralize --stdout`＋平台段（#1201）＋補差 SQL＋沙盒密碼，同一個交易；結尾跑 gate |
| 斷言 fixture | `…/prod-clone/assertions.toml` | 約 60 條斷言（A 排程、B 外連、C 身分、D 進入面、E 租戶憑證、P PII、K 網路／叢集） |

## A 授權與稽核

1. 【人類】Eugene 在本次彩排的 issue 明示核准這一次 prod clone（#1162 D14）。每次 clone 都重新核准。
2. 【人類】同一則或下一則 comment 留稽核欄位：核准人、時間、來源（live `pg_dump` 或備份檔路徑）、各檔 sha256、目標 ns `dev-sandbox-<slug>`、預定銷毀時間。

## B 建沙盒、開搬運洞、取資料

1. 【agent】建 lockdown 平台沙盒（slug 必填、不從 branch 推導；⛔ 不可是 `stg`）：

   ```bash
   SANDBOX_NEUTRALIZE_LOCKDOWN=1 bash <woow-dev plugin>/skills/k8s-dev-sandbox/scripts/create.sh --mode platform \
     --repo "$(git rev-parse --show-toplevel)" --context woow-k3s --branch <slug> --ttl 24h \
     --chart-version <X.Y.Z> --platform-image '<registry>/<repo>:<tag>@sha256:<digest>'
   ```

   - 驗：namespace 有 `dev-sandbox.io/neutralize-lockdown=1`；web／cron 0/0。
2. 【agent】開搬運洞：`seed-from-prod.sh receive <slug> --context woow-k3s --set <set> --size <N>Gi`。
   - 會建 `prod-clone-incoming` PVC、`prod-clone-receiver` pod（HTTP PUT）與 Service，以及只放行 `paas-system → receiver:8080` 的 NP。
   - 搬運洞只在 web／cron 0 副本期間存在；`restore` 第一步就把它關掉並進 gate（K04）。
   - `--size` 至少 dump＋filestore 的 1.3 倍。⚠ Longhorn 排程額度是瓶頸（woow-machines-operator#5 留言 15571）。
3. 【人類】在 `paas-system` 產出 #1145 備份組形狀的四個檔，用叢集內 HTTP PUT 送進 receiver（pitfall [kubectl exec 串流截斷](../../tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md) 的修法 1）。
   - ⛔ 不用 `kubectl exec … > file`／`kubectl cp`：超過約 20 MB 會尾端截斷且 exit 0。
   - 必要檔：`db.dump`（`pg_dump -Fc`）、`filestore.tar`（filestore 目錄內容，相對路徑）、`manifest.json`、`SHA256SUMS`；選配 `attachments.txt`。SHA256SUMS 最後送。
   - token：`kubectl --context woow-k3s -n dev-sandbox-<slug> get secret prod-clone-upload-token -o jsonpath='{.data.token}' | base64 -d`。
   - 每次 PUT 回應 `<bytes> <sha256>`，與來源端的 `sha256sum` 比對。
   - recipe 〔待驗：Bitnami postgresql／odoo image 內有沒有 `curl`；沒有的話改在 `paas-system` 起一次性 pod，由人類執行〕：

   ```bash
   # 來源 A：live dump（在 prod postgres pod 內；串流 PUT，不落 prod 磁碟）
   URL=http://prod-clone-receiver.dev-sandbox-<slug>.svc.cluster.local:8080/<set>
   kubectl --context woow-k3s exec -n paas-system paas-odoo-postgresql-0 -- bash -c '
     export PGPASSWORD="$(cat $POSTGRES_PASSWORD_FILE)"
     pg_dump -U bn_odoo -d woow_prod -Fc | tee >(sha256sum >&2) |
       curl -sS -T - -H "X-Upload-Token: <token>" "'"$URL"'/db.dump"'
   # manifest：同 woow-odoo chart backup-common.sh 的欄位（encoding／datcollate／datctype／datlocprovider／extensions…）
   kubectl --context woow-k3s exec -n paas-system paas-odoo-postgresql-0 -- bash -c '
     export PGPASSWORD="$(cat $POSTGRES_PASSWORD_FILE)"
     psql -U bn_odoo -d woow_prod -tA -c "SELECT json_build_object(
       '\''schema_version'\'',1,'\''db_name'\'',current_database(),'\''encoding'\'',pg_encoding_to_char(encoding),
       '\''datcollate'\'',datcollate,'\''datctype'\'',datctype,'\''datlocprovider'\'',datlocprovider,
       '\''extensions'\'',(SELECT json_object_agg(extname,extversion) FROM pg_extension),
       '\''pg_version'\'',current_setting('\''server_version'\''),'\''db_size_bytes'\'',pg_database_size(current_database()))
       FROM pg_database WHERE datname=current_database()" > /tmp/manifest.json && sha256sum /tmp/manifest.json &&
     curl -sS -T /tmp/manifest.json -H "X-Upload-Token: <token>" "'"$URL"'/manifest.json"; rm -f /tmp/manifest.json'
   # filestore（在 prod odoo pod 內；路徑〔待驗〕）
   kubectl --context woow-k3s exec -n paas-system deploy/paas-odoo -- bash -c '
     tar -C /bitnami/odoo/data/filestore/woow_prod -cf - . | tee >(sha256sum >&2) |
       curl -sS -T - -H "X-Upload-Token: <token>" "'"$URL"'/filestore.tar"'
   # 最後：把上面三個 sha256 組成 SHA256SUMS（格式「<sha256>  <file>」）PUT 到 $URL/SHA256SUMS
   ```

   - 來源 B：`paas-odoo-backup-pvc` 上的每日 dump（#1206 交付 1）。filestore 照上面另取，manifest 照上面另產。
   - 兩個來源都不是同一個 snapshot（dump 與 filestore 時間不同）：少數 `attachments.txt` 引用缺檔是預期內；不送 `attachments.txt` 就不驗。
4. 【人類】在 A 的稽核 comment 補上各檔 sha256。

## C restore＋Stage A（中和）

1. 【agent】`seed-from-prod.sh restore <slug> --context woow-k3s --set <set>`，依序：
   1. web／cron 必須 0 副本（含已在跑的 pod）。
   2. 關搬運洞（receiver pod／Service／NP），apply 正向標記 NP `prod-clone-lockdown-marker`。
   3. lockdown gate：NP 內容檢查＋標記 NP＋行為探針（沙盒內連 `api.line.me:443` 必須失敗，同 ns postgres 必須通）。任一不過就拒絕 restore。
   4. 一次性 Job restore：`sha256sum --strict -c SHA256SUMS`、`pg_restore -f /dev/null` 全讀。建庫 locale 取 manifest 的來源實測值（prod 是 `en_US.UTF-8`／`en_US.UTF-8`；charts#81 R2-Q4），不套 chart canonical。
      superuser 先建 `odoo` 角色（`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`，密碼＝chart credentials；platform web 0 副本時 bootstrap ② 還沒跑過）。
      資料以 `odoo` 角色 `pg_restore -1 --exit-on-error --no-owner --no-privileges -L <濾掉 EXTENSION>` 還原；⛔ 不用 `-C`、⛔ 不用 `--no-comments`。
      filestore 解到 `/var/lib/odoo/filestore/<db>`，chown 100:101。
      manifest 有 `vector` 或未知 extension 就拒絕（Step 1 期間 `postgres.extensions.vector=false`，R2-Q6）。
   5. 刪 `prod-clone-incoming` PVC（＝刪 `incoming/` 原始檔；K05）。
   6. Stage A：`neutralize-clone.sh … --write-upgrade-marker`，結尾跑斷言 gate。
      - 預寫 `woow_paas_platform.upgrade_done={"digest":<image digest>,"core_date":<image 內 dpkg 版本>}`，屬 Stage A（#1206 留言 14673）。不寫 `upgrade_attempt`。
      - 彩排 runbook A4 第 1 步要先驗「無 marker 的 fail-loud」時，改用 `restore … --no-upgrade-marker`，驗完再單獨跑 `neutralize-clone.sh <slug> --context woow-k3s --write-upgrade-marker`。
2. 【agent】gate 全綠才往下；任何一條 FAIL ⇒ ⛔ 不得把 web 拉起。

### 中和內容（摘要；全文在腳本與 SQL 檔）

| 層 | 怎麼做 | 來源 |
|---|---|---|
| 官方 | 同一張 image 起一次性 Job 跑 `odoo neutralize --stdout`：停 cron（只留 `base.autovacuum_job` 當探針）、`database.is_neutralized=true`、停 mail server 並插 dummy（host `invalid`）、清 VAPID、停 payment provider、IAP token 加 `+disabled`、Google Calendar token… | Odoo 18 各模組 `data/neutralize.sql` |
| 平台段 | #1201 的 `src/data/neutralize.sql`：operator 黑洞 `http://127.0.0.1:9`＋`neutralized`、`deploy_env=stg`、`namespace_prefix=paas-ws-stg-`、agent runtime 關、第 E 層租戶憑證、平台 OAuth 三表清空、cron nextcall＋100 年、`ir_cron_trigger` 清空。image 早於 #1201 時腳本改用 repo 版 | #1162 D3-A／D4／D3-E |
| 補差 | `prod-clone/supplement.sql`：SMTP 憑證、webhook、gmail token、mail queue、IAP、ECPay 測試碼＋`auto_invoice=manual`、LINE 三處、livechat AI、gdrive、`base_automation` 停用、`web.base.url`＋freeze、`website.domain`、`database.secret`／`uuid` 輪替、API key／TOTP／OAuth、PII 遮罩、快取 signaling | #1162 D3-B／C／D、D6 |
| 沙盒密碼 | 明文只在沙盒 Secret `<slug>-prod-clone-login`；hash 在叢集內用 image 的 passlib 算（pbkdf2_sha512、rounds 600000），只換原本有密碼的使用者 | #1162 D3-D |
| conf | `email_from` 由 plugin 給中和值 `noreply@sandbox.invalid`；`admin_passwd` 隨機、`list_db=False` 由 woow-odoo chart 內建 | R2-Q5、cc/woowprd#8 |

⛔ 不做（明寫接受）：`mail.catchall.domain`／`mail.default.from`（Odoo 18 死參數）、`mail_alias_domain`、`res_users.login`、`mail_message.email_from` 與 filestore 內的 PDF。這些靠 §G 的銷毀與 §F 的產物規則補償。

## D 起 Odoo 之後：Stage B 與每次的 gate

> #1162 不變式：**任何會啟動 Odoo 程序的步驟之後，都要再過一次 gate**（含日後新 chart 的 initContainer／cron pod）。

1. 【agent】web 1、cron 0：`/woow-dev:k8s-dev-sandbox scale <slug> --context woow-k3s --component server --replicas 1`；讀 `upgrade-gate` log，預期 marker 相等 skip。
2. 【agent】Stage B：`seed-from-prod.sh stage-b <slug> --context woow-k3s`（重跑整批中和＋gate；web 在跑也可以，SQL 結尾會推進 `base_*_signaling` 讓 worker 丟快取）。
3. 【agent】cron 1：`scale … --component cron --replicas 1`，接著 `seed-from-prod.sh gate <slug> --context woow-k3s`。
4. 【agent】之後每次 scale、`-u`、重啟，都再跑一次 `gate`。

## E 斷言表與外連證據

### 斷言表

- 【agent】每次 gate 的輸出（`check_id  status  actual ⇢ expected`，結尾 `ASSERTIONS pass=… fail=…`）原樣貼進彩排報告。輸出只有 count／聚合，不含資料值。
- `I01_admin_fingerprint`：`res_users` id 2 的 login／password 指紋（sha256 前 12 碼）。首次開機前後必須相同（R2-Q2；取代原本「bootstrap 標記不預寫」那一條）。
- 【覆核者】親跑：`seed-from-prod.sh gate <slug> --context woow-k3s`。

### 外連證據（彩排後）

1. 【agent】記 T0（UTC），觸發會外連的動作（例：寄信、確認訂單、建 cloud_service），等 **≥120 秒**（ecpay_invoice_tw SDK timeout＝120 s）再 grep。
2. 【agent】client 側（沙盒 web／cron pod log）grep 以下標記並記錄筆數：
   - SMTP：`neutralization - disable emails`、`Mail delivery failed`
   - operator：`PaaS Operator connection error` 且帶 `127.0.0.1:9`
   - 另外 `iap jsonrpc`、`Missing web push vapid keys`
3. 【agent】`mail_mail` 判準：`state = 'outgoing' AND write_date > T0` 的筆數（`mark_outgoing()` 不動 `create_date`）。
4. 【覆核者】receiver 側：prod 與 stg operator 的 uvicorn access log，扣掉 `/health` 之後，來自沙盒 pod IP 的請求必須是 0。
   - 沙盒 pod IP 在每個階段記錄：`kubectl --context woow-k3s -n dev-sandbox-<slug> get pod -o wide`。K3s 的 pod CIDR 是各 node 共用的 /24，不能用 CIDR 判斷，只能逐一比對 IP。

   ```bash
   kubectl --context woow-k3s -n paas-system logs deploy/paas-operator --since=<彩排時長> | grep -v '/health' | grep -cE '<ip1>|<ip2>|…'
   kubectl --context woow-k3s -n paas-stg-system logs deploy/paas-operator-stg --since=<彩排時長> | grep -v '/health' | grep -cE '<ip1>|<ip2>|…'
   ```

5. payment_ecpay 的收單走瀏覽器直連，pod log 看不到——在報告註記，不算證據缺口。

## F 差異表、既知限制、產物規則

### 差異表（彩排 vs 正式切換）

| 項目 | 彩排 | 正式切換 |
|---|---|---|
| cron | 全停，只留 `base.autovacuum_job` 當探針 | 照常 |
| operator | 黑洞 `http://127.0.0.1:9`＋`neutralized` | 真 operator |
| `database.secret`／`database.uuid` | 已輪替 | **維持原值**（#1142 留言 14050 ②） |
| 使用者密碼 | 叢集內隨機重設 | 不動 |
| `deploy_env` | `stg`（operator 已黑洞，測不到 CF 物件更新路徑） | `prod` |
| conf `email_from` | `noreply@sandbox.invalid` | CD `paas-odoo/values-prod.yaml` 的 `odooEmail` |
| admin（`res_users` id 2） | 首次開機前後 login／password 指紋不變（斷言） | 同左 |
| `upgrade_done` | Stage A 預寫 | 切換前預寫 |
| PII | 遮罩 | 原值 |

### 既知限制

- clone 當下卡在 `deploying`／`upgrading` 的 cloud_service，整場不會動（`_check_transition_timeout` 不會被叫到；#1162 D4）。
- **K3s 對新 pod 套 NP 有約 1.3 秒延遲**（2026-10-05 實測，cc/woowprd#13）：新 pod 開頭約 1～1.5 秒 egress 仍通，同 ns 的連線反而會被拒。
  Odoo 主程序在 initContainer 之後才起，落在窗口外；行為探針與本工具的一次性 Job 都先等待再動作。
- 同一個延遲也會讓新 pod 開頭幾秒連不到同 ns 的 postgres（表現為 `Connection refused`）：chart 的 `cron-wait` 第一次輪詢會失敗、約 10 秒後放行（2026-10-05 實測），本工具的 Job 也先等 DB 可連再動作。
- `PROD_CLONE_FIXTURE_OVERRIDE` 只給工具自己用合成資料實測（合成 fixture 沒裝 website 等模組）；真彩排 ⛔ 不得設定，gate 會大聲標示被覆寫。
- 平台 image 若早於 #1201，`odoo neutralize` 不含平台段，由腳本補 repo 版（`neutralize-clone.sh` 會印警告）。
- 彩排用的 image 綁定當時的 addon 版號，可能比 prod DB 舊（升級閘 marker 相等時不跑 `-u`）。這是彩排 runbook 的範圍。

### 產物 PII 規則

- 報告只輸出 count／聚合；截圖不拍含 PII 的頁面。
- 斷言表、log 摘錄不附到 Gitea；報告寫完後刪掉本機的產物目錄。

## G 銷毀

1. 【agent】`/woow-dev:k8s-dev-sandbox destroy <slug> --context woow-k3s`。
2. 【agent】銷毀證據：namespace、PVC、PV 都已不存在（`kubectl get ns dev-sandbox-<slug>`、`kubectl get pv | grep dev-sandbox-<slug>` 皆空）。TTL 只是後備（cc/woowprd#7：TTL CronJob 會靜默失效）。
3. 【人類】覆核銷毀證據，在 A 的稽核 comment 回填實際銷毀時間。

## 相關

- [彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md)（A3 引用本檔）
- [`shared-cluster-paas-scope-guard.md`](shared-cluster-paas-scope-guard.md)
- #1162、#1200（量測）、#1201、#1205、#1206、#1231、cc/woowprd#5／#6／#8、woow-paas/woow-paas-charts#81

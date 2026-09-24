---
title: Bitnami odoo chart 28.2.10 硬套官方 odoo:18.0 的實測
category: scout
status: draft
created: 2026-09-20
related:
  - docs/scout/2026-09-18-odoo-bitnami-coupling-inventory.md
  - docs/scout/2026-09-18-harumi-odoo-paas-migration-plan.md
---

# Bitnami odoo chart 28.2.10 硬套官方 `odoo:18.0` 的實測

> Wayfinder 票 [#1136](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1136)（map [#1134](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1134)）的 prototype 產出。
> 環境：woow-k3s，namespace `dev-sandbox-odoo1136`，chart `oci://registry-1.docker.io/bitnamicharts/odoo:28.2.10`（digest `sha256:2072042e…`），image `docker.io/odoo:18.0`（實跑版本 `18.0-20260908`），storageClass `longhorn-delete`。
> 三個 release：`t1136`（首開 → 升級冪等）、`t1136b`（首開含 admin 密碼覆寫 ＋ workers=2）、`t1136c`（postgres 換 `pgvector/pgvector:pg17`）。

## 1. 一句話結論

**能跑，而且今天就能跑**——但把 chart 的 odoo 語意全部繞掉之後，它只剩 Deployment／Service／PVC／Ingress 的樣板；需要的覆寫量已經和自建 chart 的 template 量同級，卻換不到 schema 驗證與自家 values 形狀。

## 2. 實測矩陣

| 題目 | 結果 | 關鍵覆寫／證據 |
|---|---|---|
| 能否開機 | ✅ | `image.*` ＋ `global.security.allowInsecureImages=true` ＋ `args: [odoo, -d, bitnami_odoo]`（保留官方 ENTRYPOINT `/entrypoint.sh`，只換 CMD） |
| DB 建立 | ⚠️ 要自己補 | `initContainers` 加 bootstrap：`pg_isready` → `psql … to_regclass('public.ir_module_module')` 判斷 → 沒有才 `odoo -d … -i base --stop-after-init --no-http --without-demo=all`。首開 registry 載入 60s，install→Ready 約 2 分鐘 |
| admin 建立 | ⚠️ 要自己補 | 官方 image 的 `-i base` 只給預設 `admin`／`admin`（實測登入成功 uid=2）。用同一個 initContainer 接 `odoo shell` 以 ORM 改密碼可行（`t1136b`：新密碼登入 uid=2、`admin/admin` 被拒） |
| 能否登入 | ✅ | `/web/session/authenticate` 回 uid=2 |
| `workers>0` 的 `/websocket` | ⚠️ 要分流 | `--workers=2` 下 WS handshake：**8072 → `101 Switching Protocols`**、**8069 → `500 INTERNAL SERVER ERROR`**。chart 有 `extraContainerPorts` ＋ `service.extraPorts` 可以把 8072 開到 Service，但入口只有一個 port ⇒ 仍要 nginx sidecar（chart 的 `sidecars` 可放）或平台端明確帶 `service_port` |
| filestore 持久化 | ✅ | chart 把 PVC 寫死掛 `/bitnami/odoo`（**沒有 mountPath 的 values 旋鈕**）。解法：`extraVolumeMounts` 把同一顆 `odoo-data` volume 再掛一次到 `/var/lib/odoo`。實測 `filestore/bitnami_odoo` 落在 PVC 上；`volumePermissions.enabled=false` ＋ `podSecurityContext.fsGroup=101` ＋ `containerSecurityContext.runAsUser=101`（Bitnami 預設是 0／1001，官方 image 是 101） |
| `helm upgrade` 冪等 | ✅ | `t1136` 從 revision 1 → 2（加 workers、加 admin 密碼 env）後：bootstrap 印「DB 已 init，略過 `-i base`」、admin 密碼維持原值（新密碼登入失敗、舊的成功）。冪等來自**自己寫的判斷**，不是 chart 給的 |
| postgres 換官方 lineage | ✅ 但要 4 處覆寫 | `pgvector/pgvector:pg17` 實測 Running：`select version()` → PostgreSQL 17.11、`create extension vector` → 0.8.6、`bn_odoo` 是 superuser＋createdb |

### 2.1 postgres 換 image 的四個坑（依撞到的順序）

1. **`auth.usePasswordFiles` 預設 true** ⇒ 容器同時拿到 `POSTGRES_PASSWORD_FILE` 和（我額外加的）`POSTGRES_PASSWORD`，官方 entrypoint 直接 `error: both POSTGRES_PASSWORD and POSTGRES_PASSWORD_FILE are set (but are exclusive)` 退出。設 `postgresql.auth.usePasswordFiles=false` 後，chart 自己給的 `POSTGRES_USER`／`POSTGRES_PASSWORD` 鍵名**剛好和官方 image 相容**。
2. **DB 名鍵名不同**：chart 給 `POSTGRES_DATABASE`，官方 image 要 `POSTGRES_DB` ⇒ `primary.extraEnvVars` 補一條。
3. **entrypoint 路徑**：Bitnami image 的 `/opt/bitnami/scripts/postgresql/entrypoint.sh` 不存在於官方 image ⇒ `primary.command=["docker-entrypoint.sh"]`、`primary.args=["postgres"]`。
4. **uid／rootfs**：`runAsUser`／`fsGroup` 改 999，`readOnlyRootFilesystem=false`。`PGDATA=/bitnami/postgresql/data`（chart 預設）官方 image 吃得下，不用改。

## 3. 失效的 Bitnami 機制（全部是平台現在在用的）

| 機制 | 現況 | 換官方 image 後 |
|---|---|---|
| `customPostInitScripts`（`fix-admin-credentials.sh`、`odoo-conf-system.sh`） | chart 掛成 `/docker-entrypoint-init.d` | **靜默不執行**——官方 `/entrypoint.sh` 沒有任何 init hook（`grep -c docker-entrypoint-init.d` → 0）。admin 密碼雜湊、`proxy_mode`／`list_db`／`workers`／`limit_*` 全部落空 |
| `ODOO_*` env（`ODOO_EMAIL`、`ODOO_PASSWORD`、`ODOO_DATABASE_*`、`ODOO_SKIP_MODULES_UPDATE`…） | chart 一律注入 | **全部無人消費**。官方 entrypoint 只讀 `HOST`／`PORT`／`USER`／`PASSWORD`（Odoo 17/18 不讀 `PG*`，entrypoint 明文註解） |
| 非首次啟動 `--update=all` | Bitnami entrypoint 的隱性升級語意（盤點 S2） | 消失，要自己做 module-version-gate |
| `odoo.conf` | Bitnami 放 `/opt/bitnami/odoo/conf/odoo.conf`，sed 可改 | 官方在 image 內 `/etc/odoo/odoo.conf`（root 所有）。只能走 CLI flag（本次用 `args` 驗過 `--proxy-mode --no-database-list --db-filter --workers --limit-time-real`）或掛 ConfigMap 覆蓋 |
| chart 產的 `<release>-odoo` secret／NOTES 的 `odoo-password` | 給租戶看的憑證 | 變成誤導：那組密碼不會生效 |

## 4. 最小可行 values

三個檔（疊加使用，asset 在同 commit）：

- [`assets/1136/values-official.yaml`](assets/1136/values-official.yaml)：開機＋DB bootstrap＋filestore 的最小集
- [`assets/1136/values-v2-workers.yaml`](assets/1136/values-v2-workers.yaml)：`workers=2`、8072 開埠、bootstrap 用 ORM 設 admin 密碼
- [`assets/1136/values-v3-pgvector.yaml`](assets/1136/values-v3-pgvector.yaml)：postgres 換 `pgvector/pgvector:pg17`

```bash
helm install <rel> oci://registry-1.docker.io/bitnamicharts/odoo --version 28.2.10 \
  -f values-official.yaml -f values-v2-workers.yaml -f values-v3-pgvector.yaml
```

⚠ 這些 values 是**實測用**，不是可上線的樣板：密碼寫死在 values、admin 密碼走 env（租戶讀得到 Deployment 就看得到）、沒有 `/mnt/extra-addons` 的 addon 注入、沒有 nginx sidecar。

## 5. 對 [#1140](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1140)（chart 路線）的輸入

**支持「Bitnami ＋ 覆寫」的點**

- 今天就能跑，不必等自家 chart；`t1136b` 從 `helm install` 到可登入約 2 分鐘。
- Ingress／PDB／HPA／networkPolicy／PVC 這些樣板不用重寫。
- 可當**過渡**：harumi 若時程緊，先用這組 values 把站搬進來，之後再換 chart。

**反對的點（比較重）**

- 需要覆寫的點：`image`、`args`、`extraEnvVars`、`extraVolumeMounts`、`initContainers`、`podSecurityContext`、`containerSecurityContext`、`volumePermissions`、probe path、`extraContainerPorts`／`service.extraPorts`、`postgresql.*`（4 處）——**chart 的 odoo 專屬邏輯 100% 被繞掉**，剩下的等於一份通用 Deployment 模板。
- `values.schema.json` 驗的還是 Bitnami 的鍵，平台自家形狀（`addons.selected`、plan overlay…）**沒有地方宣告**⇒ 盤點裡 C004／C005／C018 那類「寫錯鍵不報錯」的靜默失效**一條都沒解**。
- 上游 2025-08 起凍結：不會再有安全更新，而每次動它都要重驗上面那一串覆寫。
- 每次 `helm install` 都噴 `⚠ SECURITY WARNING: Original containers have been substituted`，且 Bitnami 明說替換 image 後 chart 行為不保證。
- `helm uninstall` 會把 chart 管的 odoo PVC 刪掉（本次實測撞到：重裝時 PVC 還在 Terminating，pod 卡 Pending）——這條對 [#1128](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1128)「不得 in-place 換 chart」是額外佐證。

**結論建議**：把「Bitnami ＋ 覆寫」定位成**逃生門／過渡**，不要當終局；終局仍走自建（或 fork IMIO/helm-odoo）。本次的 bootstrap initContainer 流程（等 DB → `to_regclass` 判斷 → `-i base` → ORM 設密碼）已實測可行，可以直接當自家 chart 的藍本，和盤點 §3.3 的設計一致。

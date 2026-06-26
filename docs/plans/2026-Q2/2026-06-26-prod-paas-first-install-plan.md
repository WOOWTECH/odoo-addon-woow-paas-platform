---
title: Prod PaaS 首次安裝 Plan（woow_paas_platform → paas-system）
category: plans
status: draft
created: 2026-06-26
tags: [prod, install, odoo, paas, bitnami, milestone, runbook]
related:
  - docs/guides/deployment/prod-release-sop.md
  - memory: prod-odoo-paas-not-installed
  - memory: prod-odoo-bitnami-module-mgmt
---

# Prod PaaS 首次安裝 Plan

> **給執行者**：這是 prod 操作 runbook，不是 code plan。每個 Task 是最小可驗證單位，**每步都有精確命令 + 預期輸出 + 失敗即停**。任一驗證 gate 不過 → 停下，走「Rollback」節，不要硬推。

**Goal:** 在 prod `paas-system/paas-odoo`（db `bitnami_odoo`）首次安裝 `woow_paas_platform`，讓 PaaS 平台前台 `/woow` 上線。

**背景:** prod 這個 Odoo 從未裝過 addon（module state=`uninstalled`，只有 37 Odoo core，`/woow` 一直 404 是既存狀態非回歸）。已確認 `paas-system` 就是 PaaS 的 prod 環境（operator 也在此），故 odoo 本就該裝 `woow_paas_platform`。Bitnami chart 的 `ODOO_SKIP_MODULES_UPDATE` 只控制 `--update=all`（不裝 uninstalled），`--init=all` 只在 fresh DB 跑 → 既存 DB 安裝特定 module **必須走 one-off install Job**。

**Tech context:** woow-k3s（Tailscale VPN）；odoo deploy `paas-odoo`、odoo-data PVC `paas-odoo`（RWO，mount `/bitnami/odoo`）、db 連線 env（`ODOO_DATABASE_*` + password file in `odoo-secrets` volume）、custom-addons 在 image 內 `/opt/bitnami/odoo/custom-addons`、initContainer `patch-odoo-conf` + customPostInitScripts（`install-pip-deps.sh`）。

## Global Constraints

- **認 digest 不認 tag / repo 名**：woow-k3s 同跑 prod+stg，containerd digest dedup 會讓 prod pod status 報 stg repo 別名。判版本看 deploy `.spec...image` + manifest digest。
- **每個不可逆步驟前確認有還原點**（longhorn snapshot）。
- **install 有 downtime**（scale 主 deploy 到 0 期間），選低流量時段。
- **odoo-data PVC 是 RWO**：install Job 掛它前，主 deploy 必須先 scale 0（否則 Job pod Pending）。
- 所有 kubectl 帶 `--context woow-k3s`，db 名 `bitnami_odoo`，odoo.conf 在 `/opt/bitnami/odoo/conf/odoo.conf`。

---

## ⚠️ 前置條件 Gate（全部確認才進 Task 1）

- [ ] **G1 連線**：`tailscale status` active；`kubectl --context woow-k3s get ns paas-system` 通
- [ ] **G2 還原點**：存在新鮮的 longhorn snapshot（Task 1 會建）
- [ ] **G3 第三方憑證決策**（install 後 cron 會用到，需先決定接受哪種）：
  - **ecpay 金流**（`payment_ecpay_ecpg`）：prod 商店代號/HashKey/HashIV 是否就緒？還是先裝、金流功能待配？
  - **AI provider**（`ai_base_gt` + chatgpt connector）：api_key 待配可接受（AI 功能 install 後才設）
  - **訂閱方案**（`sh_subscription`）：seed 帶預設方案，prod 是否要調整？
- [ ] **G4 cron 影響**：install 後訂閱 lifecycle / 孤兒清理 cron 會啟用，確認可接受（或 install 後先停用敏感 cron 待設定完成）
- [ ] **G5 時段**：低流量時段（有 downtime）

> **G3 是真正的產品決策**：技術上 `-i` 會把金流 module 裝起來、cron 開跑。若 prod 金流憑證未就緒，建議 install 後**立即停用金流相關 cron**（Task 7.5）直到憑證配好，避免 cron 拿未設定的金流跨。

---

## Task 1: 建立 pre-install snapshot（還原點）

**Why:** install 不可逆（建表、seed、hook）。prod DB 零定期備份，必須有即時還原點。

- [ ] **Step 1.1** 取 odoo-data PVC 的 volume 名（install 影響的是 odoo DB，但 DB 在 postgresql PVC）

```bash
CTX=woow-k3s
PGVOL=$(kubectl --context $CTX -n paas-system get pvc data-paas-odoo-postgresql-0 -o jsonpath='{.spec.volumeName}')
echo "postgres volume = $PGVOL"
```
Expected: `pvc-9aa6ed19-5798-40b5-8689-7a7ab910a67d`（或當下值）

- [ ] **Step 1.2** 建 snapshot

```bash
cat <<EOF | kubectl --context $CTX apply -f -
apiVersion: longhorn.io/v1beta2
kind: Snapshot
metadata:
  name: paas-odoo-pg-preinstall-$(date +%Y%m%d-%H%M)
  namespace: longhorn-system
spec:
  volume: $PGVOL
  createSnapshot: true
EOF
```

- [ ] **Step 1.3** 等 readyToUse=true（≤120s）

```bash
SNAP=$(kubectl --context $CTX -n longhorn-system get snapshot -o name | grep preinstall | tail -1)
kubectl --context $CTX -n longhorn-system get $SNAP -o jsonpath='{.status.readyToUse} {.status.restoreSize}{"\n"}'
```
Expected: `true 10737418240`。記下 snapshot 名（rollback 用）。**不過則停。**

---

## Task 2: 準備 install Job manifest（從 deploy 改造）

**Why:** install 要與 odoo 同 image/env/volumes/initContainers 才能連 DB + 有 custom-addons + pip deps。直接從 deploy dump 改造最可靠（避免硬編過時）。

- [ ] **Step 2.1** dump deploy pod template → 改造成 Job

```bash
CTX=woow-k3s
kubectl --context $CTX -n paas-system get deploy paas-odoo -o json | jq '
{
  apiVersion: "batch/v1", kind: "Job",
  metadata: {name: "paas-odoo-install-woow", namespace: "paas-system"},
  spec: {
    backoffLimit: 0,
    template: {
      spec: (.spec.template.spec
        | .restartPolicy = "Never"
        | .containers[0].name = "install"
        | .containers[0].command = ["sh","-c","/opt/bitnami/odoo/venv/bin/pip install -r /opt/bitnami/odoo/custom-addons/woow_paas_platform/requirements.txt --root-user-action=ignore -q || true; exec odoo -c /opt/bitnami/odoo/conf/odoo.conf -d bitnami_odoo -i woow_paas_platform --stop-after-init --no-http"]
        | del(.containers[0].args)
        | del(.containers[0].livenessProbe)
        | del(.containers[0].readinessProbe)
        | del(.containers[0].startupProbe)
        | del(.containers[0].lifecycle)
      )
    }
  }
}' > /tmp/install-woow-job.json
```

- [ ] **Step 2.2** 人工檢視改造結果（關鍵欄位）

```bash
jq '{image: .spec.template.spec.containers[0].image,
     command: .spec.template.spec.containers[0].command,
     volumes: [.spec.template.spec.volumes[].name],
     initC: [.spec.template.spec.initContainers[]?.name],
     restart: .spec.template.spec.restartPolicy}' /tmp/install-woow-job.json
```
Expected: image=`paas-odoo:20260625.1514`、command 是 `sh -c`（先 `pip install` woow_paas_platform/requirements.txt 再 `exec odoo ... -i woow_paas_platform --stop-after-init`）、volumes 含 `odoo-data`/`odoo-secrets`/`custom-postinit`、initC 含 `patch-odoo-conf`、restart=`Never`。**直跑 odoo binary 繞過 Bitnami run.sh，故 command 自帶 pip install（補上 run.sh 才會跑的 install-pip-deps）；odoo.conf 已存在於 odoo-data PVC（主 deploy 跑過必然已生成）。**

---

## Task 3: Scale 主 deploy 到 0（釋放 RWO PVC + DB 連線）

- [ ] **Step 3.1** scale 0 並等 pod 完全終止

```bash
CTX=woow-k3s
kubectl --context $CTX -n paas-system scale deploy paas-odoo --replicas=0
kubectl --context $CTX -n paas-system wait --for=delete pod -l app.kubernetes.io/name=odoo --timeout=120s
kubectl --context $CTX -n paas-system get pods -l app.kubernetes.io/name=odoo
```
Expected: `No resources found`。**downtime 自此開始**（/odoo /my 也會暫時不可用）。

---

## Task 4: 跑 install Job

- [ ] **Step 4.1** apply Job

```bash
kubectl --context $CTX -n paas-system apply -f /tmp/install-woow-job.json
```

- [ ] **Step 4.2** follow install log（看 installing modules + 有無 CRITICAL/Traceback）

```bash
sleep 8
JOBPOD=$(kubectl --context $CTX -n paas-system get pod -l job-name=paas-odoo-install-woow --no-headers | head -1 | awk '{print $1}')
kubectl --context $CTX -n paas-system logs -f "$JOBPOD" 2>&1 | grep -iE 'loading|module|init|installing|CRITICAL|ERROR|Traceback|loaded'
```
Expected: 看到 `loading N modules`（N 遠大於 37，含 woow_paas_platform + depends）、各 module init、最後 `Modules loaded.`，**無 CRITICAL/Traceback**。

- [ ] **Step 4.3** 等 Job 完成

```bash
kubectl --context $CTX -n paas-system wait --for=condition=complete job/paas-odoo-install-woow --timeout=600s \
  && echo "JOB COMPLETE" \
  || { echo "JOB 未完成/失敗"; kubectl --context $CTX -n paas-system get job paas-odoo-install-woow; }
```
Expected: `JOB COMPLETE`。**失敗 → 不要 scale 回 1，直接走 Rollback（restore snapshot）。**

---

## Task 5: 驗證 module 安裝成功（DB 層）

- [ ] **Step 5.1** Job 完成後，暫時 scale 1 起一個 pod 查 state（或用 postgres pod psql）

```bash
CTX=woow-k3s
kubectl --context $CTX -n paas-system scale deploy paas-odoo --replicas=1
kubectl --context $CTX -n paas-system rollout status deploy/paas-odoo --timeout=300s
POD=$(kubectl --context $CTX -n paas-system get pods -l app.kubernetes.io/name=odoo --no-headers | head -1 | awk '{print $1}')
kubectl --context $CTX -n paas-system exec -i "$POD" -- odoo shell -c /opt/bitnami/odoo/conf/odoo.conf -d bitnami_odoo --no-http 2>/dev/null <<'PYEOF' | grep -E 'WOOW|DEPS|TOTAL'
M=env["ir.module.module"]
print("WOOW:", M.search([("name","=","woow_paas_platform")]).mapped("state"))
deps=["ai_base_gt","sh_subscription","sh_subscription_wallet","payment_ecpay_ecpg","ecpay_invoice_tw","odoo_ai_assistant_chatgpt_connector"]
print("DEPS:", [(r.name,r.state) for r in M.search([("name","in",deps)])])
print("TOTAL:", M.search_count([("state","=","installed")]))
PYEOF
```
Expected: `WOOW: ['installed']`、DEPS 全 `installed`、TOTAL 遠大於 37。**woow 非 installed → 走 Rollback。**

---

## Task 6: 驗證 /woow 上線 + core 不受影響

- [ ] **Step 6.1** smoke test

```bash
CURL=$(command -v curl || echo /usr/bin/curl)
for p in /woow /odoo /my; do
  echo "$p -> $("$CURL" -s -o /dev/null -w '%{http_code}' --max-time 15 https://paas.woowtech.io$p)"
done
```
Expected: `/woow -> 2xx/3xx`（**首次不再 404**）、`/odoo -> 303`、`/my -> 303`。

- [ ] **Step 6.2** 確認 pod 健康（0 restart）

```bash
kubectl --context $CTX -n paas-system get pods -l app.kubernetes.io/name=odoo \
  -o custom-columns='NAME:.metadata.name,READY:.status.containerStatuses[*].ready,RESTART:.status.containerStatuses[*].restartCount'
```
Expected: ready=true, restart=0。

---

## Task 7: 收尾

- [ ] **Step 7.1** 刪 install Job

```bash
kubectl --context $CTX -n paas-system delete job paas-odoo-install-woow
```

- [ ] **Step 7.5（條件性）若 G3 金流憑證未就緒**：盤點並暫停金流相關 cron，避免 cron 拿未設定金流跨

```bash
POD=$(kubectl --context $CTX -n paas-system get pods -l app.kubernetes.io/name=odoo --no-headers | head -1 | awk '{print $1}')
kubectl --context $CTX -n paas-system exec -i "$POD" -- odoo shell -c /opt/bitnami/odoo/conf/odoo.conf -d bitnami_odoo --no-http 2>/dev/null <<'PYEOF'
crons = env["ir.cron"].search([])
for c in crons: print(c.id, c.active, c.name)
PYEOF
# 依輸出，對金流/訂閱扣款類 cron .write({"active": False}) 直到憑證配好
```

- [ ] **Step 7.6** 更新 prod-release-sop.md §2（標註 prod 已裝 addon）+ memory `prod-odoo-paas-not-installed`（從「未裝」改「已裝，日期」）

---

## Rollback（任一驗證 gate 失敗）

**install 失敗 / module 未裝成 / prod 異常時：**

- [ ] **R1** scale deploy 0

```bash
kubectl --context woow-k3s -n paas-system scale deploy paas-odoo --replicas=0
kubectl --context woow-k3s -n paas-system wait --for=delete pod -l app.kubernetes.io/name=odoo --timeout=120s
```

- [ ] **R2** restore longhorn snapshot 到 postgres volume（longhorn 需 detach volume 才能 revert）

```bash
# postgres 也要先停（scale postgres statefulset 0）讓 volume detach
kubectl --context woow-k3s -n paas-system scale statefulset paas-odoo-postgresql --replicas=0
# 等 volume detach 後，用 longhorn UI 或 Snapshot revert（snapshot revert 需 volume detached）
# Snapshot 名 = Task 1.3 記下的 paas-odoo-pg-preinstall-YYYYMMDD-HHMM
# longhorn revert: 透過 longhorn-manager API 或 UI 對該 volume 執行 snapshotRevert <snap>
```

- [ ] **R3** scale postgres + odoo 回 1，驗證 DB 回到 install 前（woow_paas_platform=uninstalled，/odoo /my 正常）

> **R2 細節**：longhorn snapshot revert 需 volume detached（所有 consumer scale 0）。若不熟 longhorn revert，保守做法是用 longhorn UI 操作（Volume → Snapshots → 選 preinstall snap → Revert）。revert 後 scale 回 1。

---

## Self-Review checklist（執行前再過一次）

- [ ] G3 第三方憑證決策已做（金流 install 後行為已決定）
- [ ] Task 1 snapshot readyToUse 確認
- [ ] Task 2 改造後的 Job image=當前 prod tag、command 正確、volumes/initContainers 齊
- [ ] downtime 時段 OK
- [ ] Rollback 的 snapshot 名已記下
- [ ] 知道 longhorn revert 怎麼操作（或 UI 路徑）

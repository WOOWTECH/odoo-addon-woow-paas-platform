# #449 密碼載體分流 — hermes-agent／open-design 密碼重設 stg 真機 E2E

> 對應 follow-up：**F-449-1**（[`../followups/follow-ups.md`](../followups/follow-ups.md)）｜issue **#449**｜PR **#455**
> 環境：`woow-k3s` / stg｜平台 `dev-sandbox-stg`（DB `woow_stg`）｜租戶 ns `paas-ws-stg-5a105e8b`
> 設計依據：[`../../decisions/patterns/helm-reset-values-unified-pipeline.md`](../../decisions/patterns/helm-reset-values-unified-pipeline.md)
> 執行：2026-07-29（UTC 2026-07-28 16:12–19:18）
> 狀態：**完成 — 核心驗收 PASS，帶兩項待後續處理的發現**
>
> **一句話結論**：PR #455 的 operator 實作在真 chart 上正確、四入口／三入口的密碼都確實輪替、失敗路徑 fail-safe；但 (a) 平台 120s 預算在 image cache miss 時必逾時（兩台第一次都逾時、第二次都成功），(b) 前台 Reset 按鈕因 slug 白名單未同步，**租戶按不到這個修好的功能**（§8.2）。

## 為什麼需要這份報告

PR #455 的驗收範圍是「程式碼＋單元＋integration」。integration 雖然是真 helm ＋ 真 kubectl，用的卻是**本地合成的最小 chart**（兩顆 Deployment ＋ chart 自管 Secret ＋刻意省略 `checksum/secret`），不是真 chart、真 image。以下四件事因此在本報告之前**沒有任何真機證據**：

1. 真 chart 的 `authproxy-init` initContainer 重算 htpasswd 後，nginx basic-auth 是否真的換成新密碼
2. dashboard 的**表單登入**（與 basic-auth 形狀不同的第二個入口）與 webui 是否同步吃到新值
3. `Recreate` ＋ 共用 RWO PVC 下，daemon／console 兩顆並行滾動的**實際耗時**能否撐住平台 120s client 預算
4. open-design 的重設在 #455 之前同樣是壞的（同根因），修好後從無 stg 實測紀錄可對照

---

## 0. 受測標的

| 項目 | hermes-agent | open-design |
| --- | --- | --- |
| CloudService id | 60（`hermes-e2e`） | 50（`my-od-2`） |
| template id | 46（`hermes-agent`） | 45（`open-design`） |
| helm release | `svc-bcb0aa5b` | `svc-beab2427` |
| namespace | `paas-ws-stg-5a105e8b` | 同左 |
| 公開 URL | `https://paas-cs-5a105e8b-532079c5.woowtech.io` | `https://paas-cs-5a105e8b-453f335b.woowtech.io` |
| chart 版本 | 0.1.0 | 0.1.4 |

沿用既有 CS、不新建，理由有二：該 workspace 的 `persistentvolumeclaims` 已 10/10 滿、`limits.cpu` 已超額（見 §2），新建必然失敗；且 svc60 本來就是 [#456](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/456) 刻意留測的實例。

### 兩個實作前提在真 chart 上的實證

PR #455 的兩個前提原本是「對 chart 原始碼實查」得出的，本輪在**線上 live 物件**上再確認一次：

| 前提 | 驗證方式 | 結果 |
| --- | --- | --- |
| 兩個 chart 的 Deployment **沒有** `checksum/secret` | `kubectl get deploy -o jsonpath='{.spec.template.metadata.annotations}'` | hermes daemon 只有 `checksum/config`、`checksum/authproxy-config`；console 只有 `checksum/console-config`；open-design daemon 只有 `checksum/config`、`checksum/authproxy-config`。**四顆皆無 `checksum/secret`** ✅ |
| 同 instance label 下是**複數** Deployment | `kubectl get deploy -l app.kubernetes.io/instance=<release>` | `svc-bcb0aa5b` → daemon ＋ console 兩顆；`svc-beab2427` → 兩顆 ✅ |
| 滾動策略 | `.spec.strategy.type` | 四顆皆 `Recreate` ✅（先殺後建，與 RWO PVC 一致） |

換言之：改 Secret 不改 pod template ⇒ helm 不滾 pod，而密碼是**啟動期**消費的 ⇒ 沒有顯式 rollout restart 就會「upgrade 成功但密碼靜默不生效」。單數版 `get_deployment_name` 也確實會撞多筆。兩個前提在真 chart 上都成立。

---

## 1. 版本前提（發版順序 F-449-2 的實查）

| 元件 | 值 | 是否含 PR #455 |
| --- | --- | --- |
| stg operator image | `paas-operator:acefe08`<br>digest `sha256:75f0b591481f49b462103ca2113889a06afe8f5d9bb2e4fce0e4f236a433262e` | ✅ 是 |
| stg operator pod | 2026-07-28T16:19:18Z 起，1/1 Ready，deployment revision 77 | — |
| Fleet pin | `paas-app-bundle-cd@main` → `paas-operator-stg/values-stg.yaml` `image.tag: "acefe08"` | — |
| 升版前 stg 平台 image | `paas-odoo-stg:20260728.0902`（build 早於 merge commit `acefe088` 的 15:46Z） | ❌ 否 |
| 升版後 stg 平台 image | `paas-odoo-stg:20260728.1632`（CI clone `develop` = `acefe088`） | ✅ 是 |

**operator 側不靠 tag 推論**，直接讀 operator 自己的 `/openapi.json`：

```
ReleaseResetConfigRequest.properties → [values, sensitive_values, chart, version,
                                        reset_values, reuse_values, admin_password_carrier]
admin_password_carrier: {"enum": ["config_json", "helm_secret"], "default": "config_json"}
```

**平台側升版是必要條件，不是可選項**：operator 端 `admin_password_carrier` 預設 `config_json`（＝既有行為，這正是 F-449-2 的順序支點）。升版前 stg 平台的 `woow_paas_platform_cloud_app_template` 表**沒有 `admin_password_carrier` 欄位**，reset 請求不會帶該欄 → operator 走既有 config_json saga → 對 hermes-agent 仍是 exec patch 一個不存在的 `/data/config.json` → 仍 500。不升版等於測不到本次修復。

### 升版路徑（版號未 bump 的處理）

PR #455 **沒有 bump `__manifest__.py` 版號**（仍 `18.0.1.0.80`，與 stg 現況同版），所以不能指望版號差異觸發升級，必須強制 `-u`：

1. 建 `stg-20260728.1632` tag 觸發 `paas-odoo-ci`（`stg-*` 前綴 → clone `develop`、跳過 prod approval gate）
2. 跑 upgrade Job `stg-upgrade-449`（照 `stg-upgrade-80` 前例）：`odoo -u woow_paas_platform --stop-after-init -d woow_stg`
3. 切 deployment image

Job 兩個必要細節（前人踩過的坑）：

- `nodeSelector: kubernetes.io/hostname=ubuntuserver-pggrg` — 綁到 odoo pod 所在 node，否則 filestore RWO PVC 會 Multi-Attach 失敗
- `resources.limits` `800m` / `2Gi` — `dev-sandbox-stg` 的 ResourceQuota `limits.cpu` hard 為 4、已用 3200m，只剩 800m 額度

**無需 migration**：`src/data/cloud_app_templates.xml` 整份在 `<data noupdate="0">` 區塊，`-u` 直接重套 template 欄位；新欄位 `required=True` ＋ `default='config_json'`，既有列自動填預設，再由 seed 把 template 45／46 蓋成 `helm_secret`。

**升版副作用（已知，不在本輪處置範圍）**：`-u` 會把 template 46 的 `is_hidden` 從 stg 現況的 `False` 翻回 seed 值 `True`（stg 於 2026-07-28 曾以 DB 直改臨時解封）；0 元測試 plan 268 同理需複查。本報告只記錄實際翻回情況，重新解封由 leader1 接手。

---

## 2. 前置處置：workspace CPU quota 超額（#456）

### 問題

E2E 開始前實查 `paas-ws-stg-5a105e8b` 的 ResourceQuota：

| 資源 | used | hard | 狀態 |
| --- | --- | --- | --- |
| `limits.cpu` | 9300m | **9** (9000m) | ⚠️ **已超額** |
| `persistentvolumeclaims` | 10 | 10 | 已滿 |
| `limits.memory` | 18944Mi | 21Gi | OK |

ResourceQuota 是 admission-time 檢查。兩顆 Deployment 都是 `Recreate`，reset 的 rollout restart 會先刪舊 pod 再建新 pod；舊 pod 刪掉後 `used` 降到 6000m，新 pod 要 3300m ⇒ 9300m > 9000m ⇒ `FailedCreate`。**在原始 quota 下 reset 的 rollout 必然失敗**，會走成 operator 的 rollback 補償路徑，測不到成功路徑。

同 namespace 稍早的既有事件就是這個形狀：

```
Warning FailedCreate replicaset/svc-38b6213c-hermes-agent
  Error creating: pods "..." is forbidden: exceeded quota: paas-ws-stg-5a105e8b-quota,
  requested: limits.cpu=3200m, used: limits.cpu=9500m, limited: limits.cpu=12
```

根因即 #456：`_compute_ns_quota` 的 CPU 公式是 `max(Σ allocated_vcpu × 3, 8)`，plan `cs_vcpu=1` 的邊際貢獻 `+3` 小於 hermes-agent 的邊際需求 `3.4`（agent 2000m ＋ webui 1000m ＋ authproxy 200m ＋ console 200m）。RAM 沒這問題（`min_ram_gb=3.0 × 3 = 9Gi ≥ 8.625Gi`）。空 workspace 靠 8 vCPU floor 蓋得住，**共用 workspace 就露餡**。

> 附帶影響：在修正前，這兩台 CS 處於「pod 一旦被驅逐就再也起不來」的狀態。

### 處置（leader3 核准）

```bash
kubectl --context woow-k3s -n paas-ws-stg-5a105e8b \
  patch resourcequota paas-ws-stg-5a105e8b-quota --type merge \
  -p '{"spec":{"hard":{"limits.cpu":"12","requests.cpu":"12"}}}'
```

執行時間 `2026-07-28T16:36:34Z`。前後完整 YAML 見 §附錄 A。

| | `limits.cpu` hard | `limits.cpu` used | headroom |
| --- | --- | --- | --- |
| patch 前 | 9 | 9300m | **−300m（超額）** |
| patch 後 | 12 | 9300m | 2700m |

峰值檢算（`Recreate` 先殺後建）：hermes daemon `6000m + 3300m = 9300m`、open-design daemon `4900m + 4400m = 9300m`，兩者皆 ≤ 12000m ✅

**收尾不翻回**：下次該 workspace 有 CS create／delete 時，平台的 `_push_ns_quota` 會依 #456 的公式重算蓋回。此處記錄「曾臨時調為 12」，並同步留言於 #456。

---

## 3. S0 — reset 前基線

### 3.1 四入口的真實端點（實測釘死，非文件推論）

hermes-agent 的四個入口共用同一個 `admin_password`，但**消費形狀各不相同** —— 這正是合成 chart 測不到的部分：

| # | 入口 | 對外路徑 | 認證形狀 | 密碼在 pod 內的來源 | 所屬 Deployment |
| --- | --- | --- | --- | --- | --- |
| 1 | webui gate | `GET /` | nginx basic-auth（realm `Hermes Agent`） | initContainer `authproxy-init` 從 Secret `admin_password` 產生 `/etc/nginx/auth/.htpasswd` | daemon |
| 2 | console | `GET /console/` | ttyd 自帶 basic-auth（realm `ttyd`） | env `TUI_PASSWORD` ← Secret `admin_password` | **console（第二顆）** |
| 3 | dashboard | `POST /dashboard/auth/password-login`（JSON） | 應用層表單登入 ＋ session cookie | env `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` ← Secret `admin_password` | daemon |
| 4 | webui app API | `POST /api/auth/login`（JSON，需先過 #1） | 應用層密碼 API | env `HERMES_WEBUI_PASSWORD` ← Secret `admin_password` | daemon |

入口 2 落在**另一顆 Deployment**，因此它是「複數版 `get_deployment_names` 是否真的滾到第二顆」的直接證據。

不受 `admin_password` 保護、可作為獨立存活信號的端點：`GET /api/health`、`GET /healthz`（皆免認證，實測 200）。agent 自身的 API server（`127.0.0.1:8642`）走 `API_SERVER_KEY`、**不是** `admin_password`，勿混。

open-design 實測只有三個受 `admin_password` 保護的入口：`/`、`/console/`、`/dashboard/`，全部落在同一個 nginx basic realm `Open Design`。其 `OD_API_TOKEN` 是獨立 secret、不隨 `admin_password` 輪替，不列入本次驗收。

### 3.2 舊密碼基線結果

hermes-agent（svc60）：

| # | 入口 | 正確舊密碼 | 錯誤密碼 | 無認證 |
| --- | --- | --- | --- | --- |
| 1 | `GET /` | **302**（過 gate → webui 自身 login） | 401 | 401 |
| 2 | `GET /console/` | **200** | 401 | 401 |
| 3 | `POST /dashboard/auth/password-login` | **200** `{"ok":true,"next":"/"}` | 401 | — |
| 3b | 帶 session cookie `GET /dashboard/` | **200** | — | 302 → `/dashboard/login` |
| 4 | `POST /api/auth/login` | **200** `{"ok": true}` | 401 `Invalid password` | 401（被 nginx 擋） |

open-design（svc50）：

| 入口 | 正確舊密碼 | 錯誤密碼 | 無認證 |
| --- | --- | --- | --- |
| `GET /` | **200** | 401 | 401 |
| `GET /console/` | **200** | 401 | 401 |
| `GET /dashboard/` | **200** | 401 | 401 |

### 3.3 密碼來源的對照（一個有意義的差異）

| | svc60 hermes-agent | svc50 open-design |
| --- | --- | --- |
| K8s Secret `admin_password` 長度 | **24** | 32 |
| 平台 DB `cloud_service_secret` 是否有列 | **無** | 有（`updated_at 2026-07-23 02:22:29`） |

平台的 `_generate_admin_password()` 產出固定 32 字元。svc60 的 24 字元代表那把是 **chart 自己生的**（`resolveSecretValue` 在 values 未供給時自動生成），與「平台 DB 沒有該列」完全對得上 —— svc60 當初不是走 marketplace `_create_service` 建的。

因此 svc60 的 reset 順帶驗到 `_set_admin_password_secret` 的 **create** 分支（而非 write 分支），這是 #357 C03「不檢查既有 secret」設計的直接證據。

---

## 4. S1／S2 — hermes-agent（svc60）

觸發方式：`POST /api/workspaces/1/services/60/reset-admin-password`（以 workspace owner `admin` 的 session）。**未走 UI 按鈕，因為那顆按鈕對本 template 不渲染** —— 見 §8.2。

### 4.1 第一次：120.6 秒逾時 ❌

| 時刻 (UTC) | 事件 |
| --- | --- |
| 19:03:13 | 平台送出 reset |
| 19:03:17 | `helm upgrade` 完成（revision 2，`Upgrade complete`）—— 約 4 秒 |
| 19:03:25.318 | `kubectl get deployments --selector app.kubernetes.io/instance=svc-bcb0aa5b` ← **複數版探索** |
| 19:03:25.629 | `rollout restart deployment/svc-bcb0aa5b-hermes-agent` |
| 19:03:25.886 | `rollout restart deployment/svc-bcb0aa5b-hermes-agent-console` ← **先全部下，才逐一等** |
| 19:03:26.117 | `rollout status …-hermes-agent --timeout=120s` 開始等 |
| **19:05:13** | **平台 client 120s 預算到期** → 回 `Reset status unknown…`，密碼**不存 DB** |
| 19:05:26.340 | operator 端 `rollout status` 也逾時 → `helm rollback svc-bcb0aa5b 1` |
| 19:05:29 | rollback 後**再滾一次**兩顆 Deployment（讓舊密碼真的回到 pod） |

**為什麼 daemon 滾不完 —— 逐段拆解**（來自 K8s events）：

| 時刻 | 事件 | 耗時 |
| --- | --- | --- |
| 19:03:29 | 新 pod 建立，`FailedAttachVolume` Multi-Attach（RWO PVC 還掛在舊節點） | |
| 19:03:48 | `SuccessfulAttachVolume` | **PVC detach→attach 19s** |
| 19:03:50→54 | pull `nginx-unprivileged`（4.482s） | 6s |
| 19:03:55→**19:05:05** | pull `hermes-agent` 954MB —— **1m9.963s** | **70s** |
| 19:05:06 | 容器才開始啟動 | |

**主因是 pod 被排到沒有 image cache 的節點**：光是「取得可執行的容器」就吃掉約 101 秒（19s PVC ＋ 6s ＋ 70s），還沒開始算應用自身的 readiness——120s 預算必然不夠。

**失敗是 fail-safe 的**（補償實測全部符合設計）：

| 驗證項 | 結果 |
| --- | --- |
| K8s Secret `admin_password` | **還原成 reset 前的舊值**（len 24，指紋一致）✅ |
| 平台 DB `cloud_service_secret` service_id=60 | **無列**（C11 不變式：DB 裡的密碼必須是真的生效過的那一把）✅ |
| 四入口（舊密碼） | `/`=302、`/console/`=200、dashboard=200、API=200 —— **租戶沒被鎖死** ✅ |
| `/api/health` | 200 ✅ |
| 平台回覆文案 | `Reset status unknown — … the password was not saved. Wait a few minutes, then press Reset again` ← 正確引導重按 ✅ |

### 4.2 第二次：49.9 秒成功 ✅

依 C11 的冪等語意重按（此時 image 已 cache 在節點上）：

| 時刻 (UTC) | 事件 | 耗時 |
| --- | --- | --- |
| 19:08:44.971 | `helm list`（取 previous_revision） | |
| 19:08:46.453 | `helm upgrade … --version 0.1.0 --reset-values --atomic --post-renderer …` | **11.9s** |
| 19:08:58.388 | 複數版 `get deployments --selector` | 0.3s |
| 19:08:58.674 / 19:08:58.890 | 兩顆 `rollout restart`（間隔 0.2 秒） | |
| 19:08:59.186 | `rollout status` daemon | **34.5s** |
| 19:09:33.667 | `rollout status` console —— **立即返回**（已與 daemon 並行滾完） | ~0s |
| 19:09:33 | `POST …/reset-config` → **200 OK** | |
| | **平台端總牆鐘** | **49.9s**（預算 120s，餘裕 58%） |

順帶被實證的兩件事（從 helm 指令原文）：

- `--reset-values` **顯式出現**在指令中 → #411 A 案在 operator 端真的接通了（不再只是靠 `reuse_values=False` 的無-flag 預設）
- `--post-renderer … pvc_keep_post_renderer.py` 仍在 → #317 的 PVC-keep 沒有被 reset 路徑繞過

### 4.3 S2 驗證清單

| 項目 | 判準 | 結果 |
| --- | --- | --- |
| operator 走 helm_secret 路 | log 有 helm upgrade ＋ 複數版探索 ＋ 兩次 rollout restart | ✅ |
| **無 rollback 痕跡** | 成功那次的 operator log `grep -c rollback` | **0** ✅ |
| helm history | revision 4、`deployed`、`Upgrade complete` | ✅ |
| 兩顆 Deployment 都滾 | `restartedAt` = 19:08:58Z / 19:08:59Z（皆新） | ✅ |
| pod 健康 | 全新 pod、Ready `true,true,true` / `true`、**RESTARTS 0**、無 CrashLoop | ✅ |
| K8s Secret | `admin_password` == API 回傳的新密碼（len 32） | ✅ |
| 平台 DB | `cloud_service_secret` **新增 id=12**（service_id=60，len 32，`updated_at 19:09:33`） | ✅ **create 分支** |
| CS state | `upgrading` → cron 翻回 `running`，`helm_revision` 1 → 4 | ✅ |
| **四入口新密碼** | webui `/`=302、console=200、dashboard=200、API=200 | ✅ |
| **四入口舊密碼** | 四者**全部 401** | ✅ |

密碼長度從 24（chart 自生）變成 **32**（平台 `_generate_admin_password`）—— 這本身就是「平台真的接管了憑證」的旁證。

## 5. 兩顆 Deployment 並行滾動的牆鐘（F-449-1 的核心量測）

| 服務 | helm upgrade | daemon `rollout status` | console `rollout status` | 平台端總牆鐘 |
| --- | --- | --- | --- | --- |
| hermes-agent（成功那次） | 11.9s | 34.5s | ~0s（已並行完成） | **49.9s** |
| open-design（成功那次） | 10.7s | 38.6s | ~0s（已並行完成） | **76.7s** |

console 的 `rollout status` 兩次都是**立即返回**，證實 PR #455「先全部下 restart 再逐一等待，總耗時 ≈ max 而非 sum」的設計在真機上成立。若改成序列（下一顆、等一顆），兩台都會多花一顆 console 的滾動時間。

⚠️ 但 **cold image cache 會直接吃掉整個預算**：同一顆 hermes daemon pod，在有 cache 的節點上 34.5 秒滾完，在沒有 cache 的節點上光 pull image 就要 70 秒。兩台服務的第一次嘗試都因此逾時。

## 6. S3 — open-design（svc50）

### 6.1 第一次：121.0 秒逾時 ❌

| 時刻 (UTC) | 事件 |
| --- | --- |
| 19:11:36.3 → 19:11:47.1 | `helm upgrade`（revision 4）10.9s |
| 19:11:47.4 / 19:11:47.7 | 兩顆 `rollout restart` |
| 19:11:47.9 | `rollout status` daemon 開始 |
| **19:13:35** | 平台 client 121.0s 逾時 |
| 19:13:48.077 | operator `helm rollback svc-beab2427 3` |
| 19:13:51 | rollback 後再滾兩顆 |

拆解（open-design 比 hermes 更慢，因為它掛**兩顆** RWO PVC）：

| 時刻 | 事件 | 耗時 |
| --- | --- | --- |
| 19:11:48 | 新 pod 建立，`pvc-98d20dfa…` 與 `pvc-ed50493e…` **兩顆都 Multi-Attach 錯誤** | |
| 19:12:05 | 第一顆 attach 成功 | 17s |
| 19:12:05 | 第二顆 attach failed（`volume is not ready for workloads`） | |
| 19:12:14 | 第二顆 attach 成功 | **兩顆序列累計 26s** |
| 19:12:15 | 才開始 pull `open-design` image（跨節點 cache miss），未能在預算內完成 | |

補償同樣有效：三入口舊密碼全通（200/200/200）、Secret 還原成舊值、平台 DB 仍停在 `2026-07-23 02:22:29` 未更新。

### 6.2 第二次：76.7 秒成功 ✅

| 項目 | 結果 |
| --- | --- |
| helm history | revision 6、`deployed` | 
| 兩顆 Deployment `restartedAt` | 皆 19:16:10Z ✅ |
| pod | Ready、**RESTARTS 0** ✅ |
| rollback 痕跡 | **0** ✅ |
| K8s Secret == API 新密碼 | ✅（len 32） |
| 平台 DB service_id=50 | `updated_at` 07-23 → **2026-07-28 19:17:13** ✅ **write 分支** |
| CS state | → `running`，`helm_revision` → 6 ✅ |
| **三入口新密碼** | `/`=200、`/console/`=200、`/dashboard/`=200 ✅ |
| **三入口舊密碼** | 全部 **401** ✅ |

svc60 走 **create** 分支、svc50 走 **write** 分支 —— `_set_admin_password_secret` 的兩條路徑本輪都被覆蓋到。

---

## 7. 新密碼的取得路徑（供 leader1／Eugene 自查）

reset 成功後，新密碼有三條取得路徑，權威性由高到低：

**(a) 平台 DB（平台認定的權威值）**

```bash
kubectl --context woow-k3s -n dev-sandbox-stg exec stg-odoo-dev-sandbox-postgres-0 -- \
  psql -U odoo -d woow_stg -t -c \
  "SELECT admin_password FROM woow_paas_platform_cloud_service_secret WHERE service_id = 60;"
```

`service_id` 對照：`60` = hermes-agent `hermes-e2e`，`50` = open-design `my-od-2`。
（該表對 `service_id` 有 UNIQUE constraint，一個服務至多一列。）

**(b) 叢集 Secret（pod 實際消費的值）**

```bash
# hermes-agent (svc60)
kubectl --context woow-k3s -n paas-ws-stg-5a105e8b \
  get secret svc-bcb0aa5b-hermes-agent-secrets -o jsonpath='{.data.admin_password}' | base64 -d

# open-design (svc50)
kubectl --context woow-k3s -n paas-ws-stg-5a105e8b \
  get secret svc-beab2427-open-design-secrets -o jsonpath='{.data.admin_password}' | base64 -d
```

**(c) 前台一次性顯示** — `POST /api/workspaces/1/services/<id>/reset-admin-password` 的回應 `data.admin_password`，只在按下 Reset 當下顯示一次，事後無法重看。

> ⚠️ (a) 與 (b) **正常情況下必須一致**。若不一致，代表發生了 §附錄 B 描述的 120s client timeout 場景：平台刻意不存新密碼（`cloud_service.py` C11），但叢集已經輪替 —— 此時 **(b) 才是能登入的那一把**。

---

## 8. 衍生發現（非 #449 範圍）

### 8.1 hermes-agent dashboard 登入頁的 reverse-proxy prefix bug

dashboard 登入頁的前端 JS 寫死：

```js
fetch('/auth/password-login', { method: 'POST', ... })
```

**沒有帶 `/dashboard` 前綴**。真瀏覽器從 `/dashboard/login` 送出時會打到 `https://<host>/auth/password-login`，那條路徑落在 nginx 的 `location /`（帶 basic-auth、上游是 webui `:8787`），不是 dashboard（`:9119`）。實測：

| 請求 | 結果 |
| --- | --- |
| `POST /auth/password-login`（無 basic-auth） | 401（nginx 擋下） |
| `POST /auth/password-login`（帶 basic-auth） | 302（webui 沒有這個端點） |
| `POST /dashboard/auth/password-login` | 200 `{"ok":true}` ✅ |

nginx 那側其實已經照 upstream 的契約送了 `X-Forwarded-Prefix: /dashboard`（authproxy ConfigMap 有明確註解說明），問題出在前端 JS 沒有消費它來組 fetch URL。

**影響**：dashboard 的表單登入在真瀏覽器裡是壞的，與密碼是否輪替無關。屬 chart／upstream 契約問題（#424 領域），本輪不修。本報告用正確端點驗證密碼輪替，結論不受影響。

### 8.2 🔴 前台 Reset 按鈕的 slug 白名單與後端 `has_admin_gui` 判準不一致

**這條直接影響 #449 的產品可達性**：PR #455 把 hermes-agent／open-design 的 reset 修好了，但**租戶在 UI 上按不到那顆按鈕**。

前端 `ServiceDetailPage.js:174` 的 gate 是硬編碼 slug 白名單：

```js
get isMcpAdminService() {
    return ["mcp-odoo", "mcp-odoo-manage", "mcp-n8n"].includes(
        this.service?.template?.slug
    );
}
```

後端 `controllers/paas.py:1293` 的 gate 則是 `template.has_admin_gui`，且該處有 **#357 C01** 的明文註解說明**為什麼**不能用 slug allowlist：

> 「原本這裡是 slug allowlist、model 端卻判 mcp_enabled → 兩端不一致（controller 放行並生密碼、model 過濾成空集合不建 secret）＝密碼永不生效的根因。」

controller 側已依該紀律遷移到 `has_admin_gui`，**前端側沒有跟著遷移**。PR #455 的 model 註解也重申同一條紀律（「判別器＝template 欄位，不得在此自建 slug／chart allowlist——與 has_admin_gui 同一紀律（#357 C01）」）——前端正是它警告的那個形狀。

**缺口量化**（stg live DB，`has_admin_gui = true` 全集）：

| template id | slug | chart | 前台有 Reset 按鈕 |
| --- | --- | --- | --- |
| 6 | `mcp-odoo` | mcp-server | ✅ |
| 7 | `mcp-n8n` | mcp-server | ✅ |
| 40 | `mcp-odoo-manage` | mcp-server | ✅ |
| 42 | `mcp-k8s` | mcp-server | ❌ |
| 43 | `mcp-vk` | mcp-server | ❌ |
| 44 | `mcp-opendesign` | mcp-server | ❌ |
| 45 | `open-design` | open-design | ❌ |
| 46 | `hermes-agent` | hermes-agent | ❌ |

**8 個 template 中有 5 個按不到**。注意缺口**早於 #449**：`mcp-k8s` / `mcp-vk` / `mcp-opendesign` 是 #375 三型 MCP 上架時新增的，它們走既有 config_json 路、後端 reset 一直是好的，只是上架時沒同步前端白名單。#449 只是讓這個既有缺口變得更顯眼（新修好的兩個也在名單外）。

**修法不是改一行 if**：live payload 實證 —— `POST /api/workspaces/1/services/60` 回傳的 `template` 物件只有 `id / name / slug / category / helm_value_specs`，**沒有 `has_admin_gui`**。全 repo grep 也確認 `has_admin_gui` 從未出現在 `src/static/`。所以要讓前端改用正確判準，得先把該欄位加進 template payload。

**對本次 E2E 的影響**：S1 因此改走 API 端點而非 UI 按鈕。這仍是完整的平台路徑（controller `has_admin_gui` gate → `model._reset_admin_password` → `template.admin_password_carrier` → operator → 密碼落 DB），只有「按鈕渲染」這一段被繞過。

### 8.3 單一 act-runner ＋ approval gate 阻斷全 repo CI

本輪 stg image build（`stg-20260728.1632` / run 1237）在 queue 中停留逾一小時，原因是唯一的 act-runner 被 `build-haos-image.yml`（run 1229）的 prod approval gate 佔住（approval issue `paas-odoo-ci#39`，`TIMEOUT_MIN=180`）。同時被卡的還有 operator 的 tag build run 1234。

這是既有已知風險的再次發生：approval gate 的等待是**佔著 runner 輪詢**，不是釋放後回頭排隊，因此一個待審的 prod build 會讓全 repo 的 CI 停擺最長 3 小時。

---

## 9. 結論

### 9.1 F-449-1 的四個待證項，逐一結案

| # | F-449-1 原始待證項 | 結論 |
| --- | --- | --- |
| 1 | 真 chart 的 `authproxy-init` 重算 htpasswd 後 nginx basic-auth 是否換新 | ✅ 換新（新密碼 302／舊密碼 401） |
| 2 | dashboard 表單登入與 webui 是否同步吃到新值 | ✅ 兩者皆同步（dashboard `POST /dashboard/auth/password-login` 與 webui `POST /api/auth/login` 新密碼 200、舊密碼 401） |
| 3 | Recreate ＋ 共用 RWO PVC 下兩顆並行滾動能否撐住 120s 預算 | ⚠️ **視 image cache 而定**：cache 命中 49.9s／76.7s（撐得住）；cache miss 必逾時（兩台第一次都逾時） |
| 4 | open-design 的重設（同根因，從無 stg 紀錄） | ✅ 修復有效，三入口全換新 |

**PR #455 的 operator 實作本身完全正確**：複數版 Deployment 探索抓到 daemon＋console 兩顆、兩顆都下 restart 且先全下再逐一等（並行成立）、失敗時 `helm rollback` ＋ 再滾一次讓舊密碼確實回到 pod。兩個實作前提（無 `checksum/secret`、複數 Deployment）在真 chart 上都成立。

### 9.2 真機才浮現的問題：120s 預算對這兩個 chart 是邊際的

兩台服務**第一次 reset 都逾時**，第二次都成功，差別只有 image 在不在該節點的 cache 裡：

| | hermes-agent | open-design |
| --- | --- | --- |
| cache miss（第一次） | **120.6s 逾時** | **121.0s 逾時** |
| cache hit（第二次） | 49.9s | 76.7s |
| 逾時的成本來源 | PVC detach/attach 19s ＋ image pull **70s**（954MB） | 兩顆 PVC 序列 attach **26s** ＋ image pull |

`Recreate` ＋ RWO PVC 意味著每次 reset 都要走完整的「殺 pod → detach → 重新排程 → attach → 可能跨節點 pull → 啟動」。落到哪個節點是排程器決定的，**租戶按 Reset 時無從預期**，所以逾時是機率事件而非確定性失敗——這也是它在合成 chart 的 integration test 裡永遠測不出來的原因（小 image、無 PVC、同節點）。

好消息是失敗路徑**完全 fail-safe**：Secret 回退、DB 不存、四入口維持舊密碼可用、文案正確引導重按，而且**重按確實會收斂**（C11 的冪等設計在真機上被證實）。

### 9.3 但租戶按不到那顆按鈕

見 §8.2 —— 這是本輪最需要後續處理的發現。`has_admin_gui=True` 的 8 個 template 中有 5 個（含本次修好的 hermes-agent、open-design）在前台不渲染 Reset 按鈕，因為前端仍用硬編碼 slug 白名單、而後端早已遷移到 `has_admin_gui`。**#449 的修復目前只有 API 可達。**

### 9.4 建議後續

| 項目 | 建議 |
| --- | --- |
| 前台白名單（§8.2） | 開 issue：template payload 補 `has_admin_gui` ＋ 前端 gate 改讀該欄。優先度高於下面兩項——修復不可達等於沒修 |
| 120s 預算（§9.2） | 考慮把 `rollout_status_deployment` 的等待與平台 client 預算解耦（例如 reset 改非同步 + 狀態輪詢），或至少讓平台預算 > operator 端最壞路徑。現況「平台先放棄、operator 繼續跑」在 UX 上是可接受的（有 C11 文案兜底），但每次冷 cache 都要租戶按兩次 |
| #456 quota | 本輪靠臨時把 `limits.cpu` 調到 12 才跑得動 rollout。正式 plan 的 `cs_vcpu` 至少 2，或把 `min_vcpu` 比照 `min_ram_gb` 改為「Σ container limits.cpu ÷ 3 上取」並補守線測試 |
| dashboard prefix bug（§8.1） | 屬 #424／chart 領域，另案 |

### 9.5 環境交還狀態

| 項目 | 狀態 |
| --- | --- |
| svc60 hermes-agent | `running`、helm revision 4、四入口以**新密碼**服務中 |
| svc50 open-design | `running`、helm revision 6、三入口以**新密碼**服務中 |
| 兩台的新密碼 | 已落平台 DB（取得路徑見 §7） |
| `paas-ws-stg-5a105e8b` quota | **`limits.cpu` / `requests.cpu` 仍為臨時值 12**（未翻回，見 §2 說明；下次該 workspace 有 CS create／delete 會被 `_push_ns_quota` 依 #456 公式重算蓋回） |
| template 46 `is_hidden` | **已被 `-u` 翻回 `True`**（stg 2026-07-28 的臨時解封被沖掉）。依分工，重新解封由 leader1 處理，本輪只記錄不處置 |
| stg 平台 image | `paas-odoo-stg:20260728.1632`（develop = `acefe088`） |

---

## 附錄 A — quota patch 前後

patch 時間 `2026-07-28T16:36:34Z`，指令見 §2。

**before**（`kubectl get resourcequota paas-ws-stg-5a105e8b-quota -o yaml` 摘錄）

```yaml
spec:
  hard:
    limits.cpu: "9"
    limits.memory: 21Gi
    persistentvolumeclaims: "10"
    requests.cpu: "9"
    requests.memory: 21Gi
    requests.storage: 100Gi
status:
  used:
    limits.cpu: 9300m        # ← 超過 hard 的 9000m
    limits.memory: 18944Mi
    persistentvolumeclaims: "10"
```

**after**

```yaml
spec:
  hard:
    limits.cpu: "12"
    requests.cpu: "12"
    # 其餘不變
status:
  used:
    limits.cpu: 9300m        # headroom 2700m
```

## 附錄 B — 120s client timeout 的語意與救援

平台 client 對 `reset-config` 的預算是 `HELM_OPERATION_TIMEOUT = 120s`（`src/services/paas_operator.py`）。operator 端的最壞情況遠大於此：

| 步驟 | operator 端上限 |
| --- | --- |
| `helm upgrade --atomic` | helm 預設 5m；subprocess `settings.helm_timeout = 300s` |
| `rollout_status_deployment` × 2 | 各 `timeout_seconds=120`（subprocess 130s） |

因此「平台已超時、operator 仍在跑」是**設計內**的場景，不是意外。依 `cloud_service.py` C11：超時時平台刻意**不存新密碼**（不變式＝DB 裡的密碼必須是真的生效過的那一把），並回覆「Reset status unknown」要租戶稍後重按。

救援步驟（本輪執行前已備妥）：

1. 從叢集 Secret 取實際生效的密碼（§7 (b)）
2. 用它驗證四入口是否已切到新值
3. 若確認叢集已輪替而平台無紀錄，可再按一次 Reset（冪等，會重新生成並收斂），或直接把 (b) 的值寫回 `cloud_service_secret`

執行前已將 svc60／svc50 的完整 Secret dump 離線留存，避免任何一步失手導致四入口全鎖死。

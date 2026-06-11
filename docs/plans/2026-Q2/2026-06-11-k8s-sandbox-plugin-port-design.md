---
title: k8s-dev-sandbox Skill 移植成 woowprd Plugin 通用 Skill — 設計
category: plans
status: draft
created: 2026-06-11
updated: 2026-06-11
topic: k8s-sandbox-plugin-port
phase: design
related:
  - .claude/skills/k8s-dev-sandbox/SKILL.md
  - .claude/skills/k8s-dev-sandbox/scripts/create.sh
  - charts/odoo-dev-sandbox/values.yaml
  - charts/odoo-dev-sandbox/templates/odoo-deployment.yaml
  - charts/odoo-dev-sandbox/templates/odoo-configmap.yaml
  - docs/tracking/pitfalls/floor-env-relative-path-empty-ext-addons.md
  - docs/tracking/pitfalls/cross-repo-artifact-landing-assumption.md
---

# k8s-dev-sandbox Skill 移植成 woowprd Plugin 通用 Skill — 設計

## 背景與目標

`/k8s-dev-sandbox` 目前是 **in-repo skill**（`.claude/skills/k8s-dev-sandbox/`），只服務 `woow_paas_platform` 單一 repo：為當前 branch 開一個隔離 namespace 的 Odoo 沙盒、裝模組、seed 憑證、跑 e2e。

目標：把它移植成 **`woowprd` plugin 內的通用 skill**（`~/.claude/plugins-local/woowprd/skills/k8s-dev-sandbox/`，觸發名 `/woowprd:k8s-dev-sandbox`），讓**任意 Odoo addon repo** 都能用，支援三種 repo 樣態：

- **(a) `src/` 放單一 addon**（`woow_paas_platform` 現況）
- **(b) repo 根目錄本身就是一個 addon**
- **(c) repo 有多個 addon 子目錄**（且有些子目錄不是 addon）

並解決：① k8s context 必須顯式指定、② addon 三樣態偵測、③ 相依的其它 addon 放哪。

> 本文件是 **design**；對應的 `-implementation.md`（檔案改動明細、phase 切分、驗收）在動工時再寫。

## 結論（先講重點）

**可行、無架構級障礙、工作量 L。** 真正的工作不在「能不能」，而在把三件事徹底解耦——它們現在全靠**同一個 `PROJECT_ROOT`** 綁在一起（`create.sh:35-36`，從 script 位置上推四層）：

| 角色 | in-repo 現況 | plugin 化後該綁誰 |
|------|-------------|------------------|
| ① 工具自帶資產（helm chart、sibling scripts） | `${PROJECT_ROOT}/charts/...` | **跟 plugin 走**（`BASH_SOURCE` 相對推導） |
| ② 被測使用者 addon repo（`src/`、`.env`、git branch、相依 addons） | `${PROJECT_ROOT}/src` 等 | **跟使用者 cwd 走**（`git rev-parse --show-toplevel` / `--repo`） |
| ③ woow 平台專屬 seed（operator / ECPay / WoowBot AI / agent-runtime / journal） | 混在 `create.sh` 主線 | **剝離成 opt-in hook，預設不跑** |

in-repo 時 ①② 同根、③ 一定是 woow，所以一個 `PROJECT_ROOT` 剛好夠用；plugin 化後 script 不在使用者 repo 內，① 指向 plugin、② 必須另尋使用者 repo、③ 對非 woow addon 會失敗。**解耦這三者是整個移植的主軸線。**

## 已查證的關鍵前提

### Claude Code plugin 機制（官方文件 code.claude.com/docs：plugins-reference、skills）

| 問題 | 結論 |
|------|------|
| skill 觸發時 Bash 的 cwd | **= 使用者專案根**（即 `${CLAUDE_PROJECT_DIR}` = Claude Code 啟動目錄）→ `git rev-parse --show-toplevel` 可定位使用者 repo |
| 定位 plugin 自帶資產 | 三重保險：`BASH_SOURCE` 相對推導（**零 env 依賴、最穩**）> `${CLAUDE_SKILL_DIR}`（skill 專用）> `${CLAUDE_PLUGIN_ROOT}`（env var，harness 注入 Bash） |
| 命名空間 / 發現 | `/woowprd:k8s-dev-sandbox`；放進 `skills/` **自動發現**，無需註冊 `plugin.json` |
| 硬限制 | plugin **不能引用 plugin root 外的檔案**（`../shared` 被擋）→ chart **必須實體 bundle 進 plugin**，不能 symlink 出去 |
| 本 session 限制 | 新加進 plugin 的 skill 要 `/reload-plugins` 或重開 session 才會被現有 session 看到 |

> **去風險意義**：原本最大的未知（「plugin 能否定位自己的 chart + 使用者 repo」）已被官方文件確認可行；且最穩的 chart 定位法是沿用現有 `SCRIPT_DIR=$(dirname BASH_SOURCE)` 推導，**連 env var 都不必賭**。命門風險因此從「make-or-break 未知」降為「低」。對照組 `jcr-disk-cleanup`（既有 plugin skill）是**純遠端操作、不碰使用者 repo**，所以它從沒遇到「定位使用者 repo」這個問題——這正是本 skill 移植的核心新難題。

### 現況事實（本 session 實地查證）

- `woow_paas_platform` 是樣態 **(a)**：repo 根**無** `__manifest__.py`、`src/__manifest__.py` 存在、`src/` 下無第二層 manifest。
- **chart 比預期更通用**，硬編集中在 `create.sh`：
  - `values.yaml:40` `addonsPath: /mnt/extra-addons,/mnt/extra-addons-ext` 已是雙路徑**參數**。
  - `values.yaml:149-155` `codeMount.{hostPath, moduleName, extraAddonsHostPath}` 已是掛載**接縫**（`moduleName` 預設 `woow_paas_platform` 但可覆寫）。
  - `values.yaml:23-24` odoo image 預設是 **Docker 官方 `odoo:18.0`**（Odoo SA 維護，**不受 Bitnami legacy 搬遷影響**；woow CI image 預設 `woow-odoo-dev` 只在 `create.sh` / `values-ci.yaml`）。
  - ⚠️ **Bitnami legacy 搬遷波及兩處**（2025-08 Bitnami 把 community image 移到 `bitnamilegacy/` 並凍結）：① `ttl-cronjob.yaml:60` `bitnami/kubectl:latest`（**就在要 bundle 的 chart 裡、與模式無關**，凍結後 pull 不到 → TTL 自動清理靜默失效）；② stg/paas-odoo 的 Bitnami **base image** 現為 `bitnamilegacy/odoo:18.0.20250805-debian-12-r9`（屬 stg profile，paas-odoo-ci 跨 repo 打包，本設計本就外置）。`odoo-configmap.yaml:25` 的 `/opt/bitnami/odoo/...` 只是 stg image 內**檔案路徑**，非 image 參照，不受影響。
  - `odoo-configmap.yaml:24-28` 只有 **stg 分支**硬編 Bitnami addons_path；非 stg 走 `.Values.odoo.addonsPath`（已參數化）。
  - 殘留硬編：`values.yaml:32` `extraPipRequirementsFile` 路徑內嵌 module 名、`values.yaml:13` label `sandbox.woow.tw/managed`、`values.yaml:153` `moduleName` 預設、`values.yaml:144` ingress domain `woowtech.io`。
- **local 模式用 hostPath**（`odoo-deployment.yaml:165-176`）→ node 必須看得到本機檔案 → **預設 local 模式實際上只在 `docker-desktop`（node = 本機）能用**；遠端 `woow-k3s` 看不到本機 `src/`，必須走 `--image`（ci-local）或 `--mode stg`（程式碼打進 image）。

## 核心障礙（依嚴重度）

| # | 障礙 | 位置 | 為什麼 blocker |
|---|------|------|---------------|
| 1 | `PROJECT_ROOT` 四層上推在 plugin 必壞 | `create.sh:35-36`（+ 8 支 script） | plugin 的 script 在 plugin 內，上推四層指向 plugin 自己而非使用者 repo；它是 chart / `src` / `.env` / git branch / module 掃描的共同根，一錯全鏈崩 |
| 2 | 單一 `PROJECT_ROOT` 同時當「chart 來源」與「被測 src 來源」 | `create.sh:166`（`CHART_DIR`） | plugin 化後兩者必須分裂成 `PLUGIN_ROOT` 與 `REPO_ROOT` |
| 3 | 硬編 `woow_paas_platform` module 名 | install gate `create.sh:521-538`、install 清單 `:567`、`test.sh --module` 預設 | 對任意 addon 永遠查不到 → 每次跑完整安裝且裝錯 module |
| 4 | DB 硬編 `woow_` 前綴 | `create.sh:196`、`e2e-seed.sh`、`seed-from-stg.sh` fallback | 三支各自推導，前綴不一致會查不到 DB |
| 5 | chart 不在使用者 repo | `create.sh:166` `CHART_DIR=${PROJECT_ROOT}/charts/odoo-dev-sandbox` | 任意 addon repo 不會有這個 chart → chart 必須隨 plugin bundle |
| 6 | 一整層 stg / 平台 seed 混在主線 | `create.sh:230-326`、`:588-719`、整支 `e2e-seed.sh` / `seed-from-stg.sh` | operator 探索、`paas_domain`、`ir_config_parameter['woow_paas_platform.*']`、ECPay、WoowBot AI（內部 gateway `eugene-minimax.woowtech.io`）、agent-runtime secret —— 通用 addon DB 沒這些 table/model，seed 全失敗或無意義 |

次要（非 blocker，去品牌）：image repo 預設 `woow-odoo-dev`、label `sandbox.woow.tw/*`、ingress `woowtech.io`、`requirements.txt` 路徑內嵌 module 名（`values.yaml:32`）、`--load-language=zh_TW`（`create.sh:582`）。

## 三大技術難點解法

### 難點 1：k8s context 顯式指定 — 現況已做對，直接搬

`create.sh:46-152` / `destroy.sh` 已是**正確且該保留的 pattern**：強制顯式 `--context` / `KUBE_CONTEXT`、驗證 context 存在於 kubeconfig、`use-context` + export、`cluster-info` 連線檢查。移植**零改動**。唯一反例 `seed-from-stg.sh` 全檔寫死 `--context woow-k3s`——但該支判定為 PaaS-only、不進通用 plugin（見「平台 seed 剝離」），矛盾自然消解。context 安全是這套工具最不需要動、且該被當作其它硬編值（namespace / domain）參數化的「正確範例」。

### 難點 2：addon repo 三樣態 — 偵測規則 + chart 參數化

偵測為純函式 `detect_layout(repo_root)`，順序 **(b)→(a)→(c)** 命中即停，全不中 → fail-loud 列已掃路徑：

| 樣態 | 判定條件 | 掛載 | module 名來源 |
|------|---------|------|--------------|
| (b) repo 根即 addon | `${REPO_ROOT}/__manifest__.py` 存在 | `REPO_ROOT → /mnt/extra-addons/{module}` | repo 目錄名正規化 或 `.sandbox.yml` 覆寫 |
| (a) `src/` 單一（woow 現況） | 根無 manifest 但 `${REPO_ROOT}/src/__manifest__.py` 存在 | `REPO_ROOT/src → /mnt/extra-addons/{module}`（把 src 內容曝光成一個叫 module 的合法 addon 目錄） | 同 (b)；**不可**用 `src` 當 module 名 |
| (c) 多 addon 子目錄 | 根與 src 皆無 manifest，掃第一層找到 ≥1 個 `<child>/__manifest__.py` | 整個 repo 直掛 `/mnt/extra-addons`，Odoo 自動發現 | 各合格子目錄名即 module 名 |

樣態 (c) 非 addon 子目錄過濾（AND 全通過）：① `<child>/__manifest__.py` 為檔案（主過濾器，刷掉 `docs/ scripts/ tests/ .git/`）；② 目錄名符合 `^[a-z][a-z0-9_]*$`（Odoo 技術名；連字號/大寫即使有 manifest 也 warn 跳過）；③（opt-in）`ast.literal_eval` 驗 manifest 是含 `name` 的合法 dict（**不用 `eval`**）；④ 固定忽略 `{setup, docs, .git, .github, .gitea, __pycache__, node_modules}`。印出接受/拒絕清單與拒絕原因。

chart / script 參數化：
- chart：`codeMount` 從「單一 `hostPath` + `moduleName`」升級為 `codeMount.mounts[]`（每項 `{hostPath, mountSubdir}`）+ 開關 `codeMount.mountWholeDir`（(c) 整目錄掛 vs (a)/(b) 逐子目錄掛）。
- chart：`extraPipRequirementsFile`（`values.yaml:32`）改由 `{primary_module}` 推導，**檔案不存在就不指**（否則 Odoo startup 失敗），fallback 走 `extraPipPackages`。
- script：`SRC_PATH=${PROJECT_ROOT}/src`（`create.sh:410`）改由 `detect_layout` 決定來源；`MODULES`、install gate 查詢、`test.sh --module` 全改用偵測/傳入的 module 名（永遠前置 `base,web,`）。
- `--load-language`（`create.sh:582`）改可選 flag（預設不帶）。

### 難點 3：相依 addons 放哪 — mode-aware 分流

| 模式 | 策略 | 理由 |
|------|------|------|
| local / dev | **`.env` 多路徑（冒號分隔）→ 多 hostPath 掛載**（`codeMount.extraMounts[]`） | 延續現有 `PAAS_CI_ADDONS_PATH → extraAddonsHostPath` 鏈，把「單一」升成「多個」；保留即時編輯 + 同改相依的價值 |
| ci-local / stg | **base image bake**（維持不動） | 本就該可重現、跨節點、與 prod 同構；改成掛載會破壞現有 release pipeline |

不推薦 sibling-repo 慣例（隱性魔法、依賴他人 checkout 佈局，已被 [cross-repo-artifact-landing-assumption](../../tracking/pitfalls/cross-repo-artifact-landing-assumption.md) 證明脆弱）。

**fail-loud 強化**（呼應 [floor-env-relative-path-empty-ext-addons](../../tracking/pitfalls/floor-env-relative-path-empty-ext-addons.md)：相對路徑誤掛**存在但空**的目錄會騙過 `-d` 檢查 → module 靜默裝不起來）：相依來源解析後不只驗目錄存在，**還要驗至少含一個 `__manifest__.py`**，空目錄一律報錯中止而非 warning 放行；相對路徑一律相對**使用者 repo** 解析（不是 plugin），強制絕對化後再掛。

> 現況補充：`PAAS_CI_ADDONS_PATH` 並非互動 prompt，而是從 env var 或 `${REPO_ROOT}/.env` 讀取（`create.sh:413-418`）；沒設只 warning 不擋（`create.sh:451-457`），真正爆在後面 Odoo `-i` 報「module not available」。泛化時把 woow 專屬變數名換成通用名（或保留為 alias）。

## 建議架構

### Plugin 目錄佈局

```
~/.claude/plugins-local/woowprd/
├── .claude-plugin/plugin.json          # name: woowprd（已存在）
└── skills/
    ├── jcr-disk-cleanup/               # 既有範本
    └── k8s-dev-sandbox/                # 新增
        ├── SKILL.md                    # frontmatter name: k8s-dev-sandbox
        ├── scripts/
        │   ├── lib/common.sh           # resolve_repo_root / detect_layout / slug（8 支共用）
        │   └── {create,destroy,list,status,logs,extend,test,build}.sh
        └── charts/odoo-dev-sandbox/    # chart 實體 bundle（不能 symlink 出 plugin）
```

### 雙根定位機制（核心）

| 要找什麼 | 用什麼定位 |
|---------|-----------|
| plugin 自帶資產（chart、sibling script） | `PLUGIN_SKILL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"`（`SCRIPT_DIR=$(dirname BASH_SOURCE)`，零 env 依賴）→ `CHART_DIR="${PLUGIN_SKILL_ROOT}/charts/odoo-dev-sandbox"` |
| 使用者 addon repo（src/.env/extra-addons/git） | `resolve_repo_root`：`--repo` flag → `${CLAUDE_PROJECT_DIR}` → `git -C "$PWD" rev-parse --show-toplevel` → **fail-loud，絕不 fallback 到 plugin 根**；加 sanity guard（驗 `$REPO_ROOT` 下有 `src/__manifest__.py` 或 `__manifest__.py` 或子目錄 manifest） |

SKILL.md 呼叫範例（從 in-repo 的 `cd <PROJECT_ROOT> && bash .claude/skills/.../create.sh` 改為）：

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/k8s-dev-sandbox/scripts/create.sh" \
     --repo "$(git rev-parse --show-toplevel)" up --context docker-desktop
```

（script 內部仍以 `BASH_SOURCE` 推 `PLUGIN_SKILL_ROOT`，不依賴 `${CLAUDE_PLUGIN_ROOT}` 是否在該情境注入，最穩。）

### 可選 per-repo 設定檔 `.sandbox.yml`

讓使用者 repo 顯式宣告 layout/module/相依/seed hook，避免偵測魔法 + 把 woow 特化外置（**慣例優於設定，但設定可覆寫**；無此檔則自動偵測作 fallback）：

```yaml
# ${REPO_ROOT}/.sandbox.yml
layout: src                 # src(a) | root(b) | multi(c)；缺省自動偵測
module: woow_paas_platform   # 樣態 (a)/(b) 的籃子名；缺省取 repo 名正規化
install_modules: [base, web, woow_paas_platform]  # 缺省由偵測推導
db_prefix: woow_
extra_addons:                # local 模式相依來源（絕對或相對 repo）
  - ../paas-odoo-ci/addons
post_create_hook: .sandbox/post-create.sh   # 平台 seed 外置入口（存在才跑）
```

### 平台 seed 剝離設計（關鍵）

那一大段 stg-only woow 平台 seed（`create.sh:230-326` stg profile、`:588-719` operator 探索 + `ir_config_parameter`/ECPay/WoowBot AI/journal SQL；整支 `e2e-seed.sh` / `seed-from-stg.sh`）對通用 addon 無意義且會失敗，**不可留在通用主線**：

1. **通用 skill 預設不跑任何平台 seed。** 把這段抽成 **opt-in 的 repo-specific post-create hook**：通用 `create.sh` 在 helm install + module install 完成後，偵測 `${REPO_ROOT}/.sandbox/post-create.sh`（或 `.sandbox/seed.sql`），**存在才執行**——平台邏輯外置回 `woow_paas_platform` repo 自己。
2. `e2e-seed.sh` / `seed-from-stg.sh` **整支不進通用 plugin**，留在 `woow_paas_platform` repo 的 in-repo 擴充（或重寫成 thin runner：通用骨架由 plugin 提供、seed 內容由 repo 提供）。
3. stg 模式（`create.sh:230-326`）整體視為「environment profile」：registry server / egress·ingress namespace / storageClass 從 values/profile 讀，不硬編 `woowtech.io` / `paas-stg-*` 字面值。
4. install gate 與 `ir_config_parameter` 注入 gate-on `primary_module == woow_paas_platform`：非 woow addon 一律 skip。

> 取捨：可插拔 hook 比「保留一堆 `if module==woow` 分支」乾淨，且把 woow 內部 URL、ECPay 測試帳號移出通用工具。

## 必須改動清單（→ implementation 雙檔展開）

**A. Script 泛化**
- `PROJECT_ROOT` 拆 `PLUGIN_SKILL_ROOT`（chart）+ `REPO_ROOT`（使用者資料）；`resolve_repo_root` fail-loud。
- grep 全檔每個 `${PROJECT_ROOT}/...` 逐處分類（`charts/` → plugin；`src`/`.env`/`extra/extra-addons`/`git -C` → repo，**勿漏**）。
- module 名參數化 → install gate（`:521-538`）+ install 清單（`:567`）+ `test.sh --module`。
- DB 前綴參數化（`--db-prefix`，預設中性）；三支共用推導函式。
- `--load-language` / image repo 預設改可選。
- **保留不動**：context 安全、彩色 header、arg 解析骨架、branch→slug 正規化、`yaml_sq` 跳脫、label-selector 動態查資源、分階段 wait、`test.sh` 的 `/$MODULE` 選擇器 + `of 0 tests` false-green guard。

**B. Chart bundle 與參數化**
- `charts/odoo-dev-sandbox/` 整包複製進 plugin。
- `codeMount` 升 `mounts[]` + `mountWholeDir`；`extraAddonsHostPath` 升 `extraMounts[]`。
- `extraPipRequirementsFile` 由 `{primary_module}` 推導、不存在不指。
- **去 Bitnami 依賴**：`ttl-cronjob.yaml` 的 `bitnami/kubectl:latest` → ✅ **已在 in-repo chart 修復**（2026-06-11，commit 待提交）：改 values-driven `ttl.image`，預設 pin `bitnamilegacy/kubectl:1.33.4-debian-12-r0`（Debian/glibc，GNU date 行為等價、零風險），bundle 進 plugin 時自然帶入。完全脫離 Bitnami（換 `rancher/kubectl` busybox date）需連 expires 解析一起改，留作 follow-up。
- 去品牌：`sandbox.woow.tw` label、ingress `woowtech.io`、`registry`/`imagePullSecrets` 參數化。

**C. Plugin 打包**
- 建 `woowprd/skills/k8s-dev-sandbox/{SKILL.md,scripts,charts}`，沿用 `jcr-disk-cleanup` 佈局。
- SKILL.md 呼叫改 `${CLAUDE_PLUGIN_ROOT}` + `--repo "$(git rev-parse --show-toplevel)"`；所有 `/k8s-dev-sandbox` → `/woowprd:k8s-dev-sandbox`。
- 決定 in-repo 副本去留：全面改走 plugin 則移除 repo 內 `.claude/skills/k8s-dev-sandbox` 避免雙寫漂移，`CLAUDE.md` 內 `./.claude/skills/.../scripts/...` 範例同步改 plugin 叫法。

**D. 平台 seed 剝離**（見上節）

## 風險與未決問題

| # | 待決 | 傾向 |
|---|------|------|
| 1 | `${CLAUDE_PLUGIN_ROOT}` / cwx 行為雖經官方文件確認，仍宜在目標 CC 版本實測一次 | 用 `BASH_SOURCE` 推 plugin 根、`--repo`+`CLAUDE_PROJECT_DIR`+git 三層 fallback 定位 repo，**設計上就不賭** |
| 2 | 樣態 (a) 的 module 技術名來源 | repo 目錄名正規化為預設 + `.sandbox.yml` 顯式覆寫（manifest 不含技術名，技術名＝目錄名） |
| 3 | 樣態 (c) 預設「全裝」還是「要求顯式 `--module`」 | 預設全裝 + 可縮（大 repo 可能裝很久） |
| 4 | (c) `primary_module`（決定 install gate / requirements / config 對象）tie-break | repo 同名目錄 → 字典序第一 + warn → 或 `--primary` |
| 5 | 相依來源是否支援「相依 repo 自己也是多 addon」遞迴偵測 | 一律當「已是可直掛的 addons 籃子」，其下子目錄即 addon，不再跑 layout detection |
| 6 | 平台 seed 剝離成「非 woow 一律 skip」還是「可插拔 hook」 | **可插拔 hook**，把 woow 特化完全移出通用工具 |
| 7 | 偵測 + 相依邏輯放 `create.sh`（bash 難單測）還是抽獨立 python（可單測） | 傾向抽出可單測 `detect_layout` |

## 下一步（phasing）

1. **變數實測（最低成本、最高槓桿）**：複製現有 scripts 進 plugin，woow repo 內呼叫 `/woowprd:k8s-dev-sandbox`，Bash `echo "$CLAUDE_PLUGIN_ROOT $CLAUDE_PROJECT_DIR"; pwd` 確認雙根。需 `/reload-plugins`。
2. 抽 `lib/common.sh`（雙根 + `detect_layout` + fail-loud），8 支共用。
3. chart bundle + 三模式參數化；用樣態 (a) woow repo 自身先驗 local 模式可起（`docker-desktop`）。
4. 剝離平台 seed 成 `.sandbox/post-create.sh` opt-in；拿一個乾淨測試 addon 驗「不帶任何 woow seed 也能起 + 裝對 module」。
5. 找真正第三方 repo 做樣態 (a)/(b)/(c) 端到端驗收，再決定移除 in-repo 副本。

> 完整四維調查（script 審計 / chart 分析 / plugin 機制 / layout 設計）原始輸出見 2026-06-11 session workflow `wl955fg4m`。

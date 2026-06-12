---
title: k8s-dev-sandbox Onboarding — 三層設定模型與 onboard 子命令 — 設計
category: plans
status: draft
created: 2026-06-12
updated: 2026-06-12
topic: sandbox-onboarding
phase: design
related:
  - docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-design.md
  - docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-implementation.md
  - .sandbox.yml
  - .sandbox/pre-install.sh
  - .sandbox/post-create.sh
  - .claude/skills/deploy-stg/scripts/deploy-stg.sh
  - docs/tracking/pitfalls/floor-env-relative-path-empty-ext-addons.md
---

# k8s-dev-sandbox Onboarding — 三層設定模型與 onboard 子命令 — 設計

## 背景與目標

[plugin port 設計](2026-06-11-k8s-sandbox-plugin-port-design.md)已把 `/woowprd:k8s-dev-sandbox`
做成服務**任意 Odoo addon repo** 的通用工具（三樣態偵測、平台 seed 外置 `.sandbox/` hook）。
但「任意 repo、任意 worktree、**agent 自主**開沙盒跑 e2e」這個目標情境還缺最後一層：

> 開發任何一個 addon（如 ecpay addon），agent 在多個 git worktree（各自資料夾、各自
> branch）各開一個 local 沙盒做自動化 end-to-end 測試 —— **不需要人先 export env、
> 不需要人記得這個 repo 要哪些參數**。

目標：新增 `onboard` 子命令與 user-scope 設定持久層，讓「每個 project 一次 onboarding、
之後任何 worktree 零互動 `up`」成立。

> 本文件是 **design**；對應的 `-implementation.md`（檔案改動明細、phase 切分、驗收）在
> 動工時再寫。

## 結論（先講重點）

**設定有三層，現在只做了兩層。** plugin 現況的設定來源全貌：

| 層 | 內容 | 放哪 | 跟著 worktree 走？ | 現況 |
|----|------|------|------------------|------|
| ① 結構宣告 | layout / module / install_modules / db_prefix / extra_addons / hook 路徑 | `.sandbox.yml`（repo 內、committed） | ✅ git 帶著走 | ✅ 已有（`common.sh load_sandbox_yml`） |
| ② 平台 seed 邏輯 | 複雜 install-time values / post-install SQL | `.sandbox/{pre-install,post-create}.sh`（repo 內、committed） | ✅ git 帶著走 | ✅ 已有 hook 機制 |
| ③ 執行期供值 | secrets、本機絕對路徑、context 偏好、env | **沒地方放** | — | ❌ **缺；每次 create 前要人（或 deploy-stg 這類上層腳本）先 export** |

缺第③層的直接後果（本 session 實地查證）：

- env 缺值時 **silent 降級**而非 fail：`AGENT_RUNTIME_NAMESPACE_SECRET` 未設 → agent-runtime
  整段靜默關閉（`.sandbox/pre-install.sh:65`）；`JCR_USER`/`JCR_PASS` 未設 → registry secret
  不寫（`pre-install.sh:50`）。對人開沙盒可接受，對 agent 自主開沙盒是不可觀測的斷點。
- 簡單參數 seed 無宣告式管道：`web.base.url` + `freeze` 只活在
  `deploy-stg.sh init_odoo_system_params`（deploy-stg 專屬）。直接用 plugin 建 woow stg
  sandbox 會漏掉 → ECPay 3DS 回呼撞 Mixed Content、綁卡/加值 UI 失敗（三層全綠但功能壞）。
  任何通用 addon 想 seed 自己的 `ir.config_parameter` 都得寫整支 post-create.sh shell。
- [floor-env-relative-path pitfall](../../tracking/pitfalls/floor-env-relative-path-empty-ext-addons.md)
  的根因正是「本機絕對路徑沒有 user-scope 的家、被塞進 repo 內相對路徑」——第③層就是它該住的地方。

**方案**：

1. `.sandbox.yml` 擴充宣告區塊：`env_required` / `env_optional`（repo 宣告「我需要哪些
   env」）與 `config_params(_{mode})`（宣告式 `ir.config_parameter` seed，支援模板與
   per-mode 覆寫）。
2. 新 `onboard` 子命令：讀宣告 → 收集缺值 → 寫 `~/.sandbox/{project-key}/config.env`
   （user-scope、chmod 600）→ dry-run 驗證。
3. `create.sh` 整合：開頭載入 profile；`env_required` 缺值 **fail-fast**（指引跑 onboard）
    取代 silent 降級；通用引擎冪等套用 `config_params`。

**契約一句話**：repo 宣告「需要什麼」（committed、跨機器共享），user/機器供一次「值是什麼」
（私有、不進 git），worktree 全部自動繼承。

## 已查證的關鍵前提

### multi-worktree 並行的地基已經是對的（零改動）

- slug 從 branch 算（`common.sh compute_slug`），git 本身禁止同一 branch 被兩個 worktree
  checkout → 每個 worktree 天然拿到不同 namespace / DB，不互撞。
- local 模式 hostPath 掛**該 worktree 自己的路徑**（`create.sh:262-326`）→ 每個沙盒看到
  自己那份程式碼、改檔即時生效 —— agent e2e 迴圈的前提。
- 限制不變：local 模式只在 node = 本機（`docker-desktop`）可用；遠端 cluster 走
  `--image` / `--mode stg`。本設計**不**改這條。

### plugin 現況沒有任何 user-scope / `.env` 載入

`common.sh load_sandbox_yml`（`:246-345`）只解析 `.sandbox.yml` 的
layout / module / db_prefix / post_create_hook / install_modules / extra_addons；
無 profile、無 `.env` 載入（註解提及的 `.env` 是舊 in-repo 時代殘句）。第③層是全新增量，
不與既有機制衝突。

### project-key：worktree 與 clone 是兩種共享情境

實測本 repo（maestri Floor）`git rev-parse --git-common-dir` 回 `.git` —— **Floor 是獨立
clone，不是 worktree**。兩種多資料夾型態的識別行為不同：

| 型態 | `--git-common-dir`（absolute） | 同 project 嗎 |
|------|------------------------------|--------------|
| `git worktree add` 出來的 worktree | 全部指向主 repo 的 `.git` | ✅ 同 key |
| maestri Floor / 手動多 clone | 各自獨立 | ❌ 不同 key（若只用 common-dir） |

故 project-key 推導順序：**① `origin` remote URL 正規化**（去 scheme/憑證/`.git` 後綴；
worktree 與 clone 都收斂到同一 key）→ **② 無 remote 時 fallback 絕對 common-dir**。
可讀形式 `{repo-name}-{hash8}`（hash 取正規化字串 sha256 前 8 碼，避免同名不同源相撞）。

## 設計

### 落點分工（回應「repo 是誰」）

| 落點 | 是誰 | 放什麼 |
|------|------|--------|
| woowprd plugin repo（`~/.claude/plugins-local/woowprd`） | 通用引擎 | `onboard.sh`（新）、`common.sh` 擴充、`create.sh` 整合點。**零專案特化** |
| 被測 addon repo（woow_paas_platform、ecpay addon、…） | 宣告需求 | `.sandbox.yml` 的 `required_env` / `config_params`；複雜邏輯才用 `.sandbox/*.sh` |
| `~/.sandbox/{project-key}/`（user-scope，不進任何 git） | 供值 | `config.env`：secrets、本機絕對路徑、context 偏好 |

### `.sandbox.yml` 宣告擴充

維持現有 line-based bash parser 可解析的**嚴格子集**（兩層縮排、`key: value`、`- item`；
不引入巢狀任意深度）：

```yaml
# ── 新增區塊 1：執行期 env 需求宣告（"NAME: desc" 純量項；required/optional 拆兩區塊）──
env_required:               # 缺值 → create fail-fast（指引跑 onboard）
  - "ECPAY_MERCHANT_ID: ECPay stage 測試商店"
env_optional:               # 缺值 → warn 後照舊降級（取代現在的完全靜默）
  - "AGENT_RUNTIME_NAMESPACE_SECRET: Odoo 端 namespace hash；兼 agent-runtime 啟用開關"
  - "JCR_USER: JCR registry pull 帳號（cluster 已有 secret 時可不設）"

# ── 新增區塊 2：宣告式 ir.config_parameter seed ──
config_params:
  web.base.url: "http://localhost:8080"
  web.base.url.freeze: "True"

config_params_stg:            # per-mode 覆寫（同 key 蓋過 config_params）
  web.base.url: "https://{{slug}}.woowtech.io"
```

- 模板變數：`{{slug}}` / `{{namespace}}` / `{{db_name}}` / `{{mode}}`（與 hook 拿到的 env
  座標同源；只做字串替換，不做運算）。
- per-mode 覆寫用**平鋪區塊名** `config_params_{mode}`（不用巢狀 YAML），遷就 bash parser。
  mode 名含連字號時區塊名以底線寫（`ci-local` → `config_params_ci_local`）。
- env 宣告同理展平：**`env_required` / `env_optional` 兩個純量 list、項目格式 `"NAME: desc"`**
  （implementation 時修正：原列 `required_env:` list-of-maps 文法超出 line parser 嚴格子集，
  list-of-maps parser 正是風險 #1 要避免的 bash 複雜化；兩區塊拆法與 `config_params_{mode}`
  同一展平哲學，語義不變——required 缺值 fail-fast、optional 缺值 warn）。
- `config_params` 定位：「upsert 幾個 `ir_config_parameter`」這種簡單 seed 的宣告式管道，
  讓大多數 addon repo **不必寫 shell hook**；複雜邏輯（operator 探索、ECPay provider
  UPDATE、journal link）仍走 `.sandbox/post-create.sh`。

### user-scope profile：`~/.sandbox/{project-key}/config.env`

```bash
# onboard 產物；chmod 600
KUBE_CONTEXT=docker-desktop
AGENT_RUNTIME_NAMESPACE_SECRET=xxxx
JCR_USER=ci-bot
JCR_PASS=!security find-generic-password -s jcr -w   # `!` 前綴 = 取值指令，create 時動態執行
```

- 值兩種形態：**字面值**（簡單）與 **`!command`**（lazy、每次 create 動態執行、不落地；
  先例：`pre-install.sh` 已動態撈 operator secret。解 secret 輪替後 profile 過期問題）。
- **`!command` 求值時機 = `require_kube_context` 切好 context 之後**（兩段式載入：字面值
  先套、`!cmd` 記 pending 延後）——`!cmd` 常含 kubectl，在 ambient context 下跑會跨
  cluster 誤讀、甚至從錯的 cluster 撈到「存在但錯」的 secret 靜默流進 helm values
  （4-lens 驗證 workflow 發現，implementation 修訂）。`KUBE_CONTEXT` 本身只能是字面值。
- profile 不接受工具內部變數名（`IMAGE_TAG` 等 deny-list）——它是 env 供值面，不是旗標
  注入面（否則 profile 寫 `IMAGE_TAG=x` 會靜默把 MODE 翻成 ci-local）。
- 檔案格式刻意選 `KEY=VALUE`（非 YAML）：load 即 `export`，零解析歧義。
- 注意：`.sandbox.yml` 的 `extra_addons` 本機路徑**不在**本設計範圍——仍由該檔字面提供；
  「extra_addons 值支援 `${VAR}` 展開（值來自 profile）」列 follow-up（風險表 #7）。

### 解析優先序（一條鏈，全工具一致）

```
CLI flag  >  當下 shell env  >  ~/.sandbox/{project-key}/config.env  >  .sandbox.yml  >  自動偵測
```

profile 只補洞、不搶顯式參數 —— deploy-stg 既有「export 後呼叫 create.sh」流程行為不變
（env 優先於 profile）。

### `onboard` 子命令

```bash
/woowprd:k8s-dev-sandbox onboard --repo "$(git rev-parse --show-toplevel)" [--context <ctx>] \
    [--set KEY=VALUE ...] [--check]
```

流程：

1. 推導 project-key；讀 `.sandbox.yml` 宣告 + 既有 profile（若有）。
2. 對每個宣告的 env：已有值（env / profile / `--set`）→ 沿用；缺值 → 互動 prompt
   （human）或由 `--set` 供給（agent 非互動路徑）。`--check` 只報缺值清單不寫檔
   （exit code 供 agent 判斷）。
3. 寫 `~/.sandbox/{project-key}/config.env`（mkdir -p、chmod 600）。
4. dry-run 驗證：context 在 kubeconfig 且連得上、`extra_addons` 路徑存在**且含
   `__manifest__.py`**（沿用 plugin-port 的 fail-loud 強化）、`!command` 形態的值試執行一次。

### `create.sh` 整合點（通用引擎）

1. **開頭**（解析 `--repo` 後）：推 project-key → **兩段式載入**：第一段只 export 字面值
   （`!command` 記 pending 不求值）；`require_kube_context` 切好 context 後第二段才求值
   pending 的 `!command` 並 export（防跨 cluster 誤讀）。
2. **fail-fast（雙段）**：第一段後 pre-check——缺 `env_required` 且無 pending `!cmd` 可補
   → 不碰 cluster 直接紅字列缺值 + 指引 `onboard`，exit 1（離線 / VPN 未開也能拿到指引）；
   `!cmd` 求值後 final check 全量判定。`env_optional` 缺值 → 黃字 warn（取代完全靜默）。
3. **config_params 套用**：module install 完成後、`post-create.sh` hook **之前**
   （宣告式為底、hook 可覆寫），通用 psql 冪等 upsert（`ON CONFLICT DO UPDATE`，
   模式同 `post-create.sh` 既有 SQL）。
4. **upgrade 路徑也套用**：現況 seed 區塊只在 fresh install 跑（`create.sh:424` gate）；
   `config_params` 改為**每次 create/up 都跑**，但 **diff-aware**——先 SELECT 現值、
   全部相同則跳過；有變更才 upsert + rollout restart（解 Odoo in-process cache）。
   避免「up 既有沙盒每次都白白 restart」。
5. **失敗 fail-loud**：個別鍵套用失敗計數、印 psql stderr，create 結尾非零退出——宣告
   seed 是功能正確性前提（web.base.url 失敗會直接回歸 ECPay Mixed Content bug），不可
   靜默降級；恢復舊 deploy-stg `set -e` 的失敗語義（驗證修訂）。

### deploy-stg 收斂（本 repo 受益）

`web.base.url` + `freeze` 從 `deploy-stg.sh init_odoo_system_params` 移到本 repo
`.sandbox.yml` 的 `config_params_stg`；deploy-stg 刪掉該 helper，退成純「CI 編排 + env
準備」薄殼。直接用 plugin 建 woow stg sandbox 從此也完整 onboard。

## 設計決策

| # | 決策 | 選擇 | 理由 |
|---|------|------|------|
| D1 | config_params 的 mode 相依值 | 平鋪 `config_params_{mode}` 覆寫區塊 + `{{slug}}` 模板 | bash parser 嚴格子集內可解；巢狀 YAML 會逼 parser 升級 |
| D2 | profile secret 形態 | 字面值與 `!command` 雙形態；**`!cmd` 延後到 context 切換後求值、`KUBE_CONTEXT` literal-only** | 字面值簡單；`!command` 解輪替/不落地需求（pre-install.sh 先例）；延後求值防跨 cluster 誤讀（驗證修訂） |
| D3 | project-key | remote URL 優先、common-dir fallback、`{name}-{hash8}`；**路徑一律 `pwd -P` 物理化** | worktree 與 maestri Floor 多 clone 都收斂同 key（實測 Floor 非 worktree）；git 對 worktree 回 realpath、macOS /tmp·/var 是 symlink，邏輯路徑會 key 漂移（驗證實證） |
| D4 | env 宣告的 required 語義 | 拆 `env_required`（fail-fast）/ `env_optional`（warn）兩區塊；預設行為＝不宣告＝照舊 | 不破壞既有「不設就降級」使用者；要硬保證的 repo 自己宣告進 `env_required` |
| D5 | config_params 與 post-create hook 順序 | 宣告式先、hook 後；**config_params 是 desired-state，每次 up 收斂回宣告值** | 宣告式是 base、hook 是 power tool；但 hook 覆寫只在首次 create 生效，之後每次 up 宣告值收斂回來——**勿宣告 hook / 人工也會改的 key**（驗證釐清，收斂時印「收斂漂移值」可觀測） |
| D6 | profile 的 `KUBE_CONTEXT` 對 destroy 是否生效 | **不生效**：destroy 仍要求顯式 `--context` flag；**onboard 也絕不從 ambient env 擷取 KUBE_CONTEXT**（只認 `--context`/`--set`） | destroy 含 `delete namespace`/`delete pv`，顯式性是既有安全紅線；殘留的 `KUBE_CONTEXT=woow-k3s` 被靜默寫入 profile 會讓之後所有零互動 up 打到 prod/stg cluster（驗證修訂） |
| D7 | onboard 落點 | woowprd plugin（通用），本 repo 只擴充 `.sandbox.yml` | 與 plugin-port「通用主線零特化」一致；ecpay 等任意 addon 直接受益 |

## 必須改動清單（→ implementation 雙檔展開）

**A. woowprd plugin repo**

- `scripts/lib/common.sh`：① `load_sandbox_yml` 擴充解析 `env_required` / `env_optional` /
  `config_params(_*)`；② 新增 `compute_project_key`；③ 新增 `load_profile`
  （含 `!command` 求值與優先序守則）。
- `scripts/onboard.sh`（新檔）：宣告讀取 → 收集（互動 / `--set` / `--check`）→ 寫
  profile → dry-run 驗證。
- `scripts/create.sh`：profile 載入、env_required fail-fast、config_params diff-aware
  套用（fresh + upgrade 路徑）。
- `SKILL.md`：onboard 子命令、`.sandbox.yml` 新區塊、解析優先序、profile 位置文件。

**B. 本 repo（第一個使用者）**

- `.sandbox.yml`：補 `env_optional`（AGENT_RUNTIME_NAMESPACE_SECRET / JCR_USER /
  JCR_PASS）與 `config_params_stg`（web.base.url + freeze；只放 stg 區塊，
  local 不設 —— 與現行 deploy-stg 行為完全對齊，local 沙盒從未 seed 過 base url）。
- `.claude/skills/deploy-stg/scripts/deploy-stg.sh`：移除 `init_odoo_system_params`
  （改由宣告式接手）。
- `CLAUDE.md`：K8s Dev Sandbox 段補 onboard 一句話。

## 風險與未決問題

| # | 風險 / 待決 | 傾向 |
|---|------------|------|
| 1 | bash line-based parser 對新區塊的脆弱性（縮排、引號） | 文件明訂嚴格子集 + parser 對不認得的行 fail-loud（不靜默跳過）；若再擴就到該抽 python 的臨界點（plugin-port 未決 #7 同源） |
| 2 | profile 明文 secret 落地 | chmod 600 + `!command` 形態可選不落地；文件明示「不要把 config.env 放進任何 git」 |
| 3 | config_params 每次 create 都跑的 restart 抖動 | diff-aware（無變更不寫不 restart）已內建於設計 |
| 4 | docker-desktop 多沙盒並行的資源壓力（每沙盒一套 odoo+pg） | 非本設計範圍；ResourceQuota 既有，文件提醒並行數與 Docker Desktop VM 配額的關係 |
| 5 | `!command` 求值失敗的語義 | 視同缺值：required→fail-fast、optional→warn；不得拿空字串繼續（呼應 pre-install.sh fail-closed 先例） |
| 6 | remote URL 變更（repo 改名/搬家）導致 project-key 漂移、profile 變孤兒 | 接受；onboard 重跑即可，`~/.sandbox/` 留舊目錄無害。不做 migration 魔法 |
| 7 | `extra_addons` 本機絕對路徑仍住 `.sandbox.yml`（跨機器共用該檔時的缺口） | follow-up：`extra_addons` 值支援 `${VAR}` 展開（值來自 profile/env、缺值 fail-loud）；本設計先收 secrets / context |
| 8 | `config_params` 值含 `#` 會被行內註解剝除截斷；不支援多行值 | 文件載明限制；parser 升級（引號值保留 `#`）列 follow-up |

## 驗收情境（implementation 檔展開成 checklist）

1. **非 paas 最小 fixture repo**（樣態 (b)）：`onboard --set` → `up --context docker-desktop`
   → `test` → `down` 全程零互動；`config_params` 進 DB 且 restart 後生效。
2. **本 repo 兩個 worktree**：onboard 一次 → 兩 worktree 各自 `up` → 兩個 namespace、
   兩個 DB、各掛自己的 src；profile 共享（同 project-key）實證。
3. **fail-fast**：把某 env 宣告進 `env_required` 且不供值 → create 紅字退出、指引 onboard。
4. **deploy-stg 回歸**：移除 `init_odoo_system_params` 後跑 deploy-stg，stg DB 的
   `web.base.url = https://{slug}.woowtech.io` 且 `freeze=True`（來源改為宣告式）。
5. **upgrade 路徑**：對既有沙盒改 `.sandbox.yml` 的某個 config_param → `up` → 值更新
   + restart 一次；再 `up` 一次 → 無變更、不 restart。

## 下一步（phasing）

1. `common.sh`：project-key + profile loader + parser 擴充（純函式，先配 bats/手測）。
2. `create.sh` 整合（profile 載入 + fail-fast + config_params）；fixture repo 驗 1、3、5。
3. `onboard.sh`；驗 1 全流程與 `--check` agent 路徑。
4. 本 repo `.sandbox.yml` 擴充 + deploy-stg 收斂；驗 2、4。
5. SKILL.md / CLAUDE.md 文件同步；寫 `-implementation.md` 對齊實際 phase。
